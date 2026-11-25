"""Gateway protocol state: strict JSON, device frames and builders (pure stdlib, no I/O).

This module (with ``gateway_provider``) defines the exact v1 device protocol of the
host voice gateway.  It imports no socket, asyncio, TLS or HTTP capability; the
transport scanner in ``tests/test_transport.py`` pins that its capability surface is
empty.  Every failure carries a fixed code only: no text, audio, token or digest is
ever copied into an error.  Offline tests are not a live or hardware result.

Encoding of every frame we emit: ``json.dumps(obj, ensure_ascii=False,
allow_nan=False, separators=(",", ":"))`` with the key order shown, text frames only,
UTF-8 length strictly below ``MAX_DEVICE_FRAME_BYTES`` (96 KiB).

Device -> gateway (inbound; ``parse_device_frame``)
    Strict decode: UTF-8 length <= 98304 bytes, JSON object, no duplicate keys, no
    NaN/Infinity, nesting depth <= 16, ``type`` is a string, no lone surrogates.
    Anything else (including a binary WebSocket message) is ``unknown_command``.

    ``session.start``           {type, event_id, session} plus optional ``max_seconds``
        (integer 5..60; only shortens the gateway's local media deadline). ``event_id``
        matches ``[A-Za-z0-9][A-Za-z0-9._:-]{0,63}``.  ``session`` keys are a subset of
        {model, store, input, instructions, audio, delegation} and include {model,
        store, audio}; ``store`` is JSON false; ``audio`` is exactly
        {"format":{"type":"audio/pcm","rate":24000},"output":{"voice":<str>}} with
        ``rate`` an int (not bool).  ``input`` (list <= 16 of objects),
        ``instructions`` (str <= 16384 chars) and ``delegation`` (object) are bounded
        and then DISCARDED: the host builds its own provider start.  A model or voice
        other than the configured one is ``model_mismatch``; every shape violation is
        ``unknown_command`` (shape is checked first).
    ``session.commentary.append``  legacy greeting; keys exactly {type, event_id,
        delegation_id, content}; ``delegation_id`` JSON null, ``content`` 1..4000
        chars.  Accepted and ignored (never forwarded).
    ``session.input_audio.append`` keys exactly {type, audio}; ``audio`` standard
        base64 (validated, padded, CANONICAL: re-encoding the decoded bytes must give
        the same string, so non-zero pad bits are refused), 2..4800 decoded bytes,
        even length (PCM16).
    ``pet.turn.begin``          keys exactly {type}.
    ``pet.converse.begin``      keys exactly {type}; selects running conversation.
    ``pet.turn.capture_stop``   keys exactly {type, turn_id}; ``turn_id`` canonical
        lowercase UUID4.
    ``pet.turn.send_confirm``   keys exactly {type, turn_id, text_sha256,
        preview_revision}; ``text_sha256`` 64 lowercase hex; ``preview_revision`` int
        (not bool) 1..2147483647.
    ``pet.stop``                keys exactly {type}.
    ``session.close``           keys exactly {type}.

Gateway -> device (outbound builders; each returns ``str``)
    ``session.started``              {"type"}
    ``session.closed``               {"type"}
    ``session.output_audio.delta``   {"type","delta"}  (standard padded base64 PCM16,
        2..4800 bytes, even; the key is ``delta``)
    ``pet.turn.began``               {"type","turn_id","safe_session_label",
        "session_id_short"}  (label <= 40 chars, short id = last 8 lowercase hex)
    ``pet.turn.preview``             {"type","turn_id","preview_revision","text",
        "text_sha256"}  (text <= 4000 code points and <= 16000 UTF-8 bytes; the digest
        must be exactly the SHA-256 of the text)
    ``pet.turn.preview_invalidated`` {"type","turn_id","reason"}  (late_input |
        stopped | expired)
    ``pet.turn.send_result``         {"type","turn_id","state"}  (accepted | rejected
        | uncertain)
    ``pet.reply.started``            {"type","turn_id","rendering"}  (rendering is the
        constant ``spoken_rendering_not_verbatim``)
    ``pet.reply.done``               {"type","turn_id","state"}  (completed |
        unavailable | stopped)
    ``pet.error``                    {"type","code"}  (``PET_ERROR_CODES`` only)
    ``pet.converse.began``           {"type","safe_session_label","session_id_short"}
    ``pet.converse.state``           {"type","state"} (CONVERSE_STATES only)
    ``pet.converse.notice``          {"type","code"} (CONVERSE_NOTICES only)
    An argument outside these sets raises ``ValueError`` (a programming error).

    Provider endings close the device WebSocket without a reason string. A validated
    same-session ``close_requested``, ``expired`` or ``remote_hangup`` emits 1000;
    1008 is upstream authentication/authorization refusal, a provider safety
    filter, or local pre-dial safety refusal. 1002 indicates invalid/incompatible
    upstream protocol (not a claim about the device's frames); 1011 is an ambiguous
    provider/network failure
    including ``connection_lost``. A 1000 says only that the voice session ended,
    not that our close was acknowledged or that a request or reply completed.
    These distinctions are best-effort: a lost close frame carries no cause.
    Existing build-11 firmware ignores close codes; reconnecting firmware must
    interpret them without replaying audio or commands.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any

from . import companion, wire

ROUTE = "/v1/pet/live"
MAX_DEVICE_FRAME_BYTES = 98304
MAX_PROVIDER_FRAME_BYTES = 1048576
MAX_DEVICE_DEPTH = 16
MAX_PROVIDER_DEPTH = 32
MAX_AUDIO_APPEND_BYTES = 4800
AUDIO_CHUNK_BYTES = 4800
AUDIO_BYTES_PER_S = 48000
AUDIO_QUEUE_CAP = 1048576
CAPTURE_QUEUE_CAP = 96000
SESSION_CAP_S = 60.0
SESSION_WORK_S = 58.0  # same absolute cap; reserve two seconds for close confirmation
CONVERSE_LIMIT_S = 600.0
CONVERSE_WORK_S = 598.0  # reserve two seconds for close within the total cap, as one-turn does
CONVERSE_IDLE_S = 90.0
CONVERSE_RECOGNITION_HINT_S = 12.0
CONVERSE_REPLY_RETRIEVAL_S = 15.0  # exact completion observed, matching row not yet visible
CONVERSE_PROGRESS_FIRST_S = 5.0
CONVERSE_FOLLOWUP_HINT_INTERVAL_S = 30.0
CONVERSE_PROGRESS_RENDER_MAX_S = 10.0
CONVERSE_PROGRESS_SENDING = "One moment. I'm sending your request."
CONVERSE_PROGRESS_WORKING = "I'm waiting for the reply."
CONVERSE_PROGRESS_FOLLOWUP_UNSENT = "That follow-up wasn't sent. Ask again after the reply."
CONVERSE_FOLLOWUP_NOTICE_INTERVAL_S = 4.0
# Display-only speech-end heuristic, not a provider EOS or acoustic proof.
CONVERSE_SPEECH_QUIET_S = 1.5
CONVERSE_RENDER_MAX_S = 60.0
START_DEADLINE_S = 25.0
CLOSE_DEADLINE_S = 15.0
AWAIT_START_S = 10.0
SETTLE_AFTER_STOP_S = 1.2
SETTLE_QUIET_S = 1.5
SETTLE_MAX_S = 6.0
# Arrival heuristic, not provider EOS. Recorded rig traces (tests/rig_arrivals.json)
# peak at 254 ms relative lag JUST BEFORE a frame on the working 160 ms RTT
# link; 300 ms leaves 46 ms headroom. Ordinary best-offset improvement is <=18 ms;
# require >25 ms of genuinely new minima, not recovery from an upward excursion.
CONVERSE_INPUT_LAG_S = 0.3
CONVERSE_INPUT_GAIN_S = 0.025
CONVERSE_INPUT_CHECK_S = 0.1
CONVERSE_INPUT_MAX_S = 6.0  # cumulative detected input wait per request, not per stall
CONVERSE_INPUT_DRAIN_GRACE_S = 2.0  # once only for a demonstrably draining long stall
POLL_INTERVAL_S = 1.0
POLL_MAX_S = 60.0
ACK_WAIT_S = 10.0
INBOX_CAP = 256
MAX_TRANSCRIPT_CHARS = 4000
MAX_SEGMENTS = 128
COMMENTARY_CHUNK_BYTES = 480
MAX_COMMENTARY_CHUNKS = 64
PREVIEW_MAX_CHARS = 4000
PREVIEW_MAX_BYTES = 16000
PREVIEW_REVISION_MAX = 2147483647
SAFE_LABEL_MAX = 40
MAX_PET_TOKENS = 16
AUTH_FAIL_LIMIT = 5
AUTH_WINDOW_S = 60.0
AUTH_REGISTRY_CAP = 256
DEFAULT_MODEL = "gpt-live-1"
DEFAULT_VOICE = "marin"

PET_ERROR_CODES = frozenset(
    {
        "busy",
        "unknown_command",
        "model_mismatch",
        "no_speech",
        "unstable_transcript",
        "expired",
        "no_delegation",
        "reply_unavailable",
        "deadline",
        "overflow",
        "close_unconfirmed",
        "invalid_preview",
    }
)
SEND_RESULT_STATES = frozenset({"accepted", "rejected", "uncertain"})
REPLY_DONE_STATES = frozenset({"completed", "unavailable", "stopped"})
INVALIDATED_REASONS = frozenset({"late_transcript", "stopped", "expired"})
CONVERSE_STATES = frozenset({"listening", "asking", "working", "answering"})
CONVERSE_NOTICES = frozenset({
    "busy", "not_sent", "send_uncertain", "reply_unavailable", "idle_timeout", "time_limit",
    "no_words_yet",
})

_RENDERING = "spoken_rendering_not_verbatim"
_EVENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SHORT8_RE = re.compile(r"[0-9a-f]{8}")
_START_REQUIRED = frozenset({"model", "store", "audio"})
_START_ALLOWED = _START_REQUIRED | {"input", "instructions", "delegation"}
_START_INPUT_MAX = 16
_START_INSTRUCTIONS_MAX = 16384
_GREETING_MAX = 4000
_PCM_RATE = 24000


class GatewayProtocolError(Exception):
    """A protocol violation.  Carries a fixed code only; ``str(exc)`` is the code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class DeviceFrame:
    """A validated device frame.  Ignored start fields are never retained."""

    kind: str
    event_id: str | None = None
    turn_id: str | None = None
    text_sha256: str | None = None
    preview_revision: int | None = None
    b64: str | None = field(default=None, repr=False)
    pcm_len: int = 0
    pcm_has_signal: bool = False  # at least one nonzero sample, not evidence of speech
    max_seconds: int | None = None  # a start-only upper bound, not a provider setting


