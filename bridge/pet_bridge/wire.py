"""Exact Unified command wire and GPT-Live event parsing (stdlib only, no I/O).

Source facts (Unified 0.80.1 @ 4fbbb51e3749ab90f1eafbe36bd18e1ae1c6d1a0):

* ``POST /api/sessions/{id}/commands`` with
  ``{"id", "action": "conversation.send", "args": {"text", "via": "chat",
  "preserveDraft": true, "attachmentIds": []}}`` and an optional TOP-LEVEL
  ``expectedRevision`` (never inside ``args``).
* A success receipt is ``accepted: true`` and ``delivery: "accepted"`` for this
  session.  That means the host accepted the input; it does not mean the run
  finished.  ``hostInstanceId``/``stateRevision`` may be absent and are validated
  only when present.

Transport authenticity and origin binding are the later transport's job.
Errors and receipts carry fixed codes only; text and response bodies never leak.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

MAX_TEXT_CHARS = 4000
MAX_RECEIPT_BYTES = 16 * 1024
MAX_COMMENTARY_BYTES = 480
MAX_COMMENTARY_INPUT_CHARS = 65536

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_HOST_ID_RE = _SESSION_ID_RE

DELIVERIES = frozenset(
    {"sending", "accepted", "failed", "unknown", "held", "queued_for_update"}
)
# On an error status these mean the host may still have the input: never "rejected".
_OPEN_DELIVERIES = frozenset(
    {"accepted", "sending", "unknown", "held", "queued_for_update"}
)
_REJECT_STATUSES = frozenset({400, 401, 403, 404, 409, 413, 422, 429})

LIVE_TRANSCRIPT_DELTA = "session.input_transcript.delta"
LIVE_DELEGATION_CREATED = "session.delegation.created"
LIVE_COMMENTARY_APPEND = "session.commentary.append"


class WireError(Exception):
    """Invalid request or live event.  Carries a fixed code only."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class Receipt:
    """Outcome of one send: ``accepted``, ``rejected`` or ``uncertain``."""

    outcome: str
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LiveEvent:
    """A validated GPT-Live event.  ``text`` is hidden from repr."""

    kind: str  # transcript_delta | delegation_hint | commentary
    event_id: str | None = None
    text: str | None = field(default=None, repr=False)
    start_ms: int | None = None
    end_ms: int | None = None
    offset_ms: int | None = None
    delegation_id: str | None = None
    truncated: bool = False
    may_paraphrase: bool = False


