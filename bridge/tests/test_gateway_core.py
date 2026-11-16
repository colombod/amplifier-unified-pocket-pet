"""Offline tests for the gateway core: CoreRunner, UnifiedOps, AudioOut, PetSession (U2).

Everything is in-process: no listener, no socket, no provider, no Unified, no device.
The sections follow plan v1 section 8 (``T-CORE-*``) plus the four manager corrections
(distinct delegation ids fail closed; zero emitted PCM is never ``completed``; Unified
preflight/poll are bound to the frozen binding; a cancelled runner job keeps its result).
These tests are offline evidence only: not a live provider, device or Unified result.
"""

# pyright: reportArgumentType=false, reportCallIssue=false, reportOptionalMemberAccess=false
# pyright: reportOptionalSubscript=false, reportAttributeAccessIssue=false
from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import pet_bridge
from pet_bridge import core
from pet_bridge import gateway_core as gc
from pet_bridge import gateway_provider as gp
from pet_bridge import gateway_state as gs
from pet_bridge.companion import Preflight
from pet_bridge.core import BridgeError, CommandResult, ReplyResult
from pet_bridge.journal import Journal

from .authfakes import (
    CLIENT_ID,
    CRED_REF,
    HOST_IDENTITY,
    INSTANCE,
    INSTANCE_2,
    ORIGIN_URL,
    TOKEN,
    FakeClient,
)
from .fakes import (
    SESSION,
    FakeClock,
    FakeTransport,
    IdFactory,
    OfflineTestCase,
    accepted_body,
    assistant_row,
    page,
    user_row,
)
from .gatewayfakes import (
    ASSISTANT_GREETING,
    ASSISTANT_START,
    AUDIO_CANARY,
    KEY_CANARY,
    PET_TOKEN_CANARY,
    REPLY_TEXT,
    TEXT_CANARY,
    TURN,
    FakeDeviceLink,
    FakeLoopClock,
    FakeOps,
    FakeProviderFactory,
    FakeProviderLink,
    FakeStore,
    OfflineAsyncCase,
    SwallowCancelFactory,
)
from .test_companion import lpage, srow
from .providerfixtures import provider_frame

PY_FILE = Path(pet_bridge.__file__).parent / "gateway_core.py"


async def until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait (real time, tiny steps) until ``predicate`` holds; fail loudly otherwise."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.001)


# =================================================================================
# CoreRunner
# =================================================================================
class RunnerCase(OfflineAsyncCase):
    async def asyncSetUp(self) -> None:
        self.runner = gc.CoreRunner()
        self.runner.start()
        self.addAsyncCleanup(self.runner.aclose)


class GuardTest(OfflineAsyncCase):
    async def test_t_guard_01_offline_guards_raise_for_every_entry_point(self):
        with self.assertRaises(AssertionError):
            socket.create_connection(("127.0.0.1", 9))
        with socket.socket() as sock:
            with self.assertRaises(AssertionError):
                sock.connect(("127.0.0.1", 9))
            with self.assertRaises(AssertionError):
                sock.bind(("127.0.0.1", 0))
            with self.assertRaises(AssertionError):
                sock.listen()
        loop = asyncio.get_running_loop()
        with self.assertRaises(AssertionError):
            await loop.create_server(lambda: None, "127.0.0.1", 0)
        with self.assertRaises(AssertionError):
            await loop.create_connection(lambda: None, "127.0.0.1", 9)


class RunnerTest(RunnerCase):
    def _blocking(self, gates, log, lock, counter):
        def make(index: int):
            def fn() -> int:
                with lock:
                    counter["active"] += 1
                    counter["max"] = max(counter["max"], counter["active"])
                    log.append(index)
                gates[index].wait(10)
                with lock:
                    counter["active"] -= 1
                return index

            return fn

        return make

    async def test_t_core_run_serial_three_blocked_ops_run_one_at_a_time_in_fifo_order(
        self,
    ):
        gates = [threading.Event() for _ in range(3)]
        log: list[int] = []
        counter = {"active": 0, "max": 0}
        make = self._blocking(gates, log, threading.Lock(), counter)
        tasks = [
            asyncio.create_task(self.runner.run("begin", make(i))) for i in range(3)
        ]
        await until(lambda: log == [0])
        self.assertEqual(self.runner.inflight_op, "begin")
        self.assertEqual(self.runner.depth, 2)
        for gate in gates:
            gate.set()
        self.assertEqual(await asyncio.gather(*tasks), [0, 1, 2])
        self.assertEqual((log, counter["max"]), ([0, 1, 2], 1))

    async def test_t_core_run_cancel_op_still_completes_and_worker_stays_busy(self):
        first = threading.Event()
        second_started = threading.Event()
        order: list[str] = []

        def slow() -> CommandResult:
            order.append("first-start")
            first.wait(10)
            order.append("first-end")
            return CommandResult(TURN, "accepted", "accepted", True)

        def next_op() -> str:
            second_started.set()
            order.append("second")
            return "two"

        t1 = asyncio.create_task(self.runner.run("confirm", slow))
        await until(lambda: order == ["first-start"])
        t1.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t1
        t2 = asyncio.create_task(self.runner.run("poll", next_op))
        for _ in range(30):
            await asyncio.sleep(0.001)
        self.assertFalse(second_started.is_set())  # the worker is still busy
        self.assertEqual(self.runner.inflight_op, "confirm")
        first.set()
        self.assertEqual(await t2, "two")
        self.assertEqual(order, ["first-start", "first-end", "second"])
        meta = [r for r in self.runner.retained() if r.op == "confirm"]
        self.assertEqual(len(meta), 1)
        self.assertTrue(meta[0].caller_cancelled)
        self.assertEqual((meta[0].outcome, meta[0].code), ("ok", "accepted:accepted"))

    async def test_t_core_run_bound_ninth_normal_job_is_busy_and_nothing_is_queued(
        self,
    ):
        gate = threading.Event()
        ran: list[int] = []
        head = self.runner.submit("begin", lambda: gate.wait(10))
        await until(lambda: self.runner.inflight_op == "begin")
        queued = [
            self.runner.submit("poll", lambda i=i: ran.append(i)) for i in range(8)
        ]
        self.assertEqual(self.runner.depth, 8)
        with self.assertRaises(gc.RunnerError) as ctx:
            self.runner.submit("poll", lambda: ran.append(99))
        self.assertEqual(ctx.exception.code, "runner_busy")
        self.assertEqual(self.runner.depth, 8)
        gate.set()
        await asyncio.gather(head.wait(), *(t.wait() for t in queued))
        self.assertEqual(ran, list(range(8)))

    async def test_t_core_run_bound_urgent_stop_goes_to_the_front_and_is_coalesced(
        self,
    ):
        gate = threading.Event()
        order: list[str] = []
        head = self.runner.submit("confirm", lambda: gate.wait(10))
        await until(lambda: self.runner.inflight_op == "confirm")
        normal = [
            self.runner.submit("poll", lambda i=i: order.append(f"p{i}"))
            for i in range(8)
        ]
        stop_a = self.runner.submit("stop", lambda: order.append("stop"), urgent=True)
        stop_b = self.runner.submit("stop", lambda: order.append("stop2"), urgent=True)
        self.assertEqual(self.runner.depth, 9)  # urgent admission ignores capacity
        gate.set()
        await asyncio.gather(head.wait(), stop_a.wait(), stop_b.wait())
        await asyncio.gather(*(t.wait() for t in normal))
        self.assertEqual(order[0], "stop")
        self.assertNotIn("stop2", order)  # one execution for two urgent stops
        self.assertEqual(order[1:], [f"p{i}" for i in range(8)])
        self.assertEqual(stop_a.job_id, stop_b.job_id)

    async def test_t_core_run_close_aclose_waits_for_inflight_and_queued_jobs(self):
        gate = threading.Event()
        done: list[str] = []

        def slow() -> None:
            gate.wait(10)
            done.append("a")

        t1 = asyncio.create_task(self.runner.run("begin", slow))
        await until(lambda: self.runner.inflight_op == "begin")
        t2 = asyncio.create_task(self.runner.run("poll", lambda: done.append("b")))
        await asyncio.sleep(0.01)
        closer = asyncio.create_task(self.runner.aclose())
        await asyncio.sleep(0.01)
        self.assertFalse(closer.done())
        with self.assertRaises(gc.RunnerError) as ctx:
            await self.runner.run("poll", lambda: None)
        self.assertEqual(ctx.exception.code, "runner_closed")
        gate.set()
        await asyncio.gather(t1, t2, closer)
        self.assertEqual(done, ["a", "b"])
        self.assertEqual(
            [t for t in threading.enumerate() if t.name == "pet-core-runner"], []
        )

    async def test_t_core_run_exc_exception_identity_and_baseexception_handling(self):
        boom = BridgeError("some_code")

        def raiser() -> None:
            raise boom

        with self.assertRaises(BridgeError) as ctx:
            await self.runner.run("begin", raiser)
        self.assertIs(ctx.exception, boom)

        class Hard(BaseException):
            pass

        def hard() -> None:
            raise Hard()

        with self.assertRaises(gc.RunnerError) as ctx2:
            await self.runner.run("begin", hard)
        self.assertEqual(ctx2.exception.code, "runner_interrupted")
        self.assertEqual(await self.runner.run("status", lambda: 5), 5)  # still alive

    async def test_t_core_run_retain_is_bounded_and_holds_no_reply_text(self):
        for i in range(20):
            await self.runner.run("status", lambda i=i: i)
        retained = self.runner.retained()
        self.assertEqual(len(retained), 16)
        self.assertEqual([r.job for r in retained], sorted(r.job for r in retained))
        self.assertEqual(retained[-1].job - retained[0].job, 15)
        result = ReplyResult(TURN, "speak", "reply_complete", True, 3, REPLY_TEXT)
        await self.runner.run("poll", lambda: result)
        self.assertNotIn("REPLYCANARY", repr(self.runner.retained()))
        self.assertNotIn("REPLYCANARY", repr(self.runner) + str(self.runner))
        last = self.runner.retained()[-1]
        self.assertEqual(
            (last.op, last.outcome, last.code), ("poll", "ok", "speak:reply_complete")
        )

    async def test_t_core_run_retain_codes_are_fixed_and_never_arbitrary(self):
        def bad_code() -> None:
            raise BridgeError("lowercasecanarycode")

        class Weird(Exception):
            pass

        def weird() -> None:
            raise Weird("TEXTCANARY")

        for fn in (bad_code, weird):
            with self.assertRaises((BridgeError, Weird)):
                await self.runner.run("begin", fn)
        refused = CommandResult(TURN, "refused", "canaryreasonxyz", False)
        await self.runner.run("confirm", lambda: refused)
        codes = [r.code for r in self.runner.retained()]
        self.assertEqual(codes, ["exc:BridgeError", "exc:other", "refused:other"])
        text = repr(self.runner.retained())
        for leak in ("lowercasecanarycode", "TEXTCANARY", "canaryreasonxyz"):
            self.assertNotIn(leak, text)

    async def test_t_core_run_threads_exactly_one_thread_and_no_executor(self):
        baseline = threading.active_count()  # the fixture runner is already running
        extra = gc.CoreRunner()
        extra.start()
        self.assertEqual(threading.active_count(), baseline + 1)
        loop = asyncio.get_running_loop()
        with (
            mock.patch.object(loop, "run_in_executor", side_effect=AssertionError),
            mock.patch("asyncio.to_thread", side_effect=AssertionError),
        ):
            self.assertEqual(await extra.run("status", lambda: 1), 1)
            self.assertEqual(await self.runner.run("status", lambda: 2), 2)
        await extra.aclose()
        self.assertEqual(threading.active_count(), baseline)

    async def test_t_core_run_source_has_no_queue_executor_or_to_thread(self):
        tree = ast.parse(PY_FILE.read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
        }
        for banned in (
            "SimpleQueue",
            "to_thread",
            "run_in_executor",
            "ThreadPoolExecutor",
            "Executor",
        ):
            self.assertNotIn(banned, names)

    async def test_t_core_run_unknown_op_start_twice_and_not_started(self):
        with self.assertRaises(ValueError):
            self.runner.submit("rm_rf", lambda: None)
        with self.assertRaises(gc.RunnerError) as ctx:
            self.runner.start()
        self.assertEqual(ctx.exception.code, "runner_started")
        fresh = gc.CoreRunner()
        with self.assertRaises(gc.RunnerError) as ctx2:
            fresh.submit("status", lambda: None)
        self.assertEqual(ctx2.exception.code, "runner_not_started")

    async def test_t_core_run_wait_idle_returns_only_after_the_worker_settles(self):
        gate = threading.Event()
        ticket = self.runner.submit("begin", lambda: gate.wait(10))
        await until(lambda: self.runner.inflight_op == "begin")
        waiter = asyncio.create_task(self.runner.wait_idle())
        await asyncio.sleep(0.01)
        self.assertFalse(waiter.done())
        self.assertFalse(self.runner.idle)
        gate.set()
        await waiter
        self.assertTrue(self.runner.idle)
        await ticket.wait()


class RunnerResultRetentionTest(RunnerCase):
    """Manager correction 4: a cancelled caller never loses the job's real outcome."""

    async def _cancelled_job(self, fn: Callable[[], Any], gate: threading.Event):
        ticket = self.runner.submit("confirm", fn)
        waiter = asyncio.create_task(ticket.wait())
        await until(lambda: self.runner.inflight_op == "confirm")
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        gate.set()
        await self.runner.wait_idle()
        return ticket

    async def test_cancelled_caller_result_is_retrievable_and_not_a_metadata_stub(self):
        gate = threading.Event()
        real = CommandResult(TURN, "accepted", "accepted", True, {"k": 1})

        def fn() -> CommandResult:
            gate.wait(10)
            return real

        ticket = await self._cancelled_job(fn, gate)
        self.assertEqual(self.runner.held_results(), (ticket.job_id,))
        self.assertIs(self.runner.take_result(ticket.job_id), real)
        with self.assertRaises(gc.RunnerError) as ctx:
            self.runner.take_result(ticket.job_id)  # reconciled exactly once
        self.assertEqual(ctx.exception.code, "runner_result_unavailable")
        self.assertEqual(self.runner.held_results(), ())

    async def test_cancelled_caller_underlying_exception_is_retrievable_by_identity(
        self,
    ):
        gate = threading.Event()
        boom = BridgeError("faulted")

        def fn() -> None:
            gate.wait(10)
            raise boom

        ticket = await self._cancelled_job(fn, gate)
        with self.assertRaises(BridgeError) as ctx:
            self.runner.take_result(ticket.job_id)
        self.assertIs(ctx.exception, boom)

    async def test_take_result_before_settlement_and_for_a_delivered_job(self):
        gate = threading.Event()
        ticket = self.runner.submit("confirm", lambda: gate.wait(10) and 1)
        await until(lambda: self.runner.inflight_op == "confirm")
        with self.assertRaises(gc.RunnerError) as ctx:
            self.runner.take_result(ticket.job_id)
        self.assertEqual(ctx.exception.code, "runner_not_settled")
        gate.set()
        await ticket.wait()  # delivered to its caller: no second copy is held
        with self.assertRaises(gc.RunnerError) as ctx2:
            self.runner.take_result(ticket.job_id)
        self.assertEqual(ctx2.exception.code, "runner_result_unavailable")
        self.assertEqual(self.runner.held_results(), ())

    async def test_unreconciled_results_are_bounded_and_eviction_is_not_silent(self):
        tickets = []
        for index in range(20):
            tickets.append(self.runner.submit("status", lambda: "v"))
            if index % 6 == 5:
                await self.runner.wait_idle()  # stay inside the queue capacity
        await self.runner.wait_idle()  # nobody awaited: 20 undelivered results
        held = self.runner.held_results()
        self.assertEqual(len(held), 16)
        evicted = tickets[0].job_id
        with self.assertRaises(gc.RunnerError) as ctx:
            self.runner.take_result(evicted)
        self.assertEqual(ctx.exception.code, "runner_result_unavailable")
        self.assertEqual(self.runner.take_result(tickets[-1].job_id), "v")

    async def test_eviction_never_lets_a_second_job_overlap_an_inflight_one(self):
        gate = threading.Event()
        flag = {"second": False}
        for index in range(18):
            self.runner.submit("status", lambda: None)
            if index % 6 == 5:
                await self.runner.wait_idle()  # stay inside the queue capacity
        slow = self.runner.submit("confirm", lambda: gate.wait(10))
        second = self.runner.submit("poll", lambda: flag.update(second=True))
        await until(lambda: self.runner.inflight_op == "confirm")
        for _ in range(20):
            await asyncio.sleep(0.001)
        self.assertFalse(flag["second"])  # retention bookkeeping never frees the slot
        gate.set()
        await asyncio.gather(slow.wait(), second.wait())
        self.assertTrue(flag["second"])

    async def test_cancelled_aclose_cannot_drop_the_thread_or_the_job(self):
        gate = threading.Event()
        result = CommandResult(TURN, "accepted", "accepted", True)
        ticket = self.runner.submit("confirm", lambda: gate.wait(10) and result)
        await until(lambda: self.runner.inflight_op == "confirm")
        closer = asyncio.create_task(self.runner.aclose())
        await asyncio.sleep(0.01)
        closer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closer
        self.assertEqual(self.runner.inflight_op, "confirm")  # still running
        gate.set()
        self.assertIs(await ticket.wait(), result)
        await self.runner.aclose()  # a later close joins the same thread
        self.assertTrue(self.runner.close(timeout=5))
        self.assertEqual(
            [t for t in threading.enumerate() if t.name == "pet-core-runner"], []
        )

    async def test_sync_close_waits_for_settlement_and_reports_the_join(self):
        extra = gc.CoreRunner()
        extra.start()
        done: list[int] = []
        extra.submit("status", lambda: done.append(1))
        self.assertTrue(extra.close(timeout=5))
        self.assertEqual(done, [1])


class RunnerRetainTest(RunnerCase):
    """U2 repair D5: retention is per job; a settled job never keeps its callable."""

    async def _cancelled(
        self, op: str, fn: Callable[[], Any], gate: threading.Event, **kw
    ):
        ticket = self.runner.submit(op, fn, **kw)
        waiter = asyncio.create_task(ticket.wait())
        await until(lambda: self.runner.inflight_op == op)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        gate.set()
        await self.runner.wait_idle()
        return ticket

    async def test_u2r_runner_retain_false_never_enters_the_reservoir(self):
        gate = threading.Event()
        ticket = await self._cancelled(
            "poll", lambda: gate.wait(10) and "REPLYCANARY", gate, retain=False
        )
        self.assertEqual(self.runner.held_results(), ())
        meta = [r for r in self.runner.retained() if r.job == ticket.job_id]
        self.assertEqual(len(meta), 1)
        self.assertEqual(meta[0].op, "poll")
        with self.assertRaises(gc.RunnerError) as ctx:
            self.runner.take_result(ticket.job_id)
        self.assertEqual(ctx.exception.code, "runner_result_unavailable")
        # the awaiter of a non-cancelled ticket still gets the value
        self.assertEqual(await self.runner.run("poll", lambda: 7, retain=False), 7)
        self.assertEqual(self.runner.held_results(), ())

    async def test_u2r_runner_settled_job_drops_its_callable(self):
        import gc as pygc
        import weakref

        class Sentinel:
            pass

        sentinel = Sentinel()
        ref = weakref.ref(sentinel)
        gate = threading.Event()

        def fn(held: Sentinel = sentinel) -> str:
            gate.wait(10)
            return "ok" if held is not None else "no"

        ticket = await self._cancelled("confirm", fn, gate)
        del fn, sentinel
        pygc.collect()
        self.assertIsNone(ref())  # the closure is gone although the job is held
        self.assertEqual(self.runner.take_result(ticket.job_id), "ok")

    async def test_u2r_runner_confirm_reservoir_survives_non_retained_flood(self):
        gate = threading.Event()
        kept = await self._cancelled("confirm", lambda: gate.wait(10) and 1, gate)
        for index in range(40):
            self.runner.submit("status", lambda: None, retain=False)
            if index % 6 == 5:
                await self.runner.wait_idle()
        await self.runner.wait_idle()
        self.assertEqual(self.runner.held_results(), (kept.job_id,))
        self.assertEqual(self.runner.take_result(kept.job_id), 1)

    async def test_u2r_runner_coalesced_urgent_keeps_first_retain_value(self):
        gate = threading.Event()
        head = self.runner.submit("confirm", lambda: gate.wait(10))
        await until(lambda: self.runner.inflight_op == "confirm")
        first = self.runner.submit("stop", lambda: None, urgent=True, retain=False)
        second = self.runner.submit("stop", lambda: None, urgent=True, retain=True)
        self.assertEqual(first.job_id, second.job_id)
        gate.set()
        await head.wait()
        await self.runner.wait_idle()
        self.assertEqual(self.runner.held_results(), ())


# =================================================================================
# UnifiedOps (real Bridge + Journal; fake transport / client / store)
# =================================================================================
def _json_response(doc: Any):
    from .authfakes import json_response

    return json_response(doc)


class OpsCase(OfflineTestCase):
    def setUp(self) -> None:
        super().setUp()
        from pet_bridge.auth import Attachment

        self.Attachment = Attachment
        self.mono = FakeClock(5000.0)
        self.root = self.journal_root()
        self.journal = Journal(self.root)
        self.addCleanup(self.journal.close)
        self.transport = FakeTransport()
        self.bridge = core.Bridge(
            self.journal, self.transport, clock=FakeClock(), new_id=IdFactory()
        )
        self.binding = self.select()
        self.attachment = Attachment(
            CRED_REF, ORIGIN_URL, CLIENT_ID, HOST_IDENTITY, INSTANCE, token=TOKEN
        )
        self.store = FakeStore(self.attachment)
        self.client = FakeClient()
        self.ops = self.make_ops()

    def select(self, session: str = SESSION):
        return self.bridge.select_session(
            host_id=HOST_IDENTITY,
            host_instance_id=INSTANCE,
            session_id=session,
            client_id=CLIENT_ID,
            credential_ref=CRED_REF,
        )

    def make_ops(self, **changes: Any) -> gc.UnifiedOps:
        args: dict[str, Any] = {
            "bridge": self.bridge,
            "store": self.store,
            "credential_ref": CRED_REF,
            "session_id": SESSION,
            "client_factory": lambda: self.client,
            "monotonic": self.mono,
            "binding": self.binding,
        }
        args.update(changes)
        return gc.UnifiedOps(
            args.pop("bridge"),
            args.pop("store"),
            args.pop("credential_ref"),
            args.pop("session_id"),
            args.pop("client_factory"),
            **args,
        )

    def sealed(self, text: str = "hello there"):
        turn = self.ops.begin()
        self.assertEqual(self.ops.stage(turn, [("ev-1", text)]), "appended")
        return turn, self.ops.seal(turn)

    def detail_client(self, turn: str, reply: str = "The answer.") -> FakeClient:
        rows = [user_row(turn), assistant_row("gen-1", reply)]
        return FakeClient(lambda _call: _json_response(page(rows)))