# -- strict JSON ---------------------------------------------------------------------
def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _check_text(value: str) -> None:
    if not value.isascii():
        value.encode("utf-8")  # a lone surrogate raises UnicodeEncodeError


def _check_tree(root: Any, max_depth: int) -> None:
    """Depth and UTF-8 validity of a parsed value, iteratively (no recursion)."""
    stack: list[tuple[Any, int]] = [(root, 0)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, str):
            _check_text(value)
        elif isinstance(value, dict):
            if depth + 1 > max_depth:
                raise ValueError("too deep")
            for key, item in value.items():
                _check_text(key)
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            if depth + 1 > max_depth:
                raise ValueError("too deep")
            stack.extend((item, depth + 1) for item in value)


def strict_loads(text: str | bytes, *, max_bytes: int, max_depth: int) -> Any:
    """Parse one JSON value strictly, or raise ``ValueError("invalid_json")``.

    Rejects: input that is not ``str``/``bytes``, more than ``max_bytes`` UTF-8 bytes,
    invalid UTF-8 or lone surrogates (also escaped ones), a BOM, duplicate object keys,
    NaN/Infinity and overflowing floats, trailing data and nesting deeper than
    ``max_depth`` containers.
    """
    try:
        if isinstance(text, str):
            raw = text.encode("utf-8")
            decoded = text
        elif isinstance(text, bytes):
            raw = text
            decoded = text.decode("utf-8")
        else:
            raise TypeError("not text")
        if len(raw) > max_bytes:
            raise ValueError("too large")
        value = json.loads(
            decoded,
            parse_float=_finite_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicates,
        )
        _check_tree(value, max_depth)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("invalid_json") from None
    return value