# -- request -----------------------------------------------------------------------
def is_canonical_uuid4(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def valid_session_id(value: Any) -> bool:
    return isinstance(value, str) and _SESSION_ID_RE.fullmatch(value) is not None


def check_text(text: Any) -> str:
    """Validate utterance text.  Rejects; never truncates or edits."""
    if not isinstance(text, str):
        raise WireError("invalid_text")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise WireError("invalid_text") from None
    if not text.strip() or "\x00" in text:
        raise WireError("invalid_text")
    if len(text) > MAX_TEXT_CHARS:
        raise WireError("text_too_long")
    return text


def send_command_request(
    session_id: str,
    command_id: str,
    text: str,
    *,
    expected_revision: int | None = None,
) -> tuple[str, str, bytes]:
    """Build the exact ``conversation.send`` request as ``(method, path, body)``."""
    if not valid_session_id(session_id):
        raise WireError("invalid_session_id")
    if not is_canonical_uuid4(command_id):
        raise WireError("invalid_command_id")
    check_text(text)
    if expected_revision is not None and (
        type(expected_revision) is not int or expected_revision < 0
    ):
        raise WireError("invalid_revision")
    payload: dict[str, Any] = {
        "id": command_id,
        "action": "conversation.send",
        "args": {
            "text": text,
            "via": "chat",
            "preserveDraft": True,
            "attachmentIds": [],
        },
    }
    if expected_revision is not None:
        payload["expectedRevision"] = expected_revision
    raw = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    return (
        "POST",
        f"/api/sessions/{quote(session_id, safe='')}/commands",
        raw.encode("utf-8"),
    )


# -- receipt -----------------------------------------------------------------------
def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _parse_object(body: Any) -> dict[str, Any] | None:
    if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_RECEIPT_BYTES:
        return None
    try:
        value = json.loads(
            bytes(body).decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicates,
        )
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _uncertain(reason: str, status: Any = None, **meta: Any) -> Receipt:
    metadata = {"http_status": status} if type(status) is int else {}
    metadata.update(meta)
    return Receipt("uncertain", reason, metadata)


_FACT_KEYS = (
    "accepted",
    "delivery",
    "sessionId",
    "inputId",
    "hostInstanceId",
    "stateRevision",
)


def _optional_metadata(facts: dict[str, Any]) -> dict[str, Any] | None:
    """Validate hostInstanceId/stateRevision only when present; None means malformed."""
    meta: dict[str, Any] = {}
    if "hostInstanceId" in facts:
        value = facts["hostInstanceId"]
        if not isinstance(value, str) or _HOST_ID_RE.fullmatch(value) is None:
            return None
        meta["host_instance_id"] = value
    if "stateRevision" in facts:
        value = facts["stateRevision"]
        if type(value) is not int or value < 0:
            return None
        meta["state_revision"] = value
    return meta


def _same(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def _merge(obj: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Fold the top-level and the nested ``receipt`` fields into one fact set.

    A value present in both places must agree exactly (type and value); a nested
    receipt that is present but not an object is malformed.  Returns
    ``(facts, "")`` or ``(None, reason)``.
    """
    nested = obj.get("receipt")
    if nested is None:
        nested = {}
    elif not isinstance(nested, dict):
        return None, "malformed_receipt"
    facts: dict[str, Any] = {}
    for key in _FACT_KEYS:
        in_top, in_nested = key in obj, key in nested
        if in_top and in_nested and not _same(obj[key], nested[key]):
            return None, "conflicting_receipt"
        if in_top:
            facts[key] = obj[key]
        elif in_nested:
            facts[key] = nested[key]
    return facts, ""


def _malformed(facts: dict[str, Any]) -> bool:
    """Wrongly typed accepted/delivery (never hash an unknown value)."""
    if "accepted" in facts and type(facts["accepted"]) is not bool:
        return True
    return "delivery" in facts and not isinstance(facts["delivery"], str)


def _foreign(facts: dict[str, Any], session_id: str, command_id: str | None) -> str:
    if "sessionId" in facts and facts["sessionId"] != session_id:
        return "foreign_session"
    if command_id is not None and "inputId" in facts and facts["inputId"] != command_id:
        return "foreign_command"
    return ""


def _checked_facts(
    obj: dict[str, Any], status: int, session_id: str, command_id: str | None
) -> tuple[dict[str, Any] | None, dict[str, Any], Receipt | None]:
    """Common validation: ``(facts, optional_metadata, uncertain_receipt_or_None)``."""
    facts, reason = _merge(obj)
    if facts is None:
        return None, {}, _uncertain(reason, status)
    if _malformed(facts):
        return None, {}, _uncertain("malformed_receipt", status)
    foreign = _foreign(facts, session_id, command_id)
    if foreign:
        return None, {}, _uncertain(foreign, status)
    extra = _optional_metadata(facts)
    if extra is None:
        return None, {}, _uncertain("malformed_receipt", status)
    return facts, extra, None


def parse_send_receipt(
    status: int,
    body: bytes,
    *,
    session_id: str,
    command_id: str | None = None,
) -> Receipt:
    """Classify one HTTP response.  Anything not provably accepted or rejected is uncertain.

    Never raises for any status/body.  Top-level and nested ``receipt`` fields are
    merged and must agree; wrongly typed, contradictory or foreign receipts are
    uncertain, and no body or error text is ever copied into the result.
    """
    if type(status) is not int or not 100 <= status <= 599:
        return _uncertain("invalid_status")
    obj = _parse_object(body)
    if status == 504:
        pending = (
            obj is not None
            and obj.get("code") == "runtime_pending"
            and "accepted" not in obj
        )
        return _uncertain("runtime_pending" if pending else "gateway_timeout", status)
    if 200 <= status < 300:
        return _classify_success(status, obj, body, session_id, command_id)
    if status in _REJECT_STATUSES:
        return _classify_rejection(status, obj, session_id, command_id)
    return _uncertain("unexpected_status", status)


def _classify_rejection(
    status: int, obj: dict[str, Any] | None, session_id: str, command_id: str | None
) -> Receipt:
    if obj is None:
        # Empty, plaintext, oversize, NaN, duplicate-key, bad-UTF-8 or non-object
        # bodies prove nothing about delivery: the host may still have the input.
        return _uncertain("malformed_receipt", status)
    facts, _extra, bad = _checked_facts(obj, status, session_id, command_id)
    if bad is not None or facts is None:
        return bad or _uncertain("malformed_receipt", status)
    if facts.get("accepted") is True:
        return _uncertain("contradictory_receipt", status)
    delivery = facts.get("delivery")
    if delivery in _OPEN_DELIVERIES:
        return _uncertain("ambiguous_error_receipt", status)
    if delivery is not None and delivery not in DELIVERIES:
        return _uncertain("unknown_delivery", status)
    return Receipt("rejected", f"http_{status}", {"http_status": status})


def _classify_success(
    status: int,
    obj: dict[str, Any] | None,
    body: Any,
    session_id: str,
    command_id: str | None,
) -> Receipt:
    if obj is None:
        too_big = isinstance(body, (bytes, bytearray)) and len(body) > MAX_RECEIPT_BYTES
        return _uncertain(
            "receipt_too_large" if too_big else "malformed_receipt", status
        )
    if type(obj.get("accepted")) is not bool:
        return _uncertain("malformed_receipt", status)
    facts, extra, bad = _checked_facts(obj, status, session_id, command_id)
    if bad is not None or facts is None:
        return bad or _uncertain("malformed_receipt", status)
    accepted, delivery = obj["accepted"], facts.get("delivery")
    if delivery is not None and delivery not in DELIVERIES:
        return _uncertain("unknown_delivery", status)
    meta = {"http_status": status, "delivery": delivery, **extra}
    if accepted:
        return _success_receipt(obj, delivery, meta, status)
    if delivery == "failed":
        return Receipt("rejected", "delivery_failed", meta)
    return Receipt("uncertain", f"not_accepted_{delivery or 'none'}", meta)


def _success_receipt(
    obj: dict[str, Any], delivery: Any, meta: dict[str, Any], status: int
) -> Receipt:
    if delivery is None:
        return _uncertain("missing_delivery", status)
    if delivery == "failed":
        return Receipt("uncertain", "contradictory_receipt", meta)
    if delivery != "accepted":
        return Receipt("uncertain", f"delivery_{delivery}", meta)
    if "sessionId" not in obj:
        return _uncertain("missing_session", status)
    return Receipt("accepted", "accepted", meta)


# -- GPT-Live ----------------------------------------------------------------------
def _live_id(value: Any) -> str:
    if not isinstance(value, str) or _EVENT_ID_RE.fullmatch(value) is None:
        raise WireError("live_event_invalid")
    return value


def _live_ms(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise WireError("live_event_invalid")
    return value


def _live_text(value: Any, limit: int, *, allow_empty: bool) -> str:
    if (
        not isinstance(value, str)
        or len(value) > limit
        or (not value and not allow_empty)
    ):
        raise WireError("live_event_invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise WireError("live_event_invalid") from None
    return value


def bound_commentary(content: str) -> tuple[str, bool]:
    """Fit commentary in 480 UTF-8 bytes including the ellipsis; report truncation."""
    if len(content.encode("utf-8")) <= MAX_COMMENTARY_BYTES:
        return content, False
    room = MAX_COMMENTARY_BYTES - len("…".encode())
    cut = content.encode("utf-8")[:room].decode("utf-8", errors="ignore")
    return cut + "…", True


def parse_live_event(event: Any) -> LiveEvent | None:
    """Validate a GPT-Live event.  Unknown event types return ``None``.

    Only the exact ``session.*`` names are recognised.  Transcript deltas have no
    final-turn event; a delegation event carries no text.  Commentary may paraphrase
    and is bounded; nothing here ever dispatches a command.
    """
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    if kind == LIVE_TRANSCRIPT_DELTA:
        start, end = _live_ms(event.get("start_ms")), _live_ms(event.get("end_ms"))
        if end < start:
            raise WireError("live_event_invalid")
        return LiveEvent(
            "transcript_delta",
            event_id=_live_id(event.get("event_id")),
            text=_live_text(event.get("delta"), MAX_TEXT_CHARS, allow_empty=True),
            start_ms=start,
            end_ms=end,
        )
    if kind == LIVE_DELEGATION_CREATED:
        delegation = event.get("delegation")
        if (
            not isinstance(delegation, dict)
            or delegation.get("type") != "delegation"
            or delegation.get("target") != "client"
        ):
            raise WireError("live_event_invalid")
        return LiveEvent(
            "delegation_hint",
            offset_ms=_live_ms(event.get("offset_ms")),
            delegation_id=_live_id(delegation.get("id")),
        )
    if kind == LIVE_COMMENTARY_APPEND:
        content = _live_text(
            event.get("content"), MAX_COMMENTARY_INPUT_CHARS, allow_empty=False
        )
        text, truncated = bound_commentary(content)
        return LiveEvent(
            "commentary",
            event_id=_live_id(event.get("event_id")),
            delegation_id=_live_id(event.get("delegation_id")),
            text=text,
            truncated=truncated,
            may_paraphrase=True,
        )
    return None