class UnifiedOpsTest(OpsCase):
    def test_t_core_ops_begin_returns_a_canonical_turn_and_opens_it(self):
        turn = self.ops.begin()
        self.assertTrue(core.wire.is_canonical_uuid4(turn))
        self.assertTrue(self.ops.status()["open_turn"])

    def test_t_core_ops_stage_appends_in_order_then_seal_joins_them(self):
        turn = self.ops.begin()
        segments = [("ev-1", "hello "), ("ev-2", "world")]
        self.assertEqual(self.ops.stage(turn, segments), "appended")
        preview = self.ops.seal(turn)
        self.assertEqual(preview.text, "hello world")
        self.assertEqual(
            preview.text_sha256, hashlib.sha256(b"hello world").hexdigest()
        )
        self.assertEqual(self.ops.stage(turn, [("ev-3", "late")]), "sealed")

    def test_t_core_ops_stage_returns_the_first_code_that_is_not_appended(self):
        turn = self.ops.begin()
        code = self.ops.stage(turn, [("ev-1", "a"), ("ev-1", "b"), ("ev-2", "c")])
        self.assertEqual(code, "duplicate_event")
        self.assertEqual(self.ops.seal(turn).text, "a")  # nothing after the failure

    def test_t_core_ops_preflight_idle_with_the_global_revision(self):
        self.client.script = [lpage([srow(SESSION)])]
        result = self.ops.preflight()
        self.assertEqual(
            (result.session_id, result.revision, result.idle, result.reason),
            (SESSION, 10, True, "idle"),
        )

    def test_t_core_ops_preflight_not_idle_unlisted_and_companion_error(self):
        self.client.script = [lpage([srow(SESSION, "working", execution=True)])]
        self.assertFalse(self.ops.preflight().idle)
        self.client.script = [lpage([srow("other-session")])]
        self.assertEqual(self.ops.preflight().reason, "unlisted")
        self.client.script = [RuntimeError("down")]
        result = self.ops.preflight()
        self.assertEqual(
            (result.idle, result.revision, result.reason),
            (False, -1, "companion_request_failed"),
        )

    def test_t_core_ops_preflight_requires_a_complete_listing(self):
        pages = [lpage([srow(SESSION)], "c1")] + [
            lpage([srow(f"s{n}")], f"c{n}") for n in range(2, 6)
        ]
        self.client.script = pages
        with mock.patch(
            "pet_bridge.companion.preflight",
            side_effect=AssertionError("preflight ran on an incomplete listing"),
        ):
            result = self.ops.preflight()
        self.assertEqual((result.idle, result.reason), (False, "list_incomplete"))

    def test_t_core_ops_confirm_omits_optional_app_global_revision(
        self,
    ):
        turn, preview = self.sealed()
        self.transport.responses.append((200, accepted_body()))
        result = self.ops.confirm(turn, preview.text_sha256, 10)
        self.assertEqual(result.outcome, "accepted")
        (call,) = self.transport.calls
        body = json.loads(call[3])
        self.assertNotIn("expectedRevision", body)
        self.assertEqual(body["args"]["text"], "hello there")

    def test_t_core_ops_poll_uses_the_detail_window_page_and_coverage(self):
        turn, preview = self.sealed()
        self.transport.responses.append((200, accepted_body()))
        self.ops.confirm(turn, preview.text_sha256, 10)
        self.client = self.detail_client(turn, "The answer.")
        result = self.ops.poll(turn)
        self.assertEqual((result.status, result.text), ("speak", "The answer."))

    def test_t_core_ops_poll_partial_coverage_never_speaks(self):
        turn, preview = self.sealed()
        self.transport.responses.append((200, accepted_body()))
        self.ops.confirm(turn, preview.text_sha256, 10)
        rows = [user_row(turn), assistant_row("gen-1", "The answer.")]
        partial = page(rows, offset=5, total=7)
        self.client = FakeClient(lambda _call: _json_response(partial))
        result = self.ops.poll(turn)
        self.assertNotEqual(result.status, "speak")
        self.assertIsNone(result.text)

    def test_t_core_ops_poll_companion_error_is_pending_never_terminal(self):
        turn, preview = self.sealed()
        self.transport.responses.append((200, accepted_body()))
        self.ops.confirm(turn, preview.text_sha256, 10)
        self.client = FakeClient(RuntimeError("down"))
        result = self.ops.poll(turn)
        self.assertEqual(
            (result.status, result.reason, result.terminal, result.epoch),
            ("pending", "companion_request_failed", False, -1),
        )

    def test_t_core_ops_stop_resume_epoch_and_status_delegate_to_the_bridge(self):
        epoch = self.ops.status()["epoch"]
        self.assertTrue(self.ops.epoch_current(epoch))
        self.ops.stop()
        self.assertTrue(self.ops.status()["stopped"])
        self.assertFalse(self.ops.epoch_current(epoch))
        self.ops.resume()
        self.assertFalse(self.ops.status()["stopped"])

    def test_t_core_ops_repr_and_str_hide_ids_credentials_and_tokens(self):
        text = repr(self.ops) + str(self.ops)
        for secret in (SESSION, CRED_REF, TOKEN, CLIENT_ID, HOST_IDENTITY, INSTANCE):
            self.assertNotIn(secret, text)


class UnifiedOpsBindingTest(OpsCase):
    """Manager correction 3: preflight/poll/confirm are bound to the FROZEN binding."""

    def mismatched_attachment(self, **changes: Any):
        import dataclasses

        return dataclasses.replace(self.attachment, **changes)

    def assert_nothing_sent(self) -> None:
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.transport.calls, [])

    def test_a_missing_binding_fails_closed_everywhere(self):
        ops = self.make_ops(binding=None)
        self.assertEqual(ops.preflight().reason, "core_binding_required")
        self.assertFalse(ops.preflight().idle)
        poll = ops.poll(TURN)
        self.assertEqual(
            (poll.status, poll.reason, poll.text),
            ("not_spoken", "core_binding_required", None),
        )
        with self.assertRaises(BridgeError) as ctx:
            ops.begin()
        self.assertEqual(ctx.exception.code, "core_binding_required")
        with self.assertRaises(BridgeError):
            ops.resume()
        self.assert_nothing_sent()

    def test_every_attachment_field_must_equal_the_frozen_binding(self):
        for field_name, value in (
            ("client_id", "D" * 32),
            ("host_identity", "6" * 64),
            ("host_instance_id", INSTANCE_2),
            ("credential_ref", "device-" + "cd" * 16),
        ):
            with self.subTest(field=field_name):
                self.store.attachment = self.mismatched_attachment(
                    **{field_name: value}
                )
                self.client.script = [lpage([srow(SESSION)])]
                result = self.ops.preflight()
                self.assertEqual(
                    (result.idle, result.reason), (False, "binding_mismatch")
                )
                poll = self.ops.poll(TURN)
                self.assertEqual(
                    (poll.status, poll.reason, poll.terminal, poll.text),
                    ("not_spoken", "binding_mismatch", True, None),
                )
                self.assertEqual(self.client.calls, [])
                self.assertEqual(self.transport.calls, [])

    def test_confirm_with_a_mismatched_attachment_is_refused_and_never_sent(self):
        turn, preview = self.sealed()
        self.store.attachment = self.mismatched_attachment(client_id="D" * 32)
        result = self.ops.confirm(turn, preview.text_sha256, 10)
        self.assertEqual(
            (result.outcome, result.reason), ("refused", "binding_mismatch")
        )
        self.assertFalse(result.persisted)
        self.assert_nothing_sent()

    def test_the_op_session_id_must_equal_the_frozen_binding_session(self):
        ops = self.make_ops(session_id="sess-other-9")
        self.assertEqual(ops.preflight().reason, "binding_mismatch")
        self.assertEqual(ops.poll(TURN).reason, "binding_mismatch")
        self.assertEqual(ops.confirm(TURN, "0" * 64, 1).reason, "binding_mismatch")

    def test_the_op_credential_ref_must_equal_the_frozen_binding_ref(self):
        ops = self.make_ops(credential_ref="device-" + "cd" * 16)
        self.assertEqual(ops.preflight().reason, "binding_mismatch")

    def test_a_live_bridge_binding_that_changed_after_startup_fails_closed(self):
        self.select("sess-other-9")  # the live core now points elsewhere
        self.client.script = [lpage([srow(SESSION)])]
        self.assertEqual(self.ops.preflight().reason, "binding_mismatch")
        self.assertEqual(self.ops.poll(TURN).reason, "binding_mismatch")
        with self.assertRaises(BridgeError) as ctx:
            self.ops.begin()
        self.assertEqual(ctx.exception.code, "binding_mismatch")
        with self.assertRaises(BridgeError):
            self.ops.resume()
        self.assert_nothing_sent()

    def test_a_reselected_same_session_gets_a_new_binding_id_and_is_also_refused(self):
        self.select(SESSION)  # same session, NEW binding id: not the frozen one
        self.assertEqual(self.ops.preflight().reason, "binding_mismatch")

    def test_a_missing_attachment_is_credential_missing_not_an_error(self):
        self.store.attachment = None
        self.assertEqual(self.ops.preflight().reason, "credential_missing")
        poll = self.ops.poll(TURN)
        self.assertEqual(
            (poll.status, poll.reason), ("not_spoken", "credential_missing")
        )

    def test_a_store_failure_is_credential_missing_with_no_detail(self):
        def failing(_ref: str):
            raise OSError("disk detail /secret/path")

        self.store.load_attachment = failing
        result = self.ops.preflight()
        self.assertEqual((result.idle, result.reason), (False, "credential_missing"))
        self.assertNotIn("secret", repr(result))

    def test_a_forged_live_status_is_refused_when_any_one_field_differs(self):
        turn, preview = self.sealed()
        genuine = self.bridge.status()
        self.assertTrue(genuine["bound"])
        for field_name, wrong in (
            ("bound", False),
            ("binding_id", "not-the-frozen-binding"),
            ("session_id", "sess-other-9"),
            ("host_id", "6" * 64),
        ):
            with self.subTest(field=field_name):
                forged = {**genuine, field_name: wrong}  # every OTHER field matches
                self.client.script = [lpage([srow(SESSION)])]
                with mock.patch.object(self.bridge, "status", return_value=forged):
                    self.assertEqual(self.ops.preflight().reason, "binding_mismatch")
                    self.assertEqual(self.ops.poll(TURN).reason, "binding_mismatch")
                    result = self.ops.confirm(turn, preview.text_sha256, 10)
                self.assertEqual(
                    (result.outcome, result.reason, result.persisted),
                    ("refused", "binding_mismatch", False),
                )
                self.assert_nothing_sent()

    def test_confirm_after_the_core_binding_changed_is_refused_with_no_post(self):
        turn, preview = self.sealed()
        self.select("sess-other-9")  # the live core now points elsewhere
        result = self.ops.confirm(turn, preview.text_sha256, 10)
        self.assertEqual(
            (result.outcome, result.reason), ("refused", "binding_mismatch")
        )
        self.assert_nothing_sent()


# =================================================================================
# AudioOut: gate, pacing, overflow, completed-send accounting
# =================================================================================
PCM_4800 = b"\x01\x02" * 2400


