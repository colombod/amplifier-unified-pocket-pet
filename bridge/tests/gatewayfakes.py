"""Offline doubles for the gateway core tests (stdlib + asyncio only).

Nothing here opens a socket.  ``OfflineAsyncCase`` makes every socket entry point,
including listen/bind and the event-loop server/connection helpers, raise, so a test can
never reach a network.  The fakes are scripted and record only metadata-sized facts.
"""

# pyright: reportArgumentType=false, reportOptionalMemberAccess=false
from __future__ import annotations

import asyncio
import base64
import json
import threading
import unittest
from collections.abc import Callable
from typing import Any
from unittest import mock

from pet_bridge import core
from pet_bridge import gateway_core as gc
from pet_bridge.companion import Preflight

from .fakes import SESSION
from .test_gateway_state import ASSISTANT_GREETING, ASSISTANT_START

__all__ = [
    "ASSISTANT_GREETING",
    "ASSISTANT_START",
    "AUDIO_CANARY",
    "KEY_CANARY",
    "PET_TOKEN_CANARY",
    "REPLY_TEXT",
    "TEXT_CANARY",
    "TURN",
    "FakeDeviceLink",
    "FakeLoopClock",
    "FakeOps",
    "FakeProviderFactory",
    "FakeProviderLink",
    "FakeStore",
    "OfflineAsyncCase",
    "SwallowCancelFactory",
    "device_frame",
]

TURN = "123e4567-e89b-42d3-a456-426614174000"
TEXT_CANARY = "TEXTCANARY-utterance-77"
REPLY_TEXT = "REPLYCANARY the answer is forty two. Second sentence here."
AUDIO_CANARY = b"\x11\x22" * 8
PET_TOKEN_CANARY = "T" * 43
KEY_CANARY = "sk-" + "K" * 40


class OfflineAsyncCase(unittest.IsolatedAsyncioTestCase):
    """Every socket entry point raises: no listener, no dial, no DNS."""

    def setUp(self) -> None:
        super().setUp()

        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("network access attempted in an offline test")

        for target in (
            "socket.socket.connect",
            "socket.socket.connect_ex",
            "socket.create_connection",
            "socket.getaddrinfo",
            "socket.socket.bind",
            "socket.socket.listen",
            "asyncio.base_events.BaseEventLoop.create_server",
            "asyncio.base_events.BaseEventLoop.create_connection",
        ):
            patcher = mock.patch(target, side_effect=refuse)
            patcher.start()
            self.addCleanup(patcher.stop)


