"""Exact GPT-Live provider wire for the host gateway (pure stdlib, no I/O).

Four builders and one classifier; there is NO generic "send this frame" function, so a
device can never make the host proxy an arbitrary provider command.  Nothing here opens a
socket or imports a network capability.  Offline tests are not a live provider result.

Gateway -> provider (builders return ``str``; compact JSON, ``ensure_ascii=False``)
    ``build_provider_start(event_id, model, voice)``::

        {"type":"session.start","event_id":E,"session":{"model":M,"store":false,
         "audio":{"format":{"type":"audio/pcm","rate":24000},"output":{"voice":V}},
         "delegation":{"type":"client","acknowledgement_delay_ms":0}}}

      This is the host-built shape plus a fixed frontend policy (the device's own ``input``, ``instructions``
      and Responses ``delegation`` are never forwarded; no reasoning model, no Responses).
    ``build_provider_audio(b64)``   ``{"type":"session.input_audio.append","audio":B64}``
      B64 is the already validated device string, forwarded verbatim (2..4800 decoded
      bytes, even).
    ``build_commentary(event_id, delegation_id, content)``
      ``{"type":"session.commentary.append","event_id":E,"delegation_id":D,"content":C}``
      with C 1..480 UTF-8 bytes.
    ``build_provider_close()``      ``{"type":"session.close"}``

Provider -> gateway (``classify_provider_event`` never raises)
    Strict decode (<= 1 MiB, one JSON object, no duplicate keys, no NaN, depth <= 32,
    ``type`` a string) or kind ``invalid``.  Exact event types only; a recognised type
    whose frame carries ANY key outside its documented envelope
    is REJECTED with the kind shown, never acted on and never forwarded (R3).  Known
    Lifecycle events validate a bounded SessionResource projection, never retain
    or forward its instructions/history/client metadata. Server event IDs and
    optional client echoes are distinct. Required timestamps and close usage are
    validated; primary audio has only type/delta (not sideband timestamps).
    Contract: https://developers.openai.com/api/reference/resources/live/primary-websocket
    Documentation compatibility is not live provider qualification.

    ``session.started`` -> ``started``;  ``session.closed`` -> ``closed``  (unknown
        key: ``invalid``)
    ``session.output_audio.delta``  -> ``audio`` (PCM16 bytes, even, >= 2, canonical
        standard base64 only) or ``invalid_audio`` (also for an unknown key)
    ``session.input_transcript.delta`` -> ``transcript`` (event id, text, start/end ms)
        through ``wire.parse_live_event``, or ``invalid_transcript`` (also for an
        unknown key)
    ``session.delegation.created`` -> ``delegation`` (delegation id, a hint only) or
        ``ignored`` when malformed or carrying an unknown key
    ``session.commentary.appended`` -> ``ack`` when ``client_event_id`` is a valid event
        id echo, else ``ack_uncorrelated``; an unknown key is ``invalid``
    ``error`` -> ``error`` with the fixed local code ``provider_error``; the provider's
        code and message are never kept anywhere; an unknown key or a non-object
        ``error`` is ``invalid``
    anything else -> ``ignored``.  There is NO ``done``/``final`` kind: the provider has
    no final-turn event and none is ever invented.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import gateway_state as _state
from . import wire

PROVIDER_URL = "wss://api.openai.com/v1/live/sessions"
FOLLOWUP_TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"

_COMMENTARY_MAX_BYTES = 480
PROVIDER_ERROR_CODE = "provider_error"  # the only error code ever kept (fixed, local)

# A voice frontend must know WHEN to request its application backend. Selecting
# delegation.type alone does not force a per-utterance handoff. This fixed policy
# is host-owned; callers cannot substitute device prompts or grant tool authority.
# https://developers.openai.com/api/docs/guides/live-prompting#delegation
_FRONTEND_INSTRUCTIONS = """You are the voice frontend for Amplifier Unified, not an independent task agent.
Backchannel policy: Listen quietly. Do not greet or give substantive answers on your own.
Interruption policy: Yield when interrupted. Stopping speech does not cancel backend work.
Delegation policy:
Backend tools:
- Amplifier Unified: the selected conversation owns prior context, reasoning, tools and work.
Generate <|SpawnThinking|> when:
- The user asks a question about earlier reports, existing work, attention or a new task.
- A correction changes the requested work.
Do not generate <|SpawnThinking|> when:
- The user only greets you or asks you to repeat host-supplied commentary.
Generate <|SpawnThinking|> before answering a substantive question.
While waiting, do not guess results or invent access to prior reports. Do not say that
an utterance was heard, queued, sent, or accepted unless that status is in host-supplied
commentary. Do not promise to queue or send a later request on your own.
Render only host-supplied commentary as the backend answer, without adding unsupported facts.
The host alone checks permissions and the device mode's send policy before any action.
The host enforces explicit transcript confirmation in one-turn preview mode; in a
running conversation, the user's mic-on choice enables sending recognised requests.
A delegation request is not authorization, confirmation, or proof work has started."""

# Source-known keys per recognised provider event type (R3).  Anything else is rejected.
_DELEGATION_KEYS = frozenset({"id", "type", "target"})
_ERROR_DETAIL_KEYS = frozenset({"type", "code", "message", "param", "client_event_id"})
_ENVELOPE_KEYS = frozenset({"type", "event_id", "client_event_id"})
_SESSION_KEYS = frozenset({
    "id", "expires_at", "model", "status", "audio", "client", "delegation",
    "input", "instructions", "safety_identifier", "store", "triggers",
})
_CLOSE_REASONS = frozenset({
    "close_requested", "expired", "content", "remote_hangup", "connection_lost",
})
_KNOWN_KEYS: dict[str, frozenset[str]] = {
    "session.started": _ENVELOPE_KEYS | {"session"},
    "session.closed": _ENVELOPE_KEYS | {"session", "reason", "usage"},
    "session.output_audio.delta": frozenset({"type", "delta"}),
    wire.LIVE_TRANSCRIPT_DELTA: frozenset(
        _ENVELOPE_KEYS | {"delta", "start_ms", "end_ms"}
    ),
    wire.LIVE_DELEGATION_CREATED: _ENVELOPE_KEYS | {"offset_ms", "delegation"},
    "session.commentary.appended": _ENVELOPE_KEYS | {"start_ms", "end_ms"},
    "error": _ENVELOPE_KEYS | {"error"},
}


@dataclass(frozen=True)
class ProviderEvent:
    """A classified provider event.  Text and PCM are hidden from ``repr``."""

    kind: str
    event_id: str | None = None
    text: str | None = field(default=None, repr=False)
    start_ms: int | None = None
    end_ms: int | None = None
    delegation_id: str | None = None
    client_event_id: str | None = None
    pcm: bytes | None = field(default=None, repr=False)
    code: str | None = None
    session_id: str | None = field(default=None, repr=False)
    model: str | None = None
    voice: str | None = None
    sample_rate: int | None = None
    format_type: str | None = None
    delegation_type: str | None = None
    stored: bool | None = None
    close_reason: str | None = None


# -- builders ------------------------------------------------------------------------
def _dump(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def new_event_id() -> str:
    """A fresh gateway event id: ``gw_`` plus a UUID4 in hex (35 characters)."""
    return "gw_" + uuid.uuid4().hex


def build_provider_start(event_id: str, model: str, voice: str) -> str:
    if not _state.is_event_id(event_id):
        raise ValueError("invalid event id")
    if not _state.valid_model(model) or not _state.valid_voice(voice):
        raise ValueError("invalid model or voice")
    return _dump(
        {
            "type": "session.start",
            "event_id": event_id,
            "session": {
                "model": model,
                "store": False,
                "audio": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "output": {"voice": voice},
                },
                "delegation": {"type": "client", "acknowledgement_delay_ms": 0},
                "instructions": _FRONTEND_INSTRUCTIONS,
            },
        }
    )


def build_provider_audio(b64: str) -> str:
    if _state.audio_b64_len(b64) is None:
        raise ValueError("invalid audio")
    return _dump({"type": "session.input_audio.append", "audio": b64})


def build_commentary(event_id: str, delegation_id: str | None, content: str) -> str:
    if not _state.is_event_id(event_id) or (
        delegation_id is not None and not _state.is_event_id(delegation_id)
    ):
        raise ValueError("invalid id")
    if not isinstance(content, str):
        raise ValueError("invalid content")
    size = len(content.encode("utf-8"))  # a lone surrogate raises (a ValueError)
    if not 1 <= size <= _COMMENTARY_MAX_BYTES:
        raise ValueError("invalid content size")
    return _dump(
        {
            "type": "session.commentary.append",
            "event_id": event_id,
            "delegation_id": delegation_id,
            "content": content,
        }
    )


def build_provider_close() -> str:
    return _dump({"type": "session.close"})


# -- classifier ----------------------------------------------------------------------
def _event_envelope(obj: dict[str, Any]) -> bool:
    return _state.is_event_id(obj.get("event_id")) and (
        "client_event_id" not in obj or _state.is_event_id(obj["client_event_id"])
    )


def _timeline(obj: dict[str, Any]) -> bool:
    start, end = obj.get("start_ms"), obj.get("end_ms")
    return type(start) is int and type(end) is int and 0 <= start <= end


def _session_projection(obj: Any) -> dict[str, Any] | None:
    """Validate/discard bounded metadata; not a recursive full-schema validator."""
    if not isinstance(obj, dict) or not set(obj) <= _SESSION_KEYS:
        return None
    if (
        not _state.is_event_id(obj.get("id"))
        or type(obj.get("expires_at")) is not int or obj["expires_at"] < 0
        or not _state.valid_model(obj.get("model"))
        or obj.get("status") != "active"
    ):
        return None
    for name in ("instructions", "safety_identifier"):
        if name in obj and obj[name] is not None and not isinstance(obj[name], str):
            return None
    for name, kind in (("client", dict), ("input", list), ("triggers", list)):
        if name in obj and not isinstance(obj[name], kind):
            return None
    if "store" in obj and type(obj["store"]) is not bool:
        return None
    audio = obj.get("audio", {})
    if not isinstance(audio, dict) or not set(audio) <= {"format", "output"}:
        return None
    fmt, output = audio.get("format", {}), audio.get("output", {})
    if (
        not isinstance(fmt, dict) or not set(fmt) <= {"type", "rate"}
        or not isinstance(output, dict) or not set(output) <= {"voice"}
        or ("type" in fmt and not isinstance(fmt["type"], str))
        or ("rate" in fmt and type(fmt["rate"]) is not int)
        or ("voice" in output and not _state.valid_voice(output["voice"]))
    ):
        return None
    delegation = obj.get("delegation")
    if delegation is not None and (
        not isinstance(delegation, dict)
        or delegation.get("type") != "client"
        or not set(delegation) <= {"type", "acknowledgement_delay_ms"}
        or ("acknowledgement_delay_ms" in delegation and (
            type(delegation["acknowledgement_delay_ms"]) is not int
            or not 0 <= delegation["acknowledgement_delay_ms"] <= 800
        ))
    ):
        return None
    return {
        "session_id": obj["id"], "model": obj["model"],
        "voice": output.get("voice"), "sample_rate": fmt.get("rate"),
        "format_type": fmt.get("type"),
        "delegation_type": None if delegation is None else delegation["type"],
        "stored": obj.get("store"),
    }


def _audio(obj: dict[str, Any]) -> ProviderEvent:
    delta = obj.get("delta")
    if not isinstance(delta, str):
        return ProviderEvent("invalid_audio")
    try:
        pcm = base64.b64decode(delta, validate=True)
    except (binascii.Error, ValueError):
        return ProviderEvent("invalid_audio")
    if len(pcm) < 2 or len(pcm) % 2:
        return ProviderEvent("invalid_audio")
    if base64.b64encode(pcm).decode("ascii") != delta:
        return ProviderEvent("invalid_audio")  # non-zero pad bits: not canonical
    return ProviderEvent("audio", pcm=pcm)


def _transcript(obj: dict[str, Any]) -> ProviderEvent:
    try:
        live = wire.parse_live_event(obj)
    except wire.WireError:
        return ProviderEvent("invalid_transcript")
    if live is None or live.kind != "transcript_delta":
        return ProviderEvent("invalid_transcript")
    return ProviderEvent(
        "transcript",
        event_id=live.event_id,
        text=live.text,
        start_ms=live.start_ms,
        end_ms=live.end_ms,
    )


def _delegation(obj: dict[str, Any]) -> ProviderEvent:
    detail = obj.get("delegation")
    if isinstance(detail, dict) and not set(detail) <= _DELEGATION_KEYS:
        return ProviderEvent("ignored")  # an unknown nested key: never a hint
    try:
        live = wire.parse_live_event(obj)
    except wire.WireError:
        return ProviderEvent("ignored")  # a hint only: never fatal
    if live is None or live.kind != "delegation_hint":
        return ProviderEvent("ignored")
    return ProviderEvent("delegation", delegation_id=live.delegation_id)


def _ack(obj: dict[str, Any]) -> ProviderEvent:
    if not _timeline(obj):
        return ProviderEvent("invalid")
    echoed = obj.get("client_event_id")
    if _state.is_event_id(echoed):
        return ProviderEvent("ack", client_event_id=echoed)
    return ProviderEvent("ack_uncorrelated")


def _error(obj: dict[str, Any]) -> ProviderEvent:
    # Nothing the provider sent is kept: not the code, not the message.
    detail = obj.get("error")
    if (
        not isinstance(detail, dict) or not set(detail) <= _ERROR_DETAIL_KEYS
        or not all(isinstance(detail.get(k), str) for k in ("type", "code", "message"))
        or ("param" in detail and not isinstance(detail["param"], str))
        or ("client_event_id" in detail and not _state.is_event_id(detail["client_event_id"]))
    ):
        return ProviderEvent("invalid")
    return ProviderEvent("error", code=PROVIDER_ERROR_CODE)


def _started(obj: dict[str, Any]) -> ProviderEvent:
    projected = _session_projection(obj.get("session"))
    if projected is None:
        return ProviderEvent("invalid")
    return ProviderEvent("started", client_event_id=obj.get("client_event_id"), **projected)


def _closed(obj: dict[str, Any]) -> ProviderEvent:
    projected = _session_projection(obj.get("session"))
    usage = obj.get("usage")
    if (
        projected is None or obj.get("reason") not in _CLOSE_REASONS
        or not isinstance(usage, dict) or set(usage) != {"seconds"}
        or type(usage["seconds"]) not in (int, float)
        or not math.isfinite(usage["seconds"]) or usage["seconds"] < 0
    ):
        return ProviderEvent("invalid")
    return ProviderEvent("closed", close_reason=obj["reason"], **projected)


_HANDLERS: dict[str, Callable[[dict[str, Any]], ProviderEvent]] = {
    "session.started": _started,
    "session.closed": _closed,
    "session.output_audio.delta": _audio,
    wire.LIVE_TRANSCRIPT_DELTA: _transcript,
    wire.LIVE_DELEGATION_CREATED: _delegation,
    "session.commentary.appended": _ack,
    "error": _error,
}
# The kind of a recognised frame that carries an unknown key.  A delegation hint is
# never fatal, so an unknown key only makes it ``ignored``.
_REJECTED_KIND: dict[str, str] = {
    "session.started": "invalid",
    "session.closed": "invalid",
    "session.output_audio.delta": "invalid_audio",
    wire.LIVE_TRANSCRIPT_DELTA: "invalid_transcript",
    wire.LIVE_DELEGATION_CREATED: "ignored",
    "session.commentary.appended": "invalid",
    "error": "invalid",
}


def classify_provider_event(text: str | bytes) -> ProviderEvent:
    """Classify one provider frame.  Never raises; undecodable input is ``invalid``."""
    try:
        obj = _state.strict_loads(
            text,
            max_bytes=_state.MAX_PROVIDER_FRAME_BYTES,
            max_depth=_state.MAX_PROVIDER_DEPTH,
        )
    except ValueError:
        return ProviderEvent("invalid")
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        return ProviderEvent("invalid")
    handler = _HANDLERS.get(obj["type"])
    if handler is None:
        return ProviderEvent("ignored")
    if not set(obj) <= _KNOWN_KEYS[obj["type"]]:
        return ProviderEvent(_REJECTED_KIND[obj["type"]])  # R3: unknown key, rejected
    if obj["type"] != "session.output_audio.delta" and not _event_envelope(obj):
        return ProviderEvent(_REJECTED_KIND[obj["type"]])
    try:
        return handler(obj)
    except (TypeError, ValueError, OverflowError):
        # Malformed structured values (e.g. an unhashable close reason) never
        # escape the pure classifier or expose arbitrary provider data.
        return ProviderEvent(_REJECTED_KIND[obj["type"]])


__all__ = [
    "PROVIDER_URL",
    "ProviderEvent",
    "build_commentary",
    "build_provider_audio",
    "build_provider_close",
    "build_provider_start",
    "classify_provider_event",
    "new_event_id",
]