class AudioCase(OfflineAsyncCase):
    async def asyncSetUp(self) -> None:
        self.clock = FakeLoopClock()
        self.frames: list[dict[str, Any]] = []
        self.starts: list[float] = []
        self.done_at: list[float] = []
        self.block: asyncio.Event | None = None
        self.fail_send = False

        async def send(text: str) -> None:
            self.starts.append(self.clock.now())
            if self.block is not None:
                await self.block.wait()
            if self.fail_send:
                raise ConnectionError("device gone")
            self.frames.append(json.loads(text))
            self.done_at.append(self.clock.now())

        self.audio = gc.AudioOut(send, self.clock)
        self.task = asyncio.create_task(self.audio.run())
        self.addAsyncCleanup(self.stop_task)

    async def stop_task(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def settle(self) -> None:
        await self.clock.settle()


class AudioGateTest(AudioCase):
    async def test_t_core_audio_gate_offer_before_open_is_dropped_not_buffered(self):
        self.assertFalse(self.audio.offer(PCM_4800))
        self.assertEqual((self.audio.queued_bytes, self.audio.dropped_bytes), (0, 4800))
        self.audio.open()
        await self.settle()
        self.assertEqual(self.frames, [])  # nothing buffered, nothing replayed on open

    async def test_t_core_audio_gate_open_then_offer_is_queued_and_sent(self):
        self.audio.open()
        self.assertTrue(self.audio.offer(PCM_4800))
        await self.settle()
        self.assertEqual(len(self.frames), 1)
        self.assertEqual(self.frames[0]["type"], "session.output_audio.delta")
        self.assertEqual(self.audio.sent_bytes, 4800)

    async def test_t_core_audio_gate_close_and_drain_clears_and_closes_the_gate(self):
        self.audio.open()
        self.block = asyncio.Event()
        self.audio.offer(PCM_4800 * 3)
        await self.settle()
        dropped = self.audio.close_and_drain()
        self.assertEqual(
            dropped, 4800 * 2
        )  # one chunk is mid-send and cannot be undone
        self.assertEqual(self.audio.queued_bytes, 0)
        self.assertFalse(self.audio.is_open)
        self.assertFalse(self.audio.offer(PCM_4800))
        self.block.set()
        await self.settle()
        self.assertTrue(self.audio.drained.is_set())
        self.assertEqual(len(self.frames), 1)

    async def test_t_core_audio_gate_seal_stops_new_audio_but_drains_the_queue(self):
        self.audio.open()
        self.audio.offer(PCM_4800 * 2)
        self.audio.seal()
        self.assertFalse(self.audio.offer(PCM_4800))
        await self.settle()
        await self.clock.advance(0.1)
        self.assertEqual(self.audio.sent_bytes, 9600)
        self.assertTrue(self.audio.drained.is_set())

    async def test_a_chunk_waiting_for_its_slot_is_discarded_by_a_drain(self):
        self.audio.open()
        self.audio.offer(PCM_4800 * 2)
        await self.settle()
        self.assertEqual(len(self.frames), 1)  # the second waits for t0+0.1
        self.audio.close_and_drain()
        await self.clock.advance(1.0)
        self.assertEqual(len(self.frames), 1)
        self.assertEqual(self.audio.sent_bytes, 4800)


class AudioPaceTest(AudioCase):
    async def test_t_core_audio_pace_three_chunks_at_exact_tenth_second_spacing(self):
        t0 = self.clock.now()
        self.audio.open()
        for _ in range(3):
            self.audio.offer(PCM_4800)
        await self.settle()
        self.assertEqual(len(self.frames), 1)
        await self.clock.advance(0.1)
        await self.clock.advance(0.1)
        self.assertEqual(len(self.frames), 3)
        for sent, expected in zip(self.done_at, (0.0, 0.1, 0.2), strict=True):
            self.assertAlmostEqual(sent - t0, expected, places=6)

    async def test_t_core_audio_pace_a_9600_byte_delta_is_split_in_two(self):
        self.audio.open()
        self.audio.offer(PCM_4800 * 2)
        await self.settle()
        await self.clock.advance(0.1)
        self.assertEqual([len(base64_len(f)) for f in self.frames], [4800, 4800])

    async def test_t_core_audio_pace_backpressure_never_sends_faster_and_queue_grows(
        self,
    ):
        self.audio.open()
        self.block = asyncio.Event()
        self.audio.offer(PCM_4800 * 2)
        await self.settle()
        self.assertEqual((self.audio.sent_bytes, self.audio.queued_bytes), (0, 4800))
        self.audio.offer(PCM_4800)
        self.assertEqual(self.audio.queued_bytes, 9600)  # grows while the device blocks
        await self.clock.advance(0.3)
        self.block.set()
        await self.settle()
        await self.clock.advance(0.5)
        self.assertEqual(self.audio.sent_bytes, 14400)
        gaps = [b - a for a, b in zip(self.starts, self.starts[1:], strict=False)]
        self.assertTrue(all(gap >= 0.1 - 1e-9 for gap in gaps), gaps)

    async def test_t_core_audio_pace_an_idle_gap_does_not_bank_credit(self):
        self.audio.open()
        self.audio.offer(PCM_4800)
        await self.settle()
        await self.clock.advance(5.0)
        self.audio.offer(PCM_4800 * 2)
        await self.settle()
        start = self.clock.now()
        self.assertEqual(len(self.frames), 2)
        await self.clock.advance(0.1)
        self.assertAlmostEqual(self.starts[2] - start, 0.1, places=6)


def base64_len(frame: dict[str, Any]) -> bytes:
    import base64

    return base64.b64decode(frame["delta"])


class AudioCountingTest(AudioCase):
    """Manager correction 2: only COMPLETED device sends count as emitted audio."""

    async def test_sent_bytes_counts_completed_frames_not_queued_or_offered_bytes(self):
        self.audio.open()
        self.block = asyncio.Event()
        self.audio.offer(PCM_4800)
        await self.settle()
        self.assertEqual((self.audio.offered_bytes, self.audio.sent_bytes), (4800, 0))
        self.assertFalse(self.audio.drained.is_set())
        self.block.set()
        await self.settle()
        self.assertEqual(self.audio.sent_bytes, 4800)
        self.assertTrue(self.audio.drained.is_set())

    async def test_a_failed_device_send_is_never_counted_and_stops_the_pacer(self):
        self.audio.open()
        self.fail_send = True
        self.audio.offer(PCM_4800)
        await self.settle()
        self.assertEqual(self.audio.sent_bytes, 0)
        self.assertTrue(self.task.done())
        self.assertIsInstance(self.task.exception(), ConnectionError)

    async def test_last_offer_time_and_offered_bytes_track_accepted_audio_only(self):
        self.assertEqual(self.audio.last_offer_t, float("-inf"))
        self.audio.offer(PCM_4800)  # gate closed: dropped
        self.assertEqual(
            (self.audio.offered_bytes, self.audio.last_offer_t), (0, float("-inf"))
        )
        self.audio.open()
        await self.clock.advance(2.5)
        self.audio.offer(PCM_4800)
        self.assertEqual(self.audio.offered_bytes, 4800)
        self.assertAlmostEqual(self.audio.last_offer_t, self.clock.now(), places=6)


class AudioOverflowTest(AudioCase):
    async def test_t_core_audio_overflow_over_one_mib_raises_and_closes_the_gate(self):
        self.audio.open()
        self.block = asyncio.Event()
        self.audio.offer(b"\x00\x00" * 1000)
        with self.assertRaises(gc.AudioOverflow) as ctx:
            self.audio.offer(b"\x00\x00" * (524288 - 1000 + 1))  # 1 MiB + 2 queued
        self.assertEqual(ctx.exception.code, "overflow")
        self.assertFalse(self.audio.is_open)
        self.assertEqual(self.audio.queued_bytes, 0)

    async def test_exactly_the_cap_is_accepted(self):
        self.audio.open()
        self.block = asyncio.Event()
        self.assertTrue(self.audio.offer(b"\x00\x00" * 524288))
        self.assertTrue(self.audio.is_open)

    async def test_repr_and_str_show_counts_only(self):
        self.audio.open()
        self.audio.offer(AUDIO_CANARY)
        text = repr(self.audio) + str(self.audio)
        self.assertNotIn("\x11", text)
        self.assertNotIn(repr(AUDIO_CANARY), text)
        self.assertIn("queued", text)


class AudioRepairTest(AudioCase):
    """U2 repair M2/M3: a held gate, a release token and the post-lock epoch recheck."""

    async def build(self, **kw: Any) -> gc.AudioOut:
        async def send(text: str) -> None:
            self.starts.append(self.clock.now())
            if self.block is not None:
                await self.block.wait()
            self.frames.append(json.loads(text))
            self.done_at.append(self.clock.now())

        audio = gc.AudioOut(send, self.clock, **kw)
        task = asyncio.create_task(audio.run())

        async def stop() -> None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(stop)
        return audio

    async def test_u2r_audio_hold_buffers_without_sending_until_release(self):
        audio = await self.build()
        t0 = self.clock.now()
        token = audio.open(hold=True)
        for _ in range(3):
            audio.offer(PCM_4800)
        await self.settle()
        self.assertEqual(self.frames, [])
        self.assertEqual(audio.queued_bytes, 14400)
        self.assertTrue(audio.is_open)
        self.assertTrue(audio.release(token))
        await self.settle()
        await self.clock.advance(0.1)
        await self.clock.advance(0.1)
        self.assertEqual(audio.sent_bytes, 14400)
        for sent, expected in zip(self.starts, (0.0, 0.1, 0.2), strict=True):
            self.assertAlmostEqual(sent - t0, expected, places=6)

    async def test_u2r_audio_hold_overflow_beyond_cap_raises_and_clears_even_while_held(
        self,
    ):
        audio = await self.build(cap=9600)
        audio.open(hold=True)
        audio.offer(PCM_4800)
        audio.offer(PCM_4800)
        with self.assertRaises(gc.AudioOverflow):
            audio.offer(PCM_4800)
        self.assertEqual(audio.queued_bytes, 0)
        self.assertFalse(audio.is_open)

    async def test_u2r_audio_stale_release_token_is_a_noop(self):
        audio = await self.build()
        first = audio.open(hold=True)
        audio.close_and_drain()
        second = audio.open(hold=True)
        audio.offer(PCM_4800)
        self.assertNotEqual(first, second)
        self.assertFalse(audio.release(first))
        await self.settle()
        self.assertEqual(self.frames, [])
        self.assertTrue(audio.release(second))
        await self.settle()
        self.assertEqual(len(self.frames), 1)
        self.assertFalse(audio.release(second))  # already released

    async def test_u2r_audio_epoch_is_rechecked_after_the_lock_is_acquired(self):
        lock = asyncio.Lock()
        await lock.acquire()
        audio = await self.build(lock=lock)
        audio.open()
        audio.offer(PCM_4800)
        await self.settle()  # the pacer popped it and now waits for the lock
        self.assertEqual(audio.queued_bytes, 4800)
        audio.close_and_drain()
        lock.release()
        await self.settle()
        self.assertEqual(self.starts, [])  # send was never called
        self.assertEqual((audio.sent_bytes, audio.dropped_bytes), (0, 4800))

    async def test_u2r_audio_chunk_already_inside_send_completes_and_is_counted_later_chunk_is_not(
        self,
    ):
        audio = await self.build()
        audio.open()
        self.block = asyncio.Event()
        audio.offer(PCM_4800 * 2)
        await self.settle()  # the first chunk is inside send; the second is queued
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(audio.close_and_drain(), 4800)
        self.block.set()
        await self.settle()
        await self.clock.advance(1.0)
        self.assertEqual(len(self.frames), 1)
        self.assertEqual((audio.sent_bytes, audio.dropped_bytes), (4800, 4800))

    async def test_u2r_audio_sealed_held_queue_can_still_release_and_drain(self):
        audio = await self.build()
        token = audio.open(hold=True)
        self.assertTrue(audio.offer(PCM_4800))
        self.assertTrue(audio.offer(PCM_4800))
        audio.seal()  # the quiet period ended while the held gate had not been released
        await self.settle()
        self.assertEqual((self.frames, audio.queued_bytes), ([], 9600))
        self.assertFalse(audio.is_open)  # no new audio is accepted ...
        self.assertTrue(audio.release(token))  # ... but what is queued may still play
        self.assertFalse(audio.release(token))  # a second release is a no-op
        await self.settle()
        await self.clock.advance(0.1)
        await self.settle()
        self.assertEqual(audio.sent_bytes, 9600)
        self.assertEqual(len(self.frames), 2)
        self.assertTrue(audio.drained.is_set())
        self.assertEqual(audio.dropped_bytes, 0)

    async def test_u2r_audio_release_never_reopens_acceptance_of_new_audio_once_sealed(
        self,
    ):
        audio = await self.build()
        token = audio.open(hold=True)
        audio.offer(PCM_4800)
        audio.seal()
        self.assertFalse(audio.offer(PCM_4800))  # sealed: dropped, as before
        self.assertEqual((audio.queued_bytes, audio.dropped_bytes), (4800, 4800))
        self.assertTrue(audio.release(token))
        self.assertFalse(audio.is_open)
        self.assertFalse(audio.offer(PCM_4800))  # still dropped after the release
        self.assertEqual((audio.queued_bytes, audio.dropped_bytes), (4800, 9600))
        await self.settle()
        self.assertEqual((audio.sent_bytes, audio.offered_bytes), (4800, 4800))
        self.assertEqual(len(self.frames), 1)

    async def test_u2r_audio_sealed_overflow_is_a_drop_not_an_overflow_and_held_counts(
        self,
    ):
        audio = await self.build(cap=9600)
        token = audio.open(hold=True)
        audio.offer(PCM_4800)
        audio.offer(PCM_4800)  # exactly the cap, held
        audio.seal()
        self.assertFalse(audio.offer(PCM_4800))  # sealed: dropped, no AudioOverflow
        self.assertEqual(audio.queued_bytes, 9600)
        self.assertTrue(audio.release(token))
        await self.settle()
        await self.clock.advance(0.1)
        self.assertEqual(audio.sent_bytes, 9600)

    async def test_u2r_audio_stale_token_and_stop_cannot_release_a_sealed_gate(self):
        audio = await self.build()
        first = audio.open(hold=True)
        audio.offer(PCM_4800)
        audio.seal()
        self.assertEqual(audio.close_and_drain(), 4800)  # Stop
        self.assertFalse(audio.release(first))  # the Stop epoch makes it stale
        self.assertFalse(audio.release(first + 1))  # nor can a guessed current epoch
        second = audio.open(hold=True)
        audio.offer(PCM_4800)
        audio.seal()
        self.assertFalse(
            audio.release(first)
        )  # an old epoch never releases the new gate
        self.assertEqual(audio.close_and_drain(), 4800)
        self.assertFalse(audio.release(second))  # Stop after seal: nothing to release
        await self.settle()
        await self.clock.advance(1.0)
        self.assertEqual((self.frames, audio.sent_bytes), ([], 0))
        self.assertEqual(audio.dropped_bytes, 9600)

    async def test_u2r_audio_release_on_a_never_opened_or_unheld_gate_is_false(self):
        audio = await self.build()
        self.assertFalse(audio.release(0))  # never opened
        token = audio.open()  # not held: already released
        audio.offer(PCM_4800)
        audio.seal()
        self.assertFalse(audio.release(token))
        await self.settle()
        self.assertEqual(audio.sent_bytes, 4800)


# =================================================================================
# PetSession: harness
# =================================================================================
UUID_OTHER = "223e4567-e89b-42d3-a456-426614174000"
LABEL = "Pet session"
SHORT8 = "0a1b2c3d"
DELEG = "dlg_1"


def audio_frame(count: int = 100) -> dict[str, Any]:
    pcm = b"\x01\x02" * count
    return {
        "type": "session.input_audio.append",
        "audio": base64.b64encode(pcm).decode(),
    }


def delta(event_id: str, text: str, start: int, end: int) -> dict[str, Any]:
    return {
        "type": "session.input_transcript.delta",
        "event_id": event_id,
        "delta": text,
        "start_ms": start,
        "end_ms": end,
    }


def delegation(delegation_id: str = DELEG) -> dict[str, Any]:
    return {
        "type": "session.delegation.created",
        "offset_ms": 0,
        "delegation": {"id": delegation_id, "type": "delegation", "target": "client"},
    }


def pending_poll(turn_id: str) -> ReplyResult:
    return ReplyResult(turn_id, "pending", "generation_running", False, -1)


class Harness:
    """One PetSession wired to fakes, a fake clock and a real CoreRunner thread."""

    def __init__(self, *, text: str = TEXT_CANARY) -> None:
        self.clock = FakeLoopClock()
        self.clock.settle_hook = self.quiesce
        self.device = FakeDeviceLink(self.clock)
        self.provider = FakeProviderLink(self.clock)
        self.factory = FakeProviderFactory(self.provider)
        self.ops = FakeOps(self.clock, text=text)
        self.runner = gc.CoreRunner()
        self.counter = 0
        self.task: asyncio.Task[Any] | None = None
        self.session: gc.PetSession | None = None
        self.model = "gpt-live-1"

    def new_id(self) -> str:
        self.counter += 1
        return f"gw_{self.counter:04d}"

    async def quiesce(self) -> None:
        for _ in range(8):
            if self.ops.held_active():
                await asyncio.sleep(
                    0.002
                )  # a call is deliberately blocked: do not wait
            else:
                await self.runner.wait_idle()
            for _ in range(20):
                await asyncio.sleep(0)

    async def open(self) -> None:
        settings = gc.SessionSettings(self.model, "marin", LABEL, SHORT8)
        self.runner.start()
        self.session = gc.PetSession(
            self.device,
            self.factory,
            self.runner,
            self.ops,
            settings,
            self.clock,
            new_event_id=self.new_id,
        )

    async def aclose(self) -> None:
        self.ops.release_holds()  # includes failed tests; ordinary assertions release explicitly
        # Failure cleanup must not wait for fake-clock deadlines behind fake links.
        # This is teardown only; tests assert production blocking behavior first.
        for link in (self.provider, self.device):
            for gate in (link.send_block, link.close_block):
                if gate is not None:
                    gate.set()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.runner.aclose()

    def go(self) -> None:
        assert self.session is not None
        self.task = asyncio.create_task(self.session.run())

    async def feed(self, frame: Any) -> None:
        self.device.feed(frame)
        await self.quiesce()

    async def push(self, event: Any) -> None:
        self.provider.push(event)
        await self.quiesce()

    async def boot(self) -> None:
        self.go()
        await self.feed(ASSISTANT_START)

    async def begin(self) -> None:
        await self.feed({"type": "pet.turn.begin"})

    async def capture(self, deltas: list[dict[str, Any]] | None = None) -> None:
        """begin, optional provider deltas while capturing, then the human stop."""
        await self.begin()
        for item in deltas or []:
            await self.push(item)
        await self.feed({"type": "pet.turn.capture_stop", "turn_id": TURN})

    async def preview(self, text: str = "hello world") -> None:
        await self.capture([delta("e1", text, 0, 500)])
        await self.clock.advance(1.5)

    async def finish(self) -> gc.SessionOutcome:
        assert self.task is not None
        await self.quiesce()
        try:
            return await asyncio.wait_for(asyncio.shield(self.task), 5)
        except TimeoutError:
            raise AssertionError("the session did not finish") from None


class SessionCase(OfflineAsyncCase):
    async def asyncSetUp(self) -> None:
        self.h = Harness()
        self.addAsyncCleanup(
            self.h.aclose
        )  # registered first: a failed open still cleans up
        await self.h.open()

    @property
    def device(self) -> FakeDeviceLink:
        return self.h.device

    @property
    def provider(self) -> FakeProviderLink:
        return self.h.provider

    @property
    def ops(self) -> FakeOps:
        return self.h.ops

    @property
    def clock(self) -> FakeLoopClock:
        return self.h.clock

    def frames(self, kind: str) -> list[dict[str, Any]]:
        return self.device.of_type(kind)


# =================================================================================
# PetSession: start, capture, settle, preview (stage 4)
# =================================================================================
class SessionStartTest(SessionCase):
    async def test_documented_startup_metadata_must_match_configuration_and_command(self):
        self.provider.auto_start = False
        await self.h.boot()
        event = provider_frame({"type": "session.started"})
        event["session"]["model"] = "gpt-live-2"
        await self.h.push(event)
        self.assertEqual(self.ops.count("resume"), 0)
        self.assertNotIn("session.started", self.device.types())
        self.assertEqual(self.device.close_codes, [1002])
        await self.h.finish()

    async def test_documented_startup_wrong_client_echo_never_becomes_ready(self):
        self.provider.auto_start = False
        await self.h.boot()
        await self.h.push(provider_frame({
            "type": "session.started", "client_event_id": "gw_wrong_start",
        }))
        self.assertEqual(self.ops.count("resume"), 0)
        self.assertNotIn("session.started", self.device.types())
        self.assertEqual(self.device.close_codes, [1002])
        await self.h.finish()

    async def test_bad_startup_protocol_is_terminal_before_device_begin(self):
        mutations = (
            lambda event: event["session"].update({"status": "invalid"}),
            lambda event: event["session"].update({"model": "gpt-live-2"}),
            lambda event: event["session"]["audio"]["output"].update({"voice": "alloy"}),
            lambda event: event["session"]["audio"]["format"].update({"rate": 16000}),
            lambda event: event["session"].update({"store": True}),
            lambda event: event.update({"client_event_id": "gw_wrong"}),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                h = Harness()
                self.addAsyncCleanup(h.aclose)
                await h.open()
                h.provider.auto_start = False
                await h.boot()
                event = provider_frame({"type": "session.started"})
                mutate(event)
                await h.push(event)
                outcome = await h.finish()
                self.assertEqual(h.device.close_codes, [1002])
                self.assertEqual(h.device.frames, [])
                self.assertEqual(h.ops.count("resume"), 0)
                self.assertEqual(h.ops.count("confirm"), 0)
                self.assertIsNone(outcome.send_outcome)

    async def test_startup_provider_event_does_not_invent_auth_classification(self):
        self.provider.auto_start = False
        await self.h.boot()
        await self.h.push({
            "type": "error",
            "error": {"type": "invalid_request_error", "code": "unknown_parameter",
                      "message": TEXT_CANARY},
        })
        outcome = await self.h.finish()
        self.assertEqual(self.device.close_codes, [1011])
        self.assertEqual(self.device.frames, [])
        self.assertEqual(self.ops.count("resume"), 0)
        self.assertNotIn(TEXT_CANARY, repr(outcome))

    async def test_startup_bad_frame_then_drop_and_drop_then_bad_frame(self):
        for bad_first, expected in ((True, 1002), (False, 1011)):
            with self.subTest(bad_first=bad_first):
                h = Harness()
                self.addAsyncCleanup(h.aclose)
                await h.open()
                h.provider.auto_start = False
                await h.boot()
                if bad_first:
                    h.provider.push("{invalid")
                    h.provider.push_closed(clean=False)
                else:
                    h.provider.push_closed(clean=False)
                    h.provider.push("{invalid")
                await h.quiesce()
                await h.finish()
                self.assertEqual(h.device.close_codes, [expected])
                self.assertEqual(h.device.frames, [])
                self.assertEqual(h.device.close_calls, 1)

    async def test_stop_during_start_wins_over_late_provider_failure(self):
        self.provider.auto_start = False
        await self.h.boot()
        await self.h.feed({"type": "pet.stop"})
        self.provider.push("{invalid")
        await self.h.quiesce()
        await self.h.finish()
        self.assertEqual(self.device.close_codes, [1000])
        self.assertEqual(self.ops.count("confirm"), 0)

    async def test_provider_send_failure_snapshot_preserves_first_terminal_evidence(self):
        cases = (
            (gp.ProviderEvent("invalid"), None, 1002),
            (gp.ProviderEvent("invalid_audio"), None, 1002),
            (gp.ProviderEvent("invalid_transcript"), None, 1002),
            (gp.ProviderEvent("started", session_id="live", model="gpt-live-2"), None, 1002),
            (gp.ProviderEvent("error", code="provider_error"), gp.ProviderEvent("invalid"), 1011),
            (None, gp.ProviderEvent("invalid"), 1011),
        )
        for first, later, expected in cases:
            with self.subTest(first=first, later=later):
                h = Harness()
                self.addAsyncCleanup(h.aclose)
                await h.open()
                h.provider.auto_start = False
                await h.boot()
                if first is not None:
                    h.session._post_external(("provider", first, None))
                h.session._provider_send_failed()
                if later is not None:
                    h.session._post_external(("provider", later, None))
                await h.quiesce()
                await h.finish()
                self.assertEqual(h.device.close_codes, [expected])
                self.assertEqual(h.device.close_calls, 1)
                self.assertEqual(h.device.frames, [])
                self.assertEqual(h.ops.count("confirm"), 0)

    async def test_send_failure_does_not_promote_an_ignored_second_start(self):
        self.provider.auto_start = False
        await self.h.boot()
        good = gp.ProviderEvent("started", session_id="live", model="gpt-live-1")
        bad = gp.ProviderEvent("started", session_id="live", model="gpt-live-2")
        self.h.session._post_external(("provider", good, None))
        self.h.session._post_external(("provider", bad, None))
        self.h.session._provider_send_failed()
        await self.h.quiesce()
        await self.h.finish()
        self.assertEqual(self.device.close_codes, [1011])
        self.assertEqual(self.ops.count("resume"), 0)

    async def test_start_send_failure_preserves_prior_malformed_provider_frame(self):
        self.provider.auto_start = False
        async def receive_before_failing(_text):
            self.provider.push("{bad")
            await asyncio.sleep(0)
            raise ConnectionError("offline canary")

        self.provider.send_text = receive_before_failing
        await self.h.boot()
        await self.h.finish()
        self.assertEqual(self.device.close_codes, [1002])
        self.assertEqual(self.device.frames, [])
        self.assertEqual(self.ops.count("confirm"), 0)

    async def test_shutdown_queued_before_send_failure_keeps_stop_priority(self):
        self.provider.auto_start = False
        await self.h.boot()
        self.h.session._post(("shutdown",))
        self.h.session._post_external(("provider", gp.ProviderEvent("invalid"), None))
        self.h.session._provider_send_failed()
        await self.h.quiesce()
        await self.h.finish()
        self.assertEqual(self.device.close_codes, [1000])
        self.assertEqual(self.ops.count("confirm"), 0)

    async def test_t_core_sess_start_accepts_the_exact_assistant_start_and_emits_started(
        self,
    ):
        await self.h.boot()
        self.assertEqual(self.device.types(), ["session.started"])
        (start,) = self.provider.of_type("session.start")
        self.assertEqual(start["session"]["delegation"]["type"], "client")
        self.assertIn("Generate <|SpawnThinking|>", start["session"]["instructions"])
        self.assertNotIn(ASSISTANT_GREETING, start["session"]["instructions"])
        self.assertEqual(self.provider.sent[0]["event_id"], "gw_0001")
        self.assertEqual(self.ops.count("resume"), 1)

    async def test_t_core_sess_start25_provider_never_starts_deadline_at_exactly_25s(
        self,
    ):
        self.provider.auto_start = False
        await self.h.boot()
        t_start = self.clock.now()
        await self.clock.advance(24.9)
        self.assertEqual(self.device.frames, [])
        await self.clock.advance(0.1)
        self.assertEqual(self.device.types(), ["pet.error"])
        self.assertEqual(self.device.error_codes(), ["deadline"])
        self.assertAlmostEqual(self.device.times[0] - t_start, 25.0, places=6)
        self.assertEqual(self.device.close_codes, [1011])
        outcome = await self.h.finish()
        self.assertEqual(
            (outcome.error_code, outcome.final_state), ("deadline", "CLOSED")
        )
        self.assertGreaterEqual(self.provider.close_calls, 1)

    async def test_t_core_sess_start_fail_connect_error_closes_1011_without_pet_error(
        self,
    ):
        self.h.factory.fail = ConnectionError("no route")
        await self.h.boot()
        self.assertEqual(self.device.frames, [])
        self.assertEqual(self.device.close_codes, [1011])
        self.assertEqual(self.ops.count("resume"), 0)
        outcome = await self.h.finish()
        self.assertIsNone(outcome.error_code)

    async def test_t_core_sess_start_connect_that_hangs_is_bounded_by_the_25s_deadline(
        self,
    ):
        self.h.factory.hang = True
        await self.h.boot()
        await self.clock.advance(25.0)
        self.assertEqual(self.device.error_codes(), ["deadline"])
        self.assertEqual(self.device.close_codes, [1011])
        await self.h.finish()

    async def test_t_core_sess_awaitstart_no_session_start_in_10s_closes_without_frames(
        self,
    ):
        self.h.go()
        await self.h.quiesce()  # the session is running and its 10 s timer is armed
        await self.clock.advance(9.9)
        self.assertEqual(self.device.close_codes, [])
        await self.clock.advance(0.1)
        self.assertEqual((self.device.frames, self.device.close_codes), ([], [1008]))
        self.assertEqual(self.h.factory.calls, 0)
        await self.h.finish()

    async def test_t_core_sess_resume_order_resume_after_provider_started_before_started(
        self,
    ):
        release = self.ops.hold("resume")
        await self.h.boot()
        self.assertEqual(self.provider.types(), ["session.start"])
        self.assertTrue(self.ops.entered["resume"].is_set())
        self.assertEqual(self.device.frames, [])  # not yet announced
        release.set()
        await self.h.quiesce()
        self.assertEqual(self.device.types(), ["session.started"])

    async def test_resume_failure_closes_1011_without_session_started(self):
        self.ops.results["resume"] = BridgeError("bridge_faulted")
        await self.h.boot()
        self.assertEqual(self.device.frames, [])
        self.assertEqual(self.device.close_codes, [1011])
        await self.h.finish()

    async def test_model_mismatch_never_connects_the_provider(self):
        self.h.go()
        await self.h.feed(ASSISTANT_START.replace("gpt-live-1", "gpt-live-2"))
        self.assertEqual(self.device.error_codes(), ["model_mismatch"])
        self.assertEqual(self.h.factory.calls, 0)
        self.assertEqual(self.device.close_codes, [1008])
        await self.h.finish()

    async def test_unknown_command_and_binary_frames_are_fatal_1008(self):
        self.h.go()
        await self.h.feed({"type": "pet.explode"})
        self.assertEqual(self.device.error_codes(), ["unknown_command"])
        self.assertEqual(self.device.close_codes, [1008])
        await self.h.finish()
        other = Harness()
        self.addAsyncCleanup(other.aclose)
        await other.open()
        other.go()
        other.device.feed_binary()
        await other.quiesce()
        self.assertEqual(other.device.error_codes(), ["unknown_command"])
        await other.finish()

    async def test_a_second_session_start_is_busy_and_closes(self):
        await self.h.boot()
        await self.h.feed(ASSISTANT_START)
        self.assertEqual(self.device.error_codes(), ["busy"])
        self.assertEqual(self.h.factory.calls, 1)
        await self.h.finish()

    async def test_greeting_is_ignored_never_forwarded(self):
        await self.h.boot()
        before = list(self.provider.sent)
        await self.h.feed(ASSISTANT_GREETING)
        self.assertEqual(self.provider.sent, before)
        self.assertEqual(self.device.types(), ["session.started"])
        self.h.device.disconnect()
        await self.h.finish()

    async def test_device_disconnect_closes_without_frames_and_closes_the_provider(
        self,
    ):
        await self.h.boot()
        self.device.disconnect()
        outcome = await self.h.finish()
        self.assertEqual(self.device.types(), ["session.started"])
        self.assertGreaterEqual(self.provider.close_calls, 1)
        self.assertEqual(outcome.final_state, "CLOSED")


class SessionCloseOriginTest(SessionCase):
    """An early close must retain which local path initiated it, not just CLOSED."""

    async def test_close_paths_with_no_wire_stop_remain_distinguishable(self):
        # These three paths previously produced the same retained outcome as
        # physical #12. This does NOT determine which occurred in that old run.
        for event in ("device_closed", "device_send_failed", "pacer_failed"):
            with self.subTest(event=event):
                h = Harness()
                self.addAsyncCleanup(h.aclose)
                await h.open()
                await h.boot()
                await h.begin()
                self.assertIsNone(h.session.outcome.close_origin)
                if event == "device_closed":
                    h.device.disconnect()  # execute the real reader -> dispatch path
                else:
                    h.session._post((event,))  # deterministic failure completion
                result = await h.finish()
                self.assertEqual(result.close_origin, event)
                self.assertTrue(result.close_confirmed)  # cleanup is not the cause
                self.assertIsNone(result.error_code)
                self.assertIsNone(result.send_outcome)
                self.assertEqual(h.ops.count("confirm"), 0)
                self.assertEqual(h.factory.calls, 1)
                self.assertEqual(h.provider.types().count("session.close"), 1)

    async def test_later_cleanup_cannot_overwrite_shutdown_origin(self):
        await self.h.boot()
        await self.h.begin()
        await self.h.session.request_shutdown()
        self.device.disconnect()
        result = await self.h.finish()
        self.assertEqual(result.close_origin, "shutdown")
        self.assertIsNone(result.send_outcome)
        self.assertEqual(self.ops.count("confirm"), 0)

    async def test_origin_never_copies_untrusted_frame_or_exception_text(self):
        await self.h.boot()
        self.device.feed({"type": "PRIVATE-TOKEN-TRANSCRIPT"})
        result = await self.h.finish()
        self.assertEqual(result.close_origin, "device_error")
        self.assertEqual(result.error_code, "unknown_command")
        self.assertNotIn("PRIVATE", repr(result))


class SessionCaptureTest(SessionCase):
    async def test_begin_emits_began_with_label_and_short_id_and_starts_capturing(self):
        await self.h.boot()
        await self.h.begin()
        (began,) = self.frames("pet.turn.began")
        self.assertEqual(
            began,
            {
                "type": "pet.turn.began",
                "turn_id": TURN,
                "safe_session_label": LABEL,
                "session_id_short": SHORT8,
            },
        )
        self.assertEqual(self.ops.count("begin"), 1)

    async def test_begin_failure_is_busy_and_closes(self):
        self.ops.results["begin"] = BridgeError("stopped")
        await self.h.boot()
        await self.h.begin()
        self.assertEqual(self.device.error_codes(), ["busy"])
        self.assertEqual(self.frames("pet.turn.began"), [])
        await self.h.finish()

    async def test_a_second_begin_is_busy_one_turn_per_websocket(self):
        await self.h.boot()
        await self.h.begin()
        await self.h.begin()
        self.assertEqual(self.device.error_codes(), ["busy"])
        self.assertEqual(self.ops.count("begin"), 1)
        await self.h.finish()

    async def test_t_core_sess_capture_only_audio_is_forwarded_only_while_capturing(
        self,
    ):
        await self.h.boot()
        frame = audio_frame(100)
        await self.h.feed(frame)  # READY: dropped
        self.assertEqual(self.provider.of_type("session.input_audio.append"), [])
        await self.h.begin()
        await self.h.feed(frame)  # CAPTURING: forwarded verbatim
        (sent,) = self.provider.of_type("session.input_audio.append")
        self.assertEqual(sent["audio"], frame["audio"])
        await self.h.feed({"type": "pet.turn.capture_stop", "turn_id": TURN})
        await self.h.feed(frame)  # SETTLING: dropped
        await self.h.push(delta("e1", "hi", 0, 100))
        await self.clock.advance(1.6)  # PREVIEW
        self.assertEqual(len(self.frames("pet.turn.preview")), 1)
        await self.h.feed(frame)  # PREVIEW: dropped
        sent = self.provider.of_type("session.input_audio.append")
        self.assertEqual(base64.b64decode(sent[0]["audio"]), base64.b64decode(frame["audio"]))
        self.assertTrue(all(base64.b64decode(f["audio"]) == bytes(4800) for f in sent[1:]))
        self.device.disconnect()
        outcome = await self.h.finish()
        self.assertEqual(outcome.audio_in_bytes, 200)  # decoded bytes forwarded

    async def test_t_core_sess_capture_overflow_over_96000_pending_bytes_closes(self):
        await self.h.boot()
        await self.h.begin()
        self.provider.send_block = asyncio.Event()
        big = audio_frame(2400)  # 4800 decoded bytes
        for _ in range(20):  # exactly the 96000 cap
            self.device.feed(big)
        await self.h.quiesce()
        self.assertEqual(self.device.error_codes(), [])
        self.device.feed(big)
        await self.h.quiesce()
        self.assertEqual(self.device.error_codes(), ["overflow"])
        self.provider.send_block.set()
        await self.h.finish()

    async def test_capture_stop_with_a_foreign_turn_id_is_unknown_command(self):
        await self.h.boot()
        await self.h.begin()
        await self.h.feed({"type": "pet.turn.capture_stop", "turn_id": UUID_OTHER})
        self.assertEqual(self.device.error_codes(), ["unknown_command"])
        await self.h.finish()

    async def test_capture_stop_before_begin_is_unknown_command(self):
        await self.h.boot()
        await self.h.feed({"type": "pet.turn.capture_stop", "turn_id": TURN})
        self.assertEqual(self.device.error_codes(), ["unknown_command"])
        await self.h.finish()


class SessionSettleTest(SessionCase):
    async def test_t_core_sess_settle_seals_at_max_of_stop_plus_1_2_and_delta_plus_1_5(
        self,
    ):
        await self.h.boot()
        await self.h.begin()
        t0 = self.clock.now()
        await self.clock.advance(0.5)
        await self.h.push(delta("e1", "hello ", 0, 400))
        await self.clock.advance(0.5)
        await self.h.feed({"type": "pet.turn.capture_stop", "turn_id": TURN})
        await self.clock.advance(1.1)
        self.assertEqual(self.ops.count("stage"), 0)
        await self.clock.advance(0.1)  # t0 + 2.2 = max(1.0 + 1.2, 0.5 + 1.5)
        (stage_t,) = self.ops.times("stage")
        self.assertAlmostEqual(stage_t - t0, 2.2, places=6)
        self.assertEqual(len(self.frames("pet.turn.preview")), 1)

    async def test_t_core_sess_settle_a_late_delta_pushes_the_seal_to_delta_plus_1_5(
        self,
    ):
        await self.h.boot()
        await self.h.begin()
        t0 = self.clock.now()
        await self.h.feed({"type": "pet.turn.capture_stop", "turn_id": TURN})
        await self.clock.advance(0.5)
        await self.h.push(delta("e1", "hello", 0, 400))
        await self.clock.advance(1.5)  # stop + 2.0 = delta + 1.5
        (stage_t,) = self.ops.times("stage")
        self.assertAlmostEqual(stage_t - t0, 2.0, places=6)

    async def test_t_core_sess_settle_six_seconds_with_ongoing_deltas_is_unstable(self):
        await self.h.boot()
        await self.h.capture()
        t_stop = self.clock.now()
        for index in range(7):
            await self.clock.advance(1.0)
            await self.h.push(delta(f"e{index}", "w ", index * 100, index * 100 + 50))
        self.assertEqual(self.device.error_codes(), ["unstable_transcript"])
        self.assertAlmostEqual(self.device.times[-1] - t_stop, 6.0, places=6)
        self.assertEqual(self.ops.count("stage"), 0)
        await self.h.finish()

    async def test_t_core_sess_settle_no_deltas_is_no_speech(self):
        await self.h.boot()
        await self.h.capture()
        await self.clock.advance(1.2)
        self.assertEqual(self.device.error_codes(), ["no_speech"])
        self.assertEqual(self.ops.count("stage"), 0)
        await self.h.finish()

    async def test_t_core_sess_settle_blank_text_is_no_speech(self):
        await self.h.boot()
        await self.h.capture([delta("e1", "   ", 0, 100)])
        await self.clock.advance(1.5)
        self.assertEqual(self.device.error_codes(), ["no_speech"])
        self.assertEqual(self.frames("pet.turn.preview"), [])
        await self.h.finish()

    async def test_t_core_sess_settle_overlapping_deltas_are_unstable_immediately(self):
        await self.h.boot()
        await self.h.begin()
        await self.h.push(delta("e1", "a", 0, 1000))
        await self.h.push(delta("e2", "b", 500, 1500))
        self.assertEqual(self.device.error_codes(), ["unstable_transcript"])
        await self.h.finish()

    async def test_t_core_sess_settle_conflicting_duplicate_is_unstable(self):
        await self.h.boot()
        await self.h.begin()
        await self.h.push(delta("e1", "a", 0, 100))
        await self.h.push(delta("e1", "different", 0, 100))
        self.assertEqual(self.device.error_codes(), ["unstable_transcript"])
        await self.h.finish()

    async def test_an_identical_duplicate_delta_is_ignored(self):
        await self.h.boot()
        await self.h.begin()
        await self.h.push(delta("e1", "a", 0, 100))
        await self.h.push(delta("e1", "a", 0, 100))
        self.assertEqual(self.device.error_codes(), [])
        self.device.disconnect()
        await self.h.finish()

    async def test_t_core_sess_settle_129_segments_is_overflow(self):
        await self.h.boot()
        await self.h.begin()
        for index in range(129):
            self.provider.push(delta(f"e{index}", "x", index * 10, index * 10 + 5))
        await self.h.quiesce()
        self.assertEqual(self.device.error_codes(), ["overflow"])
        await self.h.finish()

    async def test_t_core_sess_seal_order_stage_gets_sorted_segments_then_seal(self):
        await self.h.boot()
        await self.h.capture([delta("e2", "b", 1000, 1500), delta("e1", "a ", 0, 500)])
        await self.clock.advance(1.5)  # the deltas arrived at the stop: ready at +1.5
        self.assertEqual(self.ops.staged, [("e1", "a "), ("e2", "b")])
        names = self.ops.names()
        self.assertLess(names.index("stage"), names.index("seal"))
        self.assertEqual(names.count("stage"), 1)
        (preview,) = self.frames("pet.turn.preview")
        self.assertEqual(preview["text"], "a b")
        self.assertEqual(preview["preview_revision"], 1)
        self.assertEqual(preview["text_sha256"], hashlib.sha256(b"a b").hexdigest())

    async def test_a_stage_code_other_than_appended_is_unstable(self):
        self.ops.results["stage"] = "too_long"
        await self.h.boot()
        await self.h.capture([delta("e1", "a", 0, 100)])
        await self.clock.advance(1.5)
        self.assertEqual(self.device.error_codes(), ["unstable_transcript"])
        self.assertEqual(self.ops.count("seal"), 0)
        await self.h.finish()

    async def test_a_seal_failure_is_closed_without_a_preview(self):
        self.ops.results["seal"] = BridgeError("blank_text")
        await self.h.boot()
        await self.h.capture([delta("e1", "a", 0, 100)])
        await self.clock.advance(1.5)
        self.assertEqual(self.device.error_codes(), ["no_speech"])
        self.assertEqual(self.frames("pet.turn.preview"), [])
        await self.h.finish()

    async def test_a_delta_while_stage_or_seal_runs_is_unstable_never_a_stale_preview(
        self,
    ):
        release = self.ops.hold("stage")
        await self.h.boot()
        await self.h.capture([delta("e1", "a", 0, 100)])
        await self.clock.advance(1.5)
        self.assertTrue(self.ops.entered["stage"].is_set())
        self.provider.push(delta("e2", "late", 200, 300))
        await self.h.quiesce()  # delivered while the stage call is still blocked
        release.set()
        await self.h.quiesce()
        self.assertEqual(self.frames("pet.turn.preview"), [])
        self.assertEqual(self.device.error_codes(), ["unstable_transcript"])
        await self.h.finish()

    async def test_transcripts_outside_an_active_turn_are_ignored(self):
        await self.h.boot()
        await self.h.push(delta("e0", "before begin", 0, 10))
        await self.h.begin()
        await self.h.push(delta("e1", "x", 100, 200))
        await self.h.feed({"type": "pet.turn.capture_stop", "turn_id": TURN})
        await self.clock.advance(1.5)
        (preview,) = self.frames("pet.turn.preview")
        self.assertEqual(preview["text"], "x")
        self.device.disconnect()
        await self.h.finish()


class SessionPreviewTest(SessionCase):
    async def test_preview_text_sha_and_revision_are_exact(self):
        await self.h.boot()
        await self.h.preview("hello world")
        (preview,) = self.frames("pet.turn.preview")
        self.assertEqual(
            preview,
            {
                "type": "pet.turn.preview",
                "turn_id": TURN,
                "preview_revision": 1,
                "text": "hello world",
                "text_sha256": hashlib.sha256(b"hello world").hexdigest(),
            },
        )
        self.device.disconnect()
        await self.h.finish()

    async def test_t_core_sess_late_preview_input_invalidates_stops_and_closes(self):
        await self.h.boot()
        await self.h.preview("hello")
        await self.h.push(delta("e2", "more", 600, 700))
        (invalid,) = self.frames("pet.turn.preview_invalidated")
        self.assertEqual((invalid["turn_id"], invalid["reason"]), (TURN, "late_transcript"))
        types = self.device.types()
        self.assertEqual(self.device.error_codes(), ["invalid_preview"])
        self.assertLess(types.index("pet.turn.preview_invalidated"), types.index("pet.error"))
        self.assertEqual(self.ops.count("stop"), 1)
        self.assertEqual(self.ops.count("confirm"), 0)
        self.device.disconnect()
        await self.h.finish()

    async def test_an_identical_duplicate_delta_in_preview_is_not_late_input(self):
        await self.h.boot()
        await self.h.preview("hello")
        await self.h.push(delta("e1", "hello", 0, 500))
        self.assertEqual(self.frames("pet.turn.preview_invalidated"), [])
        self.device.disconnect()
        await self.h.finish()

    async def test_stop_in_preview_closes_and_queues_one_urgent_core_stop(self):
        await self.h.boot()
        await self.h.preview("hello")
        await self.h.feed({"type": "pet.stop"})
        self.assertEqual(self.ops.count("stop"), 1)
        self.assertEqual(self.frames("pet.reply.done"), [])  # no accepted turn yet
        outcome = await self.h.finish()
        self.assertEqual(outcome.final_state, "CLOSED")


# =================================================================================
# PetSession: send, poll, speak, close, deadlines, privacy (stage 5)
# =================================================================================
class FlowCase(SessionCase):
    TEXT = "hello world"

    async def fresh_harness(self) -> None:
        """Replace the harness (own runner thread, clock, links) for a subtest."""
        await self.h.aclose()
        self.h = Harness()
        self.addAsyncCleanup(self.h.aclose)
        await self.h.open()

    def sha(self, text: str | None = None) -> str:
        return hashlib.sha256((text or self.TEXT).encode("utf-8")).hexdigest()

    def confirm_frame(
        self, *, sha: str | None = None, rev: int = 1, turn: str = TURN
    ) -> dict[str, Any]:
        return {
            "type": "pet.turn.send_confirm",
            "turn_id": turn,
            "text_sha256": sha or self.sha(),
            "preview_revision": rev,
        }

    async def to_preview(self, *, with_delegation: bool = True) -> None:
        self.t_start = self.clock.now()
        await self.h.boot()
        await self.h.begin()
        if with_delegation:
            await self.h.push(delegation())
        await self.h.push(delta("e1", self.TEXT, 0, 500))
        await self.h.feed({"type": "pet.turn.capture_stop", "turn_id": TURN})
        await self.clock.advance(1.5)

    async def to_accepted(self, *, with_delegation: bool = True) -> None:
        await self.to_preview(with_delegation=with_delegation)
        await self.h.feed(self.confirm_frame())
        self.t_acc = self.clock.now()

    def commentary(self) -> list[dict[str, Any]]:
        return self.provider.of_type("session.commentary.append")

    def audio_out(self) -> list[dict[str, Any]]:
        return self.frames("session.output_audio.delta")

    def done_states(self) -> list[str]:
        return [f["state"] for f in self.frames("pet.reply.done")]


class SessionSendTest(FlowCase):
    async def test_confirm_runs_preflight_then_confirm_with_the_listing_revision(self):
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        names = self.ops.names()
        self.assertEqual(names[-2:], ["preflight", "confirm"])
        (call,) = [c for c in self.ops.calls if c[0] == "confirm"]
        self.assertEqual(call[1], (TURN, self.sha(), 7))  # revision from the preflight
        (result,) = self.frames("pet.turn.send_result")
        self.assertEqual((result["turn_id"], result["state"]), (TURN, "accepted"))

    async def test_t_core_sess_busy_preflight_not_idle_is_busy_and_confirm_never_runs(
        self,
    ):
        self.ops.results["preflight"] = Preflight(SESSION, 7, False, "status_not_idle")
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        self.assertEqual(self.device.error_codes(), ["busy"])
        self.assertEqual(self.frames("pet.turn.send_result")[0]["state"], "rejected")
        self.assertEqual(self.ops.count("confirm"), 0)
        outcome = await self.h.finish()
        self.assertEqual(outcome.final_state, "CLOSED")

    async def test_a_preflight_failure_is_busy_and_nothing_is_sent(self):
        self.ops.results["preflight"] = RuntimeError("boom")
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        self.assertEqual(self.device.error_codes(), ["busy"])
        self.assertEqual(self.frames("pet.turn.send_result")[0]["state"], "rejected")
        self.assertEqual(self.ops.count("confirm"), 0)
        await self.h.finish()

    async def test_t_core_sess_refused_reasons_map_per_plan_6_5(self):
        mapping = {
            "command_inflight": "busy",
            "uncertain_unresolved": "busy",
            "reply_pending": "busy",
            "bridge_faulted": "busy",
            "expired": "expired",
            "digest_mismatch": "invalid_preview",
            "binding_mismatch": "invalid_preview",
        }
        for reason, expected in mapping.items():
            with self.subTest(reason=reason):
                await self.fresh_harness()
                self.ops.results["confirm"] = CommandResult(
                    TURN, "refused", reason, False
                )
                await self.to_preview()
                await self.h.feed(self.confirm_frame())
                self.assertEqual(self.device.error_codes(), [expected])
                types = self.device.types()
                self.assertLess(  # the proven no-send is reported before the error
                    types.index("pet.turn.send_result"), types.index("pet.error")
                )
                self.assertEqual(
                    self.frames("pet.turn.send_result")[0]["state"], "rejected"
                )
                self.assertEqual(self.ops.count("poll"), 0)
                await self.h.finish()

    async def test_rejected_and_uncertain_close_after_the_send_result_without_polling(
        self,
    ):
        for outcome_name in ("rejected", "uncertain"):
            with self.subTest(outcome=outcome_name):
                await self.fresh_harness()
                self.ops.results["confirm"] = CommandResult(
                    TURN, outcome_name, "http_" + outcome_name, True
                )
                await self.to_preview()
                await self.h.feed(self.confirm_frame())
                (result,) = self.frames("pet.turn.send_result")
                self.assertEqual(result["state"], outcome_name)
                self.assertEqual(self.ops.count("poll"), 0)
                self.assertEqual(self.device.error_codes(), [])
                done = await self.h.finish()
                self.assertEqual(done.send_outcome, outcome_name)

    async def test_a_confirm_job_that_raises_is_reported_uncertain_never_resent(self):
        self.ops.results["confirm"] = RuntimeError("boom")
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        (result,) = self.frames("pet.turn.send_result")
        self.assertEqual(result["state"], "uncertain")
        self.assertEqual(self.ops.count("confirm"), 1)
        await self.h.finish()

    # (the former "mismatch keeps PREVIEW" test is superseded by RepairSendTest: a wrong
    # digest/revision/turn now closes with the frames pinned there, never keeps PREVIEW)
    async def test_confirm_outside_preview_is_invalid_preview_and_no_state_change(self):
        await self.h.boot()
        await self.h.feed(self.confirm_frame())  # READY
        await self.h.begin()
        await self.h.feed(self.confirm_frame())  # CAPTURING
        self.assertEqual(self.device.error_codes(), ["invalid_preview"] * 2)
        self.assertEqual(self.ops.count("preflight"), 0)
        self.device.disconnect()
        await self.h.finish()

    async def test_a_replayed_confirm_after_accept_never_sends_again(self):
        self.ops.results["poll"] = pending_poll
        await self.to_accepted()
        await self.h.feed(self.confirm_frame())  # old revision
        await self.h.feed(self.confirm_frame(rev=2))  # a "new" revision
        self.assertEqual(self.device.error_codes(), ["invalid_preview"] * 2)
        self.assertEqual(self.ops.count("confirm"), 1)
        self.assertEqual(len(self.frames("pet.turn.send_result")), 1)

    async def test_t_core_sess_stop_inflight_confirm_runs_once_and_stop_waits_behind_it(
        self,
    ):
        release = self.ops.hold("confirm")
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        self.assertTrue(self.ops.entered["confirm"].is_set())
        await self.h.feed({"type": "pet.stop"})
        self.assertEqual(self.ops.count("stop"), 0)  # queued behind the in-flight call
        self.assertEqual(self.h.runner.depth, 1)
        self.assertEqual(self.h.runner.inflight_op, "confirm")
        assert self.h.task is not None
        outcome = await self.h.task  # run() returns; the runner still owns the call
        self.assertEqual(outcome.send_outcome, "inflight")
        self.assertEqual(self.device.of_type("pet.reply.done"), [])
        self.assertEqual(self.h.runner.inflight_op, "confirm")
        release.set()
        await self.h.runner.wait_idle()
        names = self.ops.names()
        self.assertEqual(names.count("confirm"), 1)
        self.assertLess(names.index("confirm"), names.index("stop"))
        self.assertEqual(names.count("stop"), 1)
        # Correction 4 at session level: the cancelled waiter did not lose the real result.
        (confirm_meta,) = [r for r in self.h.runner.retained() if r.op == "confirm"]
        self.assertTrue(confirm_meta.caller_cancelled)
        self.assertEqual(outcome.confirm_job, confirm_meta.job)  # U3 reconciles this id
        result = self.h.runner.take_result(confirm_meta.job)
        self.assertEqual((result.outcome, result.command_id), ("accepted", TURN))

    async def test_t_core_sess_stop_audio_is_drained_immediately_with_a_blocked_device(
        self,
    ):
        await self.to_accepted()
        await self.clock.advance(1.0)  # speaking: commentary acked, gate open
        self.device.send_block = asyncio.Event()
        for _ in range(3):
            self.provider.push_audio(b"\x01\x02" * 2400)
        await self.h.quiesce()
        await self.h.feed({"type": "pet.stop"})
        self.device.send_block.set()
        await self.h.quiesce()
        self.assertLessEqual(len(self.audio_out()), 1)  # only the mid-send chunk
        outcome = await self.h.finish()
        self.assertEqual(outcome.final_state, "CLOSED")

    async def test_t_core_sess_close_nocancel_close_in_await_reply_only_stops_locally(
        self,
    ):
        self.ops.results["poll"] = pending_poll
        await self.to_accepted()
        before = list(self.ops.names())
        await self.h.feed({"type": "session.close"})
        added = self.ops.names()[len(before) :]
        self.assertEqual(added, ["stop"])  # no Unified call besides the local stop
        self.assertEqual(self.done_states(), ["stopped"])
        self.assertEqual(self.provider.types().count("session.close"), 1)
        outcome = await self.h.finish()
        self.assertEqual(outcome.reply_done, "stopped")


class RepairConfirmTest(FlowCase):
    """U2 repair M4: a confirm outcome is either recorded by the session or still held."""

    async def test_u2r_m4_confirm_settled_during_closing_is_recorded_without_frames(
        self,
    ):
        self.provider.auto_close = False
        release = self.ops.hold("confirm")
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        await self.h.feed({"type": "pet.stop"})  # CLOSING: waiting for the provider
        self.assertEqual(self.h.session.outcome.send_outcome, "inflight")
        release.set()
        await self.h.quiesce()
        outcome = self.h.session.outcome
        self.assertEqual(
            (outcome.send_outcome, outcome.confirm_job), ("accepted", None)
        )
        self.assertEqual(self.frames("pet.turn.send_result"), [])
        self.assertEqual(self.h.runner.held_results(), ())
        await self.clock.advance(15.0)
        final = await self.h.finish()
        self.assertEqual(
            (final.send_outcome, final.close_confirmed), ("accepted", False)
        )
        self.assertEqual(self.frames("pet.turn.send_result"), [])

    async def test_u2r_m4_confirm_inflight_at_run_return_exposes_the_job_and_take_result_returns_the_real_result(
        self,
    ):
        release = self.ops.hold("confirm")
        result = CommandResult(TURN, "accepted", "accepted", True)
        self.ops.results["confirm"] = result
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        await self.h.feed({"type": "pet.stop"})
        assert self.h.task is not None
        outcome = await self.h.task
        self.assertEqual(outcome.send_outcome, "inflight")
        self.assertIsNotNone(outcome.confirm_job)
        release.set()
        await self.h.runner.wait_idle()
        (meta,) = [r for r in self.h.runner.retained() if r.op == "confirm"]
        self.assertEqual(outcome.confirm_job, meta.job)
        self.assertIs(self.h.runner.take_result(outcome.confirm_job), result)
        with self.assertRaises(gc.RunnerError) as ctx:
            self.h.runner.take_result(outcome.confirm_job)
        self.assertEqual(ctx.exception.code, "runner_result_unavailable")

    async def test_u2r_m4_confirm_exception_is_the_exact_exception_via_take_result(
        self,
    ):
        boom = BridgeError("faulted")
        release = self.ops.hold("confirm")
        self.ops.results["confirm"] = boom
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        await self.h.feed({"type": "pet.stop"})
        assert self.h.task is not None
        outcome = await self.h.task
        self.assertEqual(outcome.send_outcome, "inflight")
        self.assertIsNotNone(outcome.confirm_job)
        release.set()
        await self.h.runner.wait_idle()
        with self.assertRaises(BridgeError) as ctx:
            self.h.runner.take_result(outcome.confirm_job)
        self.assertIs(ctx.exception, boom)

    async def test_u2r_m4_run_cancelled_while_confirm_inflight_keeps_the_job_reconcilable(
        self,
    ):
        release = self.ops.hold("confirm")
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        assert self.h.task is not None
        self.h.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.h.task
        outcome = self.h.session.outcome
        self.assertEqual(outcome.send_outcome, "inflight")
        self.assertIsNotNone(outcome.confirm_job)
        release.set()
        await self.h.runner.wait_idle()
        self.assertEqual(
            self.h.runner.take_result(outcome.confirm_job).outcome, "accepted"
        )

    async def test_u2r_m4_every_cancel_point_either_recorded_or_still_held(self):
        for turns in (0, 1, 2, 3, 5, 8):
            with self.subTest(turns=turns):
                await self.fresh_harness()
                release = self.ops.hold("confirm")
                await self.to_preview()
                await self.h.feed(self.confirm_frame())
                release.set()
                for _ in range(turns):
                    await asyncio.sleep(0)
                assert self.h.task is not None
                self.h.task.cancel()
                await asyncio.gather(self.h.task, return_exceptions=True)
                await self.h.runner.wait_idle()
                outcome = self.h.session.outcome
                recorded = outcome.send_outcome == "accepted" and (
                    outcome.confirm_job is None
                )
                held = outcome.confirm_job is not None
                self.assertTrue(recorded != held, outcome)  # exactly one, never neither
                if held:
                    self.assertEqual(
                        self.h.runner.take_result(outcome.confirm_job).outcome,
                        "accepted",
                    )

    async def test_u2r_m4_non_confirm_results_are_never_retained(self):
        release = self.ops.hold("poll")
        await self.to_accepted()
        await self.clock.advance(1.0)  # the poll is now running (blocked)
        await self.h.feed({"type": "pet.stop"})
        release.set()
        await self.h.runner.wait_idle()
        self.assertEqual(self.h.runner.held_results(), ())
        (stop,) = [r for r in self.h.runner.retained() if r.op == "stop"]
        with self.assertRaises(gc.RunnerError) as ctx:
            self.h.runner.take_result(stop.job)
        self.assertEqual(ctx.exception.code, "runner_result_unavailable")
        self.assertNotIn("REPLYCANARY", repr(self.h.runner.retained()))
        await self.h.finish()

    async def test_u2r_m4_stop_job_does_not_crowd_the_confirm_reservoir(self):
        settings = gc.SessionSettings(self.h.model, "marin", LABEL, SHORT8)
        for _ in range(20):
            device = FakeDeviceLink(self.clock)
            provider = FakeProviderLink(self.clock)
            session = gc.PetSession(
                device,
                FakeProviderFactory(provider),
                self.h.runner,
                self.ops,
                settings,
                self.clock,
                new_event_id=self.h.new_id,
            )
            task = asyncio.create_task(session.run())
            device.feed(ASSISTANT_START)
            await self.h.quiesce()
            device.feed({"type": "pet.stop"})
            await self.h.quiesce()
            await asyncio.wait_for(task, 5)
        self.assertEqual(self.h.runner.held_results(), ())
        release = self.ops.hold("confirm")
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        await self.h.feed({"type": "pet.stop"})
        assert self.h.task is not None
        outcome = await self.h.task
        release.set()
        await self.h.runner.wait_idle()
        self.assertEqual(self.h.runner.held_results(), (outcome.confirm_job,))
        self.assertEqual(
            self.h.runner.take_result(outcome.confirm_job).outcome, "accepted"
        )


class RepairSendTest(FlowCase):
    """U2 repair M1: known-no-send frames and a send_result that never lies."""

    def after_preview(self, base: int) -> list[str]:
        """Frame types after ``base``, without the closing handshake frame."""
        return [t for t in self.device.types()[base:] if t != "session.closed"]

    def send_results(self) -> list[dict[str, Any]]:
        return self.frames("pet.turn.send_result")

    async def mismatch_case(self, **bad: Any) -> gc.SessionOutcome:
        await self.to_preview()
        base = len(self.device.frames)
        await self.h.feed(self.confirm_frame(**bad))
        self.assertEqual(
            self.after_preview(base), ["pet.turn.send_result", "pet.error"]
        )
        (result,) = self.send_results()
        self.assertEqual((result["state"], result["turn_id"]), ("rejected", TURN))
        self.assertEqual(self.device.error_codes(), ["invalid_preview"])
        self.assertEqual(self.ops.count("preflight"), 0)
        self.assertEqual(self.ops.count("confirm"), 0)
        outcome = await self.h.finish()
        self.assertEqual(self.device.close_codes, [1008])
        return outcome

    async def test_u2r_m1_preview_wrong_digest_is_rejected_then_error_then_close_1008(
        self,
    ):
        outcome = await self.mismatch_case(sha=self.sha("other text"))
        self.assertEqual(
            (outcome.send_outcome, outcome.error_code), ("refused", "invalid_preview")
        )

    async def test_u2r_m1_preview_wrong_revision_is_rejected_then_error_then_close_1008(
        self,
    ):
        outcome = await self.mismatch_case(rev=2)
        self.assertEqual(
            (outcome.send_outcome, outcome.error_code), ("refused", "invalid_preview")
        )

    async def test_u2r_m1_preview_foreign_turn_is_error_only_no_send_result_of_any_id(
        self,
    ):
        await self.to_preview()
        await self.h.feed(self.confirm_frame(turn=UUID_OTHER))
        self.assertEqual(self.send_results(), [])
        self.assertEqual(self.device.error_codes(), ["invalid_preview"])
        self.assertEqual(self.ops.count("preflight") + self.ops.count("confirm"), 0)
        outcome = await self.h.finish()
        self.assertEqual(self.device.close_codes, [1008])
        self.assertIsNone(outcome.send_outcome)

    async def test_u2r_m1_malformed_confirm_is_unknown_command_close_without_send_result(
        self,
    ):
        for bad in (
            {**self.confirm_frame(), "text_sha256": "not-a-sha"},
            {**self.confirm_frame(), "extra": 1},
        ):
            with self.subTest(frame=sorted(bad)):
                await self.fresh_harness()
                await self.to_preview()
                await self.h.feed(bad)
                self.assertEqual(self.device.error_codes(), ["unknown_command"])
                self.assertEqual(self.send_results(), [])
                self.assertEqual(self.ops.count("preflight"), 0)
                await self.h.finish()

    async def test_u2r_m1_duplicate_confirm_in_sending_awaiting_and_speaking_is_error_only(
        self,
    ):
        for stage in ("sending", "awaiting", "speaking"):
            with self.subTest(stage=stage):
                await self.fresh_harness()
                release = None
                if stage == "sending":
                    release = self.ops.hold("preflight")
                    await self.to_preview()
                    await self.h.feed(self.confirm_frame())
                else:
                    if stage == "awaiting":
                        self.ops.results["poll"] = pending_poll
                    await self.to_accepted()
                    if stage == "speaking":
                        await self.clock.advance(1.0)
                before = len(self.send_results())
                await self.h.feed(self.confirm_frame())
                self.assertEqual(self.device.error_codes(), ["invalid_preview"])
                self.assertEqual(len(self.send_results()), before)
                self.assertLessEqual(len(self.send_results()), 1)
                self.assertEqual(self.device.close_codes, [])
                self.assertLessEqual(self.ops.count("confirm"), 1)
                if release is not None:
                    release.set()
                self.device.disconnect()
                await self.h.finish()

    async def test_u2r_m1_preflight_not_idle_or_error_is_rejected_before_busy(self):
        for label, scripted in (
            ("not_idle", Preflight(SESSION, 7, False, "status_not_idle")),
            ("raised", RuntimeError("boom")),
        ):
            with self.subTest(case=label):
                await self.fresh_harness()
                self.ops.results["preflight"] = scripted
                await self.to_preview()
                base = len(self.device.frames)
                await self.h.feed(self.confirm_frame())
                self.assertEqual(
                    self.after_preview(base), ["pet.turn.send_result", "pet.error"]
                )
                self.assertEqual(self.send_results()[0]["state"], "rejected")
                self.assertEqual(self.device.error_codes(), ["busy"])
                self.assertEqual(self.ops.count("confirm"), 0)
                outcome = await self.h.finish()
                self.assertEqual(outcome.send_outcome, "refused")

    async def test_u2r_m1_confirm_submit_runner_error_is_rejected_not_uncertain(self):
        original = self.h.runner.submit

        def submit(op: str, fn: Callable[[], Any], **kw: Any):
            if op == "confirm":
                raise gc.RunnerError("runner_busy")
            return original(op, fn, **kw)

        await self.to_preview()
        base = len(self.device.frames)
        with mock.patch.object(self.h.runner, "submit", submit):
            await self.h.feed(self.confirm_frame())
        self.assertEqual(
            self.after_preview(base), ["pet.turn.send_result", "pet.error"]
        )
        self.assertEqual(self.send_results()[0]["state"], "rejected")
        self.assertEqual(self.device.error_codes(), ["busy"])
        self.assertEqual(self.ops.count("confirm"), 0)
        outcome = await self.h.finish()
        self.assertEqual(outcome.send_outcome, "refused")

    async def test_u2r_m1_allowlisted_refusals_are_rejected_before_the_mapped_error(
        self,
    ):
        for reason in sorted(KNOWN_UNSENT_REASONS):
            with self.subTest(reason=reason):
                await self.fresh_harness()
                self.ops.results["confirm"] = CommandResult(
                    TURN, "refused", reason, False
                )
                await self.to_preview()
                base = len(self.device.frames)
                await self.h.feed(self.confirm_frame())
                self.assertEqual(
                    self.after_preview(base), ["pet.turn.send_result", "pet.error"]
                )
                self.assertEqual(len(self.send_results()), 1)
                self.assertEqual(self.send_results()[0]["state"], "rejected")
                self.assertEqual(
                    self.device.error_codes(),
                    [gc._REFUSED_CODES.get(reason, "invalid_preview")],
                )
                outcome = await self.h.finish()
                self.assertEqual(outcome.send_outcome, "refused")

    async def test_u2r_m1_refusal_not_proof_is_uncertain(self):
        for reason in (
            "duplicate_confirm",
            "journal_error",
            "unknown_turn",
            "zzz_other",
        ):
            with self.subTest(reason=reason):
                await self.fresh_harness()
                self.ops.results["confirm"] = CommandResult(
                    TURN, "refused", reason, False
                )
                await self.to_preview()
                base = len(self.device.frames)
                await self.h.feed(self.confirm_frame())
                self.assertEqual(
                    self.after_preview(base), ["pet.turn.send_result", "pet.error"]
                )
                (result,) = self.send_results()
                self.assertEqual(result["state"], "uncertain")  # never "rejected"
                self.assertEqual(self.device.error_codes(), ["invalid_preview"])
                outcome = await self.h.finish()
                self.assertEqual(outcome.send_outcome, "uncertain")

    async def test_u2r_m1_core_results_are_reported_exactly_once_as_the_core_said(
        self,
    ):
        cases = {
            "accepted": CommandResult(TURN, "accepted", "accepted", True),
            "rejected": CommandResult(TURN, "rejected", "http_rejected", True),
            "uncertain": CommandResult(TURN, "uncertain", "http_uncertain", True),
            "raised": RuntimeError("boom"),
        }
        for label, scripted in cases.items():
            with self.subTest(case=label):
                await self.fresh_harness()
                self.ops.results["confirm"] = scripted
                self.ops.results["poll"] = pending_poll
                await self.to_preview()
                await self.h.feed(self.confirm_frame())
                (result,) = self.send_results()
                want = "uncertain" if label == "raised" else label
                self.assertEqual(result["state"], want)
                self.device.disconnect()
                outcome = await self.h.finish()
                self.assertEqual(len(self.send_results()), 1)
                self.assertEqual(outcome.send_outcome, want)

    async def test_u2r_m1_stop_during_preflight_is_rejected_before_the_close(self):
        release = self.ops.hold("preflight")
        await self.to_preview()
        base = len(self.device.frames)
        await self.h.feed(self.confirm_frame())
        await self.h.feed({"type": "pet.stop"})
        release.set()
        outcome = await self.h.finish()
        await self.h.runner.wait_idle()
        self.assertEqual(self.after_preview(base), ["pet.turn.send_result"])
        self.assertEqual(self.send_results()[0]["state"], "rejected")
        self.assertEqual(self.ops.count("confirm"), 0)
        self.assertEqual(outcome.send_outcome, "refused")

    async def test_u2r_m1_deadline_and_overflow_during_preflight_reject_before_their_error(
        self,
    ):
        for label in ("deadline", "overflow"):
            with self.subTest(case=label):
                await self.fresh_harness()
                release = self.ops.hold("preflight")
                await self.to_preview()
                await self.h.feed(self.confirm_frame())
                if label == "deadline":
                    await self.clock.advance(self.t_start + 60.0 - self.clock.now())
                else:
                    for _ in range(300):
                        self.device.feed(audio_frame(10))
                    await self.h.quiesce()
                types = self.device.types()
                self.assertIn("pet.turn.send_result", types)
                self.assertEqual(self.device.error_codes()[:1], [label])
                self.assertLess(
                    types.index("pet.turn.send_result"), types.index("pet.error")
                )
                release.set()
                await self.h.finish()
                await self.h.runner.wait_idle()
                self.assertEqual(self.ops.count("confirm"), 0)

    async def test_u2r_m1_stop_while_confirm_is_admitted_makes_no_unsent_claim(self):
        release = self.ops.hold("confirm")
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        await self.h.feed({"type": "pet.stop"})
        assert self.h.task is not None
        outcome = await self.h.task
        self.assertEqual(self.send_results(), [])
        self.assertEqual(outcome.send_outcome, "inflight")
        release.set()
        await self.h.runner.wait_idle()

    async def test_u2r_m1_confirm_while_closing_never_reaches_the_core_and_emits_no_send_result(
        self,
    ):
        self.provider.auto_close = False
        await self.to_preview()
        await self.h.push(delta("e2", "late words", 600, 700))  # late input: closing
        await self.h.feed(self.confirm_frame())
        self.assertEqual(self.send_results(), [])
        self.assertEqual(self.ops.count("confirm") + self.ops.count("preflight"), 0)
        await self.clock.advance(15.0)
        await self.h.finish()


class SessionPollTest(FlowCase):
    async def test_t_core_sess_poll_cadence_plus_1_2_3_then_deadline_unavailable(self):
        self.ops.results["poll"] = pending_poll
        await self.to_accepted()
        await self.clock.advance(3.0)
        polls = [t - self.t_acc for t in self.ops.times("poll")]
        for got, want in zip(polls, (1.0, 2.0, 3.0), strict=True):
            self.assertAlmostEqual(got, want, places=6)
        await self.clock.advance(self.t_start + 58.0 - self.clock.now() - 0.1)
        self.assertEqual(self.device.error_codes(), [])
        await self.clock.advance(0.1)
        # Work stops at 58 s; the remaining cap is available for confirmed cleanup.
        self.assertEqual(self.device.error_codes(), ["deadline"])
        self.assertEqual(self.done_states(), ["unavailable"])
        types = self.device.types()
        self.assertLess(types.index("pet.error"), types.index("pet.reply.done"))
        outcome = await self.h.finish()
        self.assertEqual(
            (outcome.error_code, outcome.reply_done), ("deadline", "unavailable")
        )

    async def test_t_core_sess_work58_applies_to_ready_and_preserves_the_60s_cap(self):
        self.t_start = self.clock.now()
        await self.h.boot()
        await self.clock.advance(57.9)
        self.assertEqual(self.device.error_codes(), [])
        await self.clock.advance(0.1)
        self.assertEqual(self.device.error_codes(), ["deadline"])
        self.assertTrue(self.h.session.outcome.close_confirmed)
        self.assertAlmostEqual(self.device.times[-1] - self.t_start, 58.0, places=6)
        await self.h.finish()

    async def test_t_core_sess_late_await_transcript_during_await_reply_stops_polling(
        self,
    ):
        self.ops.results["poll"] = pending_poll
        await self.to_accepted()
        await self.clock.advance(1.0)
        polls = self.ops.count("poll")
        await self.h.push(delta("e9", "more", 900, 950))
        self.assertEqual(self.done_states(), ["unavailable"])
        await self.clock.advance(5.0)
        self.assertEqual(self.ops.count("poll"), polls)
        await self.h.finish()

    async def test_late_input_while_the_send_is_in_flight_contaminates_the_reply(self):
        release = self.ops.hold("confirm")
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        await self.h.push(delta("e9", "more", 900, 950))
        release.set()
        await self.h.quiesce()
        (result,) = self.frames("pet.turn.send_result")
        self.assertEqual(result["state"], "accepted")  # the fact is reported once
        self.assertEqual(self.done_states(), ["unavailable"])
        self.assertEqual(self.ops.count("poll"), 0)
        self.assertEqual(self.ops.count("confirm"), 1)
        await self.h.finish()

    async def test_a_poll_job_failure_is_reply_unavailable_not_a_retry_storm(self):
        self.ops.results["poll"] = RuntimeError("boom")
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.assertEqual(self.device.error_codes(), ["reply_unavailable"])
        self.assertEqual(self.done_states(), ["unavailable"])
        self.assertEqual(self.ops.count("poll"), 1)
        await self.h.finish()

    async def test_a_terminal_not_spoken_reply_is_unavailable(self):
        self.ops.results["poll"] = lambda turn: ReplyResult(
            turn, "not_spoken", "coverage_incomplete", True, 3
        )
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.assertEqual(self.done_states(), ["unavailable"])
        self.assertEqual(self.device.error_codes(), [])
        await self.h.finish()


PCM_CHUNK = b"\x01\x02" * 2400
ACK_FOREIGN = {"type": "session.commentary.appended", "client_event_id": "gw_9999"}
LONG_REPLY = " ".join(f"Sentence number {n} is right here." for n in range(60))


def ack_of(frame: dict[str, Any]) -> dict[str, Any]:
    return {"type": "session.commentary.appended", "client_event_id": frame["event_id"]}


class SpeakCase(FlowCase):
    async def flow(
        self,
        *,
        pre: tuple[str, ...] = (),
        capture: tuple[str, ...] = (DELEG,),
        preview: tuple[str, ...] = (),
        await_reply: tuple[str, ...] = (),
    ) -> None:
        """Start, then delegation hints at chosen states, a preview, confirm, accepted."""
        self.t_start = self.clock.now()
        await self.h.boot()
        for ident in pre:
            await self.h.push(delegation(ident))
        await self.h.begin()
        for ident in capture:
            await self.h.push(delegation(ident))
        await self.h.push(delta("e1", self.TEXT, 0, 500))
        await self.h.feed({"type": "pet.turn.capture_stop", "turn_id": TURN})
        await self.clock.advance(1.5)
        for ident in preview:
            await self.h.push(delegation(ident))
        await self.h.feed(self.confirm_frame())
        self.t_acc = self.clock.now()
        for ident in await_reply:
            await self.h.push(delegation(ident))


class SessionTimelineParityTest(SpeakCase):
    """Carry the qualified host diagnostic's bounded timeline into PetSession.

    Packet gaps, silence and commentary ACKs do not establish speech completion.
    These are synthetic boundary checks, not provider or physical audio evidence.
    """

    def setUp(self) -> None:
        super().setUp()
        attempts: list[str] = []
        self.addCleanup(lambda: self.assertEqual(attempts, []))

        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            attempts.append("forbidden_effect")
            raise AssertionError("external effect attempted in parity test")

        # A handler may catch an exception; the cleanup census must still fail.
        for target in (
            "socket.socket.connect", "socket.socket.connect_ex",
            "socket.create_connection", "socket.getaddrinfo",
            "socket.socket.bind", "socket.socket.listen", "subprocess.Popen",
        ):
            patcher = mock.patch(target, side_effect=refuse)
            patcher.start()
            self.addCleanup(patcher.stop)

    def input_pcm(self) -> list[bytes]:
        return [
            base64.b64decode(frame["audio"])
            for frame in self.provider.of_type("session.input_audio.append")
        ]

    async def test_finite_capture_then_paced_silence_keeps_microphone_muted(self):
        await self.h.boot()
        await self.h.begin()
        mic = audio_frame(100)  # 200 bytes, including a short final pacing interval
        await self.h.feed(mic)
        await self.h.push(delta("e1", self.TEXT, 0, 500))
        await self.h.feed({"type": "pet.turn.capture_stop", "turn_id": TURN})
        await self.h.feed(mic)  # no microphone content after capture_stop
        await self.clock.advance(0.004)
        self.assertEqual(len(self.input_pcm()), 1)
        await self.clock.advance(0.001)
        self.assertEqual(self.input_pcm()[1:], [bytes(4800)])
        await self.clock.advance(0.3)
        self.assertEqual(self.input_pcm()[1:], [bytes(4800)] * 4)
        times = [
            t for t, f in zip(self.provider.times, self.provider.sent, strict=True)
            if f["type"] == "session.input_audio.append"
        ]
        self.assertAlmostEqual(times[1] - times[0], 200 / 48000)
        for left, right in zip(times[1:], times[2:]):
            self.assertAlmostEqual(right - left, 0.1)
        await self.clock.advance(1.2)  # preview still has a live, silent input timeline
        self.assertEqual(len(self.frames("pet.turn.preview")), 1)
        self.assertTrue(all(pcm == bytes(4800) for pcm in self.input_pcm()[1:]))
        await self.h.feed({"type": "pet.stop"})
        outcome = await self.h.finish()
        self.assertEqual(outcome.audio_in_bytes, 200)  # generated zeros are not capture
        count = len(self.input_pcm())
        await self.clock.advance(1.0)
        self.assertEqual(len(self.input_pcm()), count)

    async def test_silence_waits_for_slow_final_finite_send_without_catchup_burst(self):
        await self.h.boot()
        await self.h.begin()
        block = asyncio.Event()
        self.provider.send_block = block
        await self.h.feed(audio_frame(2400))
        await self.h.feed(audio_frame(100))
        await self.h.feed({"type": "pet.turn.capture_stop", "turn_id": TURN})
        await self.clock.advance(0.5)
        self.assertEqual(self.input_pcm(), [])
        block.set()
        await self.h.quiesce()
        self.assertEqual(len(self.input_pcm()), 2)
        await self.clock.advance(0.004)
        self.assertEqual(len(self.input_pcm()), 2)
        await self.clock.advance(0.001)
        self.assertEqual(self.input_pcm()[2:], [bytes(4800)])
        await self.clock.advance(0.1)
        self.assertEqual(self.input_pcm()[2:], [bytes(4800)] * 2)

    async def test_packet_gap_does_not_seal_or_report_completed_and_suffix_survives(self):
        await self.to_accepted()
        await self.clock.advance(1.0)  # commentary + correlated ACK
        prefix, suffix = b"\x01\x02" * 2400, b"\x03\x04" * 2400
        self.provider.push_audio(prefix)
        await self.h.quiesce()
        await self.clock.advance(2.0)  # longer than the old 1.5-second packet-quiet cutoff
        self.assertEqual(self.done_states(), [])
        self.provider.push_audio(suffix)
        await self.h.quiesce()
        self.assertEqual(
            b"".join(base64.b64decode(f["delta"]) for f in self.audio_out()),
            prefix + suffix,
        )
        self.assertTrue(self.input_pcm())
        self.assertTrue(all(pcm == bytes(4800) for pcm in self.input_pcm()))
        await self.h.feed({"type": "pet.stop"})
        outcome = await self.h.finish()
        self.assertEqual(outcome.reply_done, "stopped")
        self.assertEqual(self.ops.count("confirm"), 1)
        self.assertEqual(self.provider.types().count("session.close"), 1)

    async def test_work_budget_reserves_cleanup_and_never_promotes_silent_pcm(self):
        self.provider.auto_close = False
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.provider.push_audio(bytes(4800))  # acoustic silence is not speech completion
        await self.h.quiesce()
        await self.clock.advance(self.t_start + 57.9 - self.clock.now())
        self.assertEqual(self.done_states(), [])
        await self.clock.advance(0.1)
        self.assertEqual(self.done_states(), ["unavailable"])
        self.assertEqual(self.provider.types().count("session.close"), 1)
        count = len(self.input_pcm())
        await self.clock.advance(1.9)
        self.assertEqual(self.device.close_codes, [])
        self.assertEqual(len(self.input_pcm()), count)
        await self.clock.advance(0.1)
        outcome = await self.h.finish()
        self.assertEqual(outcome.reply_done, "unavailable")
        self.assertFalse(outcome.close_confirmed)
        self.assertAlmostEqual(self.clock.now() - self.t_start, 60.0)
        self.assertEqual(self.ops.count("confirm"), 1)
        self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})

    async def block_generated_silence(self) -> asyncio.Event:
        await self.to_accepted()
        block = asyncio.Event()
        self.provider.send_block = block
        await self.clock.advance(0.1)
        self.assertEqual(self.h.session._pout.pending_audio, 4800)
        return block

    async def test_blocked_generated_silence_has_no_backlog_or_catchup_burst(self):
        block = await self.block_generated_silence()
        count = len(self.input_pcm())
        await self.clock.advance(0.5)
        self.assertEqual(len(self.input_pcm()), count)
        self.assertEqual(self.h.session._pout.pending_audio, 4800)
        self.assertEqual(len(self.h.session._pout._queue), 0)
        block.set()
        await self.h.quiesce()
        self.assertEqual(len(self.input_pcm()), count + 1)
        await self.clock.advance(0.099)
        self.assertEqual(len(self.input_pcm()), count + 1)
        await self.clock.advance(0.001)
        self.assertEqual(len(self.input_pcm()), count + 2)
        await self.h.feed({"type": "pet.stop"})
        await self.h.finish()
        self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})

    async def test_stop_during_generated_silence_send_does_not_rearm_on_completion(self):
        block = await self.block_generated_silence()
        count = len(self.input_pcm())
        await self.h.feed({"type": "pet.stop"})
        self.assertEqual(self.done_states(), ["stopped"])
        block.set()  # the one in-flight frame may complete, never a new one
        outcome = await self.h.finish()
        self.assertEqual(len(self.input_pcm()), count + 1)
        await self.clock.advance(1.0)
        self.assertEqual(len(self.input_pcm()), count + 1)
        self.assertEqual(self.provider.types().count("session.close"), 1)
        self.assertEqual(outcome.reply_done, "stopped")
        self.assertNotIn("silence", self.h.session._timers)
        self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})

    async def test_permanently_blocked_silence_still_stops_at_absolute_cap(self):
        await self.block_generated_silence()
        count = len(self.input_pcm())
        await self.clock.advance(self.t_start + 58.0 - self.clock.now())
        self.assertEqual(self.done_states(), ["unavailable"])
        self.assertEqual(self.h.session._pout.pending_audio, 4800)
        await self.clock.advance(2.0)
        outcome = await self.h.finish()
        self.assertEqual(len(self.input_pcm()), count)
        self.assertFalse(outcome.close_confirmed)
        self.assertEqual(outcome.reply_done, "unavailable")
        self.assertAlmostEqual(self.clock.now() - self.t_start, 60.0)
        self.assertEqual(self.provider.close_calls, 1)
        self.assertEqual(self.provider.types().count("session.close"), 0)
        self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})

    async def test_failed_assertion_with_blocked_silence_cannot_strand_harness_cleanup(self):
        block = await self.block_generated_silence()
        try:
            raise AssertionError("simulated failed test before transport release")
        except AssertionError:
            pass  # exercise the same harness cleanup that addAsyncCleanup invokes
        cleanup = asyncio.create_task(self.h.aclose())
        done, _pending = await asyncio.wait({cleanup}, timeout=1.0)
        settled_without_rescue = cleanup in done
        if not settled_without_rescue:
            # Bound the RED test itself: do not leave the demonstrated deadlock alive.
            block.set()
            self.h.task.cancel()  # documented second cancellation aborts shielded cleanup
            await asyncio.wait_for(cleanup, 2.0)
        self.assertTrue(settled_without_rescue)
        self.assertTrue(self.h.task.done())
        self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})


