"""In-process gateway flows over the REAL core stack (plan v1 section 8, ``T-FLOW-*``).

Real: ``Journal``, ``CredentialStore`` (a stored credential and attachment), ``Bridge``
(a binding made explicitly, then a second ``Bridge`` constructed serve-style on the same
journal, so it starts DISARMED), ``HttpsTransport``, ``companion``, ``UnifiedOps``,
``CoreRunner`` (one worker thread) and ``PetSession``.  Fake: the Unified HTTP client,
the device link, the provider link and every clock (wall, monotonic and loop time are
one injected fake clock, so no real 60 second expiry can happen).

Offline evidence only.  NOT a real TLS server, WebSocket, provider, Unified session,
firmware or acoustic result: no socket is opened (every entry point raises).  The device
frames asserted here are the CURRENT ``gateway_state`` output, written out as literal
golden JSON so another implementation can compare against them; this file compiles no
C and qualifies no firmware.
"""

# pyright: reportArgumentType=false, reportCallIssue=false, reportOptionalMemberAccess=false
# pyright: reportOptionalSubscript=false, reportAttributeAccessIssue=false
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from pet_bridge import auth, cli, core, transport, wire
from pet_bridge import gateway_core as gc
from pet_bridge import gateway_state as gs
from pet_bridge.journal import Journal
from pet_bridge.transport import Origin, Response

from . import test_gateway_server as tgs
from .authfakes import (
    CLIENT_ID,
    CRED_REF,
    DEVICE_ID,
    HOST_IDENTITY,
    INSTANCE,
    ORIGIN_URL,
    TOKEN,
    json_response,
)
from .fakes import (
    FakeTransport,
    IdFactory,
    accepted_body,
    assistant_row,
    page,
    user_row,
)
from .gatewayfakes import (
    ASSISTANT_GREETING,
    ASSISTANT_START,
    AUDIO_CANARY,
    REPLY_TEXT,
    TEXT_CANARY,
    FakeDeviceLink,
    FakeLoopClock,
    FakeProviderFactory,
    FakeProviderLink,
    OfflineAsyncCase,
)
from .test_companion import DetailServer, lpage, srow
from .test_gateway_core import DELEG, audio_frame, delegation, delta, until

SESSION_ID = (
    "gflowsess0123abcd"  # last 8 characters: lowercase hex, never a real session
)
SHORT8 = "0123abcd"
LABEL = "Flow session"
WALL0 = 1_700_000_000.0
TURN_ID = IdFactory(200)()  # the id the serve-style Bridge issues first (deterministic)
TEXT = "hello world"
TEXT_SHA = hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
LONG_REPLY = " ".join(f"Sentence number {n} is right here." for n in range(30))
# Multibyte exact-text oracles: 2-byte, emoji (4-byte), CJK (3-byte), a quote and a tab.
TEXT_MB = 'h\u00e9llo \U0001f600 \u4e16\u754c "quoted"\tend'
TEXT_MB_SHA = hashlib.sha256(TEXT_MB.encode("utf-8")).hexdigest()
TEXT_MB_JSON = (
    '"h\u00e9llo \U0001f600 \u4e16\u754c \\"quoted\\"\\tend"'  # compact, not ASCII
)
MB_REPLY = " ".join(
    f"Sentence {n} \u00e9\u00e8 \u4e16\u754c \U0001f600 is right here."
    for n in range(40)
)
PCM_A = b"\x01\x02" * 2400  # 4800 bytes: one full paced chunk
PCM_B = b"\x03\x04" * 1200  # 2400 bytes

COMPANION_TARGET = "/api/companion"
SEND_TARGET = f"/api/sessions/{SESSION_ID}/commands"
DETAIL_TARGET = f"/api/conversation/detail?sessionId={SESSION_ID}&part=messages"


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------------------------
# Golden device frames of ONE canonical exchange (literal text: keys, order, types)
# ---------------------------------------------------------------------------------
def lit(template: str, **values: str) -> str:
    """Fill ``@name`` placeholders of a literal JSON template (no JSON re-encoding)."""
    for name, value in values.items():
        template = template.replace("@" + name, value)
    return template


def golden_in(turn: str = TURN_ID) -> dict[str, str]:
    return {
        "begin": '{"type":"pet.turn.begin"}',
        "capture_stop": lit(
            '{"type":"pet.turn.capture_stop","turn_id":"@turn"}', turn=turn
        ),
        "confirm": lit(
            '{"type":"pet.turn.send_confirm","turn_id":"@turn",'
            '"text_sha256":"@sha","preview_revision":1}',
            turn=turn,
            sha=TEXT_SHA,
        ),
        "stop": '{"type":"pet.stop"}',
    }


def golden_out(turn: str = TURN_ID) -> dict[str, str]:
    return {
        "started": '{"type":"session.started"}',
        "began": lit(
            '{"type":"pet.turn.began","turn_id":"@turn",'
            '"safe_session_label":"@label","session_id_short":"@short"}',
            turn=turn,
            label=LABEL,
            short=SHORT8,
        ),
        "preview": lit(
            '{"type":"pet.turn.preview","turn_id":"@turn","preview_revision":1,'
            '"text":"@text","text_sha256":"@sha"}',
            turn=turn,
            text=TEXT,
            sha=TEXT_SHA,
        ),
        "accepted": lit(
            '{"type":"pet.turn.send_result","turn_id":"@turn","state":"accepted"}',
            turn=turn,
        ),
        "reply_started": lit(
            '{"type":"pet.reply.started","turn_id":"@turn",'
            '"rendering":"spoken_rendering_not_verbatim"}',
            turn=turn,
        ),
        "audio_a": lit(
            '{"type":"session.output_audio.delta","delta":"@pcm"}', pcm=b64(PCM_A)
        ),
        "audio_b": lit(
            '{"type":"session.output_audio.delta","delta":"@pcm"}', pcm=b64(PCM_B)
        ),
        "done": lit(
            '{"type":"pet.reply.done","turn_id":"@turn","state":"stopped"}',
            turn=turn,
        ),
        "closed": '{"type":"session.closed"}',
    }


FRAME_KEYS: dict[str, frozenset[str]] = {
    "session.started": frozenset({"type"}),
    "session.closed": frozenset({"type"}),
    "session.output_audio.delta": frozenset({"type", "delta"}),
    "pet.turn.began": frozenset(
        {"type", "turn_id", "safe_session_label", "session_id_short"}
    ),
    "pet.turn.preview": frozenset(
        {"type", "turn_id", "preview_revision", "text", "text_sha256"}
    ),
    "pet.turn.preview_invalidated": frozenset({"type", "turn_id", "reason"}),
    "pet.turn.send_result": frozenset({"type", "turn_id", "state"}),
    "pet.reply.started": frozenset({"type", "turn_id", "rendering"}),
    "pet.reply.done": frozenset({"type", "turn_id", "state"}),
    "pet.error": frozenset({"type", "code"}),
}


