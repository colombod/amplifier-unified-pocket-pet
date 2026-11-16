"""Read-only companion views: session list, idle preflight and message windows.

Source facts from static reading only, NOT a live wire qualification: legacy
paths reviewed at Unified 0.80.1; opt-in observation-v1 at reference 0bcb8c30.

* ``GET /api/companion`` returns up to 50 active-or-pinned root sessions per page with
  a signed cursor, a GLOBAL ``revision`` and the host identity/instance.
* Opt-in ``list_all_work`` checks that bootstrap's protocol/identity and exact
  observation resource, then reads all activity/pins in an explicit scope. The
  observation revision is opaque and NEVER a global send/preflight revision.
  Advertisement does not grant scope: a 403 stops without auth changes or fallback.
* ``GET /api/conversation/detail?sessionId=&part=messages[&before=<message id>]`` for a
  native client returns the newest resident tail
  ``{items, offset, total, before, userOffset, sourceOffset}``; ``before`` is a MESSAGE
  ID (not an offset or a web cursor) and each page ends just before it.

What this module guarantees, and what it does not:

* Every operation is bounded: ``list_sessions`` makes at most 5 GETs, ``detail_window``
  at most 4, ``list_all_work`` at most 6 including bootstrap; none retried inside
  the call. A cap, a cursor cycle, a changed snapshot
  or any malformed field fails closed or is reported as an incomplete window.
* ``detail_window`` never invents an ``offset``, ``total`` or coverage: a window is
  ``complete`` only if the pages reached ``offset == 0`` and validated end to end.
  Foreign rows are preserved untouched. Whole-history coverage and validated newest-tail
  evidence are separate: an incomplete tail needs its own generation-boundary proof
  in the reply evaluator before anything may be spoken.
* ``preflight`` is NOT atomic admission proof: it only says that a fresh exact listing
  showed the selected row as ``idle`` with all three activity flags false.  Absence,
  an unknown or error status, or a stale list is never idle.  The core's global
  ``expectedRevision`` check remains the only concurrency guard, and it too is not an
  atomic idle guarantee (an active run may still steer a message silently).

Errors carry fixed ``companion_*`` codes (and an HTTP status) and never a body.
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from . import transport, wire
from .transport import Origin, TransportError

MAX_LIST_PAGES = 5
MAX_CATALOG_PAGES = 5
MAX_DETAIL_PAGES = 4
MAX_ROWS_PER_PAGE = 200
TITLE_LIMIT = 80

ERROR_CODES = frozenset(
    {
        "companion_request_invalid",
        "companion_request_failed",
        "companion_unauthorized",
        "companion_invalid_cursor",
        "companion_bad_request",
        "companion_not_found",
        "companion_snapshot_changed",
        "companion_observation_busy",
        "companion_http_status",
        "companion_response_invalid",
        "companion_host_mismatch",
        "companion_cursor_cycle",
        "companion_observation_unsupported",
    }
)


class CompanionError(Exception):
    """A fixed ``code`` (and the HTTP ``status`` when relevant); never any payload."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        if code not in ERROR_CODES:  # pragma: no cover - programming error guard
            code = "companion_response_invalid"
        super().__init__(code)
        self.code = code
        self.status = status

    def __str__(self) -> str:
        return self.code


def _invalid() -> CompanionError:
    return CompanionError("companion_response_invalid")


# -- shared request plumbing ----------------------------------------------------------------
_LIST_STATUS = {
    400: "companion_invalid_cursor",
    401: "companion_unauthorized",
    403: "companion_unauthorized",
    409: "companion_snapshot_changed",
    503: "companion_observation_busy",
}
_DETAIL_STATUS = {
    **_LIST_STATUS,
    400: "companion_bad_request",
    404: "companion_not_found",
}