# -- small validators ----------------------------------------------------------------
def is_event_id(value: Any) -> bool:
    """True for a protocol event/delegation id: ``[A-Za-z0-9][A-Za-z0-9._:-]{0,63}``."""
    return isinstance(value, str) and _EVENT_ID_RE.fullmatch(value) is not None


def _device_pcm(value: Any) -> bytes | None:
    """Validated PCM16; never retain decoded samples on the frame."""
    if not isinstance(value, str):
        return None
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    size = len(decoded)
    if size < 2 or size > MAX_AUDIO_APPEND_BYTES or size % 2:
        return None
    if base64.b64encode(decoded).decode("ascii") != value:
        return None  # non-zero pad bits: only the canonical encoding is accepted
    return decoded


def audio_b64_len(value: Any) -> int | None:
    """Decoded length of a valid device audio string (2..4800, even), else ``None``."""
    pcm = _device_pcm(value)
    return None if pcm is None else len(pcm)


def valid_short8(session_id: Any) -> str | None:
    """Last 8 characters of the session id when they are lowercase hex, else ``None``.

    The suffix is validated AS-IS: it is never lowercased, aliased or widened.
    """
    if not isinstance(session_id, str):
        return None
    tail = session_id[-8:]
    return tail if _SHORT8_RE.fullmatch(tail) is not None else None


def safe_label(value: Any) -> str | None:
    """``value`` when it already is its own sanitized display form of 1..40 chars."""
    if not isinstance(value, str) or not 1 <= len(value) <= SAFE_LABEL_MAX:
        return None
    return value if companion.sanitize_title(value) == value else None


# -- device frames -------------------------------------------------------------------
def _unknown() -> GatewayProtocolError:
    return GatewayProtocolError("unknown_command")


def _is_int(value: Any) -> bool:
    return type(value) is int


def _check_audio_session(audio: Any) -> str:
    """Validate the nested ``audio`` object exactly; return the requested voice."""
    if not isinstance(audio, dict) or set(audio) != {"format", "output"}:
        raise _unknown()
    fmt, output = audio["format"], audio["output"]
    if not isinstance(fmt, dict) or set(fmt) != {"type", "rate"}:
        raise _unknown()
    if (
        fmt["type"] != "audio/pcm"
        or not _is_int(fmt["rate"])
        or fmt["rate"] != _PCM_RATE
    ):
        raise _unknown()
    if not isinstance(output, dict) or set(output) != {"voice"}:
        raise _unknown()
    voice = output["voice"]
    if not isinstance(voice, str):
        raise _unknown()
    return voice


def _check_ignored_start_fields(session: dict[str, Any]) -> None:
    if "input" in session:
        items = session["input"]
        if not isinstance(items, list) or len(items) > _START_INPUT_MAX:
            raise _unknown()
        if not all(isinstance(item, dict) for item in items):
            raise _unknown()
    if "instructions" in session:
        text = session["instructions"]
        if not isinstance(text, str) or len(text) > _START_INSTRUCTIONS_MAX:
            raise _unknown()
    if "delegation" in session and not isinstance(session["delegation"], dict):
        raise _unknown()


def _parse_start(obj: dict[str, Any], model: str, voice: str) -> DeviceFrame:
    if set(obj) - {"max_seconds"} != {"type", "event_id", "session"} or not is_event_id(obj["event_id"]):
        raise _unknown()
    limit = obj.get("max_seconds")
    if "max_seconds" in obj and (type(limit) is not int or not 5 <= limit <= 60):
        raise _unknown()
    session = obj["session"]
    if not isinstance(session, dict):
        raise _unknown()
    keys = set(session)
    if not (_START_REQUIRED <= keys <= _START_ALLOWED) or session["store"] is not False:
        raise _unknown()
    requested_model = session["model"]
    if not isinstance(requested_model, str):
        raise _unknown()
    requested_voice = _check_audio_session(session["audio"])
    _check_ignored_start_fields(session)
    if requested_model != model or requested_voice != voice:
        raise GatewayProtocolError("model_mismatch")
    return DeviceFrame(kind="start", event_id=obj["event_id"], max_seconds=limit)


def _parse_greeting(obj: dict[str, Any], _model: str, _voice: str) -> DeviceFrame:
    if set(obj) != {"type", "event_id", "delegation_id", "content"}:
        raise _unknown()
    content = obj["content"]
    if (
        not is_event_id(obj["event_id"])
        or obj["delegation_id"] is not None
        or not isinstance(content, str)
        or not 1 <= len(content) <= _GREETING_MAX
    ):
        raise _unknown()
    return DeviceFrame(kind="greeting", event_id=obj["event_id"])