class FakeLoopClock:
    """A deterministic ``LoopClock``: time moves only when the test advances it."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start
        self._seq = 0
        self._waiters: list[tuple[float, int, asyncio.Future[None]]] = []
        self.sleeps: list[float] = []
        self.settle_hook: Callable[[], Any] | None = None

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entry = (self._now + max(seconds, 0.0), self._seq, future)
        self._seq += 1
        self._waiters.append(entry)
        self.sleeps.append(seconds)
        try:
            await future
        finally:
            if entry in self._waiters:
                self._waiters.remove(entry)

    @property
    def pending(self) -> int:
        return sum(1 for _t, _s, future in self._waiters if not future.done())

    async def settle(self) -> None:
        if self.settle_hook is not None:
            await self.settle_hook()
            return
        for _ in range(40):
            await asyncio.sleep(0)

    async def advance(self, seconds: float) -> None:
        """Move time forward, firing every due waiter at ITS time, in time order."""
        target = self._now + seconds
        while True:
            due = sorted(
                (
                    w
                    for w in self._waiters
                    if w[0] <= target + 1e-12 and not w[2].done()
                ),
                key=lambda w: (w[0], w[1]),
            )
            if not due:
                break
            when, _seq, future = due[0]
            self._now = max(self._now, when)
            self._waiters.remove(due[0])
            future.set_result(None)
            await self.settle()
        self._now = target
        await self.settle()


def device_frame(**frame: Any) -> str:
    return json.dumps(frame, separators=(",", ":"))


class FakeDeviceLink:
    """A scripted ``DeviceLink``; records decoded outbound frames and close codes."""

    def __init__(self, clock: FakeLoopClock | None = None) -> None:
        self.inbound: asyncio.Queue[gc.DeviceMessage] = asyncio.Queue()
        self.frames: list[dict[str, Any]] = []
        self.raw: list[str] = []
        self.times: list[float] = []
        self.close_codes: list[int] = []
        self.send_block: asyncio.Event | None = None
        self.fail_sends = False
        self.clock = clock
        # Yield to the event loop inside every send (before and after it is recorded) so a
        # test proves ordering under REAL interleaving instead of a quiescing harness.
        self.yield_every_send = False
        self.close_block: asyncio.Event | None = None
        self.close_calls = 0

    def feed(self, frame: Any) -> None:
        text = frame if isinstance(frame, str) else device_frame(**frame)
        self.inbound.put_nowait(gc.DeviceMessage("text", text))

    def feed_binary(self) -> None:
        self.inbound.put_nowait(gc.DeviceMessage("binary"))

    def disconnect(self) -> None:
        self.inbound.put_nowait(gc.DeviceMessage("closed"))

    async def send_text(self, text: str) -> None:
        if self.fail_sends:
            raise ConnectionError("fake device send failure")
        if self.send_block is not None:
            await self.send_block.wait()
        if self.yield_every_send:
            await asyncio.sleep(0)
        self.raw.append(text)
        self.frames.append(json.loads(text))
        self.times.append(0.0 if self.clock is None else self.clock.now())
        if self.yield_every_send:
            await asyncio.sleep(0)

    async def receive(self) -> gc.DeviceMessage:
        return await self.inbound.get()

    async def close(self, code: int) -> None:
        self.close_calls += 1
        if self.close_block is not None:
            await self.close_block.wait()
        self.close_codes.append(code)

    def types(self) -> list[str]:
        return [str(f.get("type")) for f in self.frames]

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [f for f in self.frames if f.get("type") == kind]

    def error_codes(self) -> list[str]:
        return [str(f.get("code")) for f in self.frames if f.get("type") == "pet.error"]


class FakeProviderLink:
    """A scripted ``ProviderLink`` with optional automatic provider behaviour."""

    def __init__(
        self,
        clock: FakeLoopClock | None = None,
        *,
        auto_start: bool = True,
        auto_ack: bool = True,
        auto_close: bool = True,
    ) -> None:
        self.inbound: asyncio.Queue[gc.ProviderMessage] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.times: list[float] = []
        self.close_calls = 0
        self.close_block: asyncio.Event | None = None
        self.send_block: asyncio.Event | None = None
        self.fail_sends = False
        self.auto_start = auto_start
        self.auto_ack = auto_ack
        self.auto_close = auto_close
        self.clock = clock

    def push(self, event: Any) -> None:
        # Dict shorthand constructs a documented provider envelope. Raw strings
        # remain untouched for malformed-wire tests; parser tests bypass this.
        from .providerfixtures import provider_frame
        text = event if isinstance(event, str) else json.dumps(provider_frame(event))
        self.inbound.put_nowait(gc.ProviderMessage("text", text))

    def push_audio(self, pcm: bytes) -> None:
        delta = base64.b64encode(pcm).decode("ascii")
        self.push({"type": "session.output_audio.delta", "delta": delta})

    def push_closed(self, *, clean: bool = True) -> None:
        self.inbound.put_nowait(gc.ProviderMessage("closed", None, clean))

    async def send_text(self, text: str) -> None:
        if self.fail_sends:
            raise ConnectionError("fake provider send failure")
        if self.send_block is not None:
            await self.send_block.wait()
        frame = json.loads(text)
        self.sent.append(frame)
        self.times.append(0.0 if self.clock is None else self.clock.now())
        kind = frame.get("type")
        if kind == "session.start" and self.auto_start:
            self.push({"type": "session.started", "client_event_id": frame["event_id"]})
        elif kind == "session.commentary.append" and self.auto_ack:
            self.push(
                {
                    "type": "session.commentary.appended",
                    "client_event_id": frame["event_id"],
                }
            )
        elif kind == "session.close" and self.auto_close:
            self.push({"type": "session.closed"})

    async def receive(self) -> gc.ProviderMessage:
        return await self.inbound.get()

    async def close(self) -> None:
        self.close_calls += 1  # counted BEFORE any block: the close was initiated
        if self.close_block is not None:
            await self.close_block.wait()

    def types(self) -> list[str]:
        return [str(f.get("type")) for f in self.sent]

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [f for f in self.sent if f.get("type") == kind]


class FakeProviderFactory:
    """The ``provider_connect`` callable of a ``PetSession``."""

    def __init__(
        self,
        link: FakeProviderLink | None = None,
        *,
        fail: BaseException | None = None,
        hang: bool = False,
    ) -> None:
        self.link = link or FakeProviderLink()
        self.fail = fail
        self.hang = hang
        self.calls = 0

    async def __call__(self) -> FakeProviderLink:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        if self.hang:
            await asyncio.Event().wait()
        return self.link


class SwallowCancelFactory:
    """A connector that ignores ONE cancellation and still returns its link (advisory A1).

    Cancel-cooperative otherwise: after the swallowed cancel it returns at once, so a test
    never hangs.  ``gate`` is what it waits on.
    """

    def __init__(self, link: FakeProviderLink, gate: asyncio.Event) -> None:
        self.link = link
        self.gate = gate
        self.calls = 0
        self.swallowed = 0

    async def __call__(self) -> FakeProviderLink:
        self.calls += 1
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.swallowed += 1
        return self.link


class FakeStore:
    """A stand-in credential store: ``load_attachment`` returns a scripted attachment."""

    def __init__(self, attachment: Any) -> None:
        self.attachment = attachment
        self.loads = 0

    def load_attachment(self, ref: str) -> Any:
        self.loads += 1
        return self.attachment


class FakeOps:
    """A synchronous stand-in for ``UnifiedOps``: scripted results, call log, blocking.

    ``results[op]`` may be a value, an exception (raised) or a callable ``(*args)``.
    ``hold(op)`` returns a ``threading.Event``: the op blocks on the worker thread until
    that event is set; ``entered[op]`` is set when the op starts.
    """

    def __init__(self, clock: FakeLoopClock | None = None, *, text: str = TEXT_CANARY):
        self.clock = clock
        self.calls: list[tuple[str, tuple[Any, ...], float]] = []
        self.results: dict[str, Any] = {}
        self.entered: dict[str, threading.Event] = {}
        self._holds: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.staged: list[tuple[str, str]] = []
        self.text = text
        self.epoch = 3

    def hold(self, op: str) -> threading.Event:
        event = threading.Event()
        self._holds[op] = event
        return event

    def held_active(self) -> bool:
        """True while any hold is configured and not yet released (a call may block)."""
        return any(not event.is_set() for event in self._holds.values())

    def release_holds(self) -> None:
        """Test cleanup only: do not leave an owned worker blocked on a failed assertion."""
        for event in self._holds.values():
            event.set()

    def names(self) -> list[str]:
        return [name for name, _args, _t in self.calls]

    def count(self, op: str) -> int:
        return self.names().count(op)

    def times(self, op: str) -> list[float]:
        return [t for name, _args, t in self.calls if name == op]

    def _enter(self, op: str, args: tuple[Any, ...]) -> Any:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            now = 0.0 if self.clock is None else self.clock.now()
            self.calls.append((op, args, now))
            self.entered.setdefault(op, threading.Event()).set()
        try:
            hold = self._holds.get(op)
            if hold is not None:
                # Fake-clock cadence can take >10 real seconds under debug mode.
                # Wall-clock expiry must NEVER silently release a held fake send.
                hold.wait()
            scripted = self.results.get(op)
            if isinstance(scripted, BaseException):
                raise scripted
            if callable(scripted):
                return scripted(*args)
            return scripted
        finally:
            with self._lock:
                self.active -= 1

    def begin(self) -> str:
        scripted = self._enter("begin", ())
        return TURN if scripted is None else scripted

    def begin_after_timeout(self, turn_id: str, closed: threading.Event) -> str:
        return self.begin()

    def stage(self, turn_id: str, segments: Any) -> str:
        scripted = self._enter("stage", (turn_id, tuple(segments)))
        if scripted is None:
            self.staged = list(segments)
            return "appended"
        return scripted

    def abandon(self, turn_id: str) -> None:
        self._enter("abandon", (turn_id,))

    def timeout_reply(self, turn_id: str) -> None:
        self.abandon(turn_id)

    def seal(self, turn_id: str) -> core.TranscriptPreview:
        scripted = self._enter("seal", (turn_id,))
        if scripted is not None:
            return scripted
        import hashlib

        text = "".join(t for _e, t in self.staged) or self.text
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return core.TranscriptPreview(turn_id, "b" * 8, SESSION, text, sha, len(text))

    def preflight(self) -> Preflight:
        scripted = self._enter("preflight", ())
        return Preflight(SESSION, 7, True, "idle") if scripted is None else scripted

    def confirm(self, turn_id: str, sha: str, revision: int) -> core.CommandResult:
        scripted = self._enter("confirm", (turn_id, sha, revision))
        if scripted is None:
            return core.CommandResult(turn_id, "accepted", "accepted", True)
        return scripted

    def poll(self, turn_id: str) -> core.ReplyResult:
        scripted = self._enter("poll", (turn_id,))
        if scripted is None:
            return core.ReplyResult(
                turn_id, "speak", "reply_complete", True, self.epoch, REPLY_TEXT
            )
        return scripted

    def observe_reply(self, turn_id: str) -> core.ReplyResult:
        return self.poll(turn_id)

    def claim_observed_reply(self, turn_id: str) -> core.ReplyResult:
        return self.poll(turn_id)

    def epoch_current(self, epoch: int) -> bool:
        scripted = self._enter("epoch_current", (epoch,))
        return True if scripted is None else scripted

    def stop(self) -> None:
        self._enter("stop", ())

    def resume(self) -> None:
        self._enter("resume", ())

    def status(self) -> dict[str, Any]:
        scripted = self._enter("status", ())
        return {} if scripted is None else scripted

    def pre_dial_safe(self) -> bool:
        scripted = self._enter("pre_dial_safe", ())
        return True if scripted is None else scripted

    def pre_dial_admission(self) -> bool:
        return self.pre_dial_safe()