def _get(
    client: Any, attachment: Any, route: transport.Route, statuses: dict[int, str]
) -> dict:
    """ONE authenticated GET; the decoded JSON object, or a fixed error."""
    token = getattr(attachment, "token", "")
    if type(token) is not str or not token:
        raise CompanionError("companion_unauthorized")
    headers = {
        "Authorization": "Bearer " + token,
        "X-Amplifier-Client": attachment.client_id,
        "Accept": "application/json",
    }
    try:
        response = client.request(
            Origin.parse(attachment.origin), route, headers=headers, body=b""
        )
    except Exception:  # noqa: BLE001 - fixed code; no cause chain, no detail
        raise CompanionError("companion_request_failed") from None
    if response.status != 200:
        raise CompanionError(
            statuses.get(response.status, "companion_http_status"),
            status=response.status,
        )
    try:
        return transport.strict_object(response.body)
    except TransportError:
        raise _invalid() from None


def _route(
    builder: Callable[..., transport.Route], *args: Any, **kwargs: Any
) -> transport.Route:
    try:
        return builder(*args, **kwargs)
    except TransportError:
        raise CompanionError("companion_request_invalid") from None


def _int(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise _invalid()
    return value


def _observation_revision(value: Any) -> int | str:
    """Keep the bounded snapshot identity opaque; never use it as send CAS.

    Current Unified revision_for/directory_key emits an ASCII string containing
    the instance, collection and generation/digest. Retain legacy nonnegative
    integers without coercion so mixed versions also fail the page comparison.
    """
    if type(value) is int:
        return _int(value)
    if type(value) is str and 1 <= len(value) <= 200 and all(
        0x21 <= ord(ch) <= 0x7E for ch in value
    ):
        return value
    raise _invalid()


# -- titles ----------------------------------------------------------------------------------
def sanitize_title(value: str) -> str:
    """Printable, single-line, at most 80 characters (display only)."""
    kept = []
    for ch in value:
        if ch.isspace():
            kept.append(" ")
        elif unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Cn"):
            kept.append(ch)
    text = " ".join("".join(kept).split())
    return text if len(text) <= TITLE_LIMIT else text[: TITLE_LIMIT - 1] + "\u2026"


# -- session list -----------------------------------------------------------------------------
@dataclass(frozen=True)
class SessionRow:
    id: str
    title: str = field(repr=False)
    status: str
    execution: bool
    preparing: bool
    loading: bool
    pinned: bool = False
    role: str | None = None
    agent_created: bool | None = None


@dataclass(frozen=True)
class SessionList:
    """Exact rows with the GLOBAL revision.  ``complete`` is False if a cap was hit."""

    sessions: tuple[SessionRow, ...] = field(repr=False)
    revision: int
    observation_revision: int | str
    complete: bool
    pages: int
    host_identity: str
    host_instance_id: str
    fetched_monotonic: float


@dataclass(frozen=True)
class Preflight:
    """Conservative idle evidence; ``idle`` is never an atomic guarantee."""

    session_id: str | None
    revision: int
    idle: bool
    reason: str


def _row(raw: Any) -> SessionRow:
    if type(raw) is not dict or not wire.valid_session_id(raw.get("id")):
        raise _invalid()
    activity, title, status = (
        raw.get("activity"),
        raw.get("title", ""),
        raw.get("status"),
    )
    flags = ("execution", "preparing", "loading")
    if (
        type(activity) is not dict
        or not all(type(activity.get(flag)) is bool for flag in flags)
        or type(status) is not str
        or type(title) is not str
        or type(raw.get("pinned", False)) is not bool
    ):
        raise _invalid()
    return SessionRow(
        raw["id"],
        sanitize_title(title),
        status,
        activity["execution"],
        activity["preparing"],
        activity["loading"],
        raw.get("pinned", False),
    )


def _list_page(
    doc: dict[str, Any], attachment: Any
) -> tuple[list[SessionRow], int, int | str, str | None]:
    version = doc.get("protocolVersion")
    if type(version) is not int or version != 1:
        raise _invalid()
    if (
        doc.get("hostIdentity") != attachment.host_identity
        or doc.get("hostInstanceId") != attachment.host_instance_id
    ):
        raise CompanionError("companion_host_mismatch")
    revision = _int(doc.get("revision"))
    observed = _observation_revision(doc.get("observationRevision"))
    sessions, truncated, nxt = (
        doc.get("sessions"),
        doc.get("sessionsTruncated"),
        doc.get("sessionsNextCursor"),
    )
    if (
        type(sessions) is not list
        or len(sessions) > MAX_ROWS_PER_PAGE
        or type(truncated) is not bool
    ):
        raise _invalid()
    if truncated != (nxt is not None):
        raise _invalid()
    if nxt is not None:
        try:
            transport.route_companion(sessions_cursor=nxt)
        except TransportError:
            raise _invalid() from None
    return [_row(item) for item in sessions], revision, observed, nxt


def list_sessions(
    client: Any, attachment: Any, *, monotonic: Callable[[], float] = time.monotonic
) -> SessionList:
    """Up to 5 bounded GETs following the server cursor; fail closed on any anomaly."""
    started = monotonic()
    rows: list[SessionRow] = []
    seen_ids: set[str] = set()
    cursors: set[str] = set()
    cursor: str | None = None
    snapshot: tuple[int, int | str] | None = None
    complete = False
    pages = 0
    while pages < MAX_LIST_PAGES:
        route = _route(transport.route_companion, sessions_cursor=cursor)
        doc = _get(client, attachment, route, _LIST_STATUS)
        page, revision, observed, cursor = _list_page(doc, attachment)
        pages += 1
        if snapshot is not None and snapshot != (revision, observed):
            raise CompanionError("companion_snapshot_changed")
        snapshot = (revision, observed)
        for row in page:
            if row.id in seen_ids:
                raise _invalid()
            seen_ids.add(row.id)
            rows.append(row)
        if cursor is None:
            complete = True
            break
        if cursor in cursors:
            raise CompanionError("companion_cursor_cycle")
        cursors.add(cursor)
    assert snapshot is not None
    return SessionList(
        tuple(rows),
        snapshot[0],
        snapshot[1],
        complete,
        pages,
        attachment.host_identity,
        attachment.host_instance_id,
        started,
    )


@dataclass(frozen=True)
class SessionCatalog:
    """Read-only observation, NOT a SessionList or send/idle admission evidence."""

    sessions: tuple[SessionRow, ...] = field(repr=False)
    observation_revision: int | str
    complete: bool
    pages: int
    scope: str
    host_identity: str
    host_instance_id: str
    fetched_monotonic: float


_CATALOG_STATUS = {
    **_LIST_STATUS,
    404: "companion_observation_unsupported",
}


def _catalog_capability(doc: dict[str, Any], attachment: Any) -> None:
    version = doc.get("protocolVersion")
    if type(version) is not int or version != 1:
        raise CompanionError("companion_observation_unsupported")
    if (
        doc.get("hostIdentity") != attachment.host_identity
        or doc.get("hostInstanceId") != attachment.host_instance_id
    ):
        raise CompanionError("companion_host_mismatch")
    resources = doc.get("resources")
    if (
        type(resources) is not dict
        or resources.get("sessions") != transport.SESSIONS_OBSERVATION_RESOURCE
    ):
        raise CompanionError("companion_observation_unsupported")


def _catalog_row(raw: Any, scope: str) -> SessionRow:
    row = _row(raw)
    role, created = raw.get("role"), raw.get("agentCreated")
    if (
        role not in ("root", "delegate")
        or (scope != "all" and role != ("root" if scope == "roots" else "delegate"))
        or type(raw.get("pinned")) is not bool
        or ("agentCreated" in raw and type(created) is not bool)
        or len(raw.get("title", "")) > 200
        or len(row.status) > 200
    ):
        raise _invalid()
    return SessionRow(
        row.id, row.title, row.status, row.execution, row.preparing, row.loading,
        row.pinned, role, created,
    )


def list_all_work(
    client: Any,
    attachment: Any,
    *,
    scope: str = "roots",
    limit: int = MAX_ROWS_PER_PAGE,
    monotonic: Callable[[], float] = time.monotonic,
) -> SessionCatalog:
    """One bootstrap + <=5 sequential GETs, no retry or mutation.

    Empty pages with continuation are not terminal. Any page/status/snapshot
    failure discards the entire traversal; only the page cap returns incomplete
    rows. No workspace paths are in this resource, so no grouping is inferred.
    """
    route = _route(transport.route_sessions_observation, scope=scope, limit=limit)
    started = monotonic()
    bootstrap = _get(client, attachment, transport.route_companion(), _CATALOG_STATUS)
    _catalog_capability(bootstrap, attachment)
    rows: list[SessionRow] = []
    ids: set[str] = set()
    cursors: set[str] = set()
    snapshot: int | str | None = None
    complete = False
    pages = 0
    while pages < MAX_CATALOG_PAGES:
        doc = _get(client, attachment, route, _CATALOG_STATUS)
        observed = _observation_revision(doc.get("revision"))
        if snapshot is not None and snapshot != observed:
            raise CompanionError("companion_snapshot_changed")
        snapshot = observed
        items = doc.get("items")
        if (
            type(items) is not list
            or len(items) > limit
            or "nextCursor" not in doc
        ):
            raise _invalid()
        for raw in items:
            row = _catalog_row(raw, scope)
            if row.id in ids:
                raise _invalid()
            ids.add(row.id)
            rows.append(row)
        pages += 1
        cursor = doc["nextCursor"]
        if cursor is None:
            complete = True
            break
        try:
            route = transport.route_sessions_observation(
                scope=scope, limit=limit, cursor=cursor
            )
        except TransportError:
            raise _invalid() from None
        if cursor in cursors:
            raise CompanionError("companion_cursor_cycle")
        cursors.add(cursor)
    assert snapshot is not None
    return SessionCatalog(
        tuple(rows), snapshot, complete, pages, scope,
        attachment.host_identity, attachment.host_instance_id, started,
    )


def preflight(
    listing: SessionList,
    session_id: Any,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    max_age: float = 10.0,
) -> Preflight:
    """``idle`` only for a fresh, exactly listed, ``idle`` row with all flags false."""
    row = None
    if type(session_id) is str:
        row = next((r for r in listing.sessions if r.id == session_id), None)
    selected = session_id if type(session_id) is str else None

    def verdict(idle: bool, reason: str) -> Preflight:
        return Preflight(selected, listing.revision, idle, reason)

    if row is None:
        return verdict(False, "unlisted")
    if not 0 <= monotonic() - listing.fetched_monotonic <= max_age:
        return verdict(False, "stale_list")
    if row.status != "idle":
        return verdict(False, "status_not_idle")
    if row.execution or row.preparing or row.loading:
        return verdict(False, "activity")
    return verdict(True, "idle")


# -- message window ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DetailWindow:
    """Validated newest-tail rows, oldest to newest.

    ``offset``/``total`` are the server's own numbers for the OLDEST page fetched.
    ``complete`` is true only when that page was at ``offset == 0``.
    """

    items: tuple[dict[str, Any], ...] = field(repr=False)
    offset: int
    total: int
    pages: int
    complete: bool
    before: str | None
    user_offset: int
    source_offset: int
    last_id: str | None
    baseline_found: bool | None
    since_baseline: tuple[dict[str, Any], ...] = field(repr=False)
    # Only detail_window seals these after checking every fetched page. Constructing
    # a DetailWindow (or dataclasses.replace) is not evidence of a validated traversal.
    _tail_page: dict[str, Any] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _tail_session_id: str | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def proves_newest_tail(self, page: Any, session_id: str) -> bool:
        """Evidence is scoped to the exact validated rows/metadata AND destination.

        Keep a detached snapshot: editing nested rows, reordering or substituting
        an arbitrary page cannot borrow this window's traversal evidence.
        """
        return (
            self._tail_page is not None
            and self._tail_session_id == session_id
            and self._tail_page == page
        )

    @property
    def coverage_complete(self) -> bool:
        return self.complete and self.offset == 0 and self.total == len(self.items)

    def as_page(self) -> dict[str, Any]:
        """The core's page shape, with the server's real numbers."""
        return {
            "items": list(self.items),
            "offset": self.offset,
            "total": self.total,
            "before": self.before,
            "userOffset": self.user_offset,
            "sourceOffset": self.source_offset,
        }


@dataclass
class _Page:
    items: list[dict[str, Any]]
    offset: int
    total: int
    before: str | None
    user_offset: int
    source_offset: int


def _decode_page(doc: dict[str, Any]) -> _Page:
    items = doc.get("items")
    before = doc.get("before")
    if type(items) is not list or not all(type(r) is dict for r in items):
        raise _invalid()
    if before is not None and type(before) is not str:
        raise _invalid()
    for row in items:
        if type(row.get("id")) is not str or not row["id"]:
            raise _invalid()
    return _Page(
        items,
        _int(doc.get("offset")),
        _int(doc.get("total")),
        before,
        _int(doc.get("userOffset")),
        _int(doc.get("sourceOffset")),
    )


def _check_page(
    page: _Page, prev: _Page | None, first_total: int | None, ids: set[str]
) -> None:
    if first_total is not None and page.total != first_total:
        raise CompanionError("companion_snapshot_changed")
    end = page.total if prev is None else prev.offset
    if page.offset + len(page.items) != end:
        raise _invalid()
    expected_before = page.items[0]["id"] if page.offset > 0 and page.items else None
    if (page.offset > 0 and not page.items) or page.before != expected_before:
        raise _invalid()
    if prev is not None and (
        page.source_offset - page.offset != prev.source_offset - prev.offset
        or page.user_offset > prev.user_offset
    ):
        raise _invalid()
    for row in page.items:
        if row["id"] in ids:
            raise _invalid()
        ids.add(row["id"])


def detail_window(
    client: Any, attachment: Any, session_id: Any, baseline_last_id: Any = None
) -> DetailWindow:
    """Walk back from the newest tail by message id, at most 4 pages (1 MiB each)."""
    _route(transport.route_detail, session_id)
    if baseline_last_id is not None:
        _route(transport.route_detail, session_id, before=baseline_last_id)
    pages: list[_Page] = []
    ids: set[str] = set()
    before: str | None = None
    while len(pages) < MAX_DETAIL_PAGES:
        route = _route(transport.route_detail, session_id, before=before)
        page = _decode_page(_get(client, attachment, route, _DETAIL_STATUS))
        _check_page(
            page, pages[-1] if pages else None, pages[0].total if pages else None, ids
        )
        pages.append(page)
        if page.offset == 0:
            break
        before = page.items[0]["id"]
        _route(
            transport.route_detail, session_id, before=before
        )  # id must be route-safe
    oldest = pages[-1]
    items = tuple(row for page in reversed(pages) for row in page.items)
    ordered = [row["id"] for row in items]
    found: bool | None = None
    since: tuple[dict[str, Any], ...] = ()
    if baseline_last_id is not None:
        found = baseline_last_id in ordered
        if found:
            since = items[ordered.index(baseline_last_id) + 1 :]
    window = DetailWindow(
        items,
        oldest.offset,
        pages[0].total,
        len(pages),
        oldest.offset == 0,
        oldest.before,
        oldest.user_offset,
        oldest.source_offset,
        ordered[-1] if ordered else None,
        found,
        since,
    )
    object.__setattr__(window, "_tail_page", deepcopy(window.as_page()))
    object.__setattr__(window, "_tail_session_id", session_id)
    return window