def check_frame(case: unittest.TestCase, raw: str) -> dict[str, Any]:
    """The pinned schema of one outbound device frame (types, enums, compact JSON)."""
    case.assertLess(len(raw.encode("utf-8")), 96 * 1024)  # the device's frame bound
    frame = json.loads(raw)
    kind = frame["type"]
    case.assertIn(kind, FRAME_KEYS, kind)
    case.assertEqual(set(frame), FRAME_KEYS[kind], kind)
    compact = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
    case.assertEqual(raw, compact, f"{kind}: not compact JSON in key order")
    if "turn_id" in frame:
        case.assertTrue(wire.is_canonical_uuid4(frame["turn_id"]), kind)
    if kind == "pet.turn.began":
        case.assertLessEqual(len(frame["safe_session_label"]), gs.SAFE_LABEL_MAX)
        case.assertRegex(frame["session_id_short"], r"^[0-9a-f]{8}$")
    elif kind == "pet.turn.preview":
        case.assertIs(type(frame["preview_revision"]), int)
        case.assertTrue(1 <= frame["preview_revision"] <= gs.PREVIEW_REVISION_MAX)
        digest = hashlib.sha256(frame["text"].encode("utf-8")).hexdigest()
        case.assertEqual(frame["text_sha256"], digest)
        case.assertLessEqual(len(frame["text"]), 4000)  # characters ...
        case.assertLessEqual(len(frame["text"].encode("utf-8")), 16000)  # ... and bytes
    elif kind == "pet.turn.send_result":
        case.assertIn(frame["state"], gs.SEND_RESULT_STATES)
    elif kind == "pet.reply.done":
        case.assertIn(frame["state"], gs.REPLY_DONE_STATES)
    elif kind == "pet.turn.preview_invalidated":
        case.assertIn(frame["reason"], gs.INVALIDATED_REASONS)
    elif kind == "pet.reply.started":
        case.assertEqual(frame["rendering"], "spoken_rendering_not_verbatim")
    elif kind == "pet.error":
        case.assertIn(frame["code"], gs.PET_ERROR_CODES)
    elif kind == "session.output_audio.delta":
        pcm = base64.b64decode(frame["delta"], validate=True)
        case.assertTrue(2 <= len(pcm) <= gs.AUDIO_CHUNK_BYTES and len(pcm) % 2 == 0)
    return frame


# ---------------------------------------------------------------------------------
# The fake Unified client (the transport's ``request`` seam)
# ---------------------------------------------------------------------------------
class UnifiedFake:
    """Scripted companion client.  Records every request; never touches a socket."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.listing_status = "idle"
        self.listing_execution = False
        self.post_response: Response | Callable[[str], Response] | None = None
        self.post_gate: threading.Event | None = None
        self.post_entered = threading.Event()
        self.reply_text = LONG_REPLY
        self.partial = False
        self.last_command: str | None = None
        self.last_text: str | None = None
        self.echo_sent_text = False  # the detail user row carries the text that was sent
        self.history_rows: list[dict[str, Any]] = []
        self.reply_pending = False
        self._lock = threading.Lock()

    def request(
        self, origin: Origin, route: Any, *, headers: Any = None, body: bytes = b""
    ) -> Response:
        with self._lock:
            self.calls.append(
                {
                    "name": route.name,
                    "method": route.method,
                    "target": route.target,
                    "headers": dict(headers or {}),
                    "body": body,
                    "origin": origin,
                }
            )
        if route.name == "companion":
            return lpage(
                [
                    srow(
                        SESSION_ID,
                        self.listing_status,
                        execution=self.listing_execution,
                    )
                ]
            )
        if route.name == "send":
            command = json.loads(body)["id"]
            self.last_command = command
            self.last_text = json.loads(body)["args"]["text"]
            self.post_entered.set()
            if self.post_gate is not None:
                self.post_gate.wait(timeout=10)
            scripted = self.post_response
            if scripted is None:
                return Response(200, accepted_body(SESSION_ID, command))
            return scripted(command) if callable(scripted) else scripted
        if route.name == "detail":
            command = self.last_command
            assert command is not None
            if self.partial:
                return self._partial(command, dict(route.query).get("before"))
            shown = {"text": self.last_text} if self.echo_sent_text else {}
            status = {"phase": "working", "basis": "generation_started"} if self.reply_pending else {}
            rows = [
                *self.history_rows,
                user_row(command, **status, **shown),
                assistant_row("gen-1", self.reply_text),
            ]
            if self.history_rows:
                return DetailServer(rows)({"route": route})
            return json_response(page(rows))
        raise AssertionError("unscripted route " + route.name)

    def _partial(self, command: str, before: str | None) -> Response:
        """Four pages of two rows from a 10-row history: the oldest is never reached."""
        newest = [user_row(command), assistant_row("gen-1", self.reply_text)]
        pages: dict[str | None, tuple[list[dict[str, Any]], int]] = {None: (newest, 8)}
        first = newest[0]["id"]
        for k in (1, 2, 3):
            older = [
                assistant_row(None, "older", id=f"f{k}a"),
                assistant_row(None, "older", id=f"f{k}b"),
            ]
            pages[first] = (older, 8 - 2 * k)
            first = older[0]["id"]
        items, offset = pages[before]
        return json_response(
            page(items, offset=offset, total=10, before=items[0]["id"])
        )

    def named(self, name: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["name"] == name]

    @property
    def posts(self) -> list[dict[str, Any]]:
        return self.named("send")

    def post_blocked(self) -> bool:
        return self.post_gate is not None and not self.post_gate.is_set()


class PcmProvider(FakeProviderLink):
    """Sends its PCM immediately after the FIRST commentary ACK (same provider tick)."""

    def __init__(self, clock: FakeLoopClock, pcm: tuple[bytes, ...] = ()) -> None:
        super().__init__(clock)
        self.pcm = pcm
        self._pushed = False

    async def send_text(self, text: str) -> None:
        await super().send_text(text)
        is_commentary = json.loads(text).get("type") == "session.commentary.append"
        if is_commentary and not self._pushed:
            self._pushed = True
            for chunk in self.pcm:
                self.push_audio(chunk)


class FlowSession:
    """One ``PetSession`` with its device and provider fakes."""

    def __init__(
        self,
        case: FlowCase,
        provider: FakeProviderLink,
        session: gc.PetSession,
        device: FakeDeviceLink,
    ) -> None:
        self.case, self.provider, self.session, self.device = (
            case,
            provider,
            session,
            device,
        )
        self.task: asyncio.Task[Any] | None = None

    def go(self) -> None:
        self.task = asyncio.create_task(self.session.run())

    async def feed(self, frame: Any) -> None:
        self.device.feed(frame)
        await self.case.quiesce()

    async def push(self, event: Any) -> None:
        self.provider.push(event)
        await self.case.quiesce()

    async def finish(self) -> gc.SessionOutcome:
        assert self.task is not None
        await self.case.quiesce()
        try:
            return await asyncio.wait_for(asyncio.shield(self.task), 5)
        except TimeoutError:
            raise AssertionError("the session did not finish") from None

    async def aclose(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def types(self) -> list[str]:
        return self.device.types()

    def raw(self) -> list[str]:
        return list(self.device.raw)


def strings_of(value: Any) -> list[str]:
    """Every string anywhere inside a decoded JSON value (keys excluded)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings_of(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings_of(v)]
    return []