class SessionSpeakTest(SpeakCase):
    async def test_t_core_sess_ack_foreign_ignored_first_match_opens_the_audio_epoch(
        self,
    ):
        self.provider.auto_ack = False
        await self.to_accepted()
        await self.clock.advance(1.0)  # poll -> speak -> commentary
        (chunk,) = self.commentary()
        self.assertEqual(
            (chunk["delegation_id"], chunk["content"]), (DELEG, REPLY_TEXT)
        )
        self.provider.push_audio(PCM_CHUNK)  # post-send quarantine; not played before ACK
        await self.h.quiesce()
        self.assertEqual(self.audio_out(), [])
        await self.h.push(ACK_FOREIGN)
        await self.h.push({"type": "session.commentary.appended"})  # uncorrelated
        self.assertEqual(self.frames("pet.reply.started"), [])
        await self.h.push(ack_of(chunk))
        (started,) = self.frames("pet.reply.started")
        self.assertEqual(started["rendering"], "spoken_rendering_not_verbatim")
        self.assertEqual(
            b"".join(base64.b64decode(frame["delta"]) for frame in self.audio_out()),
            PCM_CHUNK,
        )
        self.provider.push_audio(PCM_CHUNK)
        await self.h.quiesce()
        await self.clock.advance(.1)
        self.assertEqual(
            b"".join(base64.b64decode(frame["delta"]) for frame in self.audio_out()),
            PCM_CHUNK * 2,
        )
        types = self.device.types()
        self.assertLess(
            types.index("pet.reply.started"), types.index("session.output_audio.delta")
        )

    async def test_t_core_sess_ack_missing_for_10s_after_the_last_chunk_is_unavailable(
        self,
    ):
        self.provider.auto_ack = False
        await self.to_accepted()
        await self.clock.advance(1.0)
        await self.clock.advance(9.9)
        self.assertEqual(self.done_states(), [])
        await self.clock.advance(0.1)
        self.assertEqual(self.done_states(), ["unavailable"])
        self.assertEqual(self.audio_out(), [])
        self.assertEqual(self.device.error_codes(), [])
        await self.h.finish()

    async def test_t_core_sess_no_delegation_means_no_commentary_and_unavailable(self):
        await self.to_accepted(with_delegation=False)
        await self.clock.advance(1.0)
        self.assertEqual(self.device.error_codes(), ["no_delegation"])
        self.assertEqual(self.done_states(), ["unavailable"])
        self.assertEqual(self.commentary(), [])
        await self.h.finish()

    async def test_t_core_sess_drain_audio_from_before_speaking_is_never_replayed(self):
        await self.to_accepted()
        self.provider.push_audio(PCM_CHUNK)  # old output during AWAIT_REPLY
        await self.h.quiesce()
        await self.clock.advance(1.0)  # speaking: gate opens after the ack
        self.assertEqual(self.audio_out(), [])
        self.provider.push_audio(PCM_CHUNK)
        await self.h.quiesce()
        self.assertEqual(len(self.audio_out()), 1)

    async def test_t_core_sess_packet_quiet_is_not_speech_completion(
        self,
    ):
        await self.to_accepted()
        await self.clock.advance(1.0)
        t_ack = self.clock.now()
        await self.clock.advance(0.5)
        self.provider.push_audio(PCM_CHUNK)
        await self.h.quiesce()
        await self.clock.advance(1.49)  # t_ack + 1.99: not quiet yet
        self.assertEqual(self.done_states(), [])
        await self.clock.advance(0.02)
        self.assertEqual(self.done_states(), [])
        self.assertAlmostEqual(self.clock.now() - t_ack, 2.01, places=6)
        await self.h.feed({"type": "pet.stop"})
        types = self.device.types()
        self.assertLess(
            types.index("session.output_audio.delta"), types.index("pet.reply.done")
        )
        outcome = await self.h.finish()
        self.assertEqual(outcome.audio_out_bytes, 4800)
        self.assertEqual(
            (outcome.reply_done, outcome.close_confirmed), ("stopped", True)
        )
        self.assertEqual(self.device.close_codes, [1000])
        self.assertEqual(self.device.types()[-1], "session.closed")

    async def test_t_core_sess_halfduplex_speaking_mutes_input_and_ignores_hints(self):
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.provider.push_audio(PCM_CHUNK)
        await self.h.quiesce()
        await self.h.feed(audio_frame(100))  # muted
        await self.h.push(delegation("dlg_other"))  # ignored
        await self.h.push(delta("e7", "interrupt", 800, 900))  # ignored
        self.assertTrue(all(
            base64.b64decode(f["audio"]) == bytes(4800)
            for f in self.provider.of_type("session.input_audio.append")
        ))
        self.assertEqual(self.device.error_codes(), [])
        await self.clock.advance(1.6)
        self.assertEqual(self.done_states(), [])
        await self.h.feed({"type": "pet.stop"})
        await self.h.finish()

    async def test_t_core_sess_speak_epoch_false_is_unavailable_with_no_commentary(
        self,
    ):
        self.ops.results["epoch_current"] = False
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.assertEqual(self.commentary(), [])
        self.assertEqual(self.done_states(), ["unavailable"])
        await self.h.finish()

    async def test_an_epoch_that_goes_stale_before_the_audio_gate_opens_is_unavailable(
        self,
    ):
        answers = iter([True, False])
        self.ops.results["epoch_current"] = lambda _epoch: next(answers)
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.assertEqual(len(self.commentary()), 1)  # first check passed
        self.assertEqual(self.frames("pet.reply.started"), [])
        self.assertEqual(self.done_states(), ["unavailable"])
        await self.h.finish()

    async def test_an_unchunkable_reply_is_reply_unavailable_never_truncated(self):
        self.ops.results["poll"] = lambda turn: ReplyResult(
            turn, "speak", "reply_complete", True, 3, "x" * 70000
        )
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.assertEqual(self.device.error_codes(), ["reply_unavailable"])
        self.assertEqual(self.done_states(), ["unavailable"])
        self.assertEqual(self.commentary(), [])
        await self.h.finish()

    async def test_a_long_reply_is_sent_as_lossless_chunks_with_distinct_event_ids(
        self,
    ):
        self.ops.results["poll"] = lambda turn: ReplyResult(
            turn, "speak", "reply_complete", True, 3, LONG_REPLY
        )
        await self.to_accepted()
        await self.clock.advance(1.0)
        chunks = self.commentary()
        self.assertGreaterEqual(len(chunks), 3)
        self.assertEqual("".join(c["content"] for c in chunks), LONG_REPLY)
        self.assertTrue(all(len(c["content"].encode()) <= 480 for c in chunks))
        self.assertEqual(len({c["event_id"] for c in chunks}), len(chunks))
        self.assertEqual({c["delegation_id"] for c in chunks}, {DELEG})
        self.assertEqual(
            len(self.frames("pet.reply.started")), 1
        )  # once, not per chunk
        self.device.disconnect()
        await self.h.finish()

    async def test_t_core_sess_one_turn_a_second_begin_after_the_reply_is_never_processed(
        self,
    ):
        self.provider.auto_close = False
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.provider.push_audio(PCM_CHUNK)
        await self.h.quiesce()
        await self.clock.advance(1.6)
        self.assertEqual(self.done_states(), [])
        await self.h.feed({"type": "pet.stop"})
        self.assertEqual(self.done_states(), ["stopped"])
        await self.h.feed(
            {"type": "pet.turn.begin"}
        )  # closing, waiting for the provider
        self.assertEqual(self.ops.count("begin"), 1)
        self.provider.push({"type": "session.closed"})
        await self.h.finish()


