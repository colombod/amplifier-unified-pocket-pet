"""Exact reply correlation for one command (pure; no I/O, no state).

Installed Unified message rows (Unified 0.80.1): the user row we created has
``inputId == command id`` and an ``inputStatus`` of
``{version, inputId, phase, basis, generationId?, endedAt?}``.  An assistant row
carries ``generationId``; its ``inputId`` is only the LAST input of the generation
and is never used here.  Row text is cut at 4096 characters and marked with a
``textDetail`` key.  A page is ``{items, offset, total, before, userOffset,
sourceOffset}``.

Speak only when:

* the caller asserts whole history and the page proves it (``offset == 0`` and
  ``total == len(items)``), OR companion supplies validated newest-tail evidence
  for this exact page/session and an older different generation proves our run's
  boundary is inside it (offset zero needs no older boundary);
* exactly one user row is ours, it is not a steering message, and its status is
  ``done`` with basis ``generation_finished`` for a valid generation;
* no other user row shares that generation (a shared run mixes answers);
* our own row has an unambiguous native identity (no ``nativeInputAmbiguous``, no
  ``nativeInputId`` that disagrees with ``inputId``);
* no user row AFTER ours has an ambiguous identity or an unresolved generation (it
  could belong to the same run); older unknown identities/generations do not block
  once coverage and the boundary are proven;
* the assistant rows of that exact generation carry full text of at most 4000
  characters.

Never "latest assistant", never text- or order-only matching, never a fallback
for arbitrary partial pages. ``status`` is ``speak``, ``pending`` (look again later)
or ``not_spoken``; ``terminal`` says whether the command should be closed.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any

from .companion import DetailWindow

MAX_REPLY_CHARS = 4000

_RUNNING = frozenset({"queued", "included", "working"})
_PHASES = _RUNNING | {"waiting", "done", "failed", "unknown"}


@dataclass(frozen=True)
class ReplyDecision:
    status: str  # speak | pending | not_spoken
    reason: str
    terminal: bool
    text: str | None = field(default=None, repr=False)
    generation_id: str | None = None


def _pending(reason: str) -> ReplyDecision:
    return ReplyDecision("pending", reason, False)


def _skip(reason: str, terminal: bool = True) -> ReplyDecision:
    return ReplyDecision("not_spoken", reason, terminal)


def _gen(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 200 else None


def _page_items(page: Any) -> list[dict[str, Any]] | None:
    if not isinstance(page, dict):
        return None
    items, offset, total = page.get("items"), page.get("offset"), page.get("total")
    if (
        not isinstance(items, list)
        or type(offset) is not int
        or type(total) is not int
        or not all(isinstance(item, dict) for item in items)
    ):
        return None
    return items


def _generations(row: dict[str, Any]) -> set[str]:
    status = row.get("inputStatus")
    found = {_gen(row.get("generationId"))}
    if isinstance(status, dict):
        found.add(_gen(status.get("generationId")))
    found.discard(None)
    return found  # type: ignore[return-value]


def _identity_ambiguous(row: dict[str, Any]) -> bool:
    """The host's own rule: an ambiguous flag, or inputId != nativeInputId, voids it."""
    if "nativeInputAmbiguous" in row and row["nativeInputAmbiguous"] is not False:
        return True
    native = row.get("nativeInputId")
    return native is not None and "inputId" in row and native != row["inputId"]


def _status_decision(own: dict[str, Any], command_id: str) -> ReplyDecision | None:
    """None means the run finished normally; otherwise the decision to return."""
    status = own.get("inputStatus")
    if (
        not isinstance(status, dict)
        or type(status.get("version")) is not int
        or status["version"] != 1
        or status.get("inputId") != command_id
        or not isinstance(status.get("phase"), str)
        or status["phase"] not in _PHASES
        or not isinstance(status.get("basis"), str)
    ):
        return _skip("status_invalid")
    phase, basis = status["phase"], status["basis"]
    if phase in _RUNNING:
        return _pending("generation_running")
    if phase == "waiting":
        return _pending("waiting_workers")
    if phase == "failed":
        return _skip("generation_failed")
    if phase == "unknown":
        return (
            _pending("no_evidence")
            if basis == "no_evidence"
            else _skip("generation_unknown")
        )
    return None if basis == "generation_finished" else _skip("completion_ambiguous")


def _own_generation(own: dict[str, Any]) -> tuple[str | None, ReplyDecision | None]:
    status_gen = _gen(own["inputStatus"].get("generationId"))
    row_gen = _gen(own.get("generationId"))
    if status_gen and row_gen and status_gen != row_gen:
        return None, _skip("ambiguous_generation")
    generation = status_gen or row_gen
    return generation, (None if generation else _skip("no_generation"))