class FlowCase(OfflineAsyncCase):
    """The real stack on private temp state, one fake clock for wall/monotonic/loop."""

    async def asyncSetUp(self) -> None:
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="pet-flow-test-")))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.clock = FakeLoopClock()
        self.clock.settle_hook = self.quiesce
        self.client = UnifiedFake()
        self.counter = 0
        mono = self.clock.now

        def wall() -> float:
            return WALL0 + self.clock.now()

        self.store = auth.CredentialStore(self.base / "auth")
        self.addCleanup(self.store.close)
        self.store.save_credential(
            auth.Credential(CRED_REF, ORIGIN_URL, DEVICE_ID, TOKEN)
        )
        self.store.save_attachment(
            auth.Attachment(
                CRED_REF, ORIGIN_URL, CLIENT_ID, HOST_IDENTITY, INSTANCE, token=TOKEN
            )
        )
        self.journal = Journal(self.base / "journal")
        self.addCleanup(self.journal.close)
        # An explicit binding first (what the bind command records) ...
        binder = core.Bridge(
            self.journal,
            FakeTransport(),
            clock=wall,
            new_id=IdFactory(100),
            monotonic=mono,
        )
        self.binding = binder.select_session(
            host_id=HOST_IDENTITY,
            host_instance_id=INSTANCE,
            session_id=SESSION_ID,
            client_id=CLIENT_ID,
            credential_ref=CRED_REF,
        )
        # ... then a serve-style Bridge on the same journal: it starts DISARMED.
        self.bridge = core.Bridge(
            self.journal,
            transport.HttpsTransport(self.store, lambda: self.client),
            clock=wall,
            new_id=IdFactory(200),
            monotonic=mono,
        )
        live = self.bridge.status()
        self.assertEqual(
            (live["bound"], live["stopped"], live["binding_id"]),
            (True, True, self.binding.binding_id),
        )
        self.runner = gc.CoreRunner()
        self.runner.start()
        self.addAsyncCleanup(self.runner.aclose)
        self.ops = gc.UnifiedOps(
            self.bridge,
            self.store,
            CRED_REF,
            SESSION_ID,
            lambda: self.client,
            monotonic=mono,
            binding=self.binding,
        )

    # -- plumbing -----------------------------------------------------------------
    def new_event_id(self) -> str:
        self.counter += 1
        return f"gw_{self.counter:04d}"

    async def quiesce(self) -> None:
        """Event driven: wait for the worker (unless a POST is deliberately blocked)."""
        for _ in range(8):
            if self.client.post_blocked():
                await asyncio.sleep(0.002)
            else:
                await self.runner.wait_idle()
            for _ in range(20):
                await asyncio.sleep(0)

    def new_session(
        self, provider: FakeProviderLink | None = None, *, auto_start: bool = True
    ) -> FlowSession:
        device = FakeDeviceLink(self.clock)
        provider = provider or FakeProviderLink(self.clock, auto_start=auto_start)
        session = gc.PetSession(
            device,
            FakeProviderFactory(provider),
            self.runner,
            self.ops,
            gc.SessionSettings("gpt-live-1", "marin", LABEL, SHORT8),
            self.clock,
            new_event_id=self.new_event_id,
        )
        fs = FlowSession(self, provider, session, device)
        self.addAsyncCleanup(fs.aclose)
        return fs

    def hold_posts(self) -> threading.Event:
        gate = threading.Event()
        self.client.post_gate = gate
        self.addCleanup(gate.set)
        return gate

    def record(self, turn: str) -> dict[str, Any]:
        record = self.journal.get(f"cmd-{turn}.json")
        assert record is not None
        return record

    def command_names(self) -> list[str]:
        return self.journal.names("cmd-")

    # -- drivers ------------------------------------------------------------------
    async def to_preview(
        self,
        fs: FlowSession,
        *,
        deltas: tuple[tuple[str, str, int, int], ...] = (
            ("e2", "world", 500, 900),
            ("e1", "hello ", 0, 500),
        ),
        delegation_id: str | None = DELEG,
    ) -> str:
        """start, begin, audio, out-of-order transcript/delegation, stop, settle, preview."""
        fs.go()
        await fs.feed(ASSISTANT_START)
        await fs.feed(golden_in()["begin"])
        began = fs.device.of_type("pet.turn.began")
        turn = began[0]["turn_id"]
        for _ in range(2):
            await fs.feed(audio_frame(100))
        first, *rest = deltas
        await fs.push(delta(*first))
        if delegation_id is not None:
            await fs.push(delegation(delegation_id))
        for item in rest:
            await fs.push(delta(*item))
        await fs.feed(golden_in(turn)["capture_stop"])
        await self.clock.advance(1.5)
        return turn

    def confirm_frame(self, turn: str, *, rev: int = 1, sha: str = TEXT_SHA) -> str:
        return lit(
            '{"type":"pet.turn.send_confirm","turn_id":"@turn","text_sha256":"@sha",'
            '"preview_revision":@rev}',
            turn=turn,
            sha=sha,
            rev=str(rev),
        )

    def assert_schema(self, fs: FlowSession) -> list[dict[str, Any]]:
        return [check_frame(self, raw) for raw in fs.raw()]