class SessionCloseTest(FlowCase):
    async def test_provider_own_finalization_does_not_look_like_transport_loss(self):
        for reason, code in (
            ("close_requested", 1000),
            ("expired", 1000),
            ("remote_hangup", 1000),
            ("content", 1008),
            ("connection_lost", 1011),
        ):
            with self.subTest(reason=reason):
                await self.fresh_harness()
                await self.h.boot()
                event = provider_frame({"type": "session.closed", "reason": reason})
                await self.h.push(event)
                result = await self.h.finish()
                self.assertEqual(self.device.close_codes, [code])
                self.assertEqual(self.device.types(), ["session.started"])
                self.assertFalse(result.close_confirmed)
                self.assertEqual(self.provider.types().count("session.close"), 0)
                self.assertEqual(self.ops.count("confirm"), 0)

    async def test_earlier_provider_finalization_wins_over_later_send_failure(self):
        for reason, code in (
            ("expired", 1000), ("content", 1008), ("remote_hangup", 1000),
            ("connection_lost", 1011), ("close_requested", 1000),
        ):
            with self.subTest(reason=reason):
                await self.fresh_harness()
                await self.h.boot()
                frame = provider_frame({"type": "session.closed", "reason": reason})
                event = gp.classify_provider_event(json.dumps(frame))
                self.assertEqual(event.kind, "closed")
                self.h.session._post_external(("provider", event, None))
                self.h.session._provider_send_failed()
                await self.h.quiesce()
                result = await self.h.finish()
                self.assertEqual(self.device.close_codes, [code])
                self.assertEqual(self.device.close_calls, 1)
                self.assertFalse(result.close_confirmed)
                self.assertEqual(self.ops.count("confirm"), 0)

    async def test_finalization_during_pending_resume_never_announces_start(self):
        for reason, code in (
            ("expired", 1000), ("content", 1008), ("remote_hangup", 1000),
            ("close_requested", 1000), ("connection_lost", 1011),
        ):
            with self.subTest(reason=reason):
                await self.fresh_harness()
                release = self.ops.hold("resume")
                await self.h.boot()
                await self.h.push(provider_frame({"type": "session.closed", "reason": reason}))
                result = await self.h.finish()
                self.assertEqual(self.device.close_codes, [code])
                self.assertEqual(self.device.frames, [])
                self.assertFalse(result.close_confirmed)
                release.set()
                await self.h.runner.wait_idle()
                self.assertEqual(self.ops.count("confirm"), 0)

    async def test_queued_start_then_own_finalization_uses_first_evidence(self):
        for reason, code in (
            ("expired", 1000), ("content", 1008), ("remote_hangup", 1000),
            ("close_requested", 1000), ("connection_lost", 1011),
        ):
            with self.subTest(reason=reason):
                await self.fresh_harness()
                self.provider.auto_start = False
                await self.h.boot()
                start = gp.classify_provider_event(json.dumps(provider_frame({
                    "type": "session.started",
                })))
                close = gp.classify_provider_event(json.dumps(provider_frame({
                    "type": "session.closed", "reason": reason,
                })))
                self.h.session._post_external(("provider", start, None))
                self.h.session._post_external(("provider", close, None))
                self.h.session._provider_send_failed()
                await self.h.quiesce()
                result = await self.h.finish()
                self.assertEqual(self.device.close_codes, [code])
                self.assertFalse(result.close_confirmed)
                self.assertEqual(self.ops.count("resume"), 0)
                self.assertEqual(self.ops.count("confirm"), 0)

    async def test_provider_end_race_does_not_overwrite_first_transport_failure(self):
        for order in ("send_first", "transport_first", "error_first", "invalid_first"):
            with self.subTest(order=order):
                await self.fresh_harness()
                await self.h.boot()
                close = gp.classify_provider_event(json.dumps(provider_frame({
                    "type": "session.closed", "reason": "content",
                })))
                if order == "send_first":
                    self.h.session._provider_send_failed()
                elif order == "transport_first":
                    self.h.session._post_external(("provider_closed", False))
                elif order == "error_first":
                    self.h.session._post_external(("provider", gp.ProviderEvent("error"), None))
                else:
                    self.h.session._post_external(("provider", gp.ProviderEvent("invalid"), None))
                self.h.session._post_external(("provider", close, None))
                if order != "send_first":
                    self.h.session._provider_send_failed()
                await self.h.quiesce()
                result = await self.h.finish()
                self.assertEqual(self.device.close_codes, [1002 if order == "invalid_first" else 1011])
                self.assertFalse(result.close_confirmed)
                self.assertEqual(self.device.close_calls, 1)

    async def test_all_unsolicited_ends_after_stop_stay_unconfirmed(self):
        for reason in (
            "close_requested", "expired", "content", "remote_hangup", "connection_lost",
        ):
            with self.subTest(reason=reason):
                await self.fresh_harness()
                self.provider.auto_close = False
                await self.h.boot()
                await self.h.feed({"type": "pet.stop"})
                await self.h.push(provider_frame({"type": "session.closed", "reason": reason}))
                result = await self.h.finish()
                if reason == "close_requested":
                    self.assertTrue(result.close_confirmed)
                    self.assertEqual(self.device.types()[-1], "session.closed")
                else:
                    self.assertFalse(result.close_confirmed)
                    self.assertNotIn("session.closed", self.device.types())
                    self.assertEqual(self.device.error_codes(), ["close_unconfirmed"])
                self.assertEqual(self.device.close_codes, [1000])
                self.assertEqual(self.provider.types().count("session.close"), 1)

    async def test_uncommitted_local_close_does_not_confirm_provider_finalization(self):
        for reason in ("close_requested", "expired", "content", "remote_hangup", "connection_lost"):
            with self.subTest(reason=reason):
                await self.fresh_harness()
                self.provider.auto_close = False
                await self.h.boot()
                self.provider.send_block = asyncio.Event()
                await self.h.feed({"type": "pet.stop"})
                await self.h.push(provider_frame({"type": "session.closed", "reason": reason}))
                self.assertFalse(self.h.session._close_committed)
                self.provider.send_block.set()
                result = await self.h.finish()
                self.assertFalse(result.close_confirmed)
                self.assertNotIn("session.closed", self.device.types())
                self.assertEqual(self.device.error_codes(), ["close_unconfirmed"])
                self.assertEqual(self.device.close_codes, [1000])

    async def test_close_received_before_send_commit_cannot_later_confirm(self):
        self.provider.auto_close = False
        await self.h.boot()
        self.provider.send_block = asyncio.Event()
        await self.h.feed({"type": "pet.stop"})
        self.assertFalse(self.h.session._close_committed)

        # Hold dispatch while the real provider reader admits the close. Then
        # allow the sender to commit before dispatch consumes that earlier event.
        original_post = self.h.session._post_external
        captured = []

        def capture(event):
            if event[0] == "provider" and event[1].kind == "closed":
                captured.append(event)
            else:
                original_post(event)

        self.h.session._post_external = capture
        self.provider.push({"type": "session.closed", "reason": "close_requested"})
        await self.h.quiesce()
        self.assertEqual(len(captured), 1)
        self.assertFalse(self.h.session._close_committed)
        self.provider.send_block.set()
        await self.h.quiesce()
        self.assertTrue(self.h.session._close_committed)
        self.h.session._post_external = original_post
        original_post(captured[0])
        await self.h.quiesce()
        result = await self.h.finish()
        self.assertFalse(result.close_confirmed)
        self.assertNotIn("session.closed", self.device.types())
        self.assertEqual(self.device.error_codes(), ["close_unconfirmed"])
        self.assertEqual(self.provider.types().count("session.close"), 1)

    async def test_foreign_close_requested_after_start_is_terminal_protocol_failure(self):
        await self.h.boot()
        event = provider_frame({"type": "session.closed", "reason": "close_requested"})
        event["session"]["id"] = "live_other"
        await self.h.push(event)
        outcome = await self.h.finish()
        self.assertEqual(self.device.close_codes, [1002])
        self.assertFalse(outcome.close_confirmed)
        self.assertEqual(self.device.types(), ["session.started"])
        self.assertEqual(self.ops.count("confirm"), 0)
        self.assertNotIn("live_other", repr(outcome) + " ".join(self.device.raw))

    async def test_close_reason_and_identity_negative_controls(self):
        for reason, foreign in (
            ("expired", True), ("content", True), ("remote_hangup", True),
            ("connection_lost", False), ("connection_lost", True),
        ):
            with self.subTest(reason=reason, foreign=foreign):
                await self.fresh_harness()
                await self.h.boot()
                event = provider_frame({"type": "session.closed", "reason": reason})
                if foreign:
                    event["session"]["id"] = "live_other"
                await self.h.push(event)
                outcome = await self.h.finish()
                self.assertEqual(self.device.close_codes, [1011])
                self.assertFalse(outcome.close_confirmed)
                self.assertEqual(self.ops.count("confirm"), 0)

    async def test_close_before_start_has_no_pinned_id_so_is_ambiguous(self):
        self.provider.auto_start = False
        await self.h.boot()
        await self.h.push({"type": "session.closed", "reason": "close_requested"})
        await self.h.finish()
        self.assertEqual(self.device.close_codes, [1011])
        self.assertEqual(self.ops.count("resume"), 0)

    async def test_queued_start_and_close_before_send_failure_keep_identity_in_order(self):
        for order, close_id, reason, expected in (
            ("start_first", "live_other", "close_requested", 1002),
            ("start_first", "live_fixture_1", "close_requested", 1000),
            ("start_first", "live_other", "expired", 1011),
            ("close_first", "live_other", "close_requested", 1011),
            ("failure_first", "live_other", "close_requested", 1011),
        ):
            with self.subTest(order=order, close_id=close_id, reason=reason):
                await self.fresh_harness()
                self.provider.auto_start = False
                await self.h.boot()
                start = gp.classify_provider_event(json.dumps(provider_frame({
                    "type": "session.started",
                })))
                close_doc = provider_frame({"type": "session.closed", "reason": reason})
                close_doc["session"]["id"] = close_id
                close = gp.classify_provider_event(json.dumps(close_doc))
                self.assertEqual((start.kind, close.kind), ("started", "closed"))
                if order == "start_first":
                    self.h.session._post_external(("provider", start, None))
                    self.h.session._post_external(("provider", close, None))
                elif order == "close_first":
                    self.h.session._post_external(("provider", close, None))
                    self.h.session._post_external(("provider", start, None))
                self.h.session._provider_send_failed()
                if order == "failure_first":
                    self.h.session._post_external(("provider", start, None))
                    self.h.session._post_external(("provider", close, None))
                await self.h.quiesce()
                result = await self.h.finish()
                self.assertEqual(self.device.close_codes, [expected])
                self.assertEqual(self.ops.count("resume"), 0)
                self.assertEqual(self.ops.count("confirm"), 0)
                self.assertFalse(result.close_confirmed)

    async def test_foreign_close_after_provider_start_while_resume_pending_is_terminal(self):
        release = self.ops.hold("resume")
        await self.h.boot()
        event = provider_frame({"type": "session.closed", "reason": "close_requested"})
        event["session"]["id"] = "live_other"
        await self.h.push(event)
        outcome = await self.h.finish()
        self.assertEqual(self.device.close_codes, [1002])
        self.assertEqual(self.device.frames, [])
        self.assertFalse(outcome.close_confirmed)
        release.set()
        await self.h.runner.wait_idle()
        self.assertEqual(self.ops.count("confirm"), 0)

    async def test_queued_close_then_send_failure_uses_first_terminal_evidence(self):
        cases = (
            ("close_requested", True, "close_first", 1002),
            ("expired", True, "close_first", 1011),
            ("connection_lost", False, "close_first", 1011),
            ("close_requested", False, "close_first", 1000),
            ("close_requested", True, "send_first", 1011),
            ("close_requested", True, "transport_first", 1011),
        )
        for reason, foreign, order, expected in cases:
            with self.subTest(reason=reason, foreign=foreign, order=order):
                await self.fresh_harness()
                await self.h.boot()
                frame = provider_frame({"type": "session.closed", "reason": reason})
                if foreign:
                    frame["session"]["id"] = "live_other"
                event = gp.classify_provider_event(json.dumps(frame))
                self.assertEqual(event.kind, "closed")
                if order == "transport_first":
                    self.h.session._post_external(("provider_closed", False))
                if order != "send_first":
                    self.h.session._post_external(("provider", event, None))
                self.h.session._provider_send_failed()
                if order == "send_first":
                    self.h.session._post_external(("provider", event, None))
                await self.h.quiesce()
                outcome = await self.h.finish()
                self.assertEqual(self.device.close_codes, [expected])
                self.assertEqual(self.device.close_calls, 1)
                self.assertFalse(outcome.close_confirmed)
                self.assertEqual(self.ops.count("confirm"), 0)

    async def test_foreign_close_during_stop_does_not_replace_stop(self):
        self.provider.auto_close = False
        await self.h.boot()
        await self.h.feed({"type": "pet.stop"})
        event = provider_frame({"type": "session.closed", "reason": "close_requested"})
        event["session"]["id"] = "live_other"
        await self.h.push(event)
        outcome = await self.h.finish()
        self.assertEqual(self.device.close_codes, [1000])
        self.assertEqual(self.device.error_codes(), ["close_unconfirmed"])
        self.assertFalse(outcome.close_confirmed)
        self.assertEqual(self.ops.count("confirm"), 0)

    async def test_foreign_or_unrequested_finalization_cannot_confirm_our_close(self):
        for reason, foreign in (("expired", False), ("connection_lost", False),
                                ("close_requested", True)):
            with self.subTest(reason=reason, foreign=foreign):
                await self.fresh_harness()
                self.provider.auto_close = False
                await self.h.boot()
                await self.h.feed({"type": "pet.stop"})
                event = provider_frame({"type": "session.closed", "reason": reason})
                if foreign:
                    event["session"]["id"] = "live_other"
                self.provider.push(event)
                await self.h.quiesce()
                outcome = await self.h.finish()
                self.assertFalse(outcome.close_confirmed)
                self.assertNotIn("session.closed", self.device.types())
                self.assertEqual(self.device.error_codes(), ["close_unconfirmed"])

    async def test_provider_finalization_precedes_immediately_queued_eof(self):
        self.provider.auto_close = False
        await self.h.boot()
        await self.h.feed({"type": "pet.stop"})
        # No quiesce between these: one receive burst must preserve wire order.
        self.provider.push({"type": "session.closed"})
        self.provider.push_closed(clean=True)
        await self.h.quiesce()
        outcome = await self.h.finish()
        self.assertTrue(outcome.close_confirmed)
        self.assertEqual(self.device.types()[-1], "session.closed")

    async def test_t_core_sess_close15_unconfirmed_after_exactly_15s_no_session_closed(
        self,
    ):
        self.provider.auto_close = False
        await self.h.boot()
        await self.h.feed({"type": "session.close"})
        t_close = self.clock.now()
        self.assertEqual(self.provider.types().count("session.close"), 1)
        await self.clock.advance(14.9)
        self.assertEqual(self.device.close_codes, [])
        await self.clock.advance(0.1)
        self.assertEqual(self.device.error_codes(), ["close_unconfirmed"])
        self.assertNotIn("session.closed", self.device.types())
        self.assertEqual(self.device.close_codes, [1000])
        self.assertAlmostEqual(self.device.times[-1] - t_close, 15.0, places=6)
        self.assertEqual(self.provider.types().count("session.close"), 1)
        outcome = await self.h.finish()
        self.assertFalse(outcome.close_confirmed)

    async def test_t_core_sess_close_confirmed_by_the_provider_session_closed_event(
        self,
    ):
        await self.h.boot()
        await self.h.feed({"type": "session.close"})
        self.assertEqual(self.device.types(), ["session.started", "session.closed"])
        self.assertEqual(self.device.close_codes, [1000])
        outcome = await self.h.finish()
        self.assertTrue(outcome.close_confirmed)
        self.assertGreaterEqual(self.provider.close_calls, 1)

    async def test_clean_socket_close_without_live_finalization_is_unconfirmed(self):
        self.provider.auto_close = False
        await self.h.boot()
        await self.h.feed({"type": "pet.stop"})
        self.provider.push_closed(clean=True)
        await self.h.quiesce()
        self.assertEqual(self.device.error_codes(), ["close_unconfirmed"])
        self.assertNotIn("session.closed", self.device.types())
        self.assertEqual(self.device.close_codes, [1000])
        outcome = await self.h.finish()
        self.assertFalse(outcome.close_confirmed)

    async def test_an_unclean_provider_close_does_not_qualify(self):
        self.provider.auto_close = False
        await self.h.boot()
        await self.h.feed({"type": "pet.stop"})
        self.provider.push_closed(clean=False)
        await self.h.quiesce()
        self.assertEqual(self.device.error_codes(), ["close_unconfirmed"])
        self.assertNotIn("session.closed", self.device.types())
        await self.h.finish()

    async def test_a_send_confirm_while_closing_is_invalid_preview_and_never_runs(self):
        self.provider.auto_close = False
        await self.to_preview()
        await self.h.push(delta("e2", "late words", 600, 700))  # late input: closing
        self.assertEqual(
            self.frames("pet.turn.preview_invalidated")[0]["reason"], "late_transcript"
        )
        await self.h.feed(
            self.confirm_frame()
        )  # the OLD revision, before the close ends
        self.assertEqual(self.device.error_codes(), ["invalid_preview"])
        self.assertEqual(self.ops.count("confirm"), 0)
        self.assertEqual(self.ops.count("preflight"), 0)
        await self.clock.advance(15.0)
        self.assertEqual(
            self.device.error_codes(), ["invalid_preview", "close_unconfirmed"]
        )
        await self.h.finish()

    async def test_provider_error_event_after_start_closes_1011_without_pet_error(self):
        await self.h.boot()
        await self.h.push(
            {"type": "error", "error": {
                "type": "unknown", "code": "x", "message": "TEXTCANARY"}}
        )
        self.assertEqual(self.device.close_codes, [1011])
        self.assertEqual(
            self.device.types(), ["session.started"]
        )  # no pet.error, no closed
        self.assertEqual(self.provider.types().count("session.close"), 1)
        outcome = await self.h.finish()
        self.assertIsNone(outcome.error_code)

    async def test_provider_session_closed_is_normal_but_transport_drop_is_1011(self):
        await self.h.boot()
        await self.h.push({"type": "session.closed"})
        self.assertEqual(self.device.close_codes, [1000])
        self.assertEqual(self.device.types(), ["session.started"])
        await self.h.finish()
        await self.fresh_harness()
        await self.h.boot()
        self.provider.push_closed(clean=False)
        await self.h.quiesce()
        self.assertEqual(self.device.close_codes, [1011])
        await self.h.finish()

    async def test_provider_garbage_is_fatal_1002(self):
        await self.h.boot()
        await self.h.push("{not json")
        self.assertEqual(self.device.close_codes, [1002])
        self.assertEqual(self.device.error_codes(), [])
        await self.h.finish()

    async def test_request_shutdown_behaves_like_stop(self):
        self.ops.results["poll"] = pending_poll
        await self.to_accepted()
        await self.h.session.request_shutdown()
        await self.h.quiesce()
        self.assertEqual(self.done_states(), ["stopped"])
        outcome = await self.h.finish()
        self.assertEqual(outcome.final_state, "CLOSED")
        self.assertEqual(self.ops.count("stop"), 1)

    async def test_device_disconnect_in_await_reply_sends_no_more_frames_but_closes_provider(
        self,
    ):
        self.ops.results["poll"] = pending_poll
        await self.to_accepted()
        seen = len(self.device.frames)
        self.device.disconnect()
        await self.h.quiesce()
        self.assertEqual(len(self.device.frames), seen)
        self.assertEqual(self.provider.types().count("session.close"), 1)
        await self.h.finish()

    async def test_t_core_sess_no_orphans_no_stray_task_remains_after_run(self):
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.provider.push_audio(PCM_CHUNK)
        await self.h.quiesce()
        await self.clock.advance(1.6)
        await self.h.feed({"type": "pet.stop"})
        await self.h.finish()
        self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})

    async def test_no_orphans_after_a_stop_with_a_blocked_provider_and_device(self):
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.device.send_block = asyncio.Event()
        self.provider.send_block = asyncio.Event()
        await self.h.feed({"type": "pet.stop"})
        await self.clock.advance(
            15.0
        )  # the provider never confirms: the 15 s close deadline
        for _ in range(3):
            await self.clock.advance(2.0)  # then the bounded flush / close waits expire
        await self.h.finish()  # returned although both links were blocked throughout
        self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})
        self.assertEqual(self.provider.of_type("session.close"), [])  # never got out