def _parse_audio(obj: dict[str, Any], _model: str, _voice: str) -> DeviceFrame:
    if set(obj) != {"type", "audio"}:
        raise _unknown()
    pcm = _device_pcm(obj["audio"])
    if pcm is None:
        raise _unknown()
    return DeviceFrame(kind="audio", b64=obj["audio"], pcm_len=len(pcm),
                       pcm_has_signal=pcm.count(0) != len(pcm))


def _bare(kind: str):
    def parse(obj: dict[str, Any], _model: str, _voice: str) -> DeviceFrame:
        if set(obj) != {"type"}:
            raise _unknown()
        return DeviceFrame(kind=kind)

    return parse


def _parse_capture_stop(obj: dict[str, Any], _model: str, _voice: str) -> DeviceFrame:
    if set(obj) != {"type", "turn_id"} or not wire.is_canonical_uuid4(obj["turn_id"]):
        raise _unknown()
    return DeviceFrame(kind="capture_stop", turn_id=obj["turn_id"])


def _parse_send_confirm(obj: dict[str, Any], _model: str, _voice: str) -> DeviceFrame:
    if set(obj) != {"type", "turn_id", "text_sha256", "preview_revision"}:
        raise _unknown()
    sha, revision = obj["text_sha256"], obj["preview_revision"]
    if (
        not wire.is_canonical_uuid4(obj["turn_id"])
        or not isinstance(sha, str)
        or _SHA256_RE.fullmatch(sha) is None
        or not _is_int(revision)
        or not 1 <= revision <= PREVIEW_REVISION_MAX
    ):
        raise _unknown()
    return DeviceFrame(
        kind="send_confirm",
        turn_id=obj["turn_id"],
        text_sha256=sha,
        preview_revision=revision,
    )


_DEVICE_PARSERS = {
    "session.start": _parse_start,
    "session.commentary.append": _parse_greeting,
    "session.input_audio.append": _parse_audio,
    "pet.turn.begin": _bare("begin"),
    "pet.converse.begin": _bare("converse_begin"),
    "pet.turn.capture_stop": _parse_capture_stop,
    "pet.turn.send_confirm": _parse_send_confirm,
    "pet.stop": _bare("stop"),
    "session.close": _bare("close"),
}


def parse_device_frame(text: str, *, model: str, voice: str) -> DeviceFrame:
    """Validate one device text frame or raise ``GatewayProtocolError``.

    Codes: ``unknown_command`` (anything malformed, unknown or oversize) and
    ``model_mismatch`` (a well-formed start naming another model or voice).
    """
    if not isinstance(text, str):
        raise _unknown()
    try:
        obj = strict_loads(
            text, max_bytes=MAX_DEVICE_FRAME_BYTES, max_depth=MAX_DEVICE_DEPTH
        )
    except ValueError:
        raise _unknown() from None
    if not isinstance(obj, dict):
        raise _unknown()
    frame_type = obj.get("type")
    parser = _DEVICE_PARSERS.get(frame_type) if isinstance(frame_type, str) else None
    if parser is None:
        raise _unknown()
    return parser(obj, model, voice)


# -- outbound builders ---------------------------------------------------------------
def _frame(obj: dict[str, Any]) -> str:
    text = json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(text.encode("utf-8")) >= MAX_DEVICE_FRAME_BYTES:
        raise ValueError("frame too large")
    return text


def _turn(turn_id: Any) -> str:
    if not wire.is_canonical_uuid4(turn_id):
        raise ValueError("invalid turn id")
    return turn_id