# =================================================================================
# T-FLOW-HAPPY (+ a short, immediate-audio variant under device backpressure)
# =================================================================================
class HappyFlowTest(FlowCase):
    async def test_t_flow_happy_the_whole_exchange_in_exact_frame_order(self):
        self.client.reply_text = LONG_REPLY
        provider = PcmProvider(self.clock, (PCM_A, PCM_B))
        fs = self.new_session(provider)
        turn = await self.to_preview(fs)
        self.assertEqual(turn, TURN_ID)
        gold = golden_out(turn)
        # Preview: exact text and hash; the human has not confirmed anything yet.
        self.assertEqual(fs.raw()[-2:], [gold["began"], gold["preview"]][-2:])
        self.assertEqual(
            self.client.calls, []
        )  # nothing reached Unified before confirm
        await fs.feed(golden_in(turn)["confirm"])
        self.assertEqual([c["name"] for c in self.client.calls], ["companion", "send"])
        await self.clock.advance(1.0)  # first poll -> reply -> commentary -> ack
        await self.clock.advance(3.0)  # paced audio; packet quiet does NOT finish speech
        self.assertEqual(fs.device.of_type("pet.reply.done"), [])
        await fs.feed(golden_in(turn)["stop"])
        outcome = await fs.finish()

        # Device frames: the FULL ordered list, byte for byte (literal golden JSON).
        self.assertEqual(
            fs.raw(),
            [
                gold["started"],
                gold["began"],
                gold["preview"],
                gold["accepted"],
                gold["reply_started"],
                gold["audio_a"],
                gold["audio_b"],
                gold["done"],
                gold["closed"],
            ],
        )
        self.assert_schema(fs)
        self.assertEqual(fs.device.close_codes, [1000])
        # reply.started was committed before ANY PCM, audio is the exact join, paced.
        types = fs.types()
        self.assertLess(
            types.index("pet.reply.started"), types.index("session.output_audio.delta")
        )
        audio = [
            base64.b64decode(f["delta"])
            for f in fs.device.of_type("session.output_audio.delta")
        ]
        self.assertEqual(b"".join(audio), PCM_A + PCM_B)
        sent_at = [
            t
            for t, f in zip(fs.device.times, fs.device.frames, strict=True)
            if f["type"] == "session.output_audio.delta"
        ]
        self.assertAlmostEqual(sent_at[1] - sent_at[0], len(PCM_A) / 48000, places=9)
        self.assertEqual(
            (outcome.reply_done, outcome.close_confirmed), ("stopped", True)
        )
        self.assertEqual(outcome.audio_out_bytes, len(PCM_A) + len(PCM_B))
        self.assertEqual(outcome.send_outcome, "accepted")

        # Provider side: exact start, forwarded capture audio, lossless commentary, one close.
        sent = provider.sent
        start = sent[0]
        self.assertRegex(start["event_id"], r"^gw_[0-9a-f0-9_]+$")
        self.assertEqual(
            start,
            {
                "type": "session.start",
                "event_id": start["event_id"],
                "session": {
                    "model": "gpt-live-1",
                    "store": False,
                    "audio": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "output": {"voice": "marin"},
                    },
                    "delegation": {"type": "client", "acknowledgement_delay_ms": 0},
                    "instructions": start["session"]["instructions"],
                },
            },
        )
        self.assertIn("Generate <|SpawnThinking|>", start["session"]["instructions"])
        forwarded = [f for f in sent if f["type"] == "session.input_audio.append"]
        self.assertEqual([f["audio"] for f in forwarded[:2]], [b64(b"\x01\x02" * 100)] * 2)
        self.assertTrue(forwarded[2:])
        self.assertTrue(all(base64.b64decode(f["audio"]) == bytes(4800) for f in forwarded[2:]))
        commentary = [f for f in sent if f["type"] == "session.commentary.append"]
        self.assertGreaterEqual(len(commentary), 3)  # a long reply: several chunks
        self.assertEqual("".join(c["content"] for c in commentary), LONG_REPLY)
        self.assertTrue(all(len(c["content"].encode()) <= 480 for c in commentary))
        self.assertEqual({c["delegation_id"] for c in commentary}, {DELEG})
        self.assertEqual(len({c["event_id"] for c in commentary}), len(commentary))
        self.assertEqual(sent[-1], {"type": "session.close"})
        self.assertEqual(provider.types().count("session.close"), 1)
        self.assertEqual(provider.close_calls, 1)

        # Unified: exactly listing, ONE send, one detail read; route/auth fixtures.
        calls = self.client.calls
        self.assertEqual(
            [(c["method"], c["target"]) for c in calls],
            [
                ("GET", COMPANION_TARGET),
                ("POST", SEND_TARGET),
                ("GET", DETAIL_TARGET),
            ],
        )
        for call in calls:
            self.assertEqual(call["origin"], Origin.parse(ORIGIN_URL))
            self.assertEqual(call["headers"]["Authorization"], "Bearer " + TOKEN)
            self.assertEqual(call["headers"]["X-Amplifier-Client"], CLIENT_ID)
        (post,) = self.client.posts
        self.assertEqual(
            post["body"],
            wire.send_command_request(SESSION_ID, turn, TEXT)[2],
        )
        body = json.loads(post["body"])
        # Normal native-client send semantics: listing revision is app-wide,
        # including unrelated progress, not an atomic idle-only admission token.
        self.assertNotIn("expectedRevision", body)
        self.assertNotIn("expectedRevision", body["args"])
        self.assertEqual(post["headers"]["Content-Type"], "application/json")

        # Journal: COMPLETE with the reply consumed, and no body of text left behind.
        record = self.record(turn)
        self.assertEqual(record["state"], "COMPLETE")
        self.assertIs(record["reply_consumed"], True)
        self.assertEqual(self.bridge.status()["binding_id"], self.binding.binding_id)

    async def test_t_flow_happy_short_reply_with_immediate_pcm_under_device_backpressure(
        self,
    ):
        self.client.reply_text = REPLY_TEXT  # one commentary chunk
        provider = PcmProvider(self.clock, (PCM_A, PCM_B))
        fs = self.new_session(provider)
        turn = await self.to_preview(fs)
        await fs.feed(self.confirm_frame(turn))
        block = asyncio.Event()
        fs.device.send_block = block  # the device stops draining before reply.started
        await self.clock.advance(1.0)
        self.assertEqual(len(provider.of_type("session.commentary.append")), 1)
        self.assertNotIn(
            "pet.reply.started", fs.types()
        )  # still queued behind the block
        self.assertNotIn("session.output_audio.delta", fs.types())
        block.set()
        await self.quiesce()
        await self.clock.advance(3.0)
        self.assertEqual(fs.device.of_type("pet.reply.done"), [])
        await fs.feed(golden_in(turn)["stop"])
        outcome = await fs.finish()
        types = fs.types()
        self.assertLess(
            types.index("pet.reply.started"), types.index("session.output_audio.delta")
        )
        audio = b"".join(
            base64.b64decode(f["delta"])
            for f in fs.device.of_type("session.output_audio.delta")
        )
        self.assertEqual(audio, PCM_A + PCM_B)  # the immediate PCM was NOT dropped
        self.assertEqual(outcome.dropped_audio_bytes, 0)
        self.assertEqual(outcome.reply_done, "stopped")
        self.assert_schema(fs)
        self.assertEqual(len(self.client.posts), 1)

    async def test_t_flow_happy_multibyte_transcript_and_reply_are_exact_on_every_wire(
        self,
    ):
        """Emoji + CJK + quote + tab out, a multibyte multi-chunk reply back, nothing lost."""
        self.client.reply_text = MB_REPLY
        provider = PcmProvider(self.clock, (PCM_A, PCM_B))
        fs = self.new_session(provider)
        turn = await self.to_preview(fs, deltas=(("m1", TEXT_MB, 0, 900),))
        gold = golden_out(turn)
        preview = (
            '{"type":"pet.turn.preview","turn_id":"'
            + turn
            + '","preview_revision":1,"text":'
            + TEXT_MB_JSON
            + ',"text_sha256":"'
            + TEXT_MB_SHA
            + '"}'
        )
        self.assertEqual(fs.raw()[-2:], [gold["began"], preview])  # exact text and hash
        self.assertEqual(self.client.calls, [])
        await fs.feed(self.confirm_frame(turn, sha=TEXT_MB_SHA))
        await self.clock.advance(1.0)
        await self.clock.advance(3.0)
        self.assertEqual(fs.device.of_type("pet.reply.done"), [])
        await fs.feed(golden_in(turn)["stop"])
        outcome = await fs.finish()
        self.assertEqual(
            fs.raw(),
            [
                gold["started"],
                gold["began"],
                preview,
                gold["accepted"],
                gold["reply_started"],
                gold["audio_a"],
                gold["audio_b"],
                gold["done"],
                gold["closed"],
            ],
        )
        self.assert_schema(fs)
        self.assertEqual(
            (outcome.reply_done, outcome.close_confirmed, outcome.send_outcome),
            ("stopped", True, "accepted"),
        )
        # The ONE POST uses ordinary native semantics. Neither app-global
        # listing revision nor the device's local preview revision is sent.
        (post,) = self.client.posts
        self.assertEqual(
            post["body"],
            wire.send_command_request(SESSION_ID, turn, TEXT_MB)[2],
        )
        body = json.loads(post["body"])
        self.assertNotIn("expectedRevision", body)
        self.assertNotIn("expectedRevision", body["args"])
        self.assertIn(TEXT_MB, strings_of(body))  # the exact transcript, decoded back
        # The reply: >= 3 chunks, each <= 480 UTF-8 bytes (not characters), joined EXACTLY.
        commentary = [
            c["content"] for c in provider.of_type("session.commentary.append")
        ]
        self.assertGreaterEqual(len(commentary), 3)
        self.assertGreater(len(MB_REPLY.encode("utf-8")), 3 * 480)
        self.assertGreater(len(MB_REPLY.encode("utf-8")), len(MB_REPLY))  # multibyte
        for chunk in commentary:
            self.assertLessEqual(len(chunk.encode("utf-8")), 480)
        self.assertEqual("".join(commentary), MB_REPLY)
        # Journal: COMPLETE with the reply consumed; neither text survives in any encoding.
        record = self.record(turn)
        self.assertEqual(
            (record["state"], record["reply_consumed"]), ("COMPLETE", True)
        )
        journal_bytes = b""
        for entry in os.scandir(self.base / "journal"):
            if entry.is_file():
                journal_bytes += Path(entry.path).read_bytes()
        scrubbed = journal_bytes.decode("utf-8", "replace")
        for secret in (TEXT_MB, TEXT_MB_JSON[1:-1], "\u4e16\u754c", "is right here"):
            self.assertNotIn(secret, scrubbed)
        self.assertNotIn(b"\\u4e16\\u754c", journal_bytes)  # nor as JSON escapes