class SessionCorrectionTest(SpeakCase):
    """Manager corrections 1 and 2 (and the unsent-suppression rule)."""

    async def test_c1_one_valid_id_pins_the_turn_and_identical_repeats_are_allowed(
        self,
    ):
        await self.flow(capture=(DELEG, DELEG), preview=(DELEG,))
        await self.clock.advance(1.0)
        (chunk,) = self.commentary()
        self.assertEqual(chunk["delegation_id"], DELEG)
        self.assertEqual(self.device.error_codes(), [])
        self.device.disconnect()
        await self.h.finish()

    async def test_c1_a_second_different_id_makes_it_ambiguous_for_the_websocket(self):
        cases = {
            "capturing": {"capture": ("dlg_1", "dlg_2")},
            "preview": {"capture": ("dlg_1",), "preview": ("dlg_2",)},
            "preview-then-repeat-first": {
                "capture": ("dlg_1",),
                "preview": ("dlg_2", "dlg_1"),  # the repeat must not un-ambiguate
            },
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                await self.fresh_harness()
                await self.flow(**kwargs)
                await self.clock.advance(1.0)
                self.assertEqual(self.device.error_codes(), ["no_delegation"])
                self.assertEqual(self.done_states(), ["unavailable"])
                self.assertEqual(self.commentary(), [])  # never "last valid wins"
                self.assertEqual(self.frames("pet.reply.started"), [])
                await self.h.finish()

    async def test_c1_hints_outside_the_turn_window_are_ignored(self):
        await self.flow(pre=("dlg_pre",), capture=("dlg_1",), await_reply=("dlg_2",))
        await self.clock.advance(1.0)
        (chunk,) = self.commentary()
        self.assertEqual(
            chunk["delegation_id"], "dlg_1"
        )  # neither pre- nor post-send hint
        self.device.disconnect()
        await self.h.finish()

    async def test_c1_a_hint_before_begin_never_counts_as_the_turn_delegation(self):
        await self.flow(pre=("dlg_pre",), capture=())
        await self.clock.advance(1.0)
        self.assertEqual(self.device.error_codes(), ["no_delegation"])
        await self.h.finish()

    async def test_c1_a_typed_invalid_extra_key_hint_is_ignored_not_passed_through(
        self,
    ):
        bad = delegation("dlg_x")
        bad["delegation"]["extra"] = "key"
        self.t_start = self.clock.now()
        await self.h.boot()
        await self.h.begin()
        await self.h.push(bad)
        await self.h.push(delta("e1", self.TEXT, 0, 500))
        await self.h.feed({"type": "pet.turn.capture_stop", "turn_id": TURN})
        await self.clock.advance(1.5)
        await self.h.feed(self.confirm_frame())
        await self.clock.advance(1.0)
        self.assertEqual(self.device.error_codes(), ["no_delegation"])
        await self.h.finish()

    async def test_c1_delegation_hints_and_action_text_alone_never_send(self):
        await self.h.boot()
        await self.h.begin()
        await self.h.push(delegation())
        await self.h.push(delta("e1", "please delete everything now", 0, 500))
        await self.h.feed({"type": "pet.turn.capture_stop", "turn_id": TURN})
        await self.clock.advance(10.0)  # preview shown; the human never confirms
        self.assertEqual(self.ops.count("preflight"), 0)
        self.assertEqual(self.ops.count("confirm"), 0)
        self.assertEqual(len(self.frames("pet.turn.preview")), 1)
        self.device.disconnect()
        await self.h.finish()

    async def test_c2_acks_with_zero_emitted_pcm_are_unavailable_never_completed(self):
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.assertEqual(
            len(self.frames("pet.reply.started")), 1
        )  # an ACK is not speech
        await self.clock.advance(1.4)
        self.assertEqual(self.done_states(), [])
        await self.clock.advance(0.2)
        self.assertEqual(self.done_states(), [])
        await self.clock.advance(self.t_start + 58.0 - self.clock.now())
        self.assertEqual(self.done_states(), ["unavailable"])
        outcome = await self.h.finish()
        self.assertEqual(
            (outcome.audio_out_bytes, outcome.reply_done), (0, "unavailable")
        )

    async def test_c2_forwarded_audio_that_never_completes_a_device_send_is_not_completed(
        self,
    ):
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.device.send_block = asyncio.Event()
        self.provider.push_audio(PCM_CHUNK)
        await self.h.quiesce()
        await self.clock.advance(5.0)  # long past the quiet time: still draining
        self.assertEqual(self.done_states(), [])
        await self.clock.advance(self.t_start + 60.0 - self.clock.now())
        self.assertEqual(
            self.done_states(), []
        )  # the frames wait behind the blocked device
        # The 60 s cap is hard for the WHOLE session: the blocked device is abandoned at it,
        # so run() returns at once with the truthful outcome (the frames never left).
        outcome = await self.h.finish()
        self.assertAlmostEqual(self.clock.now() - self.t_start, 60.0, places=6)
        self.assertEqual(self.device.error_codes(), [])  # never delivered
        self.assertEqual(self.device.close_calls, 1)
        self.assertEqual(outcome.reply_done, "unavailable")
        self.assertEqual(outcome.error_code, "deadline")
        self.assertEqual(outcome.audio_out_bytes, 0)  # no completed device send

    async def test_c2_stopped_counts_the_frames_actually_sent_without_claiming_completed(self):
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.provider.push_audio(PCM_CHUNK * 2)
        await self.h.quiesce()
        await self.clock.advance(0.1)
        await self.clock.advance(1.6)
        self.assertEqual(self.done_states(), [])
        await self.h.feed({"type": "pet.stop"})
        outcome = await self.h.finish()
        self.assertEqual(
            (outcome.reply_done, outcome.audio_out_bytes), ("stopped", 9600)
        )

    async def test_c2_stop_while_awaiting_acks_suppresses_every_unsent_chunk(self):
        self.ops.results["poll"] = lambda turn: ReplyResult(
            turn, "speak", "reply_complete", True, 3, LONG_REPLY
        )
        await self.to_accepted()
        self.provider.send_block = asyncio.Event()
        await self.clock.advance(1.0)  # chunk 1 is mid-send; the rest are queued
        await self.h.feed({"type": "pet.stop"})
        self.provider.send_block.set()
        await self.h.quiesce()
        self.assertLessEqual(len(self.commentary()), 1)  # unsent chunks never go out
        self.assertEqual(self.provider.types().count("session.close"), 1)
        self.assertEqual(self.done_states(), ["stopped"])
        await self.h.finish()


class RepairCloseTest(FlowCase):
    """U2 repair M5/C1/C2/A1-A3: ONE hard close deadline, truthful close, bounded cleanup."""

    def block_everything(self) -> None:
        self.device.send_block = asyncio.Event()
        self.provider.send_block = asyncio.Event()
        self.device.close_block = asyncio.Event()
        self.provider.close_block = asyncio.Event()

    async def started_at(self) -> float:
        await self.h.boot()
        return self.clock.now()

    async def test_u2r_m5_cap_returns_at_exactly_accepted_plus_60_with_blocked_device_and_provider(
        self,
    ):
        t0 = await self.started_at()
        self.block_everything()
        await self.clock.advance(59.9)
        assert self.h.task is not None
        self.assertFalse(self.h.task.done())
        await self.clock.advance(0.1)
        outcome = await self.h.finish()  # NO further advance
        self.assertAlmostEqual(self.clock.now() - t0, 60.0, places=6)
        self.assertEqual(
            (outcome.error_code, outcome.close_confirmed), ("deadline", False)
        )
        self.assertEqual((self.provider.close_calls, self.device.close_calls), (1, 1))
        self.assertLessEqual(self.provider.types().count("session.close"), 1)
        self.assertNotIn("session.closed", self.device.types())
        self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})

    async def test_u2r_m5_cap_with_unblocked_peers_still_delivers_final_frames_in_zero_time(
        self,
    ):
        self.provider.auto_close = False
        t0 = await self.started_at()
        await self.clock.advance(60.0)
        outcome = await self.h.finish()
        self.assertAlmostEqual(self.clock.now() - t0, 60.0, places=6)
        self.assertEqual(self.device.error_codes(), ["deadline", "close_unconfirmed"])
        self.assertNotIn("session.closed", self.device.types())
        self.assertEqual(self.device.close_codes, [1000])  # unchanged: provider started
        self.assertFalse(outcome.close_confirmed)

    async def test_u2r_m5_close_begun_at_t50_is_capped_at_t60_not_t65(self):
        self.provider.auto_close = False
        t0 = await self.started_at()
        await self.clock.advance(50.0)
        await self.h.feed({"type": "session.close"})
        await self.clock.advance(9.9)
        assert self.h.task is not None
        self.assertFalse(self.h.task.done())
        await self.clock.advance(0.1)
        await self.h.finish()
        self.assertAlmostEqual(self.clock.now() - t0, 60.0, places=6)
        self.assertEqual(self.device.error_codes(), ["close_unconfirmed"])

    async def test_u2r_m5_close_without_cap_pressure_is_exactly_15s_including_blocked_flush(
        self,
    ):
        self.provider.auto_close = False
        await self.started_at()
        await self.clock.advance(10.0)
        self.block_everything()
        await self.h.feed({"type": "session.close"})
        t_close = self.clock.now()
        await self.clock.advance(14.9)
        assert self.h.task is not None
        self.assertFalse(self.h.task.done())
        await self.clock.advance(0.1)
        await self.h.finish()
        self.assertAlmostEqual(self.clock.now() - t_close, 15.0, places=6)
        self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})

    async def test_u2r_m5_provider_confirm_early_returns_without_waiting_the_rest(self):
        t0 = await self.started_at()
        await self.clock.advance(5.0)
        await self.h.feed({"type": "session.close"})
        await self.h.finish()  # no advance
        types = self.device.types()
        self.assertAlmostEqual(
            self.device.times[types.index("session.closed")] - t0, 5.0, places=6
        )
        self.assertEqual(self.provider.close_calls, 1)
        self.assertEqual(self.provider.types().count("session.close"), 1)

    async def test_u2r_m5_in_flight_sync_core_op_is_not_cancelled_by_the_deadline(self):
        release = self.ops.hold("confirm")
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        await self.clock.advance(self.t_start + 60.0 - self.clock.now())
        assert self.h.task is not None
        outcome = await self.h.task  # returns at the deadline; the runner still owns it
        self.assertAlmostEqual(self.clock.now() - self.t_start, 60.0, places=6)
        self.assertEqual(outcome.send_outcome, "inflight")
        self.assertIsNotNone(outcome.confirm_job)
        self.assertEqual(self.h.runner.inflight_op, "confirm")
        self.assertFalse(self.h.runner.idle)
        self.assertEqual(self.ops.names().count("confirm"), 1)
        release.set()
        await self.h.runner.wait_idle()
        self.assertEqual(
            self.h.runner.take_result(outcome.confirm_job).outcome, "accepted"
        )
        names = self.ops.names()
        self.assertLess(names.index("confirm"), names.index("stop"))
        self.assertEqual(names.count("confirm"), 1)

    async def test_u2r_m5_start_25s_includes_the_dial_and_prestart_10s_are_unchanged(
        self,
    ):
        self.h.factory.hang = True
        self.h.go()
        await self.h.quiesce()  # the pre-start timer is armed
        t0 = self.clock.now()
        await self.clock.advance(9.9)
        assert self.h.task is not None
        self.assertFalse(self.h.task.done())
        await self.clock.advance(0.1)  # no session.start for 10 s: closed, no frame
        await self.h.finish()
        self.assertEqual((self.device.frames, self.device.close_codes), ([], [1008]))
        self.assertAlmostEqual(self.clock.now() - t0, 10.0, places=6)
        await self.fresh_harness()
        self.h.factory.hang = True
        await self.h.boot()
        t1 = self.clock.now()
        await self.clock.advance(24.9)
        self.assertEqual(self.device.frames, [])
        await self.clock.advance(0.1)
        self.assertEqual(self.device.error_codes(), ["deadline"])
        self.assertAlmostEqual(self.device.times[0] - t1, 25.0, places=6)
        await self.h.finish()

    async def test_u2r_m5_ack_wait_10s_starts_when_the_last_commentary_was_sent_and_never_loosens_the_cap(
        self,
    ):
        self.provider.auto_ack = False
        await self.to_accepted()
        await self.clock.advance(1.0)
        await self.clock.advance(9.9)
        self.assertEqual(self.done_states(), [])
        await self.clock.advance(0.1)
        self.assertEqual(self.done_states(), ["unavailable"])
        await self.h.finish()
        # a reply that starts at t0+55: the 60 s cap wins; the ACK timer never extends it
        await self.fresh_harness()
        self.provider.auto_ack = False
        late = {"at": 0.0}
        self.ops.results["poll"] = lambda turn: (
            pending_poll(turn)
            if self.clock.now() < late["at"]
            else ReplyResult(turn, "speak", "reply_complete", True, 3, REPLY_TEXT)
        )
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        late["at"] = self.t_start + 54.5
        await self.clock.advance(self.t_start + 55.0 - self.clock.now())
        self.assertEqual(len(self.commentary()), 1)
        await self.clock.advance(2.9)
        self.assertEqual(self.device.error_codes(), [])
        await self.clock.advance(0.1)
        self.assertEqual(self.device.error_codes(), ["deadline"])
        self.assertTrue(self.h.session.outcome.close_confirmed)
        self.assertAlmostEqual(self.clock.now() - self.t_start, 58.0, places=6)
        await self.h.finish()

    async def test_u2r_m5_session_close_frame_and_session_closed_at_most_once_across_all_close_entries(
        self,
    ):
        entries = ("stop_twice", "disconnect", "provider_closed", "cap")
        for entry in entries:
            with self.subTest(entry=entry):
                await self.fresh_harness()
                self.provider.auto_close = entry == "stop_twice"
                await self.started_at()
                if entry == "stop_twice":
                    self.device.feed({"type": "pet.stop"})
                    self.device.feed({"type": "session.close"})
                    await self.h.quiesce()
                elif entry == "disconnect":
                    self.device.disconnect()
                    await self.h.quiesce()
                    await self.clock.advance(15.0)
                elif entry == "provider_closed":
                    self.provider.push_closed(clean=True)
                    await self.h.quiesce()
                else:
                    await self.clock.advance(60.0)
                await self.h.finish()
                self.assertLessEqual(self.provider.types().count("session.close"), 1)
                self.assertLessEqual(self.device.types().count("session.closed"), 1)

    async def test_u2r_close_not_confirmed_when_close_frame_never_left(self):
        await self.h.boot()
        self.provider.send_block = asyncio.Event()  # the session.close frame is stuck
        await self.h.feed({"type": "pet.stop"})
        self.provider.push({"type": "session.closed"})
        self.provider.push_closed(clean=True)
        await self.h.quiesce()
        await self.clock.advance(15.0)
        outcome = await self.h.finish()
        self.assertFalse(outcome.close_confirmed)
        self.assertEqual(self.device.error_codes(), ["close_unconfirmed"])
        self.assertNotIn("session.closed", self.device.types())
        self.assertLessEqual(self.provider.types().count("session.close"), 1)

    async def test_u2r_cancelled_run_closes_provider_queues_stop_and_closes_device(
        self,
    ):
        await self.h.boot()  # READY: the core resume is owned
        assert self.h.task is not None
        self.h.task.cancel()
        with self.assertRaises(asyncio.CancelledError):  # never swallowed
            await self.h.task
        await self.h.runner.wait_idle()
        self.assertEqual(self.provider.close_calls, 1)
        self.assertLessEqual(self.provider.types().count("session.close"), 1)
        self.assertEqual(self.ops.count("stop"), 1)
        self.assertEqual(self.device.close_calls, 1)
        self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})
        self.assertEqual(self.h.session.outcome.final_state, "CLOSED")

    async def test_u2r_cancel_while_starting_with_a_hung_connector_closes_the_device(
        self,
    ):
        self.h.factory.hang = True
        await self.h.boot()
        assert self.h.task is not None
        self.h.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.h.task
        self.assertEqual((self.device.close_calls, self.provider.close_calls), (1, 0))
        self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})

    async def test_u2r_cancel_with_inflight_confirm_closes_links_and_keeps_the_result(
        self,
    ):
        release = self.ops.hold("confirm")
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        assert self.h.task is not None
        self.h.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.h.task
        outcome = self.h.session.outcome
        self.assertEqual(outcome.send_outcome, "inflight")
        self.assertEqual((self.device.close_calls, self.provider.close_calls), (1, 1))
        release.set()
        await self.h.runner.wait_idle()
        self.assertEqual(
            self.h.runner.take_result(outcome.confirm_job).outcome, "accepted"
        )
        names = self.ops.names()
        self.assertLess(names.index("confirm"), names.index("stop"))

    async def test_u2r_cancel_cleanup_is_bounded_by_the_hard_deadline_and_a_second_cancel_aborts(
        self,
    ):
        await self.h.boot()
        self.block_everything()
        assert self.h.task is not None
        self.h.task.cancel()
        await self.h.quiesce()
        self.assertFalse(
            self.h.task.done()
        )  # bounded cleanup is waiting on blocked peers
        await self.clock.advance(15.0)
        with self.assertRaises(asyncio.CancelledError):
            await self.h.task
        self.assertEqual((self.device.close_calls, self.provider.close_calls), (1, 1))
        self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})
        await self.fresh_harness()
        await self.h.boot()
        self.block_everything()
        assert self.h.task is not None
        self.h.task.cancel()
        await self.h.quiesce()
        self.h.task.cancel()  # a second cancel aborts the cleanup at once
        await asyncio.gather(self.h.task, return_exceptions=True)
        self.assertTrue(self.h.task.done())
        self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})

    async def test_run_twice_is_refused(self):
        await self.h.boot()
        assert self.h.session is not None
        with self.assertRaises(gc.SessionRunError) as ctx:  # while it is running
            await self.h.session.run()
        self.assertEqual(ctx.exception.code, "session_already_run")
        self.device.feed({"type": "pet.stop"})
        await self.h.finish()
        calls = (self.h.factory.calls, self.ops.count("resume"))
        with self.assertRaises(gc.SessionRunError):  # and after it ended
            await self.h.session.run()
        self.assertEqual((self.h.factory.calls, self.ops.count("resume")), calls)

    async def test_u2r_a1_connector_swallowing_cancel_is_closed_and_never_gets_a_session_start(
        self,
    ):
        gate = asyncio.Event()
        factory = SwallowCancelFactory(self.provider, gate)
        settings = gc.SessionSettings(self.h.model, "marin", LABEL, SHORT8)
        session = gc.PetSession(
            self.device,
            factory,
            self.h.runner,
            self.ops,
            settings,
            self.clock,
            new_event_id=self.h.new_id,
        )
        task = asyncio.create_task(session.run())
        self.device.feed(ASSISTANT_START)
        await self.h.quiesce()
        self.device.feed({"type": "pet.stop"})
        await self.h.quiesce()
        gate.set()
        await asyncio.wait_for(task, 5)
        self.assertEqual(factory.swallowed, 1)
        self.assertEqual(self.provider.close_calls, 1)
        self.assertEqual(self.provider.sent, [])  # no session.start
        self.assertEqual(self.device.error_codes(), [])
        self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})

    async def test_u2r_a2_inbox_overflow_is_posted_once(self):
        session = self.h.session
        assert session is not None
        for _ in range(300):
            session._post_external(("x",))
        self.assertEqual(len(session._inbox), gs.INBOX_CAP)
        self.assertEqual(
            [e for e in session._internal if e == ("overflow",)], [("overflow",)]
        )

    async def test_u2r_a2_readers_stop_at_finalize(self):
        await self.h.boot()
        self.device.send_block = asyncio.Event()  # the tail waits for the device
        await self.h.feed({"type": "session.close"})  # the provider confirms at once
        for _ in range(50):
            self.device.feed(audio_frame(10))
        await self.h.quiesce()
        session = self.h.session
        assert session is not None
        self.assertEqual(self.device.inbound.qsize(), 50)  # nobody reads any more
        self.assertEqual((len(session._inbox), len(session._internal)), (0, 0))
        await self.clock.advance(15.0)
        await self.h.finish()

    async def test_u2r_a3_reply_for_another_command_id_fails_closed(self):
        self.ops.results["poll"] = lambda _turn: ReplyResult(
            UUID_OTHER, "speak", "reply_complete", True, 3, REPLY_TEXT
        )
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.assertEqual(self.device.error_codes(), ["reply_unavailable"])
        self.assertEqual(self.done_states(), ["unavailable"])
        self.assertEqual(self.commentary(), [])
        blob = " ".join(self.device.raw) + json.dumps(self.provider.sent)
        self.assertNotIn("REPLYCANARY", blob)
        await self.h.finish()


