"""Offline tests for the private HTTPS worker's pure exchange logic.

The connection is always a recording fake; the default connector is checked with a
patched ``HTTPSConnection``.  The one real child process used here is given an
INVALID request, so it exits before any connection is attempted.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import signal
import socket
import ssl
import subprocess
import sys
from pathlib import Path
from typing import Any, BinaryIO, cast
from unittest import mock

from pet_bridge import _https_worker as worker

from .fakes import OfflineTestCase

CANARY = "CANARY-TOKEN-9f3a"


class FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self._headers = {k.lower(): v for k, v in headers.items()}
        self._body = body
        self.read_calls: list[int] = []

    def getheader(self, name: str, default: Any = None) -> Any:
        return self._headers.get(name.lower(), default)

    def read(self, amt: int | None = None) -> bytes:
        self.read_calls.append(-1 if amt is None else amt)
        return self._body if amt is None else self._body[:amt]


class FakeConn:
    def __init__(self, response: FakeResponse | BaseException) -> None:
        self.response = response
        self.requests: list[tuple[Any, ...]] = []
        self.closed = 0

    def request(self, method, target, body=None, headers=None) -> None:
        self.requests.append((method, target, body, dict(headers or {})))
        if isinstance(self.response, BaseException) and self.requests:
            raise self.response

    def getresponse(self) -> FakeResponse:
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    def close(self) -> None:
        self.closed += 1


class Connector:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, host, port, timeout, context):
        self.calls.append((host, port, timeout, context))
        return self.conn


def request(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "v": 1,
        "host": "unified.example.ts.net",
        "port": 443,
        "method": "GET",
        "target": "/api/sessions?view=observation&scope=roots&activity=all&pinned=all&limit=200",
        "headers": {"Authorization": "Bearer " + CANARY, "Accept": "application/json"},
        "body": "",
        "max_request": 0,
        "max_response": 1024,
        "timeout": 10.0,
    }
    base.update(overrides)
    return base


def ok_response(
    body: bytes = b'{"a":1}',
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> FakeResponse:
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    return FakeResponse(status, h, body)


def exchange(req: dict[str, Any], conn: FakeConn) -> tuple[dict[str, Any], Connector]:
    connector = Connector(conn)
    return worker.exchange(worker.validate_request(req), connector), connector


class ContextTest(OfflineTestCase):
    def test_tls_context_is_strict(self):
        ctx = worker.make_context()
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_default_connector_uses_https_connection_with_that_context(self):
        created = []

        class Recorder:
            def __init__(self, *args, **kwargs):
                created.append((args, kwargs))

        with mock.patch.object(worker.http.client, "HTTPSConnection", Recorder):
            ctx = worker.make_context()
            worker.connect("h.example", 8941, 7.5, ctx)
        args, kwargs = created[0]
        self.assertEqual(args, ("h.example", 8941))
        self.assertEqual(kwargs, {"timeout": 7.5, "context": ctx})


class ExchangeTest(OfflineTestCase):
    def test_success_returns_status_and_body_and_closes(self):
        conn = FakeConn(ok_response(b'{"ok":true}'))
        result, connector = exchange(request(), conn)
        self.assertEqual(result, {"ok": True, "status": 200, "body": b'{"ok":true}'})
        self.assertEqual(conn.closed, 1)
        self.assertEqual(len(connector.calls), 1)
        host, port, timeout, ctx = connector.calls[0]
        self.assertEqual((host, port, timeout), ("unified.example.ts.net", 443, 10.0))
        self.assertTrue(ctx.check_hostname)
        method, target, body, headers = conn.requests[0]
        self.assertEqual((method, target, body), ("GET", request()["target"], None))
        self.assertEqual(headers["Connection"], "close")
        self.assertEqual(headers["Authorization"], "Bearer " + CANARY)

    def test_post_sends_decoded_body_exactly_once(self):
        payload = b'{"id":"x"}'
        req = request(
            method="POST",
            target="/api/devices/pair",
            body=base64.b64encode(payload).decode(),
            max_request=1024,
            headers={"Content-Type": "application/json"},
        )
        conn = FakeConn(ok_response())
        result, connector = exchange(req, conn)
        self.assertTrue(result["ok"])
        self.assertEqual(conn.requests[0][2], payload)
        self.assertEqual(len(conn.requests), 1)  # never retried
        self.assertEqual(len(connector.calls), 1)

    def test_redirects_are_refused_not_followed(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                conn = FakeConn(
                    FakeResponse(status, {"Location": "https://evil.example/"}, b"")
                )
                result, _ = exchange(request(), conn)
                self.assertEqual(result, {"ok": False, "code": "redirect_refused"})
                self.assertEqual(len(conn.requests), 1)
                self.assertEqual(conn.closed, 1)

    def test_declared_length_over_cap_is_refused_without_reading(self):
        resp = ok_response(b"x" * 10, headers={"Content-Length": "1025"})
        result, _ = exchange(request(), FakeConn(resp))
        self.assertEqual(result, {"ok": False, "code": "response_too_large"})
        self.assertEqual(resp.read_calls, [])

    def test_bad_declared_length_is_refused(self):
        for value in ("-1", "abc", "1,2", "0x10", "1e3", " "):
            with self.subTest(value=value):
                resp = ok_response(b"{}", headers={"Content-Length": value})
                result, _ = exchange(request(), FakeConn(resp))
                self.assertEqual(result, {"ok": False, "code": "response_invalid"})

    def test_streamed_body_over_cap_is_refused_and_exact_cap_is_allowed(self):
        body = b"{" + b" " * 1022 + b"}"  # exactly 1024
        self.assertEqual(len(body), 1024)
        result, _ = exchange(request(), FakeConn(ok_response(body)))
        self.assertTrue(result["ok"])
        resp = ok_response(body + b" ")
        result, _ = exchange(request(), FakeConn(resp))
        self.assertEqual(result, {"ok": False, "code": "response_too_large"})
        self.assertEqual(resp.read_calls, [1025])  # bounded read of cap + 1

    def test_success_requires_json_content_type(self):
        good = (
            "application/json",
            "application/json; charset=utf-8",
            "Application/JSON;charset=UTF-8",
        )
        for value in good:
            with self.subTest(good=value):
                result, _ = exchange(
                    request(),
                    FakeConn(FakeResponse(200, {"Content-Type": value}, b"{}")),
                )
                self.assertTrue(result["ok"])
        bad = (
            None,
            "text/html",
            "application/jsonx",
            "text/json",
            "application/json; boundary=x",
            "application/json; charset=latin-1",
            "application/json, text/html",
            "",
            "application/xml+json",
        )
        for value in bad:
            with self.subTest(bad=value):
                headers = {} if value is None else {"Content-Type": value}
                result, _ = exchange(
                    request(), FakeConn(FakeResponse(200, headers, b"{}"))
                )
                self.assertEqual(result, {"ok": False, "code": "content_type_invalid"})

    def test_error_statuses_return_raw_status_and_body_for_the_caller(self):
        for status in (400, 401, 403, 404, 409, 410, 429, 500, 503, 504):
            with self.subTest(status=status):
                conn = FakeConn(
                    FakeResponse(status, {"Content-Type": "text/plain"}, b"plain words")
                )
                result, _ = exchange(request(), conn)
                self.assertEqual(
                    result, {"ok": True, "status": status, "body": b"plain words"}
                )

    def test_failures_are_mapped_to_fixed_codes_and_close_the_connection(self):
        marker = "SECRET-IN-MESSAGE"
        cases = [
            (ssl.SSLCertVerificationError(1, marker), "tls_verify_failed"),
            (ssl.SSLError(1, marker), "tls_error"),
            (TimeoutError(marker), "timeout"),
            (__import__("socket").timeout(marker), "timeout"),
            (__import__("socket").gaierror(-2, marker), "dns_error"),
            (ConnectionRefusedError(marker), "connect_error"),
            (OSError(5, marker), "connect_error"),
            (
                __import__("http.client").client.IncompleteRead(b"x"),
                "http_protocol_error",
            ),
            (
                __import__("http.client").client.BadStatusLine(marker),
                "http_protocol_error",
            ),
            (RuntimeError(marker), "worker_error"),
            (ValueError(marker), "worker_error"),
        ]
        for exc, code in cases:
            with self.subTest(code=code, exc=type(exc).__name__):
                conn = FakeConn(exc)
                result, connector = exchange(request(), conn)
                self.assertEqual(result, {"ok": False, "code": code})
                self.assertNotIn(marker, json.dumps(result))
                self.assertEqual(conn.closed, 1)
                self.assertEqual(len(connector.calls), 1)  # no retry
                self.assertEqual(len(conn.requests), 1)

    def test_connect_failure_returns_a_code_and_never_retries(self):
        calls = []

        def refuse(host, port, timeout, context):
            calls.append(1)
            raise ConnectionRefusedError("nope")

        result = worker.exchange(worker.validate_request(request()), refuse)
        self.assertEqual(result, {"ok": False, "code": "connect_error"})
        self.assertEqual(len(calls), 1)


class StagedConn:
    """A fake connection that fails at ONE stage and counts every call it receives."""

    def __init__(self, stage: str, error: BaseException) -> None:
        self.stage, self.error = stage, error
        self.requests = 0
        self.responses = 0
        self.reads = 0
        self.closed = 0

    def request(self, method, target, body=None, headers=None) -> None:
        self.requests += 1
        if self.stage == "request":
            raise self.error

    def getresponse(self) -> Any:
        self.responses += 1
        if self.stage == "response":
            raise self.error
        conn = self

        class Response(FakeResponse):
            def read(self, amt: int | None = None) -> bytes:
                conn.reads += 1
                raise conn.error

        return Response(200, {"Content-Type": "application/json"}, b"{}")

    def close(self) -> None:
        self.closed += 1


class PostNeverRetriedTest(OfflineTestCase):
    """A POST whose request may have begun is attempted EXACTLY once at every stage."""

    ERRORS = (
        (socket.gaierror(-2, "x"), "dns_error"),
        (ConnectionRefusedError("x"), "connect_error"),
        (ssl.SSLError(1, "x"), "tls_error"),
        (ssl.SSLCertVerificationError(1, "x"), "tls_verify_failed"),
        (TimeoutError("x"), "timeout"),
        (ConnectionResetError("x"), "connect_error"),
        (http.client.BadStatusLine("x"), "http_protocol_error"),
        (RuntimeError("x"), "worker_error"),
    )

    def post_request(self) -> dict[str, Any]:
        return worker.validate_request(
            request(
                method="POST",
                target="/api/devices/pair",
                body=base64.b64encode(b'{"id":"x"}').decode(),
                max_request=1024,
                headers={"Content-Type": "application/json"},
            )
        )

    def test_every_failure_after_the_connect_stage_is_one_request_one_connection(self):
        for stage in ("request", "response", "read"):
            for error, code in self.ERRORS:
                with self.subTest(stage=stage, error=type(error).__name__):
                    conn = StagedConn(stage, error)
                    connector = Connector(conn)  # type: ignore[arg-type]
                    result = worker.exchange(self.post_request(), connector)
                    self.assertEqual(result, {"ok": False, "code": code})
                    self.assertEqual(len(connector.calls), 1)
                    self.assertEqual(conn.requests, 1)
                    expected_responses = 0 if stage == "request" else 1
                    self.assertEqual(conn.responses, expected_responses)
                    self.assertEqual(conn.reads, 1 if stage == "read" else 0)
                    self.assertEqual(conn.closed, 1)

    def test_a_connect_stage_failure_is_one_connection_attempt_and_no_request(self):
        for error, code in self.ERRORS:
            with self.subTest(error=type(error).__name__):
                calls: list[int] = []

                def refuse(host, port, timeout, context, _error=error, _calls=calls):
                    _calls.append(1)
                    raise _error

                result = worker.exchange(self.post_request(), refuse)
                self.assertEqual(result, {"ok": False, "code": code})
                self.assertEqual(calls, [1])


class RunPostNeverRetriedTest(OfflineTestCase):
    """The real ``run`` entry point attempts a failed POST EXACTLY once.

    ``PostNeverRetriedTest`` drives ``exchange``; a retry added in ``run`` itself
    (after a failed exchange) would escape it.  Here the valid POST arrives as IPC
    bytes, ``run`` and ``exchange`` are NOT mocked, and the injected connector and
    connection count every call, so the production behaviour is pinned end to end.
    """

    PAYLOAD = b'{"id":"x"}'

    def post_document(self) -> bytes:
        return json.dumps(
            request(
                method="POST",
                target="/api/devices/pair",
                body=base64.b64encode(self.PAYLOAD).decode(),
                max_request=1024,
                headers={"Content-Type": "application/json"},
            )
        ).encode()

    def run_post(self, connector: Any) -> dict[str, Any]:
        out = io.BytesIO()
        self.assertEqual(
            worker.run(io.BytesIO(self.post_document()), out, connector), 0
        )
        self.assertEqual(out.getvalue().count(b"\n"), 1)
        return json.loads(out.getvalue())

    def test_success_is_one_connection_and_one_http_request_with_the_body(self):
        conn = FakeConn(ok_response())
        connector = Connector(conn)
        result = self.run_post(connector)
        self.assertEqual((result["v"], result["ok"], result["status"]), (1, True, 200))
        self.assertEqual(len(connector.calls), 1)
        self.assertEqual(len(conn.requests), 1)
        self.assertEqual(
            conn.requests[0][:3], ("POST", "/api/devices/pair", self.PAYLOAD)
        )
        self.assertEqual(conn.closed, 1)

    def test_a_failure_after_the_request_began_is_one_connection_one_request(self):
        for stage in ("request", "response", "read"):
            for error, code in PostNeverRetriedTest.ERRORS:
                with self.subTest(stage=stage, error=type(error).__name__):
                    conn = StagedConn(stage, error)
                    connector = Connector(conn)  # type: ignore[arg-type]
                    result = self.run_post(connector)
                    self.assertEqual(result, {"v": 1, "ok": False, "code": code})
                    self.assertEqual(len(connector.calls), 1)  # never reconnected
                    self.assertEqual(conn.requests, 1)  # never re-sent
                    self.assertEqual(conn.responses, 0 if stage == "request" else 1)
                    self.assertEqual(conn.reads, 1 if stage == "read" else 0)
                    self.assertEqual(conn.closed, 1)

    def test_a_connect_failure_is_one_connection_attempt_and_no_http_request(self):
        for error, code in PostNeverRetriedTest.ERRORS:
            with self.subTest(error=type(error).__name__):
                attempts: list[int] = []

                def refuse(host, port, timeout, context, _e=error, _a=attempts):
                    _a.append(1)
                    raise _e

                result = self.run_post(refuse)
                self.assertEqual(result, {"v": 1, "ok": False, "code": code})
                self.assertEqual(
                    attempts, [1]
                )  # one attempt; no HTTP request can exist


class ValidateRequestTest(OfflineTestCase):
    def test_a_valid_request_decodes_the_body(self):
        req = worker.validate_request(
            request(
                method="POST", body=base64.b64encode(b"abc").decode(), max_request=16
            )
        )
        self.assertEqual(req["body"], b"abc")

    def test_rejects_malformed_requests(self):
        long_target = "/" + "a" * 2048
        cases = {
            "version": {"v": 2},
            "bool version": {"v": True},
            "host space": {"host": "a b.example"},
            "host path": {"host": "a.example/x"},
            "host upper-unicode": {"host": "h\u00f6st.example"},
            "host empty": {"host": ""},
            "port bool": {"port": True},
            "port zero": {"port": 0},
            "port large": {"port": 70000},
            "port text": {"port": "443"},
            "method": {"method": "PUT"},
            "method lower": {"method": "get"},
            "target no slash": {"target": "api/x"},
            "target space": {"target": "/a b"},
            "target fragment": {"target": "/a#b"},
            "target ctl": {"target": "/a\r\nHost: evil"},
            "target long": {"target": long_target},
            "header name": {"headers": {"Cookie": "a"}},
            "header host": {"headers": {"Host": "evil.example"}},
            "header crlf": {"headers": {"Accept": "a\r\nX: y"}},
            "header nonascii": {"headers": {"Accept": "\u00e9"}},
            "header type": {"headers": {"Accept": 5}},
            "headers list": {"headers": []},
            "get with body": {
                "body": base64.b64encode(b"x").decode(),
                "max_request": 16,
            },
            "body not base64": {"method": "POST", "body": "***", "max_request": 16},
            "body over max_request": {
                "method": "POST",
                "body": base64.b64encode(b"x" * 20).decode(),
                "max_request": 16,
            },
            "max_request big": {"method": "POST", "max_request": 16 * 1024 + 1},
            "max_response zero": {"max_response": 0},
            "max_response big": {"max_response": 1024 * 1024 + 1},
            "timeout zero": {"timeout": 0},
            "timeout big": {"timeout": 61},
            "timeout bool": {"timeout": True},
            "extra key": {"extra": 1},
        }
        for name, change in cases.items():
            with self.subTest(name):
                with self.assertRaises(worker.WorkerError) as caught:
                    worker.validate_request(request(**change))
                exc = caught.exception
                self.assertEqual(exc.code, "request_invalid")
                self.assertNotIn(CANARY, str(exc) + repr(exc))

    def test_missing_key_and_non_dict_rejected(self):
        req = request()
        del req["host"]
        for value in (req, [], "x", None):
            with self.assertRaises(worker.WorkerError):
                worker.validate_request(value)


class RunTest(OfflineTestCase):
    def run_worker(self, data: bytes, conn: FakeConn | None = None) -> dict[str, Any]:
        out = io.BytesIO()
        connector = Connector(conn or FakeConn(ok_response()))
        code = worker.run(io.BytesIO(data), out, connector)
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().endswith(b"\n"))
        self.assertEqual(out.getvalue().count(b"\n"), 1)
        return json.loads(out.getvalue())

    def test_success_is_one_json_line_with_base64_body(self):
        result = self.run_worker(
            json.dumps(request()).encode(), FakeConn(ok_response(b'{"k":2}'))
        )
        self.assertEqual(result["v"], 1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(base64.b64decode(result["body"], validate=True), b'{"k":2}')

    def test_exchange_failure_is_a_fixed_code(self):
        result = self.run_worker(
            json.dumps(request()).encode(), FakeConn(TimeoutError("x"))
        )
        self.assertEqual(result, {"v": 1, "ok": False, "code": "timeout"})

    def test_malformed_input_never_reaches_the_connector_or_echoes(self):
        too_long = json.dumps(
            request(headers={"Authorization": CANARY * 4000})
        ).encode()
        for data in (
            b"",
            b"not json",
            b"[]",
            b"{}",
            b'{"v":1}{"v":1}',
            json.dumps(request()).encode() + b" trailing",
            json.dumps(request()).replace('"v": 1', '"v": 1, "v": 1').encode(),
            b'{"v":1,"host":NaN}',
            too_long,
            json.dumps(request(host="bad host")).encode(),
        ):
            with self.subTest(data=data[:24]):
                connector = Connector(FakeConn(ok_response()))
                out = io.BytesIO()
                self.assertEqual(worker.run(io.BytesIO(data), out, connector), 0)
                self.assertEqual(
                    json.loads(out.getvalue()),
                    {"v": 1, "ok": False, "code": "request_invalid"},
                )
                self.assertEqual(connector.calls, [])
                self.assertNotIn(CANARY.encode(), out.getvalue())

    def test_input_is_read_with_a_bound(self):
        class Endless(io.RawIOBase):
            read_total = 0

            def readable(self):
                return True

            def read(self, n=-1):
                if n is None or n < 0:
                    raise AssertionError("unbounded read")
                Endless.read_total += n
                return b"x" * n

        out = io.BytesIO()
        self.assertEqual(
            worker.run(
                cast(BinaryIO, Endless()), out, Connector(FakeConn(ok_response()))
            ),
            0,
        )
        self.assertLess(Endless.read_total, 64 * 1024)
        self.assertEqual(json.loads(out.getvalue())["code"], "request_invalid")


class HardenTest(OfflineTestCase):
    def test_core_dumps_disabled_and_finite_self_kill_alarm_armed(self):
        limits, alarms, handlers = [], [], []
        worker.harden(
            setrlimit=lambda *a: limits.append(a),
            alarm=lambda s: alarms.append(s),
            set_signal=lambda *a: handlers.append(a),
        )
        import resource

        self.assertEqual(limits, [(resource.RLIMIT_CORE, (0, 0))])
        self.assertEqual(handlers, [(signal.SIGALRM, signal.SIG_DFL)])
        self.assertEqual(len(alarms), 1)
        self.assertTrue(0 < alarms[0] <= 30)

    def test_the_alarm_is_armed_before_the_core_limit_is_attempted(self):
        order: list[str] = []
        worker.harden(
            setrlimit=lambda *a: order.append("rlimit"),
            alarm=lambda s: order.append("alarm"),
            set_signal=lambda *a: order.append("signal"),
        )
        self.assertEqual(order, ["signal", "alarm", "rlimit"])

    def test_a_core_limit_failure_is_never_swallowed_but_the_alarm_is_armed(self):
        for error in (OSError("x"), ValueError("x"), ImportError("x")):
            with self.subTest(error=type(error).__name__):
                alarms: list[int] = []

                def boom(*_a: Any, _error: BaseException = error) -> None:
                    raise _error

                with self.assertRaises(type(error)):
                    worker.harden(
                        setrlimit=boom,
                        alarm=alarms.append,
                        set_signal=lambda *a: None,
                    )
                self.assertEqual(alarms, [worker.HARD_ALARM_SECONDS])

    def test_a_missing_resource_module_is_never_swallowed(self):
        alarms: list[int] = []
        with (
            mock.patch.dict(sys.modules, {"resource": None}),
            self.assertRaises(ImportError),
        ):
            worker.harden(alarm=alarms.append, set_signal=lambda *a: None)
        self.assertEqual(alarms, [worker.HARD_ALARM_SECONDS])


class MainRefusesWithoutHardeningTest(OfflineTestCase):
    """If the core-dump limit cannot be set, main exits 70 before any secret is read.

    Everything process-global is patched: the real RLIMIT and alarm of this test
    process are never changed.
    """

    def run_main(self, error: BaseException) -> tuple[int, dict[str, Any]]:
        import resource

        seen: dict[str, Any] = {"stdin": [], "stdout": [], "run": [], "alarms": []}

        class Stdin:
            @property
            def buffer(self) -> io.BytesIO:
                seen["stdin"].append("buffer")
                return io.BytesIO(b"")

        class Stdout:
            @property
            def buffer(self) -> io.BytesIO:
                seen["stdout"].append("buffer")
                return io.BytesIO()

        def fake_run(*args: Any, **_kwargs: Any) -> int:
            seen["run"].append(args)
            return 0

        with (
            mock.patch.object(resource, "setrlimit", side_effect=error),
            mock.patch.object(worker.signal, "alarm", seen["alarms"].append),
            mock.patch.object(worker.signal, "signal"),
            mock.patch.object(worker, "run", fake_run),
            mock.patch.object(sys, "stdin", Stdin()),
            mock.patch.object(sys, "stdout", Stdout()),
        ):
            code = worker.main()
        return code, seen

    def test_failure_returns_the_fixed_status_before_any_input_or_output(self):
        for error in (OSError("x"), ValueError("x")):
            with self.subTest(error=type(error).__name__):
                code, seen = self.run_main(error)
                self.assertEqual(code, 70)
                self.assertEqual(seen["stdin"], [])  # no secret was read
                self.assertEqual(seen["stdout"], [])  # nothing was written
                self.assertEqual(seen["run"], [])  # no exchange
                self.assertEqual(seen["alarms"], [worker.HARD_ALARM_SECONDS])

    def test_a_missing_resource_module_also_refuses(self):
        seen: dict[str, list[Any]] = {"stdin": [], "run": []}

        class Stdin:
            @property
            def buffer(self) -> io.BytesIO:
                seen["stdin"].append(1)
                return io.BytesIO(b"")

        with (
            mock.patch.dict(sys.modules, {"resource": None}),
            mock.patch.object(worker.signal, "alarm"),
            mock.patch.object(worker.signal, "signal"),
            mock.patch.object(worker, "run", lambda *a, **k: seen["run"].append(1)),
            mock.patch.object(sys, "stdin", Stdin()),
        ):
            code = worker.main()
        self.assertEqual(code, 70)
        self.assertEqual(seen, {"stdin": [], "run": []})

    def test_when_hardening_works_main_runs_the_exchange(self):
        import resource

        calls: list[Any] = []
        with (
            mock.patch.object(resource, "setrlimit") as limit,
            mock.patch.object(worker.signal, "alarm"),
            mock.patch.object(worker.signal, "signal"),
            mock.patch.object(worker, "run", lambda *a, **k: calls.append(a) or 0),
        ):
            self.assertEqual(worker.main(), 0)
        limit.assert_called_once_with(resource.RLIMIT_CORE, (0, 0))
        self.assertEqual(len(calls), 1)


class RealProcessInvalidInputTest(OfflineTestCase):
    """The actual file, run with ``-I``, on input that fails before any connection."""

    def invoke(self, data: bytes) -> tuple[int, bytes, bytes]:
        path = Path(worker.__file__).resolve()
        proc = subprocess.run(
            [sys.executable, "-I", str(path)],
            input=data,
            capture_output=True,
            timeout=20,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
            cwd="/",
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_invalid_request_exits_zero_with_fixed_code_and_no_echo(self):
        data = json.dumps(request(host="bad host")).encode()
        code, out, err = self.invoke(data)
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out), {"v": 1, "ok": False, "code": "request_invalid"}
        )
        self.assertNotIn(CANARY.encode(), out + err)

    def test_empty_input(self):
        code, out, _ = self.invoke(b"")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["code"], "request_invalid")


class CapArithmeticTest(OfflineTestCase):
    def test_ipc_caps_include_base64_overhead_and_allow_a_full_detail_page(self):
        self.assertGreater(worker.ipc_response_cap(1024 * 1024), 1024 * 1024)
        encoded = base64.b64encode(b"\x00" * (1024 * 1024))
        self.assertGreaterEqual(worker.ipc_response_cap(1024 * 1024), len(encoded))
        self.assertGreaterEqual(
            worker.ipc_request_cap(16 * 1024),
            len(base64.b64encode(b"\x00" * 16 * 1024)),
        )
        self.assertGreater(worker.ipc_response_cap(1024 * 1024), 64 * 1024)