# =================================================================================
# T-FLOW-REPLAY, BUSY, 409, UNCERTAIN, PARTIAL_COVERAGE
# =================================================================================
class RefusedSendFlowTest(FlowCase):
    async def test_t_flow_replay_a_second_confirm_after_accept_is_invalid_and_never_posts(
        self,
    ):
        provider = PcmProvider(self.clock, (PCM_A,))
        fs = self.new_session(provider)
        turn = await self.to_preview(fs)
        await fs.feed(self.confirm_frame(turn))
        self.assertEqual(len(self.client.posts), 1)
        await fs.feed(self.confirm_frame(turn, rev=1))  # old revision
        await fs.feed(self.confirm_frame(turn, rev=2))  # new revision
        await fs.feed(self.confirm_frame(turn, rev=2, sha="0" * 64))  # other digest
        self.assertEqual(fs.device.error_codes(), ["invalid_preview"] * 3)
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(len(fs.device.of_type("pet.turn.send_result")), 1)
        await self.clock.advance(1.0)
        await self.clock.advance(3.0)
        self.assertEqual(fs.device.of_type("pet.reply.done"), [])
        await fs.feed(golden_in(turn)["stop"])
        outcome = await fs.finish()
        self.assertEqual(outcome.reply_done, "stopped")  # the replays changed nothing
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.record(turn)["state"], "COMPLETE")
        self.assert_schema(fs)

    async def test_t_flow_replay_an_invalid_preview_confirm_still_makes_one_decision(
        self,
    ):
        fs = self.new_session()
        turn = await self.to_preview(fs)
        await fs.feed(
            self.confirm_frame(turn, sha="f" * 64)
        )  # not the previewed digest
        self.assertEqual(self.client.calls, [])  # nothing reached Unified
        self.assertEqual(self.command_names(), [])
        types = fs.types()
        self.assertLess(  # the app was SENDING-bound: rejected comes BEFORE the error
            types.index("pet.turn.send_result"), types.index("pet.error")
        )
        self.assertEqual(
            fs.device.of_type("pet.turn.send_result")[0]["state"], "rejected"
        )
        self.assertEqual(fs.device.error_codes(), ["invalid_preview"])
        await fs.finish()

    async def test_t_flow_busy_a_working_session_is_rejected_before_the_error_no_post(
        self,
    ):
        self.client.listing_status = "working"
        self.client.listing_execution = True
        fs = self.new_session()
        turn = await self.to_preview(fs)
        await fs.feed(self.confirm_frame(turn))
        outcome = await fs.finish()
        self.assertEqual(self.client.posts, [])
        self.assertEqual([c["name"] for c in self.client.calls], ["companion"])
        self.assertEqual(self.command_names(), [])  # nothing persisted, nothing sent
        tail = fs.types()[-3:]
        self.assertEqual(tail, ["pet.turn.send_result", "pet.error", "session.closed"])
        self.assertEqual(
            fs.device.of_type("pet.turn.send_result")[0]["state"], "rejected"
        )
        self.assertEqual(fs.device.error_codes(), ["busy"])
        self.assertEqual(outcome.send_outcome, "refused")
        self.assert_schema(fs)

    async def test_t_flow_409_is_a_rejection_with_no_retry_no_speech_and_a_close(self):
        self.client.post_response = Response(409, b"{}")
        fs = self.new_session(PcmProvider(self.clock, (PCM_A,)))
        turn = await self.to_preview(fs)
        await fs.feed(self.confirm_frame(turn))
        await self.clock.advance(5.0)  # a retry would show up as another POST
        outcome = await fs.finish()
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(
            fs.device.of_type("pet.turn.send_result")[0]["state"], "rejected"
        )
        self.assertEqual(fs.types()[-2:], ["pet.turn.send_result", "session.closed"])
        self.assertEqual(fs.device.of_type("pet.reply.started"), [])
        self.assertEqual(fs.device.of_type("session.output_audio.delta"), [])
        self.assertEqual(fs.provider.of_type("session.commentary.append"), [])
        self.assertEqual(self.client.named("detail"), [])  # no reply was ever polled
        self.assertEqual(self.record(turn)["state"], "REJECTED")
        self.assertEqual(outcome.send_outcome, "rejected")

    async def test_t_flow_uncertain_504_retains_the_gate_and_the_next_session_is_busy(
        self,
    ):
        self.client.post_response = Response(504, b"")
        fs = self.new_session()
        turn = await self.to_preview(fs)
        await fs.feed(self.confirm_frame(turn))
        await self.clock.advance(5.0)
        outcome = await fs.finish()
        self.assertEqual(len(self.client.posts), 1)  # never retried
        self.assertEqual(
            fs.device.of_type("pet.turn.send_result")[0]["state"], "uncertain"
        )
        self.assertEqual(self.record(turn)["state"], "UNCERTAIN")
        self.assertEqual(outcome.send_outcome, "uncertain")
        # The NEXT session cannot send: the durable gate holds until an operator acks.
        second = self.new_session()
        second.go()
        await second.feed(ASSISTANT_START)
        await second.feed(golden_in()["begin"])
        self.assertEqual(second.device.error_codes(), ["busy"])
        self.assertEqual(self.bridge.status()["records"], {"UNCERTAIN": 1})
        self.assertEqual(len(self.client.posts), 1)
        await second.finish()

    async def test_t_flow_partial_coverage_never_speaks_and_ends_unavailable_at_the_deadline(
        self,
    ):
        self.client.partial = True
        provider = PcmProvider(self.clock, (PCM_A,))
        fs = self.new_session(provider)
        turn = await self.to_preview(fs)
        await fs.feed(self.confirm_frame(turn))
        await self.clock.advance(61.0)
        outcome = await fs.finish()
        details = self.client.named("detail")
        self.assertGreaterEqual(
            len(details), 4 * 10
        )  # polled every second, 4 pages each
        self.assertEqual(len(details) % 4, 0)
        self.assertEqual(fs.provider.of_type("session.commentary.append"), [])
        self.assertEqual(fs.device.of_type("pet.reply.started"), [])
        self.assertEqual(fs.device.of_type("session.output_audio.delta"), [])
        self.assertEqual(fs.device.error_codes(), ["deadline"])
        self.assertEqual(fs.device.of_type("session.closed"), [{"type": "session.closed"}])
        self.assertTrue(outcome.close_confirmed)  # API close, not completed speech
        done = fs.device.of_type("pet.reply.done")
        self.assertEqual([d["state"] for d in done], ["unavailable"])
        types = fs.types()
        self.assertLess(types.index("pet.error"), types.index("pet.reply.done"))
        self.assertNotEqual(self.record(turn)["state"], "COMPLETE")
        self.assertEqual(outcome.reply_done, "unavailable")
        self.assertEqual(len(self.client.posts), 1)
        self.assert_schema(fs)