class AudioOrderTest(SpeakCase):
    """U2 repair M2/M3: started precedes audio; Stop wins races on the device lock."""

    def first_audio_index(self) -> int:
        return self.device.types().index("session.output_audio.delta")

    async def preview_then_block_device_before_confirm(self) -> None:
        await self.to_preview()
        self.device.send_block = asyncio.Event()  # send_result is blocked in the device
        await self.h.feed(self.confirm_frame())
        self.t_acc = self.clock.now()

    async def test_u2r_m2_started_precedes_audio_with_a_backpressured_device(self):
        for yielding in (False, True):
            with self.subTest(yield_every_send=yielding):
                await self.fresh_harness()
                self.device.yield_every_send = yielding
                await self.preview_then_block_device_before_confirm()
                await self.clock.advance(1.0)  # acked; the epoch checks pass
                self.provider.push_audio(PCM_CHUNK)
                await self.h.quiesce()
                self.assertEqual(self.audio_out(), [])
                self.device.send_block.set()
                await self.h.quiesce()
                types = self.device.types()
                self.assertLess(
                    types.index("pet.turn.send_result"),
                    types.index("pet.reply.started"),
                )
                self.assertLess(
                    types.index("pet.reply.started"), self.first_audio_index()
                )
                self.device.disconnect()
                await self.h.finish()

    async def test_u2r_m2_error_audio_started_probe_is_ordered_started_first(self):
        for yielding in (False, True):
            with self.subTest(yield_every_send=yielding):
                await self.fresh_harness()
                self.device.yield_every_send = yielding
                await self.to_accepted()
                self.device.send_block = asyncio.Event()
                await self.h.feed(
                    self.confirm_frame()
                )  # a duplicate: blocked pet.error
                await self.clock.advance(1.0)
                self.provider.push_audio(PCM_CHUNK)
                await self.h.quiesce()
                self.device.send_block.set()
                await self.h.quiesce()
                types = self.device.types()
                self.assertLess(
                    types.index("pet.reply.started"), self.first_audio_index()
                )
                self.assertEqual(self.device.error_codes(), ["invalid_preview"])
                self.device.disconnect()
                await self.h.finish()

    async def test_u2r_m2_first_ack_then_immediate_audio_loses_no_samples_and_last_chunk_ack_same_tick(
        self,
    ):
        for yielding in (False, True):
            with self.subTest(yield_every_send=yielding):
                await self.fresh_harness()
                self.device.yield_every_send = yielding
                self.provider.auto_ack = False
                self.ops.results["poll"] = lambda turn: ReplyResult(
                    turn, "speak", "reply_complete", True, 3, LONG_REPLY
                )
                await self.to_accepted()
                await self.clock.advance(1.0)
                chunks = self.commentary()
                self.assertGreaterEqual(len(chunks), 3)
                self.provider.push(ack_of(chunks[0]))
                self.provider.push_audio(PCM_CHUNK)  # BEFORE the epoch round trip ends
                for chunk in chunks[1:]:
                    self.provider.push(ack_of(chunk))  # incl. the last chunk, same step
                await self.h.quiesce()
                await self.clock.advance(0.1)
                await self.clock.advance(2.0)
                delivered = b"".join(
                    base64.b64decode(f["delta"]) for f in self.audio_out()
                )
                self.assertEqual(delivered, PCM_CHUNK)  # no sample lost
                types = self.device.types()
                self.assertLess(
                    types.index("pet.reply.started"), self.first_audio_index()
                )
                self.assertEqual(self.done_states(), [])
                await self.h.feed({"type": "pet.stop"})
                types = self.device.types()
                self.assertLess(
                    types.index("session.output_audio.delta"),
                    types.index("pet.reply.done"),
                )
                outcome = await self.h.finish()
                self.assertEqual(outcome.reply_done, "stopped")
                self.assertEqual(outcome.audio_out_bytes, len(PCM_CHUNK))

    async def test_u2r_m2_short_reply_first_ack_burst_started_then_exact_audio_then_stopped(
        self,
    ):
        self.provider.auto_ack = False
        await self.to_accepted()
        await self.clock.advance(1.0)
        (chunk,) = self.commentary()
        pcm = (
            b"\x05\x06" * 1200
        )  # 2400 bytes, same burst as the one (= first = last) ACK
        self.provider.push(ack_of(chunk))
        self.provider.push_audio(pcm)
        await self.h.quiesce()
        await self.clock.advance(2.0)
        (frame,) = self.audio_out()
        self.assertEqual(base64.b64decode(frame["delta"]), pcm)
        types = self.device.types()
        self.assertLess(types.index("pet.reply.started"), self.first_audio_index())
        self.assertEqual(self.done_states(), [])
        await self.h.feed({"type": "pet.stop"})
        types = self.device.types()
        self.assertLess(self.first_audio_index(), types.index("pet.reply.done"))
        self.assertEqual(self.done_states(), ["stopped"])
        await self.h.finish()

    async def test_post_commentary_pre_ack_audio_is_held_not_played_until_ack(self):
        self.provider.auto_ack = False
        await self.to_accepted()
        await self.clock.advance(1.0)
        (chunk,) = self.commentary()
        self.provider.push_audio(PCM_CHUNK)  # before any correlated ACK
        await self.h.quiesce()
        self.assertEqual(self.audio_out(), [])
        self.assertEqual(self.h.session._audio.queued_bytes, len(PCM_CHUNK))
        await self.h.push(ack_of(chunk))
        await self.clock.advance(0.5)
        self.assertEqual(
            b"".join(base64.b64decode(frame["delta"]) for frame in self.audio_out()),
            PCM_CHUNK,
        )
        self.device.disconnect()
        await self.h.finish()

    async def test_u2r_m2_audio_held_for_the_epoch_counts_against_the_one_mib_cap(self):
        self.provider.auto_ack = False
        await self.to_accepted()
        await self.clock.advance(1.0)
        (chunk,) = self.commentary()
        release = self.ops.hold("epoch_current")  # the gate-opening check is pending
        await self.h.push(ack_of(chunk))
        self.provider.push_audio(b"\x00\x00" * 350000)
        self.provider.push_audio(b"\x00\x00" * 350000)  # 1.4 MB held > 1 MiB cap
        await self.h.quiesce()
        self.assertEqual(self.device.error_codes(), ["overflow"])
        release.set()
        await self.h.quiesce()
        self.assertEqual(self.audio_out(), [])
        self.assertEqual(self.frames("pet.reply.started"), [])
        await self.h.finish()

    async def test_u2r_m2_started_send_failure_means_zero_audio(self):
        self.provider.auto_ack = False
        await self.to_accepted()
        self.device.fail_sends = True  # after the result frames, before the first ACK
        await self.clock.advance(1.0)
        (chunk,) = self.commentary()
        self.provider.push(ack_of(chunk))
        self.provider.push_audio(PCM_CHUNK)  # buffered behind the started frame
        await self.h.quiesce()
        outcome = await self.h.finish()
        self.assertEqual(self.audio_out(), [])
        self.assertEqual(outcome.audio_out_bytes, 0)
        self.assertGreater(outcome.dropped_audio_bytes, 0)

    async def test_u2r_m2_stale_epoch_at_commit_discards_buffered_audio_and_never_emits_started(
        self,
    ):
        self.provider.auto_ack = False
        await self.to_accepted()
        await self.clock.advance(1.0)
        (chunk,) = self.commentary()
        release = self.ops.hold("epoch_current")
        self.ops.results["epoch_current"] = lambda _epoch: False
        await self.h.push(ack_of(chunk))
        self.provider.push_audio(PCM_CHUNK)  # buffered while the commit check runs
        await self.h.quiesce()
        release.set()
        await self.h.quiesce()
        self.assertEqual(self.frames("pet.reply.started"), [])
        self.assertEqual(self.audio_out(), [])
        self.assertEqual(self.done_states(), ["unavailable"])
        await self.h.finish()

    async def test_u2r_m2_late_ack_and_audio_after_the_ack_timeout_never_reopen_the_gate(
        self,
    ):
        self.provider.auto_ack = False
        self.provider.auto_close = False
        await self.to_accepted()
        await self.clock.advance(1.0)
        (chunk,) = self.commentary()
        await self.clock.advance(10.0)  # the ACK never came
        self.assertEqual(self.done_states(), ["unavailable"])
        await self.h.push(ack_of(chunk))  # late
        self.provider.push_audio(PCM_CHUNK)
        await self.h.quiesce()
        self.assertEqual(self.frames("pet.reply.started"), [])
        self.assertEqual(self.audio_out(), [])
        await self.clock.advance(15.0)
        await self.h.finish()

    async def test_u2r_m3_stop_while_another_frame_holds_the_device_lock_sends_no_audio_after_stop(
        self,
    ):
        for yielding in (False, True):
            with self.subTest(yield_every_send=yielding):
                await self.fresh_harness()
                self.device.yield_every_send = yielding
                await self.to_accepted()
                await self.clock.advance(1.0)  # speaking, the gate is released
                self.device.send_block = asyncio.Event()
                await self.h.feed(self.confirm_frame())  # the pet.error holds the lock
                self.provider.push_audio(PCM_CHUNK)  # popped; waits for the lock
                await self.h.quiesce()
                await self.h.feed({"type": "pet.stop"})
                self.device.send_block.set()
                await self.h.quiesce()
                self.assertEqual(self.audio_out(), [])
                outcome = await self.h.finish()
                self.assertEqual(outcome.audio_out_bytes, 0)
                self.assertGreaterEqual(outcome.dropped_audio_bytes, len(PCM_CHUNK))

    async def test_u2r_m3_in_send_chunk_completes_but_the_queued_one_does_not_after_stop(
        self,
    ):
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.device.send_block = asyncio.Event()
        self.provider.push_audio(PCM_CHUNK * 2)  # two chunks
        await self.h.quiesce()  # the first is blocked inside send_text
        await self.h.feed({"type": "pet.stop"})
        self.device.send_block.set()
        await self.h.quiesce()
        await self.clock.advance(1.0)
        self.assertEqual(len(self.audio_out()), 1)
        outcome = await self.h.finish()
        self.assertEqual(outcome.audio_out_bytes, 4800)

    async def test_u2r_m3_old_started_callback_cannot_reopen_the_gate(self):
        await self.to_accepted()
        self.device.send_block = asyncio.Event()
        await self.clock.advance(1.0)  # reply.started is now blocked in the device
        await self.h.feed({"type": "pet.stop"})
        self.device.send_block.set()  # the old started send completes after Stop
        await self.h.quiesce()
        self.provider.push_audio(PCM_CHUNK)
        await self.h.quiesce()
        self.assertEqual(self.audio_out(), [])
        outcome = await self.h.finish()
        self.assertEqual(outcome.audio_out_bytes, 0)