def _member(value: Any, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("value outside the pinned set")
    return value


def build_session_started() -> str:
    return _frame({"type": "session.started"})


def build_session_closed() -> str:
    return _frame({"type": "session.closed"})


def build_audio_delta(pcm: bytes) -> str:
    if (
        type(pcm) is not bytes
        or not 2 <= len(pcm) <= MAX_AUDIO_APPEND_BYTES
        or len(pcm) % 2
    ):
        raise ValueError("invalid pcm")
    delta = base64.b64encode(pcm).decode("ascii")
    return _frame({"type": "session.output_audio.delta", "delta": delta})


def build_turn_began(turn_id: str, label: str, short8: str) -> str:
    if safe_label(label) is None:
        raise ValueError("invalid label")
    if not isinstance(short8, str) or _SHORT8_RE.fullmatch(short8) is None:
        raise ValueError("invalid short id")
    return _frame(
        {
            "type": "pet.turn.began",
            "turn_id": _turn(turn_id),
            "safe_session_label": label,
            "session_id_short": short8,
        }
    )


def build_converse_began(label: str, short8: str) -> str:
    if safe_label(label) is None:
        raise ValueError("invalid label")
    if not isinstance(short8, str) or _SHORT8_RE.fullmatch(short8) is None:
        raise ValueError("invalid short id")
    return _frame({
        "type": "pet.converse.began",
        "safe_session_label": label,
        "session_id_short": short8,
    })


def build_converse_state(state: str) -> str:
    return _frame({"type": "pet.converse.state", "state": _member(state, CONVERSE_STATES)})


def build_converse_notice(code: str) -> str:
    return _frame({"type": "pet.converse.notice", "code": _member(code, CONVERSE_NOTICES)})


def build_preview(turn_id: str, rev: int, text: str, sha: str) -> str:
    if not _is_int(rev) or not 1 <= rev <= PREVIEW_REVISION_MAX:
        raise ValueError("invalid revision")
    if not isinstance(text, str) or len(text) > PREVIEW_MAX_CHARS:
        raise ValueError("invalid preview text")
    if len(text.encode("utf-8")) > PREVIEW_MAX_BYTES:
        raise ValueError("invalid preview text")
    if sha != hashlib.sha256(text.encode("utf-8")).hexdigest():
        raise ValueError("digest is not the sealed text digest")
    return _frame(
        {
            "type": "pet.turn.preview",
            "turn_id": _turn(turn_id),
            "preview_revision": rev,
            "text": text,
            "text_sha256": sha,
        }
    )


def build_preview_invalidated(turn_id: str, reason: str) -> str:
    return _frame(
        {
            "type": "pet.turn.preview_invalidated",
            "turn_id": _turn(turn_id),
            "reason": _member(reason, INVALIDATED_REASONS),
        }
    )


def build_send_result(turn_id: str, state: str) -> str:
    return _frame(
        {
            "type": "pet.turn.send_result",
            "turn_id": _turn(turn_id),
            "state": _member(state, SEND_RESULT_STATES),
        }
    )


def build_reply_started(turn_id: str) -> str:
    return _frame(
        {
            "type": "pet.reply.started",
            "turn_id": _turn(turn_id),
            "rendering": _RENDERING,
        }
    )


def build_reply_done(turn_id: str, state: str) -> str:
    return _frame(
        {
            "type": "pet.reply.done",
            "turn_id": _turn(turn_id),
            "state": _member(state, REPLY_DONE_STATES),
        }
    )


def build_pet_error(code: str) -> str:
    return _frame({"type": "pet.error", "code": _member(code, PET_ERROR_CODES)})


# -- transcript staging --------------------------------------------------------------
def _is_ms(value: Any) -> bool:
    return type(value) is int and value >= 0


class TranscriptStager:
    """Stage provider transcript deltas before anything reaches the core.

    ``add`` keeps identical duplicates out (``"duplicate"``), refuses a conflicting
    duplicate (same id, different text or time), an overlapping time range, more than
    ``MAX_SEGMENTS`` segments and more than ``MAX_TRANSCRIPT_CHARS`` characters.  A
    refused delta changes nothing.  Zero-length segments never overlap anything.
    ``ordered()`` sorts by ``(start_ms, end_ms, event_id)``; the repr shows counts only.
    """

    def __init__(self) -> None:
        self._segments: dict[str, tuple[str, int, int]] = {}
        self._chars = 0

    @property
    def count(self) -> int:
        return len(self._segments)

    @property
    def chars(self) -> int:
        return self._chars

    def __repr__(self) -> str:
        return f"TranscriptStager(segments={self.count}, chars={self._chars})"

    def add(self, event_id: str, text: str, start_ms: int, end_ms: int) -> str:
        """Returns ``"added"`` or ``"duplicate"``; raises ``GatewayProtocolError``.

        Codes: ``invalid``, ``conflict``, ``too_many``, ``too_long``, ``overlap``.
        """
        if (
            not is_event_id(event_id)
            or not isinstance(text, str)
            or not _is_ms(start_ms)
            or not _is_ms(end_ms)
            or end_ms < start_ms
        ):
            raise GatewayProtocolError("invalid")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            raise GatewayProtocolError("invalid") from None
        known = self._segments.get(event_id)
        if known is not None:
            if known == (text, start_ms, end_ms):
                return "duplicate"
            raise GatewayProtocolError("conflict")
        if len(self._segments) >= MAX_SEGMENTS:
            raise GatewayProtocolError("too_many")
        if self._chars + len(text) > MAX_TRANSCRIPT_CHARS:
            raise GatewayProtocolError("too_long")
        for _other_text, other_start, other_end in self._segments.values():
            if (
                start_ms < end_ms
                and other_start < other_end
                and start_ms < other_end
                and other_start < end_ms
            ):
                raise GatewayProtocolError("overlap")
        self._segments[event_id] = (text, start_ms, end_ms)
        self._chars += len(text)
        return "added"

    def ordered(self) -> tuple[tuple[str, str], ...]:
        """``(event_id, text)`` pairs sorted by ``(start_ms, end_ms, event_id)``."""
        rows = sorted(
            (start, end, event_id, text)
            for event_id, (text, start, end) in self._segments.items()
        )
        return tuple((event_id, text) for _start, _end, event_id, text in rows)

    def text(self) -> str:
        """The compatible whole text: the ordered delta texts joined without a separator."""
        return "".join(text for _event_id, text in self.ordered())


# -- lossless commentary chunks and audio pacing -------------------------------------
_SENTENCE_ENDS = (". ", "! ", "? ", "\n")


def _window(rest: str) -> str:
    """Longest prefix of ``rest`` that fits in 480 UTF-8 bytes without splitting a char."""
    head = rest[:COMMENTARY_CHUNK_BYTES]
    raw = head.encode("utf-8")
    if len(raw) <= COMMENTARY_CHUNK_BYTES:
        return head
    return raw[:COMMENTARY_CHUNK_BYTES].decode("utf-8", errors="ignore")


def _cut(window: str) -> int:
    """Cut position in a full window: after the last sentence end, else last whitespace."""
    cut = max(
        (window.rfind(end) + len(end) if end in window else 0) for end in _SENTENCE_ENDS
    )
    if cut:
        return cut
    for index in range(len(window) - 1, -1, -1):
        if window[index].isspace():
            return index + 1
    return len(window)


def _split(text: str, prefer_boundaries: bool) -> list[str] | None:
    """Greedy 480-byte chunks, or ``None`` when more than 64 would be needed."""
    chunks: list[str] = []
    rest = text
    while True:
        window = _window(rest)
        if len(window) == len(rest):
            chunks.append(rest)
            return chunks
        cut = _cut(window) if prefer_boundaries else len(window)
        chunks.append(rest[:cut])
        rest = rest[cut:]
        if len(chunks) >= MAX_COMMENTARY_CHUNKS:
            return None


def chunk_commentary(text: str) -> tuple[str, ...]:
    """Split ``text`` into lossless chunks of at most 480 UTF-8 bytes each.

    ``"".join(chunks) == text`` always; a code point is never split.  Preferred cuts:
    after the last sentence end in the window, else after the last whitespace, else at
    the window end.  When those cuts would need more than ``MAX_COMMENTARY_CHUNKS``
    chunks, the whole text is cut instead at the maximal code-point-safe 480-byte
    window, so every supported reply (<= 4000 code points, <= 16000 UTF-8 bytes, at
    most 34 maximal chunks) is deliverable.  Raises ``ValueError`` for empty/non-text
    input or when even the maximal cuts exceed ``MAX_COMMENTARY_CHUNKS``.
    """
    if not isinstance(text, str) or not text:
        raise ValueError("invalid commentary text")
    text.encode("utf-8")  # a lone surrogate raises UnicodeEncodeError (a ValueError)
    chunks = _split(text, True) or _split(text, False)
    if chunks is None:
        raise ValueError("too many commentary chunks")
    return tuple(chunks)


def split_pcm(pcm: bytes, size: int = AUDIO_CHUNK_BYTES) -> tuple[bytes, ...]:
    """Split PCM16 into chunks of at most ``size`` bytes; every chunk length is even."""
    if type(pcm) is not bytes or len(pcm) % 2:
        raise ValueError("pcm must be bytes of even length")
    if type(size) is not int or size <= 0 or size % 2:
        raise ValueError("chunk size must be a positive even int")
    return tuple(pcm[offset : offset + size] for offset in range(0, len(pcm), size))


def next_send_time(
    prev_next: float | None, now: float, nbytes: int
) -> tuple[float, float]:
    """Pacing at ``AUDIO_BYTES_PER_S``: returns ``(send_at, new_next)``.

    ``send_at`` is ``now`` when idle (no schedule, or the schedule already passed) and the
    scheduled time otherwise; ``new_next = send_at + nbytes / AUDIO_BYTES_PER_S``.
    """
    if type(nbytes) is not int or nbytes <= 0:
        raise ValueError("nbytes must be a positive int")
    send_at = now if prev_next is None else max(prev_next, now)
    return send_at, send_at + nbytes / AUDIO_BYTES_PER_S


# -- gateway config document ---------------------------------------------------------
_CRED_RE = re.compile(r"device-[0-9a-f]{32}")
_MODEL_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")
_VOICE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_PORT_RE = re.compile(r"[1-9][0-9]{0,4}")
_ALLOWED_NETWORKS = tuple(
    ipaddress.IPv4Network(net)
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
)
_TAILNET_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")
_CONFIG_REQUIRED = frozenset(
    {
        "version",
        "state_root",
        "credential_ref",
        "session_id",
        "session_label",
        "bind_host",
        "port",
        "host_header",
        "tls_cert_file",
        "tls_key_file",
        "pet_token_sha256",
    }
)
_CONFIG_OPTIONAL = frozenset(
    {
        "model", "voice", "provider_key_file", "provider_key_env", "auth_root",
        "tailnet_bind_host", "tailnet_fqdn",
    }
)
_PATH_MAX = 4096


@dataclass(frozen=True)
class GatewayConfig:
    """A validated gateway config.  Secrets, hashes and the full session id are hidden."""

    state_root: str
    credential_ref: str = field(repr=False)
    session_id: str = field(repr=False)
    session_id_short: str
    session_label: str
    bind_host: str
    port: int
    host_header: str
    tls_cert_file: str
    tls_key_file: str
    pet_token_sha256: tuple[str, ...] = field(repr=False)
    model: str = DEFAULT_MODEL
    voice: str = DEFAULT_VOICE
    provider_key_file: str | None = field(default=None, repr=False)
    provider_key_env: str | None = field(default=None, repr=False)
    # Existing shared private credentials may live outside state_root/journal.
    # This selects their location, never rebinds/copies/re-pairs or grants access.
    auth_root: str | None = field(default=None, repr=False)
    tailnet_bind_host: str | None = None
    tailnet_fqdn: str | None = None


def _private_ipv4(value: Any) -> bool:
    """Exact-form IPv4 literal inside RFC1918 or CGNAT (never 0.0.0.0 or loopback)."""
    if not isinstance(value, str):
        return False
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return str(address) == value and any(address in net for net in _ALLOWED_NETWORKS)


def _host_header_ok(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    ip, sep, port = value.partition(":")
    if not _private_ipv4(ip):
        return False
    return not sep or (_PORT_RE.fullmatch(port) is not None and int(port) <= 65535)


def _tailnet_fqdn_ok(value: Any) -> bool:
    """The device's lowercase dotted certificate hostname, never an IP or URL."""
    if not isinstance(value, str) or not 1 <= len(value) <= 63:
        return False
    labels = value.split(".")
    return (
        len(labels) >= 2
        and any("a" <= char <= "z" for char in value)
        and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                is not None for label in labels)
    )


def _tailnet_binding_ok(doc: dict[str, Any]) -> bool:
    if "tailnet_bind_host" not in doc or "tailnet_fqdn" not in doc:
        return "tailnet_bind_host" not in doc and "tailnet_fqdn" not in doc
    address = doc["tailnet_bind_host"]
    if not _private_ipv4(address) or not _tailnet_fqdn_ok(doc["tailnet_fqdn"]):
        return False
    ip = ipaddress.IPv4Address(address)
    return (
        ip in _TAILNET_NETWORK
        and ip not in (_TAILNET_NETWORK.network_address, _TAILNET_NETWORK.broadcast_address)
        and address != doc["bind_host"]
    )


def _abs_path(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= _PATH_MAX:
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return False
    return os.path.isabs(value) and not {".", ".."} & set(value.split("/"))


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def valid_model(value: Any) -> bool:
    """True for a model name in the config format ``[a-z0-9][a-z0-9.-]{0,63}``."""
    return _matches(_MODEL_RE, value)


def valid_voice(value: Any) -> bool:
    """True for a voice name in the config format ``[a-z][a-z0-9_-]{0,31}``."""
    return _matches(_VOICE_RE, value)


def _tokens_ok(value: Any) -> bool:
    return (
        isinstance(value, list)
        and 1 <= len(value) <= MAX_PET_TOKENS
        and all(_matches(_SHA256_RE, item) for item in value)
        and len(set(value)) == len(value)
    )


def _config_defects(doc: dict[str, Any]) -> bool:
    """True when any field of an otherwise well-keyed document is invalid."""
    checks = (
        type(doc["version"]) is int and doc["version"] == 1,
        _abs_path(doc["state_root"]),
        _abs_path(doc["auth_root"]) if "auth_root" in doc else True,
        _matches(_CRED_RE, doc["credential_ref"]),
        wire.valid_session_id(doc["session_id"]),
        safe_label(doc["session_label"]) is not None,
        _private_ipv4(doc["bind_host"]),
        type(doc["port"]) is int and 1024 <= doc["port"] <= 65535,
        _host_header_ok(doc["host_header"]),
        _tailnet_binding_ok(doc),
        _abs_path(doc["tls_cert_file"]) and _abs_path(doc["tls_key_file"]),
        _tokens_ok(doc["pet_token_sha256"]),
        _matches(_MODEL_RE, doc.get("model", DEFAULT_MODEL)),
        _matches(_VOICE_RE, doc.get("voice", DEFAULT_VOICE)),
        _abs_path(doc["provider_key_file"]) if "provider_key_file" in doc else True,
        _matches(_ENV_NAME_RE, doc["provider_key_env"])
        if "provider_key_env" in doc
        else True,
    )
    return not all(checks)


def parse_config(doc: Any) -> GatewayConfig:
    """Validate a gateway config document exactly or raise ``GatewayProtocolError``.

    ``config_invalid`` for every key/format defect; ``core_binding_required`` when the
    otherwise valid session id has no lowercase-hex last-eight suffix.  The error never
    quotes any value.
    """
    invalid = GatewayProtocolError("config_invalid")
    if not isinstance(doc, dict):
        raise invalid
    keys = set(doc)
    if not _CONFIG_REQUIRED <= keys <= _CONFIG_REQUIRED | _CONFIG_OPTIONAL:
        raise invalid
    if ("provider_key_file" in doc) == ("provider_key_env" in doc):
        raise invalid  # exactly one provider key source
    if _config_defects(doc):
        raise invalid
    short8 = valid_short8(doc["session_id"])
    if short8 is None:
        raise GatewayProtocolError("core_binding_required")
    return GatewayConfig(
        state_root=doc["state_root"],
        credential_ref=doc["credential_ref"],
        session_id=doc["session_id"],
        session_id_short=short8,
        session_label=doc["session_label"],
        bind_host=doc["bind_host"],
        port=doc["port"],
        host_header=doc["host_header"],
        tls_cert_file=doc["tls_cert_file"],
        tls_key_file=doc["tls_key_file"],
        pet_token_sha256=tuple(doc["pet_token_sha256"]),
        model=doc.get("model", DEFAULT_MODEL),
        voice=doc.get("voice", DEFAULT_VOICE),
        provider_key_file=doc.get("provider_key_file"),
        provider_key_env=doc.get("provider_key_env"),
        auth_root=doc.get("auth_root"),
        tailnet_bind_host=doc.get("tailnet_bind_host"),
        tailnet_fqdn=doc.get("tailnet_fqdn"),
    )


# -- the private ``.env`` spelling of the SAME config document -------------------------
# One variable per config key.  This table only renames and re-types text; every value is
# then judged by ``parse_config`` exactly as a JSON document is (there is no second
# validator).  ``POCKET_GATEWAY_PROVIDER_KEY`` holds the key itself and becomes
# ``provider_key_env`` naming that same variable, so "exactly one key source" still holds.
ENV_PROVIDER_KEY = "POCKET_GATEWAY_PROVIDER_KEY"
_ENV_TOKENS = "POCKET_GATEWAY_PET_TOKEN_SHA256"
_ENV_INT = {"POCKET_GATEWAY_CONFIG_VERSION": "version", "POCKET_GATEWAY_PORT": "port"}
_ENV_TEXT = {
    "POCKET_GATEWAY_STATE_ROOT": "state_root",
    "POCKET_GATEWAY_AUTH_ROOT": "auth_root",
    "POCKET_GATEWAY_CREDENTIAL_REF": "credential_ref",
    "POCKET_GATEWAY_SESSION_ID": "session_id",
    "POCKET_GATEWAY_SESSION_LABEL": "session_label",
    "POCKET_GATEWAY_BIND_HOST": "bind_host",
    "POCKET_GATEWAY_HOST_HEADER": "host_header",
    "POCKET_GATEWAY_TAILNET_BIND_HOST": "tailnet_bind_host",
    "POCKET_GATEWAY_TAILNET_FQDN": "tailnet_fqdn",
    "POCKET_GATEWAY_TLS_CERT_FILE": "tls_cert_file",
    "POCKET_GATEWAY_TLS_KEY_FILE": "tls_key_file",
    "POCKET_GATEWAY_MODEL": "model",
    "POCKET_GATEWAY_VOICE": "voice",
    "POCKET_GATEWAY_PROVIDER_KEY_FILE": "provider_key_file",
}
ENV_CONFIG_NAMES = frozenset(_ENV_TEXT) | frozenset(_ENV_INT) | {_ENV_TOKENS, ENV_PROVIDER_KEY}
_ENV_LINE_RE = re.compile(r"([A-Z][A-Z0-9_]{0,63})=(.*)")
_ENV_EDGE = frozenset(" \"'")


def env_config_document(text: Any) -> tuple[dict[str, Any], str | None]:
    """Translate private ``NAME=value`` text into the document ``parse_config`` validates.

    Returns ``(document, inline provider key or None)``; the caller must pass the document
    to ``parse_config`` and hide the key.  Lines are separated by LF only.  A line is blank
    (spaces only), a comment (``#`` in the first column) or ``NAME=value`` with a name from
    ``ENV_CONFIG_NAMES``.  The value is literal text to the end of the line: no quoting,
    escapes, expansion, ``export`` or inline comment.  It is refused when empty, when it
    starts or ends with a space or a quote character, or when it contains a space followed
    by ``#`` (an attempted inline comment).  The version and port are canonical decimal
    integers; token hashes are separated by commas.

    Any other line, an unknown or repeated name, or a control character (CR and TAB
    included) is ``config_invalid``.  The error never quotes a name or a value.
    """
    invalid = GatewayProtocolError("config_invalid")
    if not isinstance(text, str):
        raise invalid
    values: dict[str, str] = {}
    for line in text.split("\n"):
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in line):
            raise invalid
        if not line.strip(" ") or line.startswith("#"):
            continue
        match = _ENV_LINE_RE.fullmatch(line)
        if match is None:
            raise invalid
        name, value = match.group(1), match.group(2)
        if name not in ENV_CONFIG_NAMES or name in values or not value:
            raise invalid
        if value[0] in _ENV_EDGE or value[-1] in _ENV_EDGE or " #" in value:
            raise invalid
        values[name] = value
    doc: dict[str, Any] = {}
    inline_key: str | None = None
    for name, value in values.items():
        if name in _ENV_INT:
            if _PORT_RE.fullmatch(value) is None:
                raise invalid
            doc[_ENV_INT[name]] = int(value)
        elif name == _ENV_TOKENS:
            doc["pet_token_sha256"] = value.split(",")
        elif name == ENV_PROVIDER_KEY:
            doc["provider_key_env"] = ENV_PROVIDER_KEY
            inline_key = value
        else:
            doc[_ENV_TEXT[name]] = value
    return doc, inline_key


__all__ = [
    "ACK_WAIT_S",
    "AUDIO_BYTES_PER_S",
    "AUDIO_CHUNK_BYTES",
    "AUDIO_QUEUE_CAP",
    "AUTH_FAIL_LIMIT",
    "AUTH_REGISTRY_CAP",
    "AUTH_WINDOW_S",
    "AWAIT_START_S",
    "CAPTURE_QUEUE_CAP",
    "CLOSE_DEADLINE_S",
    "CONVERSE_IDLE_S",
    "CONVERSE_INPUT_CHECK_S",
    "CONVERSE_INPUT_GAIN_S",
    "CONVERSE_INPUT_LAG_S",
    "CONVERSE_INPUT_MAX_S",
    "CONVERSE_LIMIT_S",
    "CONVERSE_NOTICES",
    "CONVERSE_PROGRESS_FIRST_S",
    "CONVERSE_FOLLOWUP_HINT_INTERVAL_S",
    "CONVERSE_PROGRESS_RENDER_MAX_S",
    "CONVERSE_PROGRESS_SENDING",
    "CONVERSE_PROGRESS_WORKING",
    "CONVERSE_RENDER_MAX_S",
    "CONVERSE_REPLY_RETRIEVAL_S",
    "CONVERSE_SPEECH_QUIET_S",
    "CONVERSE_STATES",
    "CONVERSE_WORK_S",
    "COMMENTARY_CHUNK_BYTES",
    "DEFAULT_MODEL",
    "DEFAULT_VOICE",
    "ENV_CONFIG_NAMES",
    "ENV_PROVIDER_KEY",
    "INBOX_CAP",
    "INVALIDATED_REASONS",
    "MAX_AUDIO_APPEND_BYTES",
    "MAX_COMMENTARY_CHUNKS",
    "MAX_DEVICE_DEPTH",
    "MAX_DEVICE_FRAME_BYTES",
    "MAX_PET_TOKENS",
    "MAX_PROVIDER_DEPTH",
    "MAX_PROVIDER_FRAME_BYTES",
    "MAX_SEGMENTS",
    "MAX_TRANSCRIPT_CHARS",
    "PET_ERROR_CODES",
    "POLL_INTERVAL_S",
    "POLL_MAX_S",
    "PREVIEW_MAX_BYTES",
    "PREVIEW_MAX_CHARS",
    "PREVIEW_REVISION_MAX",
    "REPLY_DONE_STATES",
    "ROUTE",
    "SAFE_LABEL_MAX",
    "SEND_RESULT_STATES",
    "SESSION_CAP_S",
    "SESSION_WORK_S",
    "SETTLE_AFTER_STOP_S",
    "SETTLE_MAX_S",
    "SETTLE_QUIET_S",
    "START_DEADLINE_S",
    "DeviceFrame",
    "GatewayConfig",
    "GatewayProtocolError",
    "TranscriptStager",
    "audio_b64_len",
    "build_audio_delta",
    "build_converse_began",
    "build_converse_notice",
    "build_converse_state",
    "build_pet_error",
    "build_preview",
    "build_preview_invalidated",
    "build_reply_done",
    "build_reply_started",
    "build_send_result",
    "build_session_closed",
    "build_session_started",
    "build_turn_began",
    "chunk_commentary",
    "env_config_document",
    "is_event_id",
    "next_send_time",
    "parse_config",
    "parse_device_frame",
    "safe_label",
    "split_pcm",
    "strict_loads",
    "valid_model",
    "valid_short8",
    "valid_voice",
]