class RealRefusalFlowTest(FlowCase):
    """A refusal produced by the REAL ``Bridge`` (no FakeOps, no patched result).

    The real ``Journal`` / ``Bridge`` / ``UnifiedOps`` / ``PetSession`` run unchanged; the
    only intervention is the state change a hostile moment would cause between preview and
    confirm (the journal's limit shrinks / a Stop lands from another path).
    """

    async def refused_confirm(self, intervene: Any) -> tuple[Any, ...]:
        real, results = self.bridge.confirm_send, []

        def spy(*args: Any, **kwargs: Any) -> Any:
            results.append(real(*args, **kwargs))  # the genuine core answer, unchanged
            return results[-1]

        fs = self.new_session(PcmProvider(self.clock, (PCM_A,)))
        turn = await self.to_preview(fs)
        intervene()
        with mock.patch.object(self.bridge, "confirm_send", spy):
            await fs.feed(self.confirm_frame(turn))
            await self.clock.advance(5.0)  # a retry would show up as another call
            outcome = await fs.finish()
        return fs, turn, results, outcome

    def assert_known_unsent_rejection(
        self, fs: FlowSession, turn: str, outcome: gc.SessionOutcome
    ) -> None:
        self.assertEqual(self.client.posts, [])  # nothing was sent
        self.assertEqual(self.command_names(), [])  # no command record exists
        self.assertEqual([c["name"] for c in self.client.calls], ["companion"])
        # the SENDING-bound app hears "rejected" BEFORE the error, then the session closes
        self.assertEqual(
            fs.types()[-3:], ["pet.turn.send_result", "pet.error", "session.closed"]
        )
        self.assertEqual(
            fs.device.of_type("pet.turn.send_result")[0]["state"], "rejected"
        )
        self.assertEqual(fs.device.error_codes(), ["invalid_preview"])
        self.assertEqual(
            outcome.send_outcome, "refused"
        )  # known-unsent, not "uncertain"
        self.assertEqual(
            fs.device.of_type("pet.reply.started"), []
        )  # nothing is spoken
        self.assertEqual(fs.device.of_type("session.output_audio.delta"), [])
        self.assertEqual(fs.provider.of_type("session.commentary.append"), [])
        self.assertEqual(self.client.named("detail"), [])
        self.assert_schema(fs)

    async def test_t_flow_a_real_journal_full_refusal_is_known_unsent_and_rejected(
        self,
    ):
        # begin needs room for one more record; shrink the REAL limit to the current count
        # so the later claim of the command record finds the journal full.
        fs, turn, results, outcome = await self.refused_confirm(
            lambda: setattr(self.journal, "_max", self.journal.count())
        )
        self.assertEqual(
            results, [core.CommandResult(turn, "refused", "journal_full", False)]
        )
        self.assert_known_unsent_rejection(fs, turn, outcome)

    async def test_t_flow_a_real_stop_from_another_path_refusal_is_known_unsent_too(
        self,
    ):
        fs, turn, results, outcome = await self.refused_confirm(self.bridge.stop)
        self.assertEqual(
            results, [core.CommandResult(turn, "refused", "stopped", False)]
        )
        self.assert_known_unsent_rejection(fs, turn, outcome)