def _reply_text(items: list[dict[str, Any]], generation: str) -> ReplyDecision:
    rows = [
        r
        for r in items
        if r.get("role") == "assistant" and r.get("generationId") == generation
    ]
    if not rows:
        return _pending("reply_row_not_visible")
    parts: list[str] = []
    for row in rows:
        if "textDetail" in row:
            return _skip("long_reply_not_spoken")
        text = row.get("text")
        if not isinstance(text, str):
            return _skip("reply_invalid")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            return _skip("reply_invalid")
        if text.strip():
            parts.append(text)
    if not parts:
        return _skip("no_reply_text")
    joined = "\n\n".join(parts)
    if len(joined) > MAX_REPLY_CHARS:
        return _skip("long_reply_not_spoken")
    return ReplyDecision("speak", "reply_complete", True, joined, generation)


def _page_decision(
    page: dict[str, Any],
    items: list[dict[str, Any]],
    session_id: str,
    coverage_complete: Any,
    newest_tail: bool,
) -> ReplyDecision | None:
    if coverage_complete is not True and not newest_tail:
        return _pending("coverage_incomplete")
    if not newest_tail and (page["offset"] != 0 or page["total"] != len(items)):
        return _pending("pagination_required")
    for source in (page, *items):
        if "sessionId" in source and source["sessionId"] != session_id:
            return _skip("foreign_session", terminal=False)
    return None


def _generation_boundary(
    items: list[dict[str, Any]], own: dict[str, Any], generation: str
) -> bool:
    """A different valid generation strictly OLDER than ours bounds the run."""
    for row in items:
        if row is own:
            break
        if (other := _gen(row.get("generationId"))) and other != generation:
            return True
        status = row.get("inputStatus")
        if row.get("role") == "user" and isinstance(status, dict):
            if (other := _gen(status.get("generationId"))) and other != generation:
                return True
    return False


def _own_row(
    items: list[dict[str, Any]], command_id: str
) -> tuple[dict[str, Any] | None, ReplyDecision | None]:
    rows = [
        r for r in items if r.get("role") == "user" and r.get("inputId") == command_id
    ]
    if not rows:
        return None, _pending("user_row_not_visible")
    if len(rows) > 1:
        return None, _skip("ambiguous_user_row")
    own = rows[0]
    decoys = (
        r
        for r in items
        if r is not own
        and r.get("role") == "user"
        and r.get("nativeInputId") == command_id
    )
    if _identity_ambiguous(own) or next(decoys, None) is not None:
        return None, _skip("ambiguous_input_identity")
    if "steering" in own:
        return None, _skip("steering_not_spoken")
    return own, None


def _later_input_block(
    items: list[dict[str, Any]], own: dict[str, Any]
) -> ReplyDecision | None:
    """A user row after ours that is ambiguous or has no single generation may mix."""
    seen_own = False
    for row in items:
        if row is own:
            seen_own = True
            continue
        if not seen_own or row.get("role") != "user":
            continue
        if _identity_ambiguous(row):
            return _skip("ambiguous_later_input")
        if len(_generations(row)) != 1:
            return _pending("later_input_unresolved")
    return None


def input_delivered(page: Any, *, command_id: str, text_sha256: str) -> bool:
    """Positive proof that ``command_id`` reached the conversation; never a guess.

    True only when exactly one user row carries ``command_id`` as its unambiguous input
    id AND that row's complete text hashes to what was sent.  Absence proves nothing
    (the row may not be visible yet), so ``False`` means "not proven", not "not sent".
    """
    items = _page_items(page)
    if items is None or not isinstance(text_sha256, str):
        return False
    rows = [
        r for r in items if r.get("role") == "user" and r.get("inputId") == command_id
    ]
    if len(rows) != 1 or _identity_ambiguous(rows[0]) or "textDetail" in rows[0]:
        return False
    text = rows[0].get("text")
    if not isinstance(text, str):
        return False
    try:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(digest, text_sha256)


def evaluate_reply(
    page: Any,
    *,
    command_id: str,
    session_id: str,
    coverage_complete: bool,
    newest_tail: DetailWindow | None = None,
) -> ReplyDecision:
    """Decide whether ``command_id``'s exact finished generation may be spoken."""
    items = _page_items(page)
    if items is None:
        return _skip("page_invalid", terminal=False)
    validated_tail = (
        type(newest_tail) is DetailWindow
        and newest_tail.proves_newest_tail(page, session_id)
    )
    blocked = _page_decision(page, items, session_id, coverage_complete, validated_tail)
    if blocked is not None:
        return blocked
    own, blocked = _own_row(items, command_id)
    if own is None:
        return blocked or _skip("ambiguous_user_row")
    blocked = _status_decision(own, command_id)
    if blocked is not None:
        return blocked
    generation, blocked = _own_generation(own)
    if blocked is not None or generation is None:
        return blocked or _skip("no_generation")
    if (
        validated_tail and page["offset"] != 0
        and not _generation_boundary(items, own, generation)
    ):
        return _pending("generation_boundary_not_visible")
    for row in items:
        if (
            row.get("role") == "user"
            and row is not own
            and generation in _generations(row)
        ):
            return _skip("shared_generation")
    blocked = _later_input_block(items, own)
    if blocked is not None:
        return blocked
    return _reply_text(items, generation)