PCM_SHORT = b"\x05\x06" * 1200  # 2400 bytes: one chunk
PCM_LONG = b"\x07\x08" * 6000  # 12000 bytes: three paced chunks
# Literal pin of the reviewed refusal reasons that prove "nothing was sent" (not derived
# from the production set, so adding or removing one there fails a test).
KNOWN_UNSENT_REASONS = frozenset(
    {
        "digest_mismatch",
        "invalid_revision",
        "not_sealed",
        "epoch_stale",
        "stopped",
        "expired",
        "command_inflight",
        "uncertain_unresolved",
        "reply_pending",
        "bridge_faulted",
        "binding_mismatch",
        "credential_missing",
        "core_binding_required",
        "invalid_request",
        "journal_full",
    }
)


class AudioReleaseLivenessTest(SpeakCase):
    """Held audio survives device/epoch stalls longer than the old packet-quiet cutoff.

    Forwarded fixture bytes do not prove speech completion; explicit Stop ends the turn.
    """

    VARIANTS = (
        ("short", False, PCM_SHORT),
        ("long", False, PCM_LONG),
        ("long_reply", True, PCM_LONG),
    )
    DURATIONS = (1.6, 3.0, 12.0)

    async def start_burst(
        self, *, hold: str, long_reply: bool, pcm: bytes, yielding: bool
    ):
        """Accepted, speaking, then ONE burst: first matching ACK + PCM (+ other ACKs),
        with the device (or the epoch commit) stalled.  Returns the stall releaser."""
        await self.fresh_harness()
        self.device.yield_every_send = yielding
        self.provider.auto_ack = False
        if long_reply:
            self.ops.results["poll"] = lambda turn: ReplyResult(
                turn, "speak", "reply_complete", True, 3, LONG_REPLY
            )
        await self.to_accepted()
        await self.clock.advance(1.0)
        chunks = self.commentary()
        self.assertEqual(len(chunks), 5 if long_reply else 1)
        if hold == "device":
            self.device.send_block = asyncio.Event()
            release = self.device.send_block.set
        else:
            release = self.ops.hold("epoch_current").set
        self.provider.push(ack_of(chunks[0]))
        self.provider.push_audio(pcm)
        for chunk in chunks[1:]:
            self.provider.push(ack_of(chunk))
        await self.h.quiesce()
        return release

    def assert_played_whole(self, pcm: bytes) -> None:
        types = self.device.types()
        self.assertEqual(self.done_states(), [])
        self.assertEqual(self.device.error_codes(), [])
        started = types.index("pet.reply.started")
        audio_at = [i for i, t in enumerate(types) if t == "session.output_audio.delta"]
        self.assertLess(started, audio_at[0])
        delivered = b"".join(base64.b64decode(f["delta"]) for f in self.audio_out())
        self.assertEqual(delivered, pcm)  # exact bytes, in order

    async def check(self, hold: str) -> None:
        for name, long_reply, pcm in self.VARIANTS:
            for duration in self.DURATIONS:
                for yielding in (False, True):
                    with self.subTest(
                        variant=name, stall_s=duration, yield_every_send=yielding
                    ):
                        release = await self.start_burst(
                            hold=hold,
                            long_reply=long_reply,
                            pcm=pcm,
                            yielding=yielding,
                        )
                        await self.clock.advance(
                            duration
                        )  # past the 1.5 s quiet period
                        self.assertEqual(self.audio_out(), [])
                        release()
                        await self.h.quiesce()
                        await self.clock.advance(3.0)
                        self.assert_played_whole(pcm)
                        self.assertLess(self.clock.now() - self.t_acc, 60.0)
                        await self.h.feed({"type": "pet.stop"})
                        outcome = await self.h.finish()
                        self.assertEqual(outcome.reply_done, "stopped")
                        self.assertEqual(outcome.audio_out_bytes, len(pcm))
                        self.assertEqual(outcome.dropped_audio_bytes, 0)

    async def test_u2r_v2_device_backpressure_beyond_quiet_still_plays_all_audio(self):
        await self.check("device")

    async def test_u2r_v2_epoch_commit_hold_beyond_quiet_still_plays_all_audio(self):
        await self.check("epoch")

    async def test_u2r_v2_stop_after_the_seal_still_clears_the_held_buffer(self):
        for hold in ("device", "epoch"):
            with self.subTest(hold=hold):
                release = await self.start_burst(
                    hold=hold, long_reply=False, pcm=PCM_LONG, yielding=False
                )
                await self.clock.advance(3.0)  # sealed while still held
                await self.h.feed({"type": "pet.stop"})
                release()
                await self.h.quiesce()
                await self.clock.advance(3.0)
                self.assertEqual(self.audio_out(), [])
                outcome = await self.h.finish()
                self.assertEqual(outcome.audio_out_bytes, 0)
                self.assertGreaterEqual(outcome.dropped_audio_bytes, len(PCM_LONG))

    async def test_u2r_v2_known_unsent_reasons_are_exactly_the_reviewed_list(self):
        self.assertEqual(len(KNOWN_UNSENT_REASONS), 15)
        self.assertEqual(gc._KNOWN_UNSENT, KNOWN_UNSENT_REASONS)


PROVIDER_ERROR = {
    "type": "error",
    "error": {"type": "unknown", "code": "x", "message": "TEXTCANARY"},
}


class RepairFatalTest(SpeakCase):
    """U2 repair M6: a fatal provider failure after an accepted send ends the reply truthfully."""

    def assert_truthful_fatal_end(
        self, outcome: gc.SessionOutcome, close_code: int = 1011,
    ) -> None:
        self.assertEqual(self.done_states(), ["unavailable"])  # exactly once
        self.assertEqual(self.device.error_codes(), [])  # no provider text anywhere
        self.assertNotIn("session.closed", self.device.types())
        self.assertEqual(self.device.close_codes, [close_code])
        types = self.device.types()
        self.assertLess(
            types.index("pet.turn.send_result"), types.index("pet.reply.done")
        )
        self.assertEqual(
            (outcome.reply_done, outcome.error_code), ("unavailable", None)
        )
        self.assertNotIn("TEXTCANARY", " ".join(self.device.raw))

    async def test_u2r_m6_fatal_provider_error_in_await_reply_emits_reply_done_unavailable_then_1011(
        self,
    ):
        self.ops.results["poll"] = pending_poll
        await self.to_accepted()
        await self.h.push(PROVIDER_ERROR)
        self.assert_truthful_fatal_end(await self.h.finish())

    async def test_u2r_m6_fatal_provider_error_in_speaking_emits_reply_done_unavailable_then_1011(
        self,
    ):
        await self.to_accepted()
        await self.clock.advance(1.0)  # speaking, gate open
        self.provider.push_audio(PCM_CHUNK)
        await self.h.quiesce()
        await self.h.push(PROVIDER_ERROR)
        outcome = await self.h.finish()
        self.assert_truthful_fatal_end(outcome)

    async def test_invalid_provider_frame_after_accepted_send_never_replays(self):
        self.ops.results["poll"] = pending_poll
        await self.to_accepted()
        await self.h.push("{bad provider frame")
        outcome = await self.h.finish()
        await self.h.runner.wait_idle()
        self.assertEqual(self.done_states(), ["unavailable"])
        self.assertEqual(self.device.close_codes, [1002])
        self.assertEqual(self.ops.count("confirm"), 1)
        self.assertEqual(outcome.send_outcome, "accepted")
        self.assertNotIn("session.closed", self.device.types())

    async def test_invalid_provider_frame_with_confirm_inflight_never_replays(self):
        release = self.ops.hold("confirm")
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        await self.h.push("{bad provider frame")
        outcome = await self.h.finish()
        self.assertEqual(self.device.close_codes, [1002])
        self.assertEqual(outcome.send_outcome, "inflight")
        self.assertIsNotNone(outcome.confirm_job)
        self.assertEqual(self.frames("pet.turn.send_result"), [])
        release.set()
        await self.h.runner.wait_idle()
        self.assertEqual(self.ops.count("confirm"), 1)

    async def test_foreign_close_with_confirm_inflight_preserves_reconciliation(self):
        release = self.ops.hold("confirm")
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        event = provider_frame({"type": "session.closed", "reason": "close_requested"})
        event["session"]["id"] = "live_other"
        await self.h.push(event)
        outcome = await self.h.finish()
        self.assertEqual(self.device.close_codes, [1002])
        self.assertEqual(outcome.send_outcome, "inflight")
        self.assertIsNotNone(outcome.confirm_job)
        self.assertEqual(self.frames("pet.turn.send_result"), [])
        release.set()
        await self.h.runner.wait_idle()
        self.assertEqual(self.h.runner.take_result(outcome.confirm_job).outcome, "accepted")
        self.assertEqual(self.ops.count("confirm"), 1)

    async def test_provider_end_during_preflight_proves_no_send(self):
        for reason, code in (("expired", 1000), ("content", 1008), ("connection_lost", 1011)):
            with self.subTest(reason=reason):
                await self.fresh_harness()
                release = self.ops.hold("preflight")
                await self.to_preview()
                await self.h.feed(self.confirm_frame())
                await self.h.push(provider_frame({"type": "session.closed", "reason": reason}))
                result = await self.h.finish()
                release.set()
                await self.h.runner.wait_idle()
                self.assertEqual(self.device.close_codes, [code])
                self.assertEqual(self.frames("pet.turn.send_result")[0]["state"], "rejected")
                self.assertEqual(result.send_outcome, "refused")
                self.assertEqual(self.ops.count("confirm"), 0)
                self.assertFalse(result.close_confirmed)

    async def test_provider_end_during_inflight_send_never_replays(self):
        for reason, code in (
            ("close_requested", 1000), ("expired", 1000), ("remote_hangup", 1000),
            ("content", 1008), ("connection_lost", 1011),
        ):
            with self.subTest(reason=reason):
                await self.fresh_harness()
                release = self.ops.hold("confirm")
                await self.to_preview()
                await self.h.feed(self.confirm_frame())
                await self.h.push(provider_frame({"type": "session.closed", "reason": reason}))
                result = await self.h.finish()
                self.assertEqual(self.device.close_codes, [code])
                self.assertEqual(result.send_outcome, "inflight")
                self.assertIsNotNone(result.confirm_job)
                self.assertEqual(self.frames("pet.turn.send_result"), [])
                self.assertEqual(self.frames("pet.reply.done"), [])
                self.assertFalse(result.close_confirmed)
                release.set()
                await self.h.runner.wait_idle()
                self.assertEqual(self.h.runner.take_result(result.confirm_job).outcome, "accepted")
                self.assertEqual(self.ops.count("confirm"), 1)

    async def test_provider_end_after_accepted_send_never_reports_reply_done(self):
        for reason, code in (("expired", 1000), ("content", 1008), ("connection_lost", 1011)):
            with self.subTest(reason=reason):
                await self.fresh_harness()
                self.ops.results["poll"] = pending_poll
                await self.to_accepted()
                await self.h.push(provider_frame({"type": "session.closed", "reason": reason}))
                result = await self.h.finish()
                self.assertEqual(self.device.close_codes, [code])
                self.assertEqual(self.done_states(), ["unavailable"])
                self.assertEqual(result.send_outcome, "accepted")
                self.assertEqual(self.ops.count("confirm"), 1)
                self.assertFalse(result.close_confirmed)
                self.assertNotIn("session.closed", self.device.types())

    async def test_u2r_m6_unexpected_provider_close_and_provider_send_failure_are_the_same_funnel(
        self,
    ):
        for label in ("session_closed", "unclean_close", "send_failure"):
            with self.subTest(case=label):
                await self.fresh_harness()
                self.ops.results["poll"] = pending_poll
                if label == "send_failure":
                    self.ops.results["poll"] = None  # a speakable reply
                await self.to_accepted()
                if label == "session_closed":
                    await self.h.push({"type": "session.closed"})
                elif label == "unclean_close":
                    self.provider.push_closed(clean=False)
                    await self.h.quiesce()
                else:
                    self.provider.fail_sends = True
                    await self.clock.advance(1.0)  # the commentary send fails
                self.assert_truthful_fatal_end(
                    await self.h.finish(), 1000 if label == "session_closed" else 1011
                )

    async def test_u2r_m6_fatal_after_stopped_reply_does_not_duplicate_done(self):
        self.provider.auto_close = False
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.provider.push_audio(PCM_CHUNK)
        await self.h.quiesce()
        await self.clock.advance(1.6)
        self.assertEqual(self.done_states(), [])
        await self.h.feed({"type": "pet.stop"})
        self.assertEqual(self.done_states(), ["stopped"])
        self.provider.push_closed(clean=False)  # before the close finishes
        await self.h.quiesce()
        self.assertEqual(self.done_states(), ["stopped"])
        await self.h.finish()

    async def test_u2r_m6_fatal_during_preflight_is_known_no_send_and_confirm_never_runs(
        self,
    ):
        release = self.ops.hold("preflight")
        await self.to_preview()
        base = len(self.device.frames)
        await self.h.feed(self.confirm_frame())
        await self.h.push(PROVIDER_ERROR)
        release.set()
        await self.h.finish()
        await self.h.runner.wait_idle()
        self.assertEqual(self.device.types()[base:], ["pet.turn.send_result"])
        self.assertEqual(self.frames("pet.turn.send_result")[0]["state"], "rejected")
        self.assertEqual(self.ops.count("confirm"), 0)
        self.assertEqual(self.device.close_codes, [1011])

    async def test_u2r_m6_fatal_with_admitted_confirm_makes_no_rejected_claim(self):
        release = self.ops.hold("confirm")
        await self.to_preview()
        await self.h.feed(self.confirm_frame())
        await self.h.push(PROVIDER_ERROR)
        assert self.h.task is not None
        outcome = await self.h.task
        self.assertEqual(self.frames("pet.turn.send_result"), [])
        self.assertEqual(outcome.send_outcome, "inflight")
        self.assertIsNotNone(outcome.confirm_job)
        release.set()
        await self.h.runner.wait_idle()

    async def test_u2r_m6_fatal_before_any_send_has_no_send_result_or_reply_done(self):
        await self.h.boot()
        await self.h.begin()
        await self.h.push(PROVIDER_ERROR)
        await self.h.finish()
        self.assertEqual(self.frames("pet.turn.send_result"), [])
        self.assertEqual(self.done_states(), [])
        self.assertEqual(self.device.close_codes, [1011])


class SessionPrivacyTest(SpeakCase):
    TEXT = TEXT_CANARY

    async def test_t_core_sess_privacy_no_canary_in_repr_str_outcome_or_runner(self):
        await self.to_accepted()
        await self.clock.advance(1.0)
        self.provider.push_audio(AUDIO_CANARY * 4)
        await self.h.quiesce()
        await self.clock.advance(1.6)
        await self.h.feed({"type": "pet.stop"})
        outcome = await self.h.finish()
        blob = " ".join(
            [
                repr(self.h.session),
                str(self.h.session),
                repr(outcome),
                str(outcome),
                repr(self.h.runner),
                repr(self.h.runner.retained()),
                repr(gc.AudioOut(self.noop_send, self.clock)),
            ]
        )
        for secret in (
            TEXT_CANARY,
            "REPLYCANARY",
            repr(AUDIO_CANARY),
            "\x11\x22",
            PET_TOKEN_CANARY,
            KEY_CANARY,
            SESSION,
        ):
            self.assertNotIn(secret, blob)
        self.assertIn("CLOSED", repr(outcome))

    @staticmethod
    async def noop_send(_text: str) -> None:
        return


# =================================================================================
# Source hygiene (a deliberate, AST-exact check of the module's own text)
# =================================================================================
class SourceHygieneTest(OfflineTestCase):
    def tree(self) -> ast.Module:
        return ast.parse(PY_FILE.read_text(encoding="utf-8"))

    def test_every_wait_goes_through_the_injected_clock(self):
        tree = self.tree()
        offenders: list[str] = []

        def visit(node: ast.AST, inside_real_clock: bool) -> None:
            if isinstance(node, ast.ClassDef):
                inside_real_clock = node.name == "RealClock"
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                pair = (node.value.id, node.attr)
                if pair in {
                    ("time", "sleep"),
                    ("asyncio", "wait_for"),
                    ("asyncio", "timeout"),
                }:
                    offenders.append(".".join(pair))
                if pair == ("asyncio", "sleep") and not inside_real_clock:
                    offenders.append("asyncio.sleep outside RealClock")
            for child in ast.iter_child_nodes(node):
                visit(child, inside_real_clock)

        visit(tree, False)
        self.assertEqual(offenders, [])

    def test_no_logging_print_or_dynamic_execution(self):
        tree = self.tree()
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        self.assertFalse(names & {"print", "eval", "exec", "__import__", "open"})
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertNotIn("logging", imported)

    def test_imports_are_standard_library_and_the_package_only(self):
        tree = self.tree()
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        self.assertTrue(roots <= set(__import__("sys").stdlib_module_names))
        self.assertTrue({"asyncio", "threading"} <= roots)
        self.assertFalse(roots & {"aiohttp", "socket", "ssl", "http", "subprocess"})

    def test_all_names_exist_and_are_the_public_surface(self):
        expected = {
            "AudioOut",
            "AudioOverflow",
            "CoreRunner",
            "DeviceLink",
            "DeviceMessage",
            "JobTicket",
            "LoopClock",
            "PetSession",
            "ProviderLink",
            "ProviderMessage",
            "RealClock",
            "RetainedResult",
            "RunnerError",
            "SessionOutcome",
            "SessionRunError",
            "SessionSettings",
            "UnifiedOps",
        }
        self.assertEqual(set(gc.__all__), expected)
        for name in expected:
            self.assertTrue(hasattr(gc, name), name)

    def test_the_runner_op_names_are_exactly_the_plan_list(self):
        # Conversation mode may refuse one request and continue on the same
        # connection. Discard only its unsent capture, never Stop+auto-resume.
        self.assertEqual(
            gc.CoreRunner.OPS,
            frozenset(
                {
                    "begin",
                    "abandon",
                    "stage",
                    "seal",
                    "preflight",
                    "confirm",
                    "poll",
                    "epoch_current",
                    "stop",
                    "resume",
                    "status",
                    "pre_dial_admission",
                    "follow_host_restart",
                }
            ),
        )

    def test_no_hard_coded_selected_session_suffix(self):
        text = PY_FILE.read_text(encoding="utf-8")
        for forbidden in ("0a1b2c3d", "5555aaaa", "api.openai.com"):
            self.assertNotIn(forbidden, text)