class ReplyEdgeFlowTest(FlowCase):
    async def test_t_flow_ack_only_with_zero_pcm_is_unavailable_never_completed(self):
        self.client.reply_text = REPLY_TEXT
        fs = self.new_session()  # the provider acknowledges but never speaks
        turn = await self.to_preview(fs)
        await fs.feed(self.confirm_frame(turn))
        await self.clock.advance(1.0)
        await self.clock.advance(3.0)  # past the former packet-quiet period
        self.assertEqual(fs.device.of_type("pet.reply.done"), [])
        await self.clock.advance(58.0 - (self.clock.now() - fs.session._accepted_t))
        outcome = await fs.finish()
        done = fs.device.of_type("pet.reply.done")
        self.assertEqual([d["state"] for d in done], ["unavailable"])
        self.assertEqual(fs.device.of_type("session.output_audio.delta"), [])
        self.assertEqual(outcome.audio_out_bytes, 0)
        self.assertEqual(
            self.record(turn)["state"], "COMPLETE"
        )  # the core text was released
        self.assert_schema(fs)

    async def _eof_during_pcm(
        self, clean: bool
    ) -> tuple[FlowSession, gc.SessionOutcome, str]:
        self.client.reply_text = REPLY_TEXT
        provider = PcmProvider(self.clock, (PCM_A,))
        fs = self.new_session(provider)
        turn = await self.to_preview(fs)
        await fs.feed(self.confirm_frame(turn))
        await self.clock.advance(1.0)  # poll -> reply -> commentary -> ack -> the PCM
        self.assertEqual(len(fs.device.of_type("session.output_audio.delta")), 1)
        self.assertEqual(
            fs.device.of_type("pet.reply.done"), []
        )  # the 1.5 s quiet is NOT over
        provider.push_closed(clean=clean)  # the provider link ends mid-reply
        await self.quiesce()
        await self.clock.advance(
            3.0
        )  # well past the quiet period: nothing may "complete"
        return fs, await fs.finish(), turn

    def assert_eof_during_pcm(
        self, fs: FlowSession, outcome: gc.SessionOutcome, turn: str
    ) -> None:
        done = [d["state"] for d in fs.device.of_type("pet.reply.done")]
        self.assertEqual(done, ["unavailable"])  # never "completed"
        self.assertNotIn("completed", done)
        self.assertEqual(outcome.reply_done, "unavailable")
        self.assertEqual(fs.device.of_type("session.closed"), [])  # no false close ack
        self.assertFalse(outcome.close_confirmed)
        self.assertEqual(fs.device.close_codes, [1011])  # an abnormal close, not 1000
        self.assertEqual(  # the audio already paced stays; nothing is faked after the EOF
            [c["type"] for c in fs.device.frames[-3:]],
            ["pet.reply.started", "session.output_audio.delta", "pet.reply.done"],
        )
        self.assertEqual(outcome.audio_out_bytes, len(PCM_A))
        self.assertEqual(len(self.client.posts), 1)  # one POST, never retried
        self.assertEqual(outcome.send_outcome, "accepted")
        self.assert_schema(fs)

    async def test_t_flow_unclean_provider_eof_during_pcm_never_completes_or_confirms(
        self,
    ):
        self.assert_eof_during_pcm(*await self._eof_during_pcm(False))

    async def test_t_flow_clean_provider_close_during_pcm_is_not_a_completion_either(
        self,
    ):
        self.assert_eof_during_pcm(*await self._eof_during_pcm(True))

    async def test_t_flow_two_distinct_delegation_ids_fail_closed_after_the_send(self):
        fs = self.new_session(PcmProvider(self.clock, (PCM_A,)))
        turn = await self.to_preview(fs)
        await fs.push(delegation("dlg_two"))  # a second DIFFERENT id: ambiguous
        await fs.feed(self.confirm_frame(turn))
        await self.clock.advance(1.0)
        await fs.finish()
        self.assertEqual(fs.device.error_codes(), ["no_delegation"])
        self.assertEqual(fs.provider.of_type("session.commentary.append"), [])
        self.assertEqual(fs.device.of_type("session.output_audio.delta"), [])
        done = fs.device.of_type("pet.reply.done")
        self.assertEqual([d["state"] for d in done], ["unavailable"])
        self.assertEqual(len(self.client.posts), 1)  # the human's confirm sent once
        self.assert_schema(fs)

    async def test_t_flow_disconnect_during_send_stays_uncertain_and_is_never_retried(
        self,
    ):
        self.client.post_response = Response(504, b"")
        fs = self.new_session()
        turn = await self.to_preview(fs)
        gate = self.hold_posts()
        fs.device.feed(self.confirm_frame(turn))
        await self.quiesce()
        await until(self.client.post_entered.is_set)
        fs.device.disconnect()
        await self.quiesce()
        outcome = await fs.finish()
        self.assertEqual(outcome.send_outcome, "inflight")
        self.assertEqual(fs.device.of_type("pet.turn.send_result"), [])
        gate.set()
        await self.runner.wait_idle()
        result = self.runner.take_result(outcome.confirm_job)
        self.assertEqual(result.outcome, "uncertain")  # never relabelled rejected
        with self.assertRaises(gc.RunnerError) as ctx:  # reconciled exactly once
            self.runner.take_result(outcome.confirm_job)
        self.assertEqual(ctx.exception.code, "runner_result_unavailable")
        self.assertEqual(self.record(turn)["state"], "UNCERTAIN")
        await self.clock.advance(5.0)
        self.assertEqual(len(self.client.posts), 1)  # no retry, no new id


# =================================================================================
# T-FLOW-LEGACY, DELEGATION_NOSEND
# =================================================================================
class NoSendFlowTest(FlowCase):
    async def test_t_flow_legacy_device_never_posts_previews_or_polls_and_closes_normally(
        self,
    ):
        provider = FakeProviderLink(self.clock)
        fs = self.new_session(provider)
        fs.go()
        await fs.feed(ASSISTANT_START)
        await fs.feed(ASSISTANT_GREETING)  # the old firmware's greeting is ignored
        for _ in range(3):
            await fs.feed(audio_frame(100))  # READY: never forwarded
        await fs.push(delta("l1", "send this to the session now", 0, 800))
        await fs.push(delegation("dlg_legacy"))
        await self.clock.advance(5.0)
        await fs.feed('{"type":"session.close"}')
        outcome = await fs.finish()
        self.assertEqual(self.client.calls, [])  # zero POST, zero GET of any kind
        self.assertEqual(self.command_names(), [])
        self.assertEqual(fs.types(), ["session.started", "session.closed"])
        self.assertEqual(provider.types(), ["session.start", "session.close"])
        self.assertEqual(
            (outcome.turn_started, outcome.send_outcome, outcome.close_confirmed),
            (False, None, True),
        )
        self.assertEqual(fs.device.close_codes, [1000])
        self.assert_schema(fs)

    async def test_t_flow_delegation_nosend_hints_and_action_text_never_send(self):
        fs = self.new_session()
        turn = await self.to_preview(
            fs,
            deltas=(("a1", "please send it to production and run the deploy", 0, 900),),
        )
        await fs.push(delegation("dlg_other"))  # a later, different hint in PREVIEW
        await fs.push(delegation(DELEG))
        await self.clock.advance(61.0)  # the human never confirms
        await fs.finish()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.command_names(), [])
        self.assertEqual(fs.device.of_type("pet.turn.send_result"), [])
        self.assertEqual(len(fs.device.of_type("pet.turn.preview")), 1)
        # The 58 s work limit leaves cleanup time within the same absolute 60 s cap.
        # A confirmed provider finalization says nothing about speech completion.
        self.assertEqual(fs.device.error_codes(), ["deadline"])
        self.assertEqual(fs.device.of_type("session.closed"), [{"type": "session.closed"}])
        self.assertEqual(self.bridge.status()["records"], {})
        self.assertTrue(wire.is_canonical_uuid4(turn))
        self.assert_schema(fs)


