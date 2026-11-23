"""Offline tests for the supervising ``HttpsClient`` and its global child slot.

Every child here is a HARMLESS local python script written to a private temporary
directory and injected as ``worker_command``.  None of them opens a socket; the real
``_https_worker`` HTTP path is never executed.  Not a live qualification.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from pet_bridge import _https_worker, transport
from pet_bridge.transport import (
    HttpsClient,
    Origin,
    TransportError,
    route_sessions_observation,
    route_detail,
    route_pair_start,
)

from .fakes import OfflineTestCase

CANARY = "CANARY-TOKEN-7c1e"
ORIGIN = Origin.parse("https://unified.example.ts.net:8941")
PRELUDE = (
    "import base64, json, os, sys, time\n"
    "def emit(doc):\n"
    "    sys.stdout.write(json.dumps(doc) + '\\n'); sys.stdout.flush()\n"
    "def ok(body=b'{}', status=200):\n"
    "    emit({'v': 1, 'ok': True, 'status': status, 'body': base64.b64encode(body).decode()})\n"
)


class SupervisorCase(OfflineTestCase):
    def setUp(self) -> None:
        super().setUp()
        transport._reset_child_slot_for_tests()
        self.addCleanup(transport._reset_child_slot_for_tests)
        self.dir = Path(os.path.realpath(tempfile.mkdtemp(prefix="pet-sup-test-")))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.procs: list[subprocess.Popen] = []
        self.spawn_args: list[tuple[Any, Any]] = []
        self.created = threading.Event()

    def script(self, body: str) -> list[str]:
        path = self.dir / f"w{len(os.listdir(self.dir))}.py"
        path.write_text(PRELUDE + textwrap.dedent(body), encoding="utf-8")
        return [sys.executable, "-I", str(path)]

    def spawn(self, *args: Any, **kwargs: Any) -> subprocess.Popen:
        self.spawn_args.append((args, kwargs))
        proc = subprocess.Popen(*args, **kwargs)
        self.procs.append(proc)
        self.created.set()
        return proc

    def client(self, body: str, *, timeout: float = 8.0, **kwargs: Any) -> HttpsClient:
        return HttpsClient(
            timeout=timeout,
            worker_command=self.script(body),
            _spawn=kwargs.pop("_spawn", self.spawn),
            **kwargs,
        )

    def call(self, client: HttpsClient, route=None, **kwargs: Any):
        route = route or route_pair_start()
        kwargs.setdefault("headers", {"Content-Type": "application/json"})
        if route.method == "POST":
            kwargs.setdefault("body", b'{"x":1}')
        return client.request(ORIGIN, route, **kwargs)

    def failure(self, client: HttpsClient, route=None, **kwargs: Any) -> TransportError:
        try:
            self.call(client, route, **kwargs)
        except TransportError as exc:
            return exc
        self.fail("expected a TransportError")

    def assertAllDead(self) -> None:
        for proc in self.procs:
            self.assertIsNotNone(proc.poll(), "child was left running")


class HappyPathTest(SupervisorCase):
    def test_success_returns_status_and_exact_body(self):
        c = self.client("sys.stdin.buffer.read(); ok(b'{\"k\":7}', 200)")
        r = self.call(c)
        self.assertEqual((r.status, r.body), (200, b'{"k":7}'))
        self.assertAllDead()

    def test_error_status_bodies_are_returned_raw(self):
        for status in (401, 409, 504):
            with self.subTest(status=status):
                c = self.client(f"sys.stdin.buffer.read(); ok(b'oops', {status})")
                r = self.call(c)
                self.assertEqual((r.status, r.body), (status, b"oops"))

    def test_child_gets_one_strict_request_and_nothing_secret_in_argv_or_env(self):
        script = """
            raw = sys.stdin.buffer.read()
            doc = json.loads(raw)
            ok(json.dumps({'argv': sys.argv, 'env': dict(os.environ), 'doc': doc}).encode())
        """
        c = self.client(script)
        with mock.patch.dict(
            os.environ, {"PET_CANARY_SECRET": CANARY, "HOME": "/nowhere"}
        ):
            r = self.call(
                c,
                route_detail("sess-1"),
                headers={
                    "Authorization": "Bearer " + CANARY,
                    "X-Amplifier-Client": "cid",
                },
            )
        seen = json.loads(r.body)
        self.assertNotIn(CANARY, json.dumps(seen["argv"]) + json.dumps(seen["env"]))
        self.assertLessEqual(set(seen["env"]), {"PYTHONDONTWRITEBYTECODE", "LC_CTYPE"})
        self.assertEqual(seen["env"]["PYTHONDONTWRITEBYTECODE"], "1")
        doc = seen["doc"]
        self.assertEqual(doc["host"], "unified.example.ts.net")
        self.assertEqual(doc["port"], 8941)
        self.assertEqual(doc["method"], "GET")
        self.assertEqual(
            doc["target"], "/api/conversation/detail?sessionId=sess-1&part=messages"
        )
        self.assertEqual(doc["headers"]["Authorization"], "Bearer " + CANARY)
        self.assertEqual(doc["max_response"], 1024 * 1024)
        self.assertEqual(doc["max_request"], 0)
        self.assertEqual(doc["body"], "")
        self.assertEqual(doc["timeout"], 8.0)
        ((args, kwargs),) = self.spawn_args
        command = args[0]
        self.assertEqual(command[:2], [sys.executable, "-I"])
        self.assertEqual(len(command), 3)
        self.assertTrue(os.path.isabs(command[2]))
        self.assertEqual(kwargs["env"], {"PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(kwargs["close_fds"])
        self.assertNotIn("shell", kwargs)

    def test_post_body_is_base64_in_the_request(self):
        script = """
            doc = json.loads(sys.stdin.buffer.read())
            ok(base64.b64decode(doc['body']))
        """
        r = self.call(self.client(script), body=b'{"hello":"world"}')
        self.assertEqual(r.body, b'{"hello":"world"}')

    def test_default_command_is_the_fixed_private_worker(self):
        c = HttpsClient()
        expected = [sys.executable, "-I", str(Path(_https_worker.__file__).resolve())]
        self.assertEqual(c._command, expected)

    def test_a_one_mebibyte_detail_page_fits_the_calculated_ipc_cap(self):
        body = b"{" + b" " * (1024 * 1024 - 2) + b"}"
        script = f"""
            sys.stdin.buffer.read()
            ok(b'{{' + b' ' * ({len(body)} - 2) + b'}}')
        """
        r = self.call(self.client(script), route_detail("s1"))
        self.assertEqual(len(r.body), 1024 * 1024)
        self.assertGreater(_https_worker.ipc_response_cap(1024 * 1024), 64 * 1024)


class RequestValidationTest(SupervisorCase):
    def test_bad_requests_never_spawn(self):
        c = self.client("ok()")
        bad = [
            dict(headers={"Cookie": "a"}),
            dict(headers={"Authorization": "a\r\nHost: evil"}),
            dict(headers={"Accept": "\u00e9"}),
            dict(headers={"Host": "evil.example"}),
            dict(body=b"x" * (16 * 1024 + 1)),
        ]
        for kwargs in bad:
            with self.subTest(kwargs=list(kwargs)):
                exc = self.failure(c, **kwargs)
                self.assertEqual(exc.code, "request_invalid")
                self.assertFalse(exc.maybe_sent)
        exc = self.failure(c, route_sessions_observation(), body=b"x")
        self.assertEqual(exc.code, "request_invalid")
        self.assertEqual(self.spawn_args, [])

    def test_origin_must_be_an_origin_object(self):
        c = self.client("ok()")
        with self.assertRaises(TransportError):
            c.request("https://unified.example.ts.net", route_pair_start(), body=b"{}")  # type: ignore[arg-type]
        self.assertEqual(self.spawn_args, [])


class FailureModesTest(SupervisorCase):
    def test_hang_is_killed_at_the_deadline_and_reaped(self):
        c = self.client("sys.stdin.buffer.read(); time.sleep(60)", timeout=0.6)
        started = time.monotonic()
        exc = self.failure(c)
        self.assertEqual((exc.code, exc.maybe_sent), ("timeout", True))
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertAllDead()

    def test_trickle_never_extends_the_deadline(self):
        script = """
            sys.stdin.buffer.read()
            while True:
                sys.stdout.write(' '); sys.stdout.flush(); time.sleep(0.02)
        """
        started = time.monotonic()
        exc = self.failure(self.client(script, timeout=0.6))
        self.assertIn(exc.code, {"timeout", "worker_flood"})
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertAllDead()

    def test_flood_past_the_ipc_cap_is_killed_immediately(self):
        script = """
            sys.stdin.buffer.read()
            chunk = b'x' * 65536
            while True:
                sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush()
        """
        started = time.monotonic()
        exc = self.failure(self.client(script, timeout=8.0))
        self.assertEqual(exc.code, "worker_flood")
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertAllDead()

    def test_invalid_ipc_is_a_fixed_error(self):
        cases = {
            "garbage": "print('not json')",
            "empty": "pass",
            "array": "print('[]')",
            "no newline eof partial": 'sys.stdout.write(\'{"v":1,"ok":tr\'); sys.stdout.flush()',
            "wrong version": "emit({'v': 2, 'ok': False, 'code': 'timeout'})",
            "bool version": "emit({'v': True, 'ok': False, 'code': 'timeout'})",
            "ok not bool": "emit({'v': 1, 'ok': 1, 'status': 200, 'body': ''})",
            "unknown code": "emit({'v': 1, 'ok': False, 'code': 'weird'})",
            "extra key": "emit({'v': 1, 'ok': False, 'code': 'timeout', 'x': 1})",
            "status string": "emit({'v': 1, 'ok': True, 'status': '200', 'body': ''})",
            "status bool": "emit({'v': 1, 'ok': True, 'status': True, 'body': ''})",
            "status redirect": "emit({'v': 1, 'ok': True, 'status': 302, 'body': ''})",
            "status low": "emit({'v': 1, 'ok': True, 'status': 99, 'body': ''})",
            "bad base64": "emit({'v': 1, 'ok': True, 'status': 200, 'body': '***'})",
            "nan": 'sys.stdout.write(\'{"v":1,"ok":true,"status":NaN,"body":""}\\n\')',
            "dup keys": 'sys.stdout.write(\'{"v":1,"v":1,"ok":false,"code":"timeout"}\\n\')',
            "two documents": "emit({'v':1,'ok':True,'status':200,'body':''}); emit({'v':1,'ok':True,'status':200,'body':''})",
            "trailing bytes": "emit({'v':1,'ok':True,'status':200,'body':''}); sys.stdout.write('x')",
        }
        for name, code in cases.items():
            with self.subTest(name):
                exc = self.failure(
                    self.client("sys.stdin.buffer.read()\n" + code, timeout=4.0)
                )
                self.assertEqual(exc.code, "worker_ipc_invalid")
                self.assertTrue(exc.maybe_sent)
        self.assertAllDead()

    def test_decoded_body_over_the_route_cap_is_refused(self):
        script = """
            sys.stdin.buffer.read()
            ok(b'x' * (16 * 1024 + 1))
        """
        exc = self.failure(self.client(script))
        self.assertEqual(exc.code, "response_too_large")

    def test_nonzero_exit_or_signal_even_with_a_valid_document_is_a_failure(self):
        for tail in ("sys.exit(3)", "os.kill(os.getpid(), 9)"):
            with self.subTest(tail=tail):
                script = "sys.stdin.buffer.read()\nok()\nsys.stdout.flush()\n" + tail
                exc = self.failure(self.client(script, timeout=4.0))
                self.assertEqual(exc.code, "worker_failed")

    def test_a_document_without_eof_is_not_accepted(self):
        script = """
            sys.stdin.buffer.read()
            ok()
            time.sleep(60)
        """
        exc = self.failure(self.client(script, timeout=0.8))
        self.assertEqual(exc.code, "timeout")
        self.assertAllDead()

    def test_child_that_never_reads_stdin_does_not_crash_the_parent(self):
        exc = self.failure(self.client("sys.exit(0)", timeout=4.0))
        self.assertIn(exc.code, {"worker_ipc_invalid", "worker_failed"})
        self.assertAllDead()

    def test_failure_report_from_the_child_maps_to_its_code(self):
        for code, sent in (
            ("tls_verify_failed", True),
            ("redirect_refused", True),
            ("timeout", True),
            ("request_invalid", False),
        ):
            with self.subTest(code=code):
                script = f"sys.stdin.buffer.read(); emit({{'v':1,'ok':False,'code':'{code}'}})"
                exc = self.failure(self.client(script, timeout=4.0))
                self.assertEqual(
                    (exc.code, exc.maybe_sent, exc.route), (code, sent, "pair_start")
                )

    def test_late_valid_result_is_rejected_after_the_deadline(self):
        offset = [0.0]

        def mono() -> float:
            return time.monotonic() + offset[0]

        class Jumping(subprocess.Popen):
            def wait(self, timeout=None):  # the child exits; then the clock jumps
                result = super().wait(timeout)
                offset[0] = 1000.0
                return result

        script = "sys.stdin.buffer.read(); ok(b'{}')"
        c = self.client(script, timeout=5.0, monotonic=mono, _spawn=Jumping)
        exc = self.failure(c)
        self.assertEqual(exc.code, "timeout")

    def test_deadline_starts_before_the_spawn(self):
        calls: list[str] = []
        real = time.monotonic

        def mono() -> float:
            calls.append("mono")
            return real()

        def spawn(*a: Any, **k: Any):
            calls.append("spawn")
            return self.spawn(*a, **k)

        c = self.client("sys.stdin.buffer.read(); ok()", monotonic=mono, _spawn=spawn)
        self.call(c)
        self.assertEqual(calls[0], "mono")
        self.assertLess(calls.index("mono"), calls.index("spawn"))

    def test_spawn_failure_is_reported_as_not_sent_and_frees_the_slot(self):
        def boom(*_a: Any, **_k: Any):
            raise OSError("no fork")

        exc = self.failure(self.client("ok()", _spawn=boom))
        self.assertEqual((exc.code, exc.maybe_sent), ("spawn_failed", False))
        r = self.call(self.client("sys.stdin.buffer.read(); ok()"))
        self.assertEqual(r.status, 200)

    def test_keyboard_interrupt_still_kills_and_reaps_the_child(self):
        real_select = transport.selectors.DefaultSelector.select
        state = {"n": 0}

        def interrupting(self_, timeout=None):
            state["n"] += 1
            if state["n"] == 3:
                raise KeyboardInterrupt
            return real_select(self_, timeout)

        c = self.client("sys.stdin.buffer.read(); time.sleep(60)", timeout=8.0)
        with (
            mock.patch.object(
                transport.selectors.DefaultSelector, "select", interrupting
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.call(c)
        self.assertAllDead()
        self.assertEqual(transport.child_slot_status(), ("free", None))


class PostNeverRetriedBySupervisorTest(SupervisorCase):
    """Every failure of a POST exchange costs EXACTLY one spawn for the whole request."""

    CASES: ClassVar[dict[str, str]] = {
        "dns_error": "sys.stdin.buffer.read(); emit({'v':1,'ok':False,'code':'dns_error'})",
        "connect_error": "sys.stdin.buffer.read(); emit({'v':1,'ok':False,'code':'connect_error'})",
        "tls_error": "sys.stdin.buffer.read(); emit({'v':1,'ok':False,'code':'tls_error'})",
        "tls_verify_failed": "sys.stdin.buffer.read(); emit({'v':1,'ok':False,'code':'tls_verify_failed'})",
        "timeout code": "sys.stdin.buffer.read(); emit({'v':1,'ok':False,'code':'timeout'})",
        "worker_error": "sys.stdin.buffer.read(); emit({'v':1,'ok':False,'code':'worker_error'})",
        "invalid ipc": "sys.stdin.buffer.read(); print('not json')",
        "empty ipc": "sys.stdin.buffer.read()",
        "nonzero exit": "sys.stdin.buffer.read(); ok(); sys.stdout.flush(); sys.exit(3)",
        "killed": "sys.stdin.buffer.read(); os.kill(os.getpid(), 9)",
    }

    def test_each_failure_is_one_spawn_one_child_and_a_free_slot(self):
        for name, script in self.CASES.items():
            with self.subTest(name):
                before = len(self.spawn_args)
                exc = self.failure(self.client(script, timeout=4.0))
                self.assertIsInstance(exc, TransportError)
                self.assertEqual(len(self.spawn_args) - before, 1)
                self.assertEqual(transport.child_slot_status(), ("free", None))
        self.assertAllDead()

    def test_a_real_timeout_is_one_spawn_and_no_retry(self):
        before = len(self.spawn_args)
        exc = self.failure(
            self.client("sys.stdin.buffer.read(); time.sleep(60)", timeout=0.5)
        )
        self.assertEqual(exc.code, "timeout")
        self.assertEqual(len(self.spawn_args) - before, 1)
        self.assertAllDead()

    def test_a_spawn_failure_is_one_attempt_and_no_retry(self):
        attempts: list[int] = []

        def boom(*_a: Any, **_k: Any):
            attempts.append(1)
            raise OSError("no fork")

        exc = self.failure(self.client("ok()", _spawn=boom))
        self.assertEqual(exc.code, "spawn_failed")
        self.assertEqual(attempts, [1])


class ConfiguredLimitsTest(SupervisorCase):
    def test_the_default_client_timeout_is_ten_seconds(self):
        self.assertEqual(transport.DEFAULT_TIMEOUT, 10.0)
        self.assertEqual(HttpsClient()._timeout, 10.0)

    def test_only_the_default_client_gives_the_send_route_sixty_seconds(self):
        # The host answers a send only after dispatching it (it may first start a
        # dormant conversation); every other route keeps the ten-second default.
        self.assertEqual(transport.SEND_TIMEOUT, 60.0)
        self.assertEqual(HttpsClient()._send_timeout, 60.0)
        self.assertEqual(HttpsClient(timeout=3.0)._send_timeout, 3.0)
        self.assertEqual(HttpsClient(send_timeout=20.0)._send_timeout, 20.0)
        for bad in (60.01, 0, True, "10"):
            with self.subTest(bad=bad):
                with self.assertRaises(TransportError) as ctx:
                    HttpsClient(send_timeout=bad)  # type: ignore[arg-type]
                self.assertEqual(ctx.exception.code, "request_invalid")

    def test_the_send_route_deadline_is_sixty_seconds_on_a_fake_clock(self):
        readings: list[float] = []

        def mono() -> float:
            readings.append(float(len(readings)))
            return readings[-1]

        def spawn(*_a: Any, **_k: Any) -> FakeProc:
            proc = FakeProc()
            proc.reapable = True
            self.addCleanup(proc.cleanup)
            return proc

        client = HttpsClient(worker_command=["unused"], monotonic=mono, _spawn=spawn)
        route = transport.route_send("sess-fixture-0a1b2c3d")
        with mock.patch.object(
            transport.selectors.DefaultSelector, "select", lambda *_a, **_k: []
        ):
            with self.assertRaises(TransportError) as ctx:
                client.request(
                    transport.Origin.parse("https://host.example"), route, body=b"{}"
                )
        self.assertEqual((ctx.exception.code, ctx.exception.maybe_sent), ("timeout", True))
        self.assertEqual(readings, [float(n) for n in range(61)])

    def test_the_default_deadline_is_exactly_ten_seconds_on_a_fake_clock(self):
        readings: list[float] = []

        def mono() -> float:
            readings.append(float(len(readings)))
            return readings[-1]

        procs: list[FakeProc] = []

        def spawn(*_a: Any, **_k: Any) -> FakeProc:
            proc = FakeProc()
            proc.reapable = True
            procs.append(proc)
            self.addCleanup(proc.cleanup)
            return proc

        client = HttpsClient(worker_command=["unused"], monotonic=mono, _spawn=spawn)
        with mock.patch.object(
            transport.selectors.DefaultSelector, "select", lambda *_a, **_k: []
        ):
            exc = self.failure(client)
        self.assertEqual((exc.code, exc.maybe_sent), ("timeout", True))
        # one reading fixes the deadline (0 + 10); the loop then reads 1..10 and
        # expires exactly when the clock reaches 10.0, not before and not after
        self.assertEqual(readings, [float(n) for n in range(11)])
        self.assertEqual(len(procs), 1)

    def test_the_maximum_configurable_timeout_is_sixty_seconds(self):
        self.assertEqual(_https_worker.MAX_TIMEOUT, 60.0)
        HttpsClient(timeout=60.0)
        for bad in (60.01, 61, 0, -1.0, True, "10", None):
            with self.subTest(bad=bad):
                with self.assertRaises(TransportError) as ctx:
                    HttpsClient(timeout=bad)  # type: ignore[arg-type]
                self.assertEqual(ctx.exception.code, "request_invalid")

    def test_the_spawn_flags_and_environment_are_pinned(self):
        with mock.patch.dict(os.environ, {"PET_CANARY_SECRET": CANARY}):
            self.call(self.client("sys.stdin.buffer.read(); ok()"))
        ((args, kwargs),) = self.spawn_args
        self.assertEqual(
            set(kwargs),
            {
                "stdin",
                "stdout",
                "stderr",
                "env",
                "close_fds",
                "cwd",
                "start_new_session",
            },
        )
        self.assertEqual(kwargs["cwd"], "/")
        self.assertIs(kwargs["start_new_session"], True)
        self.assertIs(kwargs["close_fds"], True)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["env"], {"PYTHONDONTWRITEBYTECODE": "1"})
        self.assertNotIn(CANARY, repr(args) + repr(kwargs))
        self.assertEqual(len(args), 1)  # fixed argv only; no shell string

    def test_a_closed_stdout_without_exit_still_times_out_and_is_reaped(self):
        script = "sys.stdin.buffer.read(); ok(); sys.stdout.flush(); os.close(1); time.sleep(60)"
        exc = self.failure(self.client(script, timeout=0.8))
        self.assertEqual(exc.code, "timeout")
        self.assertAllDead()

    def test_a_broken_pipe_on_the_request_write_does_not_crash_the_parent(self):
        real_write = os.write

        def broken(fd: int, data: Any) -> int:
            stdin = self.procs[-1].stdin if self.procs else None
            if stdin is not None and fd == stdin.fileno():
                raise BrokenPipeError
            return real_write(fd, data)

        with mock.patch.object(transport.os, "write", broken):
            result = self.call(self.client("ok()", timeout=4.0))
        self.assertEqual(result.status, 200)  # the child's status decides, no crash
        self.assertAllDead()


class SlotTest(SupervisorCase):
    def test_slot_is_free_after_success_and_after_every_failure(self):
        self.call(self.client("sys.stdin.buffer.read(); ok()"))
        self.assertEqual(transport.child_slot_status(), ("free", None))
        self.failure(
            self.client("sys.stdin.buffer.read(); time.sleep(60)", timeout=0.5)
        )
        self.assertEqual(transport.child_slot_status(), ("free", None))

    def test_concurrent_call_fails_closed_while_a_child_is_active(self):
        first_error: list[TransportError] = []
        c1 = self.client("sys.stdin.buffer.read(); time.sleep(60)", timeout=1.5)

        def first() -> None:
            try:
                self.call(c1)
            except TransportError as exc:
                first_error.append(exc)

        t = threading.Thread(target=first)
        t.start()
        self.assertTrue(self.created.wait(5))
        self.assertEqual(transport.child_slot_status()[0], "busy")
        before = len(self.spawn_args)
        exc = self.failure(self.client("ok()"))
        self.assertEqual((exc.code, exc.maybe_sent), ("busy", False))
        self.assertEqual(len(self.spawn_args), before)  # no second child
        t.join(10)
        self.assertEqual(first_error[0].code, "timeout")
        self.assertAllDead()
        self.assertEqual(transport.child_slot_status(), ("free", None))
        self.assertEqual(
            self.call(self.client("sys.stdin.buffer.read(); ok()")).status, 200
        )

    def test_busy_slot_is_not_reentrant_from_the_same_thread(self):
        script = """
            sys.stdin.buffer.read()
            ok()
        """
        inner: list[TransportError] = []
        outer = self.client(script)
        original = transport._parse_ipc

        def reenter(*a: Any, **k: Any):
            try:
                self.call(self.client("ok()"))
            except TransportError as exc:
                inner.append(exc)
            return original(*a, **k)

        with mock.patch.object(transport, "_parse_ipc", reenter):
            self.call(outer)
        self.assertEqual(inner[0].code, "busy")


class FakeProc:
    """A child that cannot be reaped (kill is accepted but wait never succeeds)."""

    pid = 424242

    def __init__(self, *_a: Any, **_k: Any) -> None:
        r1, w1 = os.pipe()
        r2, w2 = os.pipe()
        self.stdin = os.fdopen(w1, "wb", buffering=0)
        self.stdout = os.fdopen(r2, "rb", buffering=0)
        self._extra = (r1, w2)
        self.returncode = None
        self.reapable = False
        self.kills = 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.kills += 1

    def wait(self, timeout=None):
        if self.reapable:
            self.returncode = -9
            return -9
        raise subprocess.TimeoutExpired("fake", timeout or 0.0)

    def cleanup(self):
        for fd in self._extra:
            with contextlib.suppress(OSError):
                os.close(fd)


class UnreapableChildTest(SupervisorCase):
    def test_unreaped_child_faults_the_slot_until_released(self):
        procs: list[FakeProc] = []

        def spawn(*a: Any, **k: Any):
            p = FakeProc()
            procs.append(p)
            self.addCleanup(p.cleanup)
            return p

        c = self.client("ok()", timeout=0.4, _spawn=spawn)
        started = time.monotonic()
        exc = self.failure(c)
        self.assertEqual((exc.code, exc.maybe_sent), ("child_unreaped", True))
        self.assertGreaterEqual(procs[0].kills, 1)
        # operation budget (0.4 s) and cleanup wait (<= 2 s) are separate
        self.assertLess(time.monotonic() - started, 0.4 + transport.REAP_TIMEOUT + 2.0)
        self.assertEqual(transport.child_slot_status(), ("faulted", 424242))
        # no further calls until resolved; nothing is spawned
        again = self.failure(self.client("ok()", _spawn=spawn))
        self.assertEqual((again.code, again.maybe_sent), ("child_faulted", False))
        self.assertEqual(len(procs), 1)
        self.assertFalse(transport.release_faulted_child())
        self.assertEqual(transport.child_slot_status(), ("faulted", 424242))
        procs[0].reapable = True
        self.assertTrue(transport.release_faulted_child())
        self.assertEqual(transport.child_slot_status(), ("free", None))
        self.assertEqual(
            self.call(self.client("sys.stdin.buffer.read(); ok()")).status, 200
        )


class InterruptedCleanupTest(SupervisorCase):
    """A BaseException (or any error) inside kill/wait must never leave the slot busy.

    The residual window inside ``Popen.__init__`` before a handle exists is NOT
    covered here (no child handle exists to fault); that is stated, not proven.
    """

    def interrupted_proc(self, *, kill=None, wait=None) -> type[FakeProc]:
        test = self
        errors = {"kill": kill, "wait": wait}

        class Interrupted(FakeProc):
            def kill(self) -> None:
                if errors["kill"] is not None:
                    exc, errors["kill"] = errors["kill"], None
                    raise exc
                super().kill()

            def wait(self, timeout=None):
                if errors["wait"] is not None:
                    exc, errors["wait"] = errors["wait"], None
                    raise exc
                return super().wait(timeout)

        procs: list[Any] = test.__dict__.setdefault("fake_procs", [])
        self.spawned = procs

        def spawn(*_a: Any, **_k: Any):
            proc = Interrupted()
            procs.append(proc)
            test.addCleanup(proc.cleanup)
            return proc

        self.fake_spawn = spawn
        return Interrupted

    def run_interrupted(self, *, kill=None, wait=None, headers=None):
        self.interrupted_proc(kill=kill, wait=wait)
        client = self.client("ok()", timeout=0.3, _spawn=self.fake_spawn)
        headers = headers or {
            "Authorization": "Bearer " + CANARY,
            "X-Amplifier-Client": "cid",
        }
        return client, headers

    def assertFaultedWithTheExactPid(self) -> None:
        self.assertEqual(transport.child_slot_status(), ("faulted", 424242))
        self.assertEqual(len(self.spawned), 1)
        again = self.failure(self.client("ok()", _spawn=self.fake_spawn))
        self.assertEqual((again.code, again.maybe_sent), ("child_faulted", False))
        self.assertEqual(len(self.spawned), 1)  # zero extra spawns

    def assertRecoversOnlyWhenReaped(self) -> None:
        self.assertFalse(transport.release_faulted_child())  # alive: stays faulted
        self.assertEqual(transport.child_slot_status(), ("faulted", 424242))
        self.spawned[0].reapable = True
        self.assertTrue(transport.release_faulted_child())
        self.assertEqual(transport.child_slot_status(), ("free", None))

    def caught(self, client: HttpsClient, headers: Any) -> BaseException:
        try:
            self.call(client, headers=headers)
        except BaseException as exc:  # noqa: BLE001 - the test inspects the type
            return exc
        self.fail("expected an exception")

    def assertNoCanary(self, exc: BaseException) -> None:
        text = repr(exc) + str(exc) + repr(transport.child_slot_status())
        text += repr(self.spawn_args) + "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        self.assertNotIn(CANARY, text)

    def test_keyboard_interrupt_in_kill_faults_the_exact_pid(self):
        client, headers = self.run_interrupted(kill=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt) as ctx:
            self.call(client, headers=headers)
        self.assertNoCanary(ctx.exception)
        self.assertFaultedWithTheExactPid()
        self.assertRecoversOnlyWhenReaped()

    def test_keyboard_interrupt_in_wait_faults_the_exact_pid(self):
        client, headers = self.run_interrupted(wait=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt) as ctx:
            self.call(client, headers=headers)
        self.assertNoCanary(ctx.exception)
        self.assertFaultedWithTheExactPid()
        self.assertRecoversOnlyWhenReaped()

    def test_runtime_error_in_wait_is_a_fixed_unreaped_error_and_faults(self):
        client, headers = self.run_interrupted(wait=RuntimeError(CANARY))
        exc = self.caught(client, headers)
        self.assertIsInstance(exc, TransportError)
        assert isinstance(exc, TransportError)
        self.assertEqual((exc.code, exc.maybe_sent), ("child_unreaped", True))
        self.assertNoCanary(exc)
        self.assertFaultedWithTheExactPid()
        self.assertRecoversOnlyWhenReaped()

    def test_the_original_interrupt_is_kept_when_cleanup_also_fails(self):
        real_select = transport.selectors.DefaultSelector.select
        state = {"n": 0}

        def interrupting(self_, timeout=None):
            state["n"] += 1
            if state["n"] == 2:
                raise KeyboardInterrupt
            return real_select(self_, timeout)

        client, headers = self.run_interrupted(wait=RuntimeError("cleanup"))
        with mock.patch.object(
            transport.selectors.DefaultSelector, "select", interrupting
        ):
            exc = self.caught(client, headers)
        self.assertIsInstance(exc, KeyboardInterrupt)
        self.assertNoCanary(exc)
        self.assertFaultedWithTheExactPid()


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