# =================================================================================
# T-FLOW-STOP-DURING-SEND and T-FLOW-NEXT-SESSION-RESUME
# =================================================================================
class StopFlowTest(FlowCase):
    async def test_t_flow_stop_during_send_posts_once_records_accepted_then_suppresses(
        self,
    ):
        provider = PcmProvider(self.clock, (PCM_A,))
        fs = self.new_session(provider)
        turn = await self.to_preview(fs)
        gate = self.hold_posts()
        fs.device.feed(self.confirm_frame(turn))
        await self.quiesce()
        await until(self.client.post_entered.is_set)
        self.assertEqual(len(self.client.posts), 1)  # the POST is in flight, blocked
        await fs.feed(golden_in(turn)["stop"])
        # Stop is immediate and local: the session closes while the POST is still held.
        outcome = await fs.finish()
        self.assertEqual(outcome.send_outcome, "inflight")
        self.assertIsNotNone(outcome.confirm_job)
        self.assertEqual(fs.device.of_type("pet.turn.send_result"), [])
        self.assertEqual(fs.device.of_type("session.output_audio.delta"), [])
        self.assertEqual(fs.device.of_type("pet.reply.started"), [])
        self.assertEqual(provider.of_type("session.commentary.append"), [])
        self.assertEqual(self.client.named("detail"), [])
        # Release: exactly ONE POST completes; the urgent stop ran after it.
        gate.set()
        await self.runner.wait_idle()
        self.assertEqual(len(self.client.posts), 1)
        result = self.runner.take_result(outcome.confirm_job)
        self.assertEqual((result.outcome, result.command_id), ("accepted", turn))
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")  # recorded, not spoken
        status = self.bridge.status()
        self.assertTrue(status["stopped"])
        self.assertEqual(status["records"], {"ACCEPTED": 1})
        self.assertEqual(fs.device.of_type("session.output_audio.delta"), [])
        # The NEXT session resumes and its first begin suppresses the old epoch.
        second = self.new_session()
        second.go()
        await second.feed(ASSISTANT_START)
        self.assertFalse(self.bridge.status()["stopped"])
        await second.feed(golden_in()["begin"])
        self.assertEqual(self.record(turn)["state"], "SUPPRESSED")
        self.assertEqual(len(second.device.of_type("pet.turn.began")), 1)
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(second.device.of_type("session.output_audio.delta"), [])
        await second.feed(golden_in()["stop"])
        await second.finish()

    async def test_t_flow_next_session_resume_only_after_start_and_provider_started(
        self,
    ):
        first = self.new_session()
        first.go()
        await first.feed(ASSISTANT_START)
        await first.feed(golden_in()["stop"])
        await first.finish()
        await self.runner.wait_idle()
        self.assertTrue(self.bridge.status()["stopped"])  # Stop closed the gate
        self.assertEqual(self.client.calls, [])
        # A new session: still stopped until ITS start and the provider's session.started.
        second = self.new_session(auto_start=False)
        seen: list[tuple[list[str], list[str]]] = []
        real_resume = self.ops.resume

        def resume_spy() -> (
            None
        ):  # what had the device and provider seen at that moment?
            seen.append((second.device.types(), second.provider.types()))
            real_resume()

        with mock.patch.object(self.ops, "resume", resume_spy):
            second.go()
            await self.quiesce()
            self.assertEqual(seen, [])  # connected: nothing resumed
            self.assertTrue(self.bridge.status()["stopped"])
            await second.feed(ASSISTANT_START)
            self.assertEqual(second.provider.types(), ["session.start"])
            self.assertEqual(seen, [])  # the provider has not started yet
            self.assertTrue(self.bridge.status()["stopped"])
            self.assertEqual(second.device.of_type("session.started"), [])
            await second.push({"type": "session.started"})
        self.assertFalse(self.bridge.status()["stopped"])  # resumed, then announced
        # resume ran exactly once, BEFORE any device frame, and after the provider's start
        self.assertEqual(seen, [([], ["session.start"])])
        self.assertEqual(second.types(), ["session.started"])
        await second.feed(golden_in()["stop"])
        await second.finish()


# =================================================================================
# T-FLOW-CANARY
# =================================================================================
class CanaryFlowTest(FlowCase):
    async def test_t_flow_canary_nothing_sensitive_is_left_behind_by_the_core(self):
        self.client.reply_text = REPLY_TEXT
        provider = PcmProvider(self.clock, (AUDIO_CANARY,))
        fs = self.new_session(provider)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            turn = await self.to_preview(fs, deltas=(("c1", TEXT_CANARY, 0, 900),))
            await fs.feed(
                self.confirm_frame(
                    turn, sha=hashlib.sha256(TEXT_CANARY.encode()).hexdigest()
                )
            )
            await self.clock.advance(1.0)
            await self.clock.advance(3.0)
            self.assertEqual(fs.device.of_type("pet.reply.done"), [])
            await fs.feed(golden_in(turn)["stop"])
            outcome = await fs.finish()
        self.assertEqual(self.record(turn)["state"], "COMPLETE")
        self.assertEqual(outcome.reply_done, "stopped")
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        journal_bytes = b""
        for entry in os.scandir(self.base / "journal"):
            if entry.is_file():
                journal_bytes += Path(entry.path).read_bytes()
        views = [
            repr(self.bridge.status()),
            repr(self.runner.retained()),
            repr(outcome),
            repr(fs.session),
            repr(self.ops),
            repr(self.runner),
            journal_bytes.decode("utf-8", "replace"),
        ]
        secrets = (
            TEXT_CANARY,
            "REPLYCANARY",
            AUDIO_CANARY.decode("latin-1"),
            b64(AUDIO_CANARY),
            TOKEN,
        )
        for view in views:
            for secret in secrets:
                self.assertNotIn(secret, view)
        # The credential store holds the token BY DESIGN (a private fixture, mode 0600):
        # it is deliberately not part of the absence claim above.
        # A core claim releases text to the rendering path; it does not prove
        # that the user heard audio, or even that the gateway played any.
        self.assertEqual(self.record(turn).get("reply_outcome"), "claimed_for_render")


# =================================================================================
# The CLI reaches the REAL serve_main (spies stand in for TLS, site, provider, signals)
# =================================================================================
class CliServeRealWiringTest(tgs.ServerOfflineCase):
    """Runs the REAL ``serve_main``: bind/listen/create_server raise as well as connect."""

    def test_the_offline_guard_really_intercepts_bind_listen_and_create_server(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        with self.assertRaises(AssertionError):
            sock.bind(("127.0.0.1", 0))  # a real bind would succeed: the guard stops it
        with self.assertRaises(AssertionError):
            sock.listen()

        async def listen() -> None:
            await asyncio.get_running_loop().create_server(
                asyncio.Protocol, "127.0.0.1", 0
            )

        with self.assertRaises(AssertionError):
            asyncio.run(listen())

    def test_serve_through_the_cli_starts_and_stops_with_only_the_metadata_lines(self):
        path = tgs.StateFixture(self.base).build()
        spies = tgs.Spies()
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            ["serve", "--config", str(path)],
            env={},
            stdout=out,
            stderr=err,
            serve_deps=spies.deps(FakeLoopClock()),
        )
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertEqual(
            out.getvalue().splitlines(),
            [
                "gateway: listening on 10.1.2.3:8941 for session ...0a1b2c3d",
                "gateway: stopped",
            ],
        )
        self.assertEqual(spies.events, ["site", "wait", "closer"])
        # No provider dial; exactly the one startup attach attempt.
        self.assertEqual((spies.provider_calls, spies.client_calls), (0, 1))

    def test_serve_through_the_cli_refuses_a_missing_config_with_a_fixed_code(self):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            ["serve", "--config", str(self.base / "absent.json")],
            env={},
            stdout=out,
            stderr=err,
        )
        self.assertEqual((code, out.getvalue()), (1, ""))
        self.assertEqual(err.getvalue(), "error: config_invalid\n")
        self.assertFalse(re.search(r"Traceback|absent", err.getvalue()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
