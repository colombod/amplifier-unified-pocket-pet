"""Gateway core: the serialized sync-core runner, the Unified adapter, audio and one session.

Four bricks live here, all offline-testable and none opening a socket:

``CoreRunner``
    ONE worker thread executes EVERY synchronous ``core.Bridge`` / credential store /
    companion / HTTPS-transport call, strictly one at a time.  It is a ``collections.deque``
    plus a ``threading.Condition`` (no ``queue.SimpleQueue``, no executor, no
    ``asyncio.to_thread``).  The worker only moves on when the job's function has actually
    RETURNED, even if the awaiting caller was cancelled: a caller cancellation never
    cancels the job or the worker, and the job's real outcome (the actual
    ``CommandResult`` / exception, not just metadata) is kept until it is reconciled with
    ``take_result``.  ``retained()`` is fixed-vocabulary METADATA only.
``UnifiedOps``
    The sync adapter run only on the runner.  Preflight and poll are bound to the FROZEN
    startup ``core.Binding``: the credential attachment and the live ``Bridge.status`` must
    match it on client id, host identity, host instance id, credential ref and session id.
``AudioOut``
    The gated, paced (48000 B/s), bounded (1 MiB) provider-audio queue.  It counts the
    bytes whose device send actually COMPLETED.
``PetSession``
    One authenticated device WebSocket: one turn or a running conversation, one event consumer
    that never awaits blocking core work), tracked tasks, injected clock.

Wire facts are in ``gateway_state`` / ``gateway_provider``.  Offline tests are not a live
provider, Unified or device result.  Nothing here logs and no repr/str/error carries
transcript, reply text, audio, token, key or full session id.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import hmac
import math
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from . import companion, core
from . import gateway_provider as gp
from . import gateway_state as gs
from .companion import CompanionError, Preflight

T = TypeVar("T")

_EPS = 1e-9
_JOIN_S = 5.0

# Fixed vocabularies for the PUBLIC retained metadata: nothing arbitrary is ever echoed.
_OUTCOMES = frozenset({"accepted", "rejected", "uncertain", "refused"})
_STATUSES = frozenset({"speak", "ready", "pending", "not_spoken"})
_SAFE_REASONS = frozenset(
    {
        "accepted",
        "reply_complete",
        "bridge_faulted",
        "digest_mismatch",
        "epoch_stale",
        "invalid_request",
        "invalid_revision",
        "journal_error",
        "journal_fault",
        "not_sealed",
        "unknown_turn",
        "unknown_command",
        "stopped",
        "expired",
        "duplicate_confirm",
        "command_inflight",
        "uncertain_unresolved",
        "reply_pending",
        "delivery_uncertain",
        "delivery_rejected",
        "delivery_in_flight",
        "abandoned",
        "resolved",
        "suppressed",
        "reply_closed",
        "already_consumed",
        "foreign_session",
        "reply_invalid",
        "binding_mismatch",
        "credential_missing",
        "core_binding_required",
        "list_incomplete",
        "no_evidence",
        "coverage_incomplete",
        "generation_boundary_not_visible",
        "generation_running",
        "generation_unknown",
        "no_generation",
        "no_reply_text",
        "page_invalid",
        "pagination_required",
        "journal_full",
        "turn_pending",
        "no_binding",
        "transport_exception",
        "transport_interrupted",
    }
)
_EXC_NAMES = frozenset(
    {
        "BridgeError",
        "CompanionError",
        "JournalError",
        "AuthError",
        "TransportError",
        "ValueError",
        "TypeError",
        "OSError",
        "KeyError",
    }
)


class ProviderError(Exception):
    """A locally classified link failure with only a fixed, non-provider code."""

    CODES = frozenset({
        "provider_connect", "provider_redirect", "provider_link", "provider_auth",
        "provider_protocol", "provider_policy",
    })

    def __init__(self, code: str) -> None:
        if code not in self.CODES:
            raise ValueError("unknown provider code")
        super().__init__(code)
        self.code = code

    def __str__(self) -> str:
        return self.code


class RunnerError(Exception):
    """A runner refusal identified only by a fixed ``code``.

    ``runner_busy``, ``runner_closed``, ``runner_started``, ``runner_interrupted`` (plan
    7.3) plus three additions: ``runner_not_started`` (work before ``start``),
    ``runner_not_settled`` and ``runner_result_unavailable`` (``take_result``).
    """

    CODES = frozenset(
        {
            "runner_busy",
            "runner_closed",
            "runner_started",
            "runner_interrupted",
            "runner_not_started",
            "runner_not_settled",
            "runner_result_unavailable",
        }
    )

    def __init__(self, code: str) -> None:
        if code not in self.CODES:  # pragma: no cover - programming error guard
            code = "runner_interrupted"
        super().__init__(code)
        self.code = code

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class RetainedResult:
    """Metadata only (never text, audio or an arbitrary error string)."""

    job: int
    op: str
    outcome: str  # "ok" | "error"
    code: str | None
    caller_cancelled: bool


def _safe_reason(reason: Any) -> str:
    return reason if isinstance(reason, str) and reason in _SAFE_REASONS else "other"


def _safe_code(value: Any, exc: BaseException | None) -> str | None:
    """A fixed-vocabulary code for the public metadata; never raw error text."""
    if exc is not None:
        if isinstance(exc, RunnerError):
            return exc.code
        name = type(exc).__name__
        return "exc:" + (name if name in _EXC_NAMES else "other")
    if isinstance(value, core.CommandResult):
        outcome = value.outcome if value.outcome in _OUTCOMES else "other"
        return outcome + ":" + _safe_reason(value.reason)
    if isinstance(value, core.ReplyResult):
        status = value.status if value.status in _STATUSES else "other"
        return status + ":" + _safe_reason(value.reason)
    return None


class _Meta:
    __slots__ = ("caller_cancelled", "code", "job", "op", "outcome")

    def __init__(self, job: int, op: str, outcome: str, code: str | None) -> None:
        self.job = job
        self.op = op
        self.outcome = outcome
        self.code = code
        self.caller_cancelled = False


class _Job:
    __slots__ = (
        "caller_cancelled",
        "exc",
        "fn",
        "job_id",
        "loop",
        "meta",
        "op",
        "retain",
        "settled",
        "urgent",
        "value",
        "waiter",
    )

    def __init__(
        self,
        job_id: int,
        op: str,
        fn: Callable[[], Any] | None,
        urgent: bool,
        loop: asyncio.AbstractEventLoop | None,
        waiter: asyncio.Future[Any] | None,
        retain: bool = True,
    ) -> None:
        self.job_id = job_id
        self.op = op
        self.fn = fn
        self.urgent = urgent
        self.retain = retain
        self.loop = loop
        self.waiter = waiter
        self.value: Any = None
        self.exc: BaseException | None = None
        self.settled = False
        self.caller_cancelled = False
        self.meta: _Meta | None = None


def _resolve(job: _Job) -> None:
    """On the loop thread: hand the settled outcome to the job's waiter."""
    waiter = job.waiter
    if waiter is not None and not waiter.done():
        if job.exc is not None:
            waiter.set_exception(job.exc)
            waiter.exception()  # retrieved: an unawaited failure must not log at GC
        else:
            waiter.set_result(job.value)
    if not job.retain:  # nothing holds a copy: a non-retained outcome is dropped here
        job.value = None
        job.exc = None


def _set_if_pending(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


def _notify(
    loop: asyncio.AbstractEventLoop | None, callback: Callable[..., Any], *args: Any
) -> None:
    if loop is None:
        return
    try:
        loop.call_soon_threadsafe(callback, *args)
    except RuntimeError:  # the loop is closed: the outcome stays retained only
        return


class JobTicket:
    """A submitted job.  ``wait`` awaits its outcome; cancelling ``wait`` never cancels it."""

    __slots__ = ("_job", "_runner")

    def __init__(self, runner: CoreRunner, job: _Job) -> None:
        self._runner = runner
        self._job = job

    @property
    def job_id(self) -> int:
        return self._job.job_id

    async def wait(self) -> Any:
        job = self._job
        waiter = job.waiter
        assert waiter is not None
        try:
            value = await asyncio.shield(waiter)
        except asyncio.CancelledError:
            self._runner._caller_cancelled(job)
            raise
        except Exception:
            self._runner._delivered(job)
            raise
        self._runner._delivered(job)
        return value

    def __repr__(self) -> str:
        return f"JobTicket(job={self._job.job_id}, op={self._job.op})"


class CoreRunner:
    """One worker thread, one job at a time, results kept until reconciled."""

    OPS = frozenset(
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
    )

    def __init__(self, *, capacity: int = 8, retain: int = 16) -> None:
        if (
            type(capacity) is not int
            or capacity < 1
            or type(retain) is not int
            or retain < 1
        ):
            raise ValueError("capacity and retain must be positive ints")
        self._capacity = capacity
        self._retain = retain
        self._cond = threading.Condition()
        self._queue: collections.deque[_Job] = collections.deque()
        self._pending: dict[int, _Job] = {}
        self._active: _Job | None = None
        self._thread: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._exited = False
        self._sentinel_queued = False
        self._next_id = 1
        self._meta: collections.deque[_Meta] = collections.deque(maxlen=retain)
        self._held: collections.OrderedDict[int, _Job] = collections.OrderedDict()
        self._idle_waiters: list[
            tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]
        ] = []
        self._exit_waiters: list[
            tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]
        ] = []

    def __repr__(self) -> str:
        return f"CoreRunner(depth={self.depth}, inflight={self.inflight_op})"

    # -- lifecycle -------------------------------------------------------------------
    def start(self) -> None:
        with self._cond:
            if self._started:
                raise RunnerError("runner_started")
            if self._closed:
                raise RunnerError("runner_closed")
            thread = threading.Thread(
                target=self._work, name="pet-core-runner", daemon=False
            )
            self._thread = thread
            self._started = True
        thread.start()

    def _enqueue_sentinel_locked(self) -> None:
        if not self._sentinel_queued:
            self._sentinel_queued = True
            self._queue.append(_Job(0, "close", None, False, None, None))
            self._cond.notify_all()

    async def aclose(self) -> None:
        """Reject new work, wait for every queued/in-flight job to settle, join the thread."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] | None = None
        with self._cond:
            self._closed = True
            if self._started and not self._exited:
                self._enqueue_sentinel_locked()
                future = loop.create_future()
                self._exit_waiters.append((loop, future))
        if future is not None:
            await asyncio.shield(future)  # a cancelled close leaves the thread running
        self._join(_JOIN_S)

    def close(self, timeout: float | None = None) -> bool:
        """Sync close: wait for settlement and join; ``True`` when the thread is gone."""
        with self._cond:
            self._closed = True
            if self._started and not self._exited:
                self._enqueue_sentinel_locked()
        return self._join(timeout)

    def _join(self, timeout: float | None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        if thread is not threading.current_thread():
            thread.join(timeout)
        return not thread.is_alive()

    # -- submit / run ----------------------------------------------------------------
    def submit(
        self,
        op: str,
        fn: Callable[[], Any],
        *,
        urgent: bool = False,
        retain: bool = True,
    ) -> JobTicket:
        """Queue ``fn`` for the worker and return at once (synchronously, on the loop).

        Normal jobs are FIFO and bounded by ``capacity`` (``runner_busy``, nothing queued).
        An urgent job goes to the front (behind other urgent jobs), is NOT counted against
        the capacity, and an urgent job whose op is already queued (not started) is coalesced
        into it (the ORIGINAL job's ``retain`` value stays).  Submission itself is what
        orders the job, so a caller that never awaits it still has its job run.

        ``retain=False`` jobs never enter the held-result reservoir (only the fixed
        vocabulary metadata row is kept); the default keeps the earlier behaviour.
        """
        if op not in self.OPS:
            raise ValueError("unknown op")
        if not callable(fn):
            raise TypeError("fn must be callable")
        loop = asyncio.get_running_loop()
        with self._cond:
            existing = self._admit_locked(op, urgent)
            if existing is not None:
                return JobTicket(self, existing)  # coalesced into a queued urgent job
            job = _Job(
                self._next_id, op, fn, urgent, loop, loop.create_future(), retain
            )
            self._next_id += 1
            self._pending[job.job_id] = job
            self._queue.insert(self._front_index() if urgent else len(self._queue), job)
            self._cond.notify_all()
        return JobTicket(self, job)

    def _admit_locked(self, op: str, urgent: bool) -> _Job | None:
        """Admission checks (lock held).  Returns a queued urgent job to coalesce into."""
        if self._closed:
            raise RunnerError("runner_closed")
        if not self._started:
            raise RunnerError("runner_not_started")
        if urgent:
            return next((q for q in self._queue if q.urgent and q.op == op), None)
        if (
            sum(1 for j in self._queue if not j.urgent and j.fn is not None)
            >= self._capacity
        ):
            raise RunnerError("runner_busy")
        return None

    def _front_index(self) -> int:
        """Where an urgent job goes: the front, behind urgent jobs already queued."""
        index = 0
        for queued in self._queue:
            if not queued.urgent:
                break
            index += 1
        return index

    async def run(
        self,
        op: str,
        fn: Callable[[], T],
        *,
        urgent: bool = False,
        retain: bool = True,
    ) -> T:
        """``submit`` then await.  Cancelling the caller cancels ONLY the caller."""
        return await self.submit(op, fn, urgent=urgent, retain=retain).wait()

    # -- worker ----------------------------------------------------------------------
    def _work(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                job = self._queue.popleft()
                if job.fn is None:
                    self._exited = True
                    exits, self._exit_waiters = self._exit_waiters, []
                    break
                self._active = job
            value: Any = None
            exc: BaseException | None = None
            try:
                value = job.fn()
            except Exception as err:  # noqa: BLE001 - the awaiter receives this very object
                exc = err
            except BaseException:  # noqa: BLE001 - never kill the worker; fixed code only
                exc = RunnerError("runner_interrupted")
            code = _safe_code(value, exc)
            with self._cond:
                job.value, job.exc, job.settled = value, exc, True
                meta = _Meta(job.job_id, job.op, "ok" if exc is None else "error", code)
                meta.caller_cancelled = job.caller_cancelled
                job.meta = meta
                self._meta.append(meta)
                self._pending.pop(job.job_id, None)
                self._hold_locked(job)
                self._active = None
                idle = [] if self._queue else self._idle_waiters
                if idle:
                    self._idle_waiters = []
            _notify(job.loop, _resolve, job)
            for loop, future in idle:
                _notify(loop, _set_if_pending, future)
        for loop, future in exits:
            _notify(loop, _set_if_pending, future)

    def _hold_locked(self, job: _Job) -> None:
        """A settled job drops its callable; only a retained job keeps its outcome."""
        job.fn = None  # the closure (it may capture a transcript) is never kept
        if not job.retain:
            return
        self._held[job.job_id] = job
        while len(self._held) > self._retain:
            self._held.popitem(last=False)

    # -- caller bookkeeping ----------------------------------------------------------
    def _caller_cancelled(self, job: _Job) -> None:
        with self._cond:
            job.caller_cancelled = True
            if job.meta is not None:
                job.meta.caller_cancelled = True

    def _delivered(self, job: _Job) -> None:
        """The awaiter received the outcome: drop the held copy (and any text with it)."""
        with self._cond:
            if self._held.pop(job.job_id, None) is not None:
                job.value = None
                job.exc = None

    # -- inspection ------------------------------------------------------------------
    @property
    def inflight_op(self) -> str | None:
        with self._cond:
            return None if self._active is None else self._active.op

    @property
    def depth(self) -> int:
        with self._cond:
            return sum(1 for j in self._queue if j.fn is not None)

    @property
    def idle(self) -> bool:
        with self._cond:
            return self._idle_locked()

    def _idle_locked(self) -> bool:
        return self._active is None and not any(j.fn is not None for j in self._queue)

    async def wait_idle(self) -> None:
        """Return once nothing is queued or running (the worker no longer owns a call)."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        with self._cond:
            if self._idle_locked():
                return
            entry = (loop, future)
            self._idle_waiters.append(entry)
        try:
            await future
        finally:
            with self._cond:
                if entry in self._idle_waiters:
                    self._idle_waiters.remove(entry)

    def retained(self) -> tuple[RetainedResult, ...]:
        """The last ``retain`` jobs, oldest first.  Metadata only; fixed codes only."""
        with self._cond:
            return tuple(
                RetainedResult(m.job, m.op, m.outcome, m.code, m.caller_cancelled)
                for m in self._meta
            )

    def held_results(self) -> tuple[int, ...]:
        """Job ids whose real outcome is still held, unreconciled (at most ``retain``)."""
        with self._cond:
            return tuple(self._held)

    def take_result(self, job_id: int) -> Any:
        """Reconcile a settled job: return its actual value or raise its actual exception.

        Exactly once per job.  ``runner_not_settled`` while it is queued or running;
        ``runner_result_unavailable`` when it was delivered to its caller, already taken,
        unknown, or evicted from the bounded (``retain``) store.  A cancelled caller's
        ``CommandResult`` therefore survives until it is taken.
        """
        with self._cond:
            if job_id in self._pending:
                raise RunnerError("runner_not_settled")
            job = self._held.pop(job_id, None)
        if job is None:
            raise RunnerError("runner_result_unavailable")
        if job.exc is not None:
            raise job.exc
        return job.value


class UnifiedOps:
    """The sync adapter to ``core.Bridge`` + companion; call it ONLY through ``CoreRunner``.

    ``binding`` is the immutable ``core.Binding`` verified at startup (replaced only
    by the strict same-host follow at a new device session boundary). Before ``begin``,
    ``resume``, ``preflight``, ``confirm`` and ``poll`` it is compared (never repr'd) with
    the live ``Bridge.status`` (binding id, session id, host id) and, for the three Unified
    calls, with the credential attachment loaded from the store (client id, host identity,
    host instance id, credential ref).  ``None`` or any difference fails closed with a fixed
    code and nothing is sent: ``core_binding_required`` / ``binding_mismatch`` /
    ``credential_missing``.  A later ``select_session`` can therefore never redirect this
    adapter to another session.
    """

    def __init__(
        self,
        bridge: core.Bridge,
        store: Any,
        credential_ref: str,
        session_id: str,
        client_factory: Callable[[], Any],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        binding: core.Binding | None = None,
    ) -> None:
        self._bridge = bridge
        self._store = store
        self._credential_ref = credential_ref
        self._session_id = session_id
        self._client_factory = client_factory
        self._monotonic = monotonic
        self._binding = binding
        self._observed_reply: tuple[str, companion.DetailWindow] | None = None

    def __repr__(self) -> str:
        return "UnifiedOps(<hidden>)"

    def follow_host_restart(
        self, follow: Callable[[core.Binding], core.Binding]
    ) -> tuple[str, str] | None:
        """Session boundary only, on CoreRunner: follow, then replace the frozen pin.

        No send or reconciliation is performed here. The callback is the same strict
        same-host follow used at gateway startup, never general session retargeting.
        """
        _attachment, reason = self._attachment()
        if reason is not None:
            raise core.BridgeError("core_binding_required")
        old = self._binding
        assert old is not None
        new = follow(old)
        if (
            new.host_id, new.session_id, new.client_id, new.credential_ref
        ) != (
            old.host_id, old.session_id, old.client_id, old.credential_ref
        ):
            raise core.BridgeError("core_binding_required")
        self._binding = new
        _attachment, reason = self._attachment()
        if reason is not None:
            self._binding = old
            raise core.BridgeError("core_binding_required")
        if new.host_instance_id != old.host_instance_id:
            return old.host_instance_id, new.host_instance_id
        return None

    # -- binding guards --------------------------------------------------------------
    def _bridge_reason(self) -> str | None:
        frozen = self._binding
        if frozen is None:
            return "core_binding_required"
        if (
            frozen.session_id != self._session_id
            or frozen.credential_ref != self._credential_ref
        ):
            return "binding_mismatch"
        live = self._bridge.status()
        if not (
            live.get("bound") is True
            and live.get("binding_id") == frozen.binding_id
            and live.get("session_id") == frozen.session_id
            and live.get("host_id") == frozen.host_id
        ):
            return "binding_mismatch"
        return None

    def _attachment(self) -> tuple[Any, str | None]:
        reason = self._bridge_reason()
        if reason is not None:
            return None, reason
        frozen = self._binding
        assert frozen is not None
        try:
            attachment = self._store.load_attachment(self._credential_ref)
        except Exception:  # noqa: BLE001 - fixed code; no cause chain, no detail
            return None, "credential_missing"
        if attachment is None:
            return None, "credential_missing"
        if not (
            attachment.client_id == frozen.client_id
            and attachment.host_identity == frozen.host_id
            and attachment.host_instance_id == frozen.host_instance_id
            and attachment.credential_ref == frozen.credential_ref
        ):
            return None, "binding_mismatch"
        return attachment, None

    def _client(self) -> Any:
        try:
            return self._client_factory()
        except Exception:  # noqa: BLE001 - fixed code; no cause chain, no detail
            raise CompanionError("companion_request_failed") from None

    # -- core calls ------------------------------------------------------------------
    def begin(self) -> str:
        reason = self._bridge_reason()
        if reason is not None:
            raise core.BridgeError(reason)
        try:
            return self._bridge.begin_turn()
        except core.BridgeError as err:
            # An earlier send with a lost acknowledgement blocks new turns.  If the
            # conversation proves it was delivered, it is no longer uncertain.
            if err.code != "uncertain_unresolved" or not self._reconcile():
                raise
        return self._bridge.begin_turn()

    def begin_after_timeout(self, command_id: str, closed: threading.Event) -> str:
        """Recheck this call's timed-out command before starting a distinct turn."""
        if closed.is_set():
            raise core.BridgeError("stopped")
        attachment, reason = self._attachment()
        if reason is not None:
            raise core.BridgeError(reason)
        state = self._bridge.timed_out_reply_state(command_id, self._binding)
        if state == "pending":
            try:
                window = companion.detail_window(self._client(), attachment, self._session_id)
            except CompanionError:
                raise core.BridgeError("reply_pending") from None
            # The credential/host pin can change during the read.
            _attachment, reason = self._attachment()
            if reason is not None:
                raise core.BridgeError(reason)
            if closed.is_set():
                raise core.BridgeError("stopped")
            if not self._bridge.settle_timed_out_reply(command_id, self._binding, window):
                raise core.BridgeError("reply_pending")
        elif state != "cleared":
            raise core.BridgeError("reply_pending")
        # Recheck epoch/binding atomically with the ordinary turn gate, including
        # uncertainty and journal faults.
        if closed.is_set():
            raise core.BridgeError("stopped")
        return self._bridge.begin_after_timed_out_reply(command_id, self._binding)

    def _reconcile(self, *, pre_dial: bool = False) -> tuple[str, ...]:
        """One read of the conversation; the ids of sends it proves were delivered."""
        attachment, reason = self._attachment()
        if reason is not None:
            return ()
        try:
            window = companion.detail_window(
                self._client(), attachment, self._session_id
            )
            if pre_dial:
                # The pre-dial boundary requires a complete, validated read, not
                # merely the newest tail or a partial/inconsistent snapshot.
                if window.coverage_complete is not True:
                    return ()
                return self._bridge.reconcile_pre_dial(
                    self._binding, window.as_page(), session_id=self._session_id
                )
            return self._bridge.reconcile_delivered(
                window.as_page(), session_id=self._session_id
            )
        except (CompanionError, core.BridgeError):
            return ()

    def stage(self, turn_id: str, segments: Any) -> str:
        """Append each ``(event_id, text)`` in order; return the first non-``appended`` code."""
        for event_id, text in segments:
            code = self._bridge.append_transcript(turn_id, text, event_id)
            if code != "appended":
                return code
        return "appended"

    def abandon(self, turn_id: str) -> None:
        self._bridge.abandon_turn(turn_id)

    def timeout_reply(self, turn_id: str) -> None:
        # Serialized behind any observation/claim and before new input.
        self._observed_reply = None
        self._bridge.abandon_turn(turn_id)

    def seal(self, turn_id: str) -> core.TranscriptPreview:
        return self._bridge.seal_turn(turn_id)

    def preflight(self) -> Preflight:
        session = self._session_id
        attachment, reason = self._attachment()
        if reason is not None:
            return Preflight(session, -1, False, reason)
        try:
            listing = companion.list_sessions(
                self._client(), attachment, monotonic=self._monotonic
            )
            if not listing.complete:
                return Preflight(session, listing.revision, False, "list_incomplete")
            return companion.preflight(listing, session, monotonic=self._monotonic)
        except CompanionError as err:
            return Preflight(session, -1, False, err.code)

    def confirm(self, turn_id: str, sha: str, revision: int) -> core.CommandResult:
        """Send an ordinary native-client message, not an app-global CAS mutation.

        ``revision`` is the observed listing revision for adapter compatibility;
        it is NOT a session admission token. Preflight rejects observed busy work,
        but Companion-v1 cannot atomically prevent a later steering race.
        """
        _attachment, reason = self._attachment()
        if reason is not None:
            return core.CommandResult(turn_id, "refused", reason, False)
        result = self._bridge.confirm_send(
            turn_id, expected_text_sha256=sha, expected_revision=None
        )
        if result.outcome == "uncertain" and turn_id in self._reconcile():
            # The acknowledgement was lost but the conversation shows this exact input.
            return core.CommandResult(
                turn_id, "accepted", "observed_in_conversation", True,
                dict(result.metadata),
            )
        return result

    def poll(self, turn_id: str) -> core.ReplyResult:
        attachment, reason = self._attachment()
        if reason is not None:
            return core.ReplyResult(turn_id, "not_spoken", reason, True, -1)
        try:
            window = companion.detail_window(
                self._client(), attachment, self._session_id
            )
        except CompanionError as err:
            return core.ReplyResult(turn_id, "pending", err.code, False, -1)
        return self._bridge.poll_reply(
            turn_id,
            window.as_page(),
            coverage_complete=window.coverage_complete,
            newest_tail=window,
            session_id=self._session_id,
        )

    def observe_reply(self, turn_id: str) -> core.ReplyResult:
        """A conversational poll never consumes or exports reply text."""
        self._observed_reply = None
        attachment, reason = self._attachment()
        if reason is not None:
            return core.ReplyResult(turn_id, "not_spoken", reason, True, -1)
        try:
            window = companion.detail_window(self._client(), attachment, self._session_id)
        except CompanionError as err:
            return core.ReplyResult(turn_id, "pending", err.code, False, -1)
        _attachment, reason = self._attachment()
        if reason is not None:
            return core.ReplyResult(turn_id, "not_spoken", reason, True, -1)
        result = self._bridge.observe_reply(turn_id, window, session_id=self._session_id)
        if result.status == "ready":
            self._observed_reply = (turn_id, window)
        return result

    def claim_observed_reply(self, turn_id: str) -> core.ReplyResult:
        observed, self._observed_reply = self._observed_reply, None
        _attachment, reason = self._attachment()
        if reason is not None or observed is None or observed[0] != turn_id:
            return core.ReplyResult(turn_id, "not_spoken", reason or "reply_unavailable", False, -1)
        window = observed[1]
        return self._bridge.poll_reply(
            turn_id, window.as_page(), coverage_complete=window.coverage_complete,
            newest_tail=window, session_id=self._session_id,
        )

    def epoch_current(self, epoch: int) -> bool:
        return self._bridge.epoch_current(epoch)

    def stop(self) -> None:
        self._observed_reply = None
        self._bridge.stop()

    def resume(self) -> None:
        reason = self._bridge_reason()
        if reason is not None:
            raise core.BridgeError(reason)
        self._bridge.resume()

    def status(self) -> dict[str, Any]:
        return self._bridge.status()

    def pre_dial_safe(self) -> bool:
        """A local, non-mutating admission check against the frozen session binding."""
        if (
            self._binding is None
            or self._binding.session_id != self._session_id
            or self._binding.credential_ref != self._credential_ref
        ):
            return False
        return self._bridge.pre_dial_safe(self._binding)

    def pre_dial_admission(self) -> bool:
        """One bounded positive-proof reconciliation, then the original gate again."""
        if (
            self._binding is None
            or self._binding.session_id != self._session_id
            or self._binding.credential_ref != self._credential_ref
        ):
            return False
        state = self._bridge.pre_dial_state(self._binding)
        if state == "safe":
            return self.pre_dial_safe()
        if state != "uncertain":
            return False
        promoted = self._reconcile(pre_dial=True)
        safe = self.pre_dial_safe()
        return bool(promoted) and safe


class LoopClock(Protocol):
    """The only source of time and waiting for a session (inject a fake in tests)."""

    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    """The production clock: the running loop's monotonic time and ``asyncio.sleep``."""

    def now(self) -> float:
        return asyncio.get_running_loop().time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class AudioOverflow(Exception):
    """The audio queue exceeded its cap: the gate is closed and the queue emptied."""

    code = "overflow"

    def __str__(self) -> str:
        return self.code


class AudioOut:
    """Gated, paced, bounded provider-audio queue for a reply or continuous stream.

    Audio offered while the gate is closed (never opened, drained or sealed) is DROPPED, never
    buffered.  ``run()`` is the pacer: chunks of at most 4800 bytes at 48000 B/s, so no two
    send STARTS are closer than 0.1 s; a drain or close while a chunk waits for its slot (or
    for a blocked device) discards it.  ``sent_bytes`` counts only the bytes whose device
    send actually COMPLETED (not queued, not merely offered); ``offered_bytes`` is what the
    gate accepted.  ``drained`` is set when nothing is queued or being sent.

    ``open(hold=True)`` accepts and buffers audio (under the same byte cap) but sends
    nothing until ``release(token)`` is called with the token ``open`` returned; a token
    from an earlier epoch can never release or reopen a later gate.  ``lock`` (the
    device-frame lock when shared with the outbox) is taken for every send and the epoch
    is re-checked once it is held, so a chunk that waited behind another frame while a
    drain ran is dropped, never sent and never counted.
    """

    def __init__(
        self,
        send: Callable[[str], Awaitable[None]],
        clock: LoopClock,
        *,
        cap: int = gs.AUDIO_QUEUE_CAP,
        lock: asyncio.Lock | None = None,
        on_sent: Callable[[bytes], None] | None = None,
    ) -> None:
        self._send = send
        self._clock = clock
        self._cap = cap
        self._lock = lock if lock is not None else asyncio.Lock()
        self._on_sent = on_sent
        self._released = asyncio.Event()
        self._queue: collections.deque[tuple[int, bytes]] = collections.deque()
        self._queued = 0
        self._held = 0
        self._epoch = 0
        self._open = False
        self._sealed = False
        self._sending = False
        self._sent = 0
        self._offered = 0
        self._dropped = 0
        self._last_offer = -math.inf
        self._data = asyncio.Event()
        self._drained = asyncio.Event()
        self._drained.set()

    def __repr__(self) -> str:
        return (
            f"AudioOut(open={self._open}, queued={self.queued_bytes}, "
            f"sent={self._sent}, offered={self._offered})"
        )

    @property
    def is_open(self) -> bool:
        return self._open and not self._sealed

    @property
    def queued_bytes(self) -> int:
        return self._queued + self._held

    @property
    def sent_bytes(self) -> int:
        return self._sent

    @property
    def offered_bytes(self) -> int:
        return self._offered

    @property
    def dropped_bytes(self) -> int:
        return self._dropped

    @property
    def last_offer_t(self) -> float:
        return self._last_offer

    @property
    def drained(self) -> asyncio.Event:
        return self._drained

    def _refresh(self) -> None:
        if self._queued == 0 and self._held == 0 and not self._sending:
            self._drained.set()
        else:
            self._drained.clear()

    def open(self, *, hold: bool = False) -> int:
        """Open the gate; returns the epoch token for ``release``.

        ``hold=True`` buffers without sending until ``release``; the default releases at once.
        """
        self._open = True
        self._sealed = False
        if hold:
            self._released.clear()
        else:
            self._released.set()
        return self._epoch

    def release(self, token: int) -> bool:
        """Let a held gate send.  ``False`` (and no change) for a stale token or closed gate.

        A SEALED gate is still released (what it already queued must play out, see
        ``seal``); sealing never makes it accept new audio and ``close_and_drain`` (Stop)
        bumps the epoch, so a stale token can never release anything.
        """
        if token != self._epoch or not self._open or self._released.is_set():
            return False
        self._released.set()
        return True

    def seal(self) -> None:
        """No new audio; what is already queued still plays out."""
        self._sealed = True

    def close_and_drain(self) -> int:
        """Close the gate and discard everything queued; returns the dropped byte count."""
        dropped = self._queued + self._held
        self._dropped += dropped
        self._queue.clear()
        self._queued = 0
        self._held = 0
        self._epoch += 1
        self._open = False
        self._sealed = False
        self._released.clear()
        self._data.clear()
        self._refresh()
        return dropped

    def offer(self, pcm: bytes) -> bool:
        """Queue provider PCM if the gate is open; ``False`` when dropped.

        Raises ``AudioOverflow`` (after closing and draining) beyond the byte cap.
        """
        if not self.is_open:
            self._dropped += len(pcm)
            return False
        try:
            chunks = gs.split_pcm(pcm)
        except ValueError:
            self._dropped += len(pcm)
            return False
        if self._queued + self._held + len(pcm) > self._cap:
            self._dropped += len(pcm)
            self.close_and_drain()
            raise AudioOverflow()
        for chunk in chunks:
            self._queue.append((self._epoch, chunk))
        self._queued += len(pcm)
        self._offered += len(pcm)
        self._last_offer = self._clock.now()
        self._data.set()
        self._drained.clear()
        return True

    def discard(self, pcm: bytes) -> None:
        """Account receive-ineligible PCM without touching a later open epoch."""
        self._dropped += len(pcm)

    async def run(self) -> None:
        """The pacer.  A failed device send ends it with that error (nothing is counted)."""
        prev_next: float | None = None
        while True:
            if not self._queue:
                self._data.clear()
                self._refresh()
                await self._data.wait()
                continue
            if not self._released.is_set():  # held gate: nothing is popped or sent
                await self._released.wait()
                continue  # re-evaluate: a drain may have emptied the queue meanwhile
            epoch, chunk = self._queue.popleft()
            self._queued -= len(chunk)
            self._held = len(chunk)
            now = self._clock.now()
            send_at, prev_next = gs.next_send_time(prev_next, now, len(chunk))
            delay = send_at - now
            if delay > _EPS:
                await self._clock.sleep(delay)
            if await self._send_chunk(epoch, chunk):
                self._sent += len(chunk)
                if self._on_sent is not None:
                    self._on_sent(chunk)

    async def _send_chunk(self, epoch: int, chunk: bytes) -> bool:
        """Send one chunk under the device lock; ``False`` when a drain already dropped it."""
        async with self._lock:
            if epoch != self._epoch:
                return False  # drained while it waited: already counted as dropped
            self._held = 0
            self._sending = True
            try:
                await self._send(gs.build_audio_delta(chunk))
            finally:
                self._sending = False
                self._refresh()
        return True


@dataclass(frozen=True)
class DeviceMessage:
    """One inbound device WebSocket message: ``text`` | ``binary`` | ``closed``."""

    kind: str
    data: str | None = field(default=None, repr=False)


class DeviceLink(Protocol):
    async def send_text(self, text: str) -> None: ...

    async def receive(self) -> DeviceMessage: ...

    async def close(self, code: int) -> None: ...


@dataclass(frozen=True)
class ProviderMessage:
    """Provider message: ``text``, ``invalid``, or ``closed`` (clean = code 1000)."""

    kind: str
    data: str | None = field(default=None, repr=False)
    clean: bool = False


class ProviderLink(Protocol):
    async def send_text(self, text: str) -> None: ...

    async def receive(self) -> ProviderMessage: ...

    async def close(self) -> None: ...


class SessionRunError(Exception):
    """``PetSession.run`` is once only: a second call never reopens the provider or core."""

    code = "session_already_run"

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class SessionSettings:
    """Fixed per-gateway values: ``short8`` is the validated last-8-hex session suffix."""

    model: str
    voice: str
    label: str
    short8: str


@dataclass(frozen=True)
class SessionOutcome:
    """Metadata only: never transcript, reply text, audio, token or full session id.

    ``send_outcome`` is ``None`` | ``inflight`` | ``accepted`` | ``rejected`` |
    ``uncertain`` | ``refused`` (``refused`` = proven that nothing was submitted to the
    core).  ``confirm_job`` is the runner job id whose real outcome is still unreconciled
    exactly while ``send_outcome == "inflight"``: after ``CoreRunner.wait_idle`` the
    server takes it ONCE with ``CoreRunner.take_result`` (never resend, never a new id).
    """

    final_state: str
    turn_started: bool
    send_outcome: str | None
    reply_done: str | None
    error_code: str | None
    close_confirmed: bool
    audio_in_bytes: int
    audio_out_bytes: int
    dropped_audio_bytes: int
    confirm_job: int | None = None
    # First local dispatcher path that entered closing, not provider content or
    # proof of a remote root cause. Cleanup confirmation must not overwrite it.
    close_origin: str | None = None
    # Conversation counters: submitted = confirm jobs admitted; sent = settled
    # accepted/rejected/uncertain attempts (possible transport sends), excluding
    # known-unsent refusals. If confirm_job is set, its outcome still needs the
    # existing server reconciliation; no terminal outcome is guessed here.
    conversation: bool = False
    requests_submitted: int = 0
    requests_sent: int = 0
    requests_accepted: int = 0
    requests_uncertain: int = 0
    requests_refused: int = 0
    # ACKed commentary + audible output followed by quiet, not proof of hearing.
    replies_rendered: int = 0
    conversation_end: str | None = None


# Shared request/lifecycle states; conversation mode reuses the request states.
_AWAIT_START = "AWAIT_START"
_STARTING = "STARTING"
_READY = "READY"
_CAPTURING = "CAPTURING"
_SETTLING = "SETTLING"
_PREVIEW = "PREVIEW"
_SENDING = "SENDING"
_AWAIT_REPLY = "AWAIT_REPLY"
_SPEAKING = "SPEAKING"
_CLOSING = "CLOSING"
_CLOSED = "CLOSED"
_CONVERSE_RECOVERING = "CONVERSE_RECOVERING"
_INVALID_PROVIDER_KINDS = frozenset({"invalid", "invalid_audio", "invalid_transcript"})

# After the shared close deadline there is no more clock waiting: an unblocked peer gets
# this many event-loop TURNS (not seconds) to finish its final frames, a blocked one is
# cancelled.  A turn count, not a wall-clock proof.
_GRACE_TURNS = 128
_DEVICE_OUTBOX_CAP = 64
_PROVIDER_OUTBOX_CAP = 512
_CLOSE_CODES = {
    "unknown_command": 1008,
    "model_mismatch": 1008,
    "busy": 1008,
    "invalid_preview": 1008,
    "deadline": 1011,
    "overflow": 1011,
}
_REFUSED_CODES = {  # a refused confirm sent nothing (plan 6.5)
    "command_inflight": "busy",
    "uncertain_unresolved": "busy",
    "reply_pending": "busy",
    "bridge_faulted": "busy",
    "expired": "expired",
}
# EXACTLY the refusal reasons the core / UnifiedOps produce BEFORE any transport call: only
# these prove nothing was sent.  Any other refusal (duplicate_confirm, journal_error,
# unknown_turn, an unmapped reason...) is NOT such proof and is reported as ``uncertain``.
_KNOWN_UNSENT = frozenset(
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
_STAGER_FAIL = {
    "conflict": "unstable_transcript",
    "overlap": "unstable_transcript",
    "invalid": "unstable_transcript",
    "too_many": "overflow",
    "too_long": "overflow",
}


class _Outbox:
    """A bounded FIFO drained by ONE tracked sender task.

    The dispatcher only ``put``s (never awaits a blocked link), so a stalled device or
    provider can never stop timers or deadlines.  ``pending_audio`` counts decoded input
    audio bytes queued OR in flight; ``clear`` drops what is not yet being sent.
    """

    def __init__(self, send: Callable[[str], Awaitable[None]], cap: int) -> None:
        self._send = send
        self._cap = cap
        self._queue: collections.deque[tuple[str, int, str | None]] = (
            collections.deque()
        )
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._busy = False
        self.pending_audio = 0

    def _refresh(self) -> None:
        if self._queue or self._busy:
            self._idle.clear()
        else:
            self._idle.set()

    def put(self, text: str, *, audio: int = 0, tag: str | None = None) -> bool:
        if len(self._queue) >= self._cap:
            return False
        self._queue.append((text, audio, tag))
        self.pending_audio += audio
        self._refresh()
        self._wake.set()
        return True

    def clear(self) -> int:
        dropped = len(self._queue)
        for _text, audio, _tag in self._queue:
            self.pending_audio -= audio
        self._queue.clear()
        self._refresh()
        return dropped

    async def flush(self) -> None:
        await self._idle.wait()

    async def run(
        self, on_sent: Callable[[str | None], None], on_error: Callable[[], None]
    ) -> None:
        while True:
            if not self._queue:
                self._wake.clear()
                self._refresh()
                await self._wake.wait()
                continue
            text, audio, tag = self._queue.popleft()
            self._busy = True
            try:
                await self._send(text)
            except Exception:  # noqa: BLE001 - a dead link: report once, fixed code only
                on_error()
                return
            finally:
                self._busy = False
                self.pending_audio -= audio
                self._refresh()
            on_sent(tag)


class PetSession:
    """One authenticated WebSocket: explicit one-turn or running-conversation mode.

    A single dispatcher consumes a bounded inbox (device frames, provider events), internal
    results and timers one event at a time.  Handlers are synchronous: they never await
    blocking core work.  Every runner call is submitted as a tracked task that posts its
    result back as an event, every frame goes through a bounded outbox, and every wait goes
    through the injected ``LoopClock``, so ``Stop`` and startup / mode-cap / close deadlines
    fire even when a core call, a device send or a provider send is blocked.  The runner
    may still own an in-flight call when ``run`` returns; the server holds its global client
    lock until ``CoreRunner.wait_idle``.
    """

    def __init__(
        self,
        device: DeviceLink,
        provider_connect: Callable[[], Awaitable[ProviderLink]],
        runner: CoreRunner,
        ops: UnifiedOps,
        settings: SessionSettings,
        clock: LoopClock,
        *,
        new_event_id: Callable[[], str] = gp.new_event_id,
    ) -> None:
        self._device = device
        self._provider_connect = provider_connect
        self._runner = runner
        self._ops = ops
        self._settings = settings
        self._clock = clock
        self._new_event_id = new_event_id
        self._state = _AWAIT_START
        self._closed_for_recovery = threading.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._timers: dict[str, asyncio.Task[Any]] = {}
        self._connect_task: asyncio.Task[Any] | None = None
        self._wake = asyncio.Event()
        self._inbox: collections.deque[tuple[Any, ...]] = collections.deque()
        self._internal: collections.deque[tuple[Any, ...]] = collections.deque()
        self._finalize_flag = False
        self._dlock = asyncio.Lock()
        self._plock = asyncio.Lock()
        self._dout = _Outbox(self._device_send, _DEVICE_OUTBOX_CAP)
        self._pout = _Outbox(self._provider_send, _PROVIDER_OUTBOX_CAP)
        # one device lock for every outbound frame: audio and control frames never interleave
        self._audio = AudioOut(
            self._device.send_text, clock, lock=self._dlock, on_sent=self._on_audio_sent
        )
        self._provider: ProviderLink | None = None
        self._provider_started = False
        self._provider_start_id: str | None = None
        self._provider_session_id: str | None = None
        self._resume_submitted = False
        self._fatal = False
        self._device_gone = False
        self._device_code = 1000
        self._accepted_t: float | None = None
        self._turn_id: str | None = None
        self._timed_out_turn: str | None = None
        self._turn_used = False
        self._begin_pending = False
        self._stager = gs.TranscriptStager()
        self._stop_t = 0.0
        self._last_delta = -math.inf
        self._sealing = False
        self._preview_sha: str | None = None
        self._preview_rev = 0
        self._error_code: str | None = None
        self._close_origin: str | None = None
        self._audio_in = 0
        self._input_bytes = 0
        self._input_offset: float | None = None
        self._input_rate_offset: float | None = None
        self._input_catchup_until = -math.inf
        self._input_wait_t: float | None = None
        self._input_quiet_t = -math.inf
        self._provider_audio_until = -math.inf  # completion-derived input pacing
        self._dropped_in = 0
        self._close_confirmed = False
        self._provider_closed_seen = False
        self._send_state: str | None = None
        self._send_accepted = False
        self._send_accept_t = 0.0
        self._reply_done: str | None = None
        self._contaminated = False
        self._poll_inflight = False
        self._speak_pending = False
        self._reply_text: str | None = None
        self._reply_epoch = -1
        self._delegation_id: str | None = None
        self._delegation_ambiguous = False
        self._chunk_ids: tuple[str, ...] = ()
        self._chunks_sent: set[str] = set()
        self._acked: set[str] = set()
        self._gate_opened = False
        self._gate_check_pending = False
        self._ran = False  # run() is once only
        self._audio_token: int | None = None  # gate token from open(hold=True)
        self._commentary_committed = False  # capture eligibility, never playback permission
        self._send_stage = ""  # "" | "preflight" | "confirm" | "settled" | "unsent"
        self._send_result_emitted = False  # at most ONE pet.turn.send_result
        self._confirm_job: int | None = None  # runner job id while unreconciled here
        self._policy_close = False  # preview policy violation: close code 1008
        self._close_hard: float | None = None  # the ONE absolute close deadline
        self._close_committed = False  # the provider session.close frame really left
        self._overflow_posted = False
        self._shutdown_framed = False
        self._provider_close_called = False
        self._device_close_called = False
        self._device_reader: asyncio.Task[Any] | None = None
        self._provider_reader: asyncio.Task[Any] | None = None
        self._converse = False
        self._converse_end: str | None = None
        self._requests_sent = 0
        self._requests_submitted = 0
        self._requests_accepted = 0
        self._requests_uncertain = 0
        self._requests_refused = 0
        self._replies_rendered = 0
        self._last_speech = 0.0
        self._seen_delegations: set[str] = set()
        self._seen_transcripts: set[str] = set()
        self._transcript_end = -1
        self._transcript_floor = -1
        self._render_reply = False
        self._render_audible = False
        self._last_audible = -math.inf
        self._render_start = 0.0
        self._converse_audio_ready = False
        self._request_seq = 0
        self._notice_code: str | None = None
        self._events: dict[str, Callable[..., None]] = {
            "device": self._on_device,
            "provider": self._on_provider,
            "op": self._on_op,
            "confirm_done": self._on_confirm_done,
            "started_sent": self._on_started_sent,
            "timer": self._on_timer,
            "device_error": self._fail,
            "device_closed": self._device_lost,
            "device_send_failed": self._device_lost,
            "pacer_failed": self._device_lost,
            "provider_closed": self._on_provider_closed,
            "provider_failed": self._on_provider_failed,
            "sent": self._on_chunk_sent,
            "overflow": self._on_overflow,
            "shutdown": self._on_stop,
        }

    def __repr__(self) -> str:
        return f"PetSession(state={self._state})"

    async def request_shutdown(self) -> None:
        """Server shutdown hook: behaves like a device ``pet.stop``."""
        self._post(("shutdown",))

    # -- run ---------------------------------------------------------------------------
    async def run(self) -> SessionOutcome:
        """Run the one session.  Once only (``SessionRunError``).

        A cancelled or failed run still closes both links, queues the local core stop and
        cancels every task within the SAME bounded deadline, then re-raises; a second
        cancellation during that cleanup aborts it at once.  ``outcome`` stays readable.
        """
        if self._ran:
            raise SessionRunError()
        self._ran = True
        try:
            self._device_reader = self._spawn(self._read_device())
            self._spawn(self._dout.run(self._device_sent, self._device_send_failed))
            self._spawn(self._audio.run(), failed="pacer_failed")
            self._arm("await_start", gs.AWAIT_START_S)
            await self._dispatch_loop()
            await self._shutdown()
        except (asyncio.CancelledError, Exception):
            await self._abort_after_cancel()
            raise
        finally:
            await self._cancel_all()
            self._state = _CLOSED
        return self._outcome()

    async def _abort_after_cancel(self) -> None:
        """Bounded, shielded cleanup of an interrupted run (the caller re-raises)."""
        task = self._spawn(self._abort_tail())
        with contextlib.suppress(Exception):
            await asyncio.shield(task)  # a second cancel falls through to _cancel_all

    async def _abort_tail(self) -> None:
        if self._state not in (_CLOSING, _CLOSED):
            self._close_origin = self._close_origin or "interrupted"
            self._begin_close(
                1011, fatal=True
            )  # gate shut, core stop queued, close frame
        self._finalize()
        with contextlib.suppress(Exception):
            await self._shutdown()

    @property
    def outcome(self) -> SessionOutcome:
        """The metadata outcome so far; readable at ANY time, also after a cancelled ``run``."""
        return self._outcome()

    def _outcome(self) -> SessionOutcome:
        inflight = self._send_state == "inflight"
        return SessionOutcome(
            final_state=self._state,
            turn_started=self._turn_id is not None,
            send_outcome=self._send_state,
            reply_done=self._reply_done,
            error_code=self._error_code,
            close_confirmed=self._close_confirmed,
            audio_in_bytes=self._audio_in,
            audio_out_bytes=self._audio.sent_bytes,
            dropped_audio_bytes=self._audio.dropped_bytes + self._dropped_in,
            confirm_job=self._confirm_job if inflight else None,
            close_origin=self._close_origin,
            conversation=self._converse,
            requests_submitted=self._requests_submitted,
            requests_sent=self._requests_sent,
            requests_accepted=self._requests_accepted,
            requests_uncertain=self._requests_uncertain,
            requests_refused=self._requests_refused,
            replies_rendered=self._replies_rendered,
            conversation_end=self._converse_end,
        )

    async def _dispatch_loop(self) -> None:
        while not self._finalize_flag:
            self._wake.clear()
            while (self._internal or self._inbox) and not self._finalize_flag:
                event = (
                    self._internal.popleft()
                    if self._internal
                    else self._inbox.popleft()
                )
                self._handle(event)
            if self._finalize_flag:
                break
            await self._wake.wait()

    # -- tracked tasks, timers, bounded waits ------------------------------------------
    def _spawn(
        self, coro: Awaitable[Any], *, failed: str | None = None
    ) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)

        def done(finished: asyncio.Task[Any]) -> None:
            self._tasks.discard(finished)
            if finished.cancelled():
                return
            if finished.exception() is not None and failed is not None:
                self._post((failed,))

        task.add_done_callback(done)
        return task

    def _post(self, event: tuple[Any, ...]) -> None:
        if self._finalize_flag:
            return  # nothing is consumed any more: nothing may accumulate
        self._internal.append(event)
        self._wake.set()

    def _post_external(self, event: tuple[Any, ...]) -> None:
        if self._finalize_flag:
            return
        if len(self._inbox) >= gs.INBOX_CAP:
            if (
                not self._overflow_posted
            ):  # posted once, never a flood of overflow events
                self._overflow_posted = True
                self._post(("overflow",))
            return
        self._inbox.append(event)
        self._wake.set()

    def _arm(self, name: str, seconds: float) -> None:
        old = self._timers.pop(name, None)
        if old is not None:
            old.cancel()
        self._timers[name] = self._spawn(self._timer(name, seconds))

    def _disarm(self, name: str) -> None:
        old = self._timers.pop(name, None)
        if old is not None:
            old.cancel()

    def _cancel_timers(self) -> None:
        for name in list(self._timers):
            self._disarm(name)

    async def _timer(self, name: str, seconds: float) -> None:
        await self._clock.sleep(seconds)
        self._post(("timer", name))

    async def _within(self, awaitable: Awaitable[Any], deadline: float) -> bool:
        """Await ``awaitable`` until the ABSOLUTE ``deadline`` (clock time); True if it finished.

        The remaining time is recomputed from the clock at every call, so a chain of steps
        shares one deadline.  Once the deadline is reached there is no more clock waiting: an
        unblocked peer gets ``_GRACE_TURNS`` event-loop turns, a blocked one is cancelled (a
        turn count, not a wall-clock proof).  Work that did not finish is always cancelled.
        """
        work = asyncio.ensure_future(awaitable)
        timer: asyncio.Future[Any] | None = None
        try:
            remaining = deadline - self._clock.now()
            if remaining > _EPS:
                timer = asyncio.ensure_future(self._clock.sleep(remaining))
                await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
            else:
                for _ in range(_GRACE_TURNS):
                    if work.done():
                        break
                    await asyncio.wait({work}, timeout=0)
            finished = work.done() and not work.cancelled()
        finally:
            live = [item for item in (work, timer) if item is not None]
            for item in live:
                if not item.done():
                    item.cancel()
            await asyncio.gather(*live, return_exceptions=True)
        return finished

    async def _cancel_all(self) -> None:
        for _ in range(3):
            live = [task for task in self._tasks if not task.done()]
            if not live:
                self._timers.clear()
                return
            for task in live:
                task.cancel()
            await asyncio.gather(*live, return_exceptions=True)
        self._timers.clear()

    # -- link I/O ----------------------------------------------------------------------
    async def _device_send(self, text: str) -> None:
        async with self._dlock:
            await self._device.send_text(text)

    async def _provider_send(self, text: str) -> None:
        provider = self._provider
        assert provider is not None
        async with self._plock:
            await provider.send_text(text)

    def _device_send_failed(self) -> None:
        self._post(("device_send_failed",))

    async def _read_device(self) -> None:
        while True:
            try:
                message = await self._device.receive()
            except Exception:  # noqa: BLE001 - any link failure is a disconnect
                self._post(("device_closed",))
                return
            if message.kind == "closed":
                self._post(("device_closed",))
                return
            if message.kind != "text" or message.data is None:
                self._post_external(("device_error", "unknown_command"))
                continue
            try:
                frame = gs.parse_device_frame(
                    message.data, model=self._settings.model, voice=self._settings.voice
                )
            except gs.GatewayProtocolError as err:
                self._post_external(("device_error", err.code))
                continue
            if self._converse and frame.kind == "audio":
                self._note_converse_audio(frame.pcm_len)
            self._post_external(("device", frame))

    async def _connect(self) -> None:
        try:
            link = await self._provider_connect()
        except ProviderError as err:
            # Only fixed connector classifications, never exception details.
            code = {
                "provider_auth": 1008, "provider_policy": 1008, "provider_protocol": 1002,
            }.get(err.code, 1011)
            self._post(("provider_failed", code))
            return
        except Exception:  # noqa: BLE001 - fixed outcome, no detail
            self._post(("provider_failed",))
            return
        if self._state != _STARTING:
            # A connector that ignored its cancellation: the link is closed (bounded), no
            # session.start is built or sent and nothing else is spawned for it.
            hard = (
                self._close_hard if self._close_hard is not None else self._clock.now()
            )
            await self._within(link.close(), hard)
            return
        self._provider = link
        self._provider_reader = self._spawn(self._read_provider(link))
        self._spawn(self._pout.run(self._provider_sent, self._provider_send_failed))
        self._provider_start_id = self._new_event_id()
        start = gp.build_provider_start(
            self._provider_start_id, self._settings.model, self._settings.voice
        )
        try:
            await self._provider_send(start)
        except Exception:  # noqa: BLE001 - fixed outcome, no detail
            self._provider_send_failed()

    def _provider_sent(self, tag: str | None) -> None:
        """A provider frame finished sending: commentary chunks (ACK timing) and the close.

        The close is COMMITTED here, synchronously in the sender's own step, only after
        ``send_text`` returned: a provider close/``session.closed`` is a confirmation only
        once our ``session.close`` really left.
        """
        if tag == "close":
            self._close_committed = True
        elif tag is not None and tag.startswith("audio:"):
            # Locally generated tags, never provider-controlled.  One timer follows
            # the actual send completion, including a short final capture frame.
            size = int(tag.removeprefix("audio:"))
            self._provider_audio_until = self._clock.now() + size / gs.AUDIO_BYTES_PER_S
            self._arm_silence()
            if self._converse and self._input_wait_t is not None:
                self._arm_settle()  # recovery quiet starts after the actual drain
        elif tag is not None:
            # Commit the capture boundary synchronously AFTER send_text returned,
            # before yielding to the reader/dispatcher. Only this reply's FIRST
            # commentary can arm the existing held queue. ACK still gates playback.
            if (self._state == _SPEAKING and self._chunk_ids
                    and tag == self._chunk_ids[0] and not self._commentary_committed):
                self._commentary_committed = True
                if not self._converse:
                    self._audio_token = self._audio.open(hold=True)
            self._post(("sent", tag))

    def _provider_send_failed(self) -> None:
        # An already admitted terminal provider event must not be silently
        # overtaken by a later send failure on the higher-priority queue.
        # Scan only the bounded inbox; do not execute queued input or allow a
        # later malformed frame to retroactively change an earlier failure.
        code = 1011
        starting = self._state == _STARTING and not self._resume_submitted
        pinned = self._provider_session_id if self._provider_started else None
        for queued in self._inbox:
            if queued[0] == "provider_closed":
                break
            if queued[0] == "provider":
                event = queued[1]
                if event.kind in _INVALID_PROVIDER_KINDS:
                    code = 1002
                    break
                if event.kind == "started" and starting:
                    if self._startup_mismatch(event):
                        code = 1002
                        break
                    pinned = event.session_id
                    starting = False  # a later started event would be ignored
                if event.kind == "closed":
                    code = self._provider_end_code(event, pinned)
                    break
                if event.kind == "error":
                    break
        self._post(("provider_failed", code))

    async def _read_provider(self, link: ProviderLink) -> None:
        while True:
            try:
                message = await link.receive()
            except Exception:  # noqa: BLE001 - any link failure is an unclean close
                self._post_external(("provider_closed", False))
                return
            if message.kind == "closed":
                # Terminal transport evidence follows earlier frames in the SAME
                # bounded FIFO, never the higher-priority internal event queue.
                self._post_external(("provider_closed", message.clean))
                return
            if message.kind == "invalid":
                event = gp.ProviderEvent("invalid")
            elif message.data is None:
                continue
            else:
                event = gp.classify_provider_event(message.data)
            # Stamp eligibility at LOCAL receive, not eventual dispatch: internal
            # send-completion events have priority over earlier queued provider
            # events and must never retroactively admit that earlier audio.
            eligible = (self._audio_token if self._converse or (
                self._state == _SPEAKING and self._commentary_committed) else None)
            close_eligible = self._state == _CLOSING and self._close_committed
            if self._converse:
                # Input that arrived while pending cannot become a new request
                # just because an internal render timer dispatched ahead of it.
                window = (self._request_seq, self._state, self._sealing,
                          self._commentary_committed)
                self._post_external(("provider", event, eligible, window, close_eligible))
            else:
                self._post_external(("provider", event, eligible, None, close_eligible))

    # -- runner results ----------------------------------------------------------------
    def _submit(self, op: str, fn: Callable[[], Any], tag: str) -> None:
        """Every session job except ``confirm`` is NOT retained (no reply text left behind)."""
        try:
            ticket = self._runner.submit(op, fn, retain=False)
        except RunnerError as err:
            self._post(("op", tag, False, err))
            return
        self._spawn(self._await_ticket(
            ticket, tag, self._request_seq if self._converse else None
        ))

    async def _await_ticket(
        self, ticket: JobTicket, tag: str, generation: int | None = None
    ) -> None:
        try:
            value = await ticket.wait()
        except Exception as exc:  # noqa: BLE001 - delivered to the handler as an event
            self._post(("op", tag, False, exc, generation))
            return
        self._post(("op", tag, True, value, generation))

    # -- outbound frames ---------------------------------------------------------------
    def _emit(self, text: str, *, tag: str | None = None) -> bool:
        """Queue one device frame; ``False`` when it was not queued (device gone / full)."""
        if self._device_gone:
            return False
        if not self._dout.put(text, tag=tag):
            self._device_gone = True  # the device is not draining: stop talking to it
            self._begin_close(1011)
            return False
        return True

    def _emit_error(self, code: str) -> None:
        if self._error_code is None:
            self._error_code = code
        self._emit(gs.build_pet_error(code))

    def _emit_send_result(self, state: str) -> None:
        """THE only place a ``pet.turn.send_result`` is built: at most once per session."""
        if self._converse:
            return
        if self._send_result_emitted:
            return
        self._send_result_emitted = True
        self._emit(gs.build_send_result(self._turn_str(), state))

    def _known_unsent(self) -> None:
        """Record a PROVEN no-send once: nothing was ever submitted to the core."""
        if self._send_state is None and self._send_stage in ("", "preflight"):
            self._send_state = "refused"
            self._send_stage = "unsent"
            if self._converse:
                self._requests_refused += 1
            self._emit_send_result("rejected")

    def _settle_unsent_send(self) -> None:
        """A preflight still pending when the session ends is a proven no-send.

        Later results are ignored in CLOSING/CLOSED, so no confirm can follow.
        """
        if self._state == _SENDING and self._send_stage == "preflight":
            self._known_unsent()
        elif self._converse and self._state == _SETTLING:
            self._known_unsent()

    def _fail(self, code: str, device_code: int | None = None) -> None:
        if self._state in (_CLOSING, _CLOSED):
            return
        self._settle_unsent_send()
        self._emit_error(code)
        self._finish_reply("unavailable")
        self._begin_close(
            _CLOSE_CODES.get(code, 1000) if device_code is None else device_code
        )

    def _finish_reply(self, state: str) -> None:
        """``pet.reply.done`` once, and only for a turn whose send was accepted."""
        if self._converse:
            return
        if (
            self._send_accepted
            and self._reply_done is None
            and self._turn_id is not None
        ):
            self._reply_done = state
            self._emit(gs.build_reply_done(self._turn_id, state))

    # -- close -------------------------------------------------------------------------
    def _begin_close(self, device_code: int = 1000, *, fatal: bool = False) -> None:
        """Enter CLOSING once: gate and queues off, local core stop queued, provider closed.

        The provider ``session.close`` is sent ONCE; a normal close then waits for the
        provider's matching ``session.closed`` after our request actually left
        and only then reports ``session.closed`` to the device.  A fatal provider failure
        does not wait: the device gets no ``session.closed`` either way.

        ONE absolute deadline covers the confirmation wait, the final frames, the flushes and
        both link closes: ``min(now + 15, accepted_start + mode_cap)`` (60 or 600 s).
        At or after it nothing is
        waited for and the close is reported truthfully as unconfirmed.
        """
        if self._state in (_CLOSING, _CLOSED):
            return
        self._closed_for_recovery.set()
        self._settle_unsent_send()
        now = self._clock.now()
        hard = now + gs.CLOSE_DEADLINE_S
        if self._accepted_t is not None:
            cap = gs.CONVERSE_LIMIT_S if self._converse else gs.SESSION_CAP_S
            hard = min(hard, self._accepted_t + cap)
        if self._converse:
            self._converse_end = self._converse_end or (
                "provider_failure" if fatal else self._error_code or "closed"
            )
        self._close_hard = hard
        self._state = _CLOSING
        self._device_code = device_code
        self._fatal = fatal
        self._cancel_timers()
        if self._connect_task is not None:
            self._connect_task.cancel()
        self._audio.close_and_drain()
        self._pout.clear()  # unsent audio / commentary is suppressed
        if self._resume_submitted:
            with contextlib.suppress(
                RunnerError
            ):  # closed runner: nothing more to stop
                self._runner.submit("stop", self._ops.stop, urgent=True, retain=False)
        if (
            self._provider is None
            or not self._provider_started
            or self._provider_closed_seen
        ):
            self._finalize()
            return
        self._pout.put(gp.build_provider_close(), tag="close")
        delay = hard - now
        if fatal or delay <= _EPS:
            self._finalize()  # fatal, or already at the cap: no wait for the provider
        else:
            self._arm("close", delay)

    def _finalize(self) -> None:
        """No more events: stop both readers so nothing accumulates while the tail runs."""
        self._finalize_flag = True
        self._wake.set()
        for reader in (self._device_reader, self._provider_reader):
            if reader is not None:
                reader.cancel()

    async def _shutdown(self) -> None:
        """Final frames, flushes and both link closes, all inside the ONE close deadline.

        Idempotent per step (a cancelled run may re-enter it): the final frame decision, the
        provider ``close`` and the device ``close`` each happen at most once.
        """
        hard = self._close_hard if self._close_hard is not None else self._clock.now()
        connect = self._connect_task
        if connect is not None and not connect.done():
            await self._within(
                connect, hard
            )  # a stray connector is settled or cancelled
        if not self._shutdown_framed:
            self._shutdown_framed = True
            if not self._device_gone and self._provider_started and not self._fatal:
                if self._close_confirmed:
                    self._emit(gs.build_session_closed())
                else:
                    self._emit_error("close_unconfirmed")
        tails = [self._device_tail(hard)]
        if self._provider is not None:
            tails.append(self._provider_tail(self._provider, hard))
        await asyncio.gather(*tails)

    async def _provider_tail(self, provider: ProviderLink, hard: float) -> None:
        mid = self._clock.now() + max(0.0, hard - self._clock.now()) / 2
        await self._within(self._pout.flush(), mid)
        if not self._provider_close_called:
            self._provider_close_called = True
            await self._within(provider.close(), hard)

    async def _device_tail(self, hard: float) -> None:
        mid = self._clock.now() + max(0.0, hard - self._clock.now()) / 2
        if not self._device_gone:
            await self._within(self._dout.flush(), mid)
        if not self._device_close_called:
            self._device_close_called = True
            await self._within(self._device.close(self._final_code()), hard)

    def _final_code(self) -> int:
        if self._fatal:
            return self._device_code
        if self._policy_close:
            return 1008  # a preview policy violation, even though the provider started
        return 1000 if self._provider_started else self._device_code

    # -- event dispatch ----------------------------------------------------------------
    def _handle(self, event: tuple[Any, ...]) -> None:
        handler = self._events.get(event[0])
        if handler is not None:
            was_closing = self._state in (_CLOSING, _CLOSED)
            try:
                handler(*event[1:])
            finally:
                if (not was_closing and self._state in (_CLOSING, _CLOSED)
                        and self._close_origin is None):
                    # Keys come from the fixed internal handler map, never the
                    # frame's contents/exception text. First transition wins.
                    self._close_origin = event[0]

    def _device_lost(self) -> None:
        """The device disconnected or stopped draining: close without more device frames."""
        self._device_gone = True
        if self._converse:
            self._converse_end = self._converse_end or "device_lost"
        self._begin_close(1000)

    def _on_provider_failed(self, device_code: int = 1011) -> None:
        self._provider_closed_seen = True  # a dead link: no close frame can be sent
        self._provider_fatal(device_code)

    def _provider_fatal(self, device_code: int = 1011) -> None:
        """The ONE funnel for a fatal provider failure (error, bad event, close, send fail).

        A preflight still pending is a proven no-send; an ACCEPTED turn without a
        ``reply.done`` gets exactly one ``unavailable`` before the close; never a
        ``session.closed`` and never provider text.
        """
        if self._state in (_CLOSING, _CLOSED):
            return
        self._settle_unsent_send()
        self._finish_reply("unavailable")
        self._begin_close(device_code, fatal=True)

    def _on_overflow(self) -> None:
        self._fail("overflow")

    def _on_stop(self) -> None:
        """Local audio stop only (never a Unified cancel): gate shut, core stop queued."""
        if self._state in (_CLOSING, _CLOSED):
            return
        if self._converse:
            self._converse_end = self._converse_end or "stopped"
        self._settle_unsent_send()
        self._audio.close_and_drain()
        self._finish_reply("stopped")
        self._begin_close(1000)

    # -- device commands (plan 6.2) ----------------------------------------------------
    def _on_device(self, frame: gs.DeviceFrame) -> None:
        kind = frame.kind
        if self._state in (_CLOSING, _CLOSED):
            if kind == "send_confirm" and self._error_code != "invalid_preview":
                # A late confirm never reaches the core or duplicates an
                # invalid-preview outcome already emitted before close.
                self._emit_error("invalid_preview")
            return
        if self._converse and kind in ("begin", "capture_stop", "send_confirm"):
            self._policy_close = True
            self._fail("unknown_command")
            return
        if kind == "greeting":
            return
        if kind == "start":
            self._on_start()
        elif kind == "audio":
            self._on_audio(frame)
        elif kind in ("stop", "close"):
            self._on_stop()
        elif kind == "begin":
            self._on_begin()
        elif kind == "converse_begin":
            self._on_converse_begin()
        elif kind == "capture_stop":
            self._on_capture_stop(frame)
        elif kind == "send_confirm":
            self._on_send_confirm(frame)
        else:
            self._fail("unknown_command")

    def _on_send_confirm(self, frame: gs.DeviceFrame) -> None:
        """Only the exact PREVIEW turn, revision and digest starts a send (plan 6.2).

        In PREVIEW a wrong digest/revision for THIS turn is a proven no-send
        (``send_result rejected``, then ``invalid_preview`` and close 1008); a foreign turn id
        gets the error and close only, never a send_result.  In every other state it is an
        error only (no state change, no send).
        """
        if self._state != _PREVIEW:
            self._emit_error("invalid_preview")
            return
        if frame.turn_id != self._turn_id:
            self._policy_close = True
            self._fail("invalid_preview")
            return
        exact = frame.preview_revision == self._preview_rev and hmac.compare_digest(
            frame.text_sha256 or "", self._preview_sha or ""
        )
        if not exact:
            self._policy_close = True
            self._send_state = "refused"
            self._emit_send_result("rejected")
            self._fail("invalid_preview")
            return
        self._state = _SENDING
        self._send_stage = "preflight"
        self._submit("preflight", self._ops.preflight, "preflight")

    def _on_start(self) -> None:
        if self._state != _AWAIT_START:
            self._fail("busy")
            return
        self._state = _STARTING
        self._accepted_t = self._clock.now()
        self._disarm("await_start")
        self._arm("start", gs.START_DEADLINE_S)
        self._arm("cap", gs.SESSION_WORK_S)
        self._connect_task = self._spawn(self._connect())

    def _on_audio(self, frame: gs.DeviceFrame) -> None:
        if self._state != _CAPTURING and not self._converse:
            self._dropped_in += frame.pcm_len
            return
        if self._pout.pending_audio + frame.pcm_len > gs.CAPTURE_QUEUE_CAP:
            self._fail("overflow")
            return
        assert frame.b64 is not None
        sent = self._pout.put(
            gp.build_provider_audio(frame.b64),
            audio=frame.pcm_len,
            tag=f"audio:{frame.pcm_len}",
        )
        if not sent:
            self._fail("overflow")
            return
        self._audio_in += frame.pcm_len

    def _on_begin(self) -> None:
        if self._state != _READY or self._turn_used or self._begin_pending:
            self._fail("busy")
            return
        self._turn_used = True
        self._begin_pending = True
        self._submit("begin", self._ops.begin, "begin")

    def _on_capture_stop(self, frame: gs.DeviceFrame) -> None:
        if self._state == _CAPTURING and frame.turn_id == self._turn_id:
            self._state = _SETTLING
            self._stop_t = self._clock.now()
            self._arm_settle()
            self._arm_silence()
        elif self._state in (_SETTLING, _PREVIEW) and frame.turn_id == self._turn_id:
            return
        else:
            self._fail("unknown_command")

    def _arm_silence(self) -> None:
        """Maintain PRIMARY's input timeline without forwarding any more microphone.

        Only one silence frame may be queued/in flight.  Backpressure never builds
        a backlog or causes a catch-up burst; its next interval starts at completed
        send time.  Close cancels this timer and clears the bounded outbox.
        """
        if self._converse:
            return  # device owns the continuous input timeline, including muted PCM
        if self._state not in (_SETTLING, _PREVIEW, _SENDING, _AWAIT_REPLY, _SPEAKING):
            return
        if self._pout.pending_audio:
            return  # the last finite/silence send completion will arm it
        self._arm("silence", max(0.0, self._provider_audio_until - self._clock.now()))

    def _on_silence_timer(self) -> None:
        if self._state not in (_SETTLING, _PREVIEW, _SENDING, _AWAIT_REPLY, _SPEAKING):
            return
        if self._pout.pending_audio:
            return
        size = gs.AUDIO_CHUNK_BYTES
        silence = base64.b64encode(bytes(size)).decode("ascii")
        if not self._pout.put(
            gp.build_provider_audio(silence), audio=size, tag=f"audio:{size}"
        ):
            self._fail("overflow")

    # -- settle / seal -----------------------------------------------------------------
    def _settle_target(self) -> tuple[float, float]:
        ready = max(
            self._stop_t + gs.SETTLE_AFTER_STOP_S, self._last_delta + gs.SETTLE_QUIET_S
        )
        if self._converse:
            ready = max(ready, self._input_quiet_t + gs.SETTLE_QUIET_S)
        return ready, self._stop_t + gs.SETTLE_MAX_S

    def _arm_settle(self) -> None:
        if self._converse:
            if self._state != _SETTLING or self._sealing:
                return
            self._refresh_converse_input()
        ready, limit = self._settle_target()
        if self._converse:
            # A delegation can arrive near the START of a long utterance. The
            # one-turn six-second limit is for late input AFTER capture_stop,
            # not a reason to truncate/reject someone still speaking. Global
            # time, segment and character bounds still apply.
            limit = ready
            if self._input_wait_t is not None:
                ready = self._clock.now() + gs.CONVERSE_INPUT_CHECK_S
                limit = self._input_wait_t + gs.CONVERSE_INPUT_MAX_S
        delay = min(ready, limit) - self._clock.now()
        if delay <= _EPS:
            self._post(("timer", "settle"))
        else:
            self._arm("settle", delay)

    def _on_settle_timer(self) -> None:
        if self._converse:
            self._converse_settle()
            return
        if self._state != _SETTLING or self._sealing:
            return
        ready, limit = self._settle_target()
        now = self._clock.now()
        if now + _EPS >= ready:
            self._seal_attempt()
        elif now + _EPS >= limit:
            self._fail("unstable_transcript" if self._stager.count else "no_speech")
        else:
            self._arm_settle()

    def _seal_attempt(self) -> None:
        text = self._stager.text()
        if not text.strip():
            self._fail("no_speech")
            return
        turn = self._turn_str()
        segments = self._stager.ordered()
        self._sealing = True
        self._submit("stage", lambda: self._ops.stage(turn, segments), "stage")

    def _on_stage_result(self, ok: bool, value: Any) -> None:
        if self._state != _SETTLING:
            return
        if not ok or value != "appended":
            if self._converse:
                self._requests_refused += 1
                self._converse_notice("not_sent")
                return
            self._fail("unstable_transcript")
            return
        turn = self._turn_str()
        self._submit("seal", lambda: self._ops.seal(turn), "seal")

    def _on_seal_result(self, ok: bool, value: Any) -> None:
        if self._state != _SETTLING:
            return
        if not ok:
            if self._converse:
                self._requests_refused += 1
                self._converse_notice("not_sent")
                return
            blank = isinstance(value, core.BridgeError) and value.code == "blank_text"
            self._fail("no_speech" if blank else "unstable_transcript")
            return
        try:
            frame = gs.build_preview(self._turn_str(), 1, value.text, value.text_sha256)
        except ValueError:
            self._fail("invalid_preview")
            return
        self._preview_sha = value.text_sha256
        self._preview_rev = 1
        if self._converse:
            self._state = _SENDING
            self._send_stage = "preflight"
            self._submit("preflight", self._ops.preflight, "preflight")
            return  # no preview or device confirmation in this mode
        self._state = _PREVIEW
        self._emit(frame)

    # -- operations --------------------------------------------------------------------
    def _on_op(
        self, tag: str, ok: bool, value: Any, generation: int | None = None
    ) -> None:
        if self._state in (_CLOSING, _CLOSED):
            return  # a result after close is recorded by the runner, never acted on
        if generation is not None and generation != self._request_seq:
            return  # a late read from a previous conversational request
        handler = {
            "resume": lambda: self._on_resume_result(ok),
            "begin": lambda: self._on_begin_result(ok, value),
            "stage": lambda: self._on_stage_result(ok, value),
            "seal": lambda: self._on_seal_result(ok, value),
            "preflight": lambda: self._on_preflight_result(ok, value),
            "poll": lambda: self._on_poll_result(ok, value),
            "claim_reply": lambda: self._on_poll_result(ok, value),
            "epoch_pre": lambda: self._on_epoch_pre(ok, value),
            "epoch_gate": lambda: self._on_epoch_gate(ok, value),
            "converse_abandon": lambda: self._on_converse_abandon(ok),
        }.get(tag)
        if handler is not None:
            handler()

    def _on_resume_result(self, ok: bool) -> None:
        if self._state != _STARTING:
            return
        if not ok:
            self._begin_close(1011, fatal=True)
            return
        self._state = _READY
        self._emit(gs.build_session_started())

    def _on_begin_result(self, ok: bool, value: Any) -> None:
        self._begin_pending = False
        if self._converse:
            if self._state != _SETTLING or not self._sealing:
                return
            if not ok:
                self._requests_refused += 1
                # An earlier send whose outcome is unknown blocks new ones: say that,
                # not "busy" (which reads as the conversation working on something).
                unresolved = getattr(value, "code", None) == "uncertain_unresolved"
                self._converse_notice("send_uncertain" if unresolved else "busy")
                return
            self._timed_out_turn = None
            self._turn_id = value
            self._seal_attempt()
            return
        if self._state != _READY:
            return
        if not ok:
            self._fail("busy")
            return
        try:
            frame = gs.build_turn_began(
                value, self._settings.label, self._settings.short8
            )
        except ValueError:
            self._fail("busy")
            return
        self._turn_id = value
        self._state = _CAPTURING
        self._emit(frame)

    # -- send / poll ---------------------------------------------------------------------
    def _turn_str(self) -> str:
        assert self._turn_id is not None
        return self._turn_id

    def _on_preflight_result(self, ok: bool, value: Any) -> None:
        if self._state != _SENDING:
            return
        if not ok or not getattr(value, "idle", False):
            self._known_unsent()  # preflight is read-only: nothing was submitted
            if self._converse:
                self._converse_notice("busy")
                return
            self._fail("busy")
            return
        sha = self._preview_sha
        assert sha is not None
        turn, revision = self._turn_str(), value.revision
        self._send_stage = "confirm"
        try:
            ticket = self._runner.submit(
                "confirm", lambda: self._ops.confirm(turn, sha, revision)
            )  # the ONE retained job: its real outcome is never lost
        except RunnerError:  # not admitted: provably nothing was sent
            self._send_stage = "unsent"
            self._send_state = "refused"
            if self._converse:
                self._requests_refused += 1
                self._converse_notice("not_sent")
                return
            self._emit_send_result("rejected")
            self._fail("busy")
            return
        self._confirm_job = ticket.job_id
        self._send_state = "inflight"
        if self._converse:
            self._requests_submitted += 1
            # The host may take many seconds to answer (it starts a dormant
            # conversation first): tell the device now, not only once accepted.
            self._emit(gs.build_converse_state("asking"))
        self._spawn(self._await_confirm(ticket))

    async def _await_confirm(self, ticket: JobTicket) -> None:
        """Wait for the confirm job and record its outcome in the SAME loop step.

        Nothing suspends between ``JobTicket.wait`` delivering (which drops the runner's
        held copy) and ``_settle_confirm``; a cancellation before delivery leaves the real
        outcome held by the runner.  So the session recorded it, or the runner still holds
        it: never neither.
        """
        try:
            value = await ticket.wait()
        except Exception as exc:  # noqa: BLE001 - the very exception the job raised
            self._settle_confirm(False, exc)
            return
        self._settle_confirm(True, value)

    @staticmethod
    def _classify_confirm(ok: bool, value: Any) -> tuple[str, str | None]:
        """``(send_state, refusal_reason)``; the reason is set only for a refusal."""
        if not ok or not isinstance(value, core.CommandResult):
            return "uncertain", None
        if value.outcome == "refused":
            reason = value.reason
            if isinstance(reason, str) and reason in _KNOWN_UNSENT:
                return "refused", reason  # proof that nothing was sent
            return "uncertain", reason if isinstance(reason, str) else "unknown"
        if value.outcome in gs.SEND_RESULT_STATES:
            return value.outcome, None
        return "uncertain", None

    def _settle_confirm(self, ok: bool, value: Any) -> None:
        """Synchronous and state-independent: record the real outcome, then post the frames."""
        state, reason = self._classify_confirm(ok, value)
        self._send_state = state
        self._send_stage = "settled"
        self._confirm_job = None
        if self._converse:
            if state != "refused":
                self._requests_sent += 1
            if state == "accepted":
                self._requests_accepted += 1
            elif state == "uncertain":
                self._requests_uncertain += 1
            else:
                self._requests_refused += 1
        self._post(("confirm_done", state, reason))

    def _on_confirm_done(self, state: str, reason: str | None) -> None:
        """Frames only (the outcome is already recorded); ignored once CLOSING."""
        if self._state != _SENDING:
            return  # a late result is metadata only: no frame, no poll, no speech
        if self._converse:
            if state != "accepted":
                self._converse_notice(
                    "send_uncertain"
                    if state == "uncertain" or reason == "uncertain_unresolved" else
                    "busy" if reason in _REFUSED_CODES else "not_sent"
                )
                return
            self._send_accepted = True
            self._send_accept_t = self._clock.now()
            self._state = _AWAIT_REPLY
            self._arm("poll", gs.POLL_INTERVAL_S)
            self._arm("converse_reply", gs.CONVERSE_REPLY_MAX_S)
            return
        if reason is not None:  # a refusal: proof of no send only if it was allowlisted
            self._emit_send_result("rejected" if state == "refused" else "uncertain")
            self._fail(_REFUSED_CODES.get(reason, "invalid_preview"))
            return
        self._emit_send_result(state)
        if (
            state != "accepted"
        ):  # rejected (409 included) and uncertain: one turn, closed
            self._begin_close(1000)
            return
        self._send_accepted = True
        self._send_accept_t = self._clock.now()
        self._state = _AWAIT_REPLY
        if self._contaminated:
            self._finish_reply("unavailable")
            self._begin_close(1000)
            return
        self._arm("poll", gs.POLL_INTERVAL_S)

    def _on_poll_timer(self) -> None:
        busy = self._poll_inflight or self._speak_pending or self._contaminated
        if self._state != _AWAIT_REPLY or busy:
            return
        limit = gs.CONVERSE_REPLY_MAX_S if self._converse else gs.POLL_MAX_S
        if self._clock.now() + _EPS >= self._send_accept_t + limit:
            if self._converse:
                self._converse_timeout()
                return
            self._fail("deadline")
            return
        self._poll_inflight = True
        turn = self._turn_str()
        action = self._ops.observe_reply if self._converse else self._ops.poll
        self._submit("poll", lambda: action(turn), "poll")

    def _on_poll_result(self, ok: bool, value: Any) -> None:
        self._poll_inflight = False
        if self._state != _AWAIT_REPLY or self._contaminated:
            return
        if self._converse and self._clock.now() + _EPS >= self._send_accept_t + gs.CONVERSE_REPLY_MAX_S:
            self._converse_timeout()
            return
        if not ok:
            if self._converse:
                self._converse_notice("reply_unavailable")
                return
            self._fail("reply_unavailable")
            return
        if not isinstance(value, core.ReplyResult) or value.command_id != self._turn_id:
            self._reply_text = None  # never speak a reply that is not THIS turn's
            if self._converse:
                self._converse_notice("reply_unavailable")
                return
            self._fail("reply_unavailable")
            return
        if value.status == "ready" and self._converse:
            self._poll_inflight = True
            turn = self._turn_str()
            self._submit("poll", lambda: self._ops.claim_observed_reply(turn), "claim_reply")
        elif value.status == "pending":
            self._arm("poll", gs.POLL_INTERVAL_S)
        elif value.status == "speak" and isinstance(value.text, str):
            self._speak_pending = True
            self._reply_text = value.text
            self._reply_epoch = value.epoch
            epoch = value.epoch
            self._submit(
                "epoch_current", lambda: self._ops.epoch_current(epoch), "epoch_pre"
            )
        else:  # not_spoken (terminal or not) or anything unexpected
            if self._converse:
                self._converse_notice("reply_unavailable")
                return
            self._finish_reply("unavailable")
            self._begin_close(1000)

    # -- speaking ----------------------------------------------------------------------
    def _on_epoch_pre(self, ok: bool, value: Any) -> None:
        if self._state != _AWAIT_REPLY or self._contaminated:
            return
        if self._converse and self._clock.now() + _EPS >= self._send_accept_t + gs.CONVERSE_REPLY_MAX_S:
            self._converse_timeout()
            return
        if not ok or value is not True:  # the epoch moved on: nothing is spoken
            self._reply_text = None
            if self._converse:
                self._converse_notice("reply_unavailable")
                return
            self._finish_reply("unavailable")
            self._begin_close(1000)
            return
        self._start_speaking()

    def _start_speaking(self) -> None:
        text, self._reply_text = self._reply_text, None
        if self._converse:
            self._converse_render(text or "", reply=True)
            return
        self._state = _SPEAKING
        self._audio.close_and_drain()  # old audio is gone before the first commentary
        self._audio_token = None
        self._commentary_committed = False
        delegation = self._delegation_id
        if delegation is None or self._delegation_ambiguous:
            self._fail("no_delegation")
            return
        try:
            chunks = gs.chunk_commentary(text or "")
        except ValueError:
            self._fail("reply_unavailable")  # never truncated
            return
        ids = tuple(self._new_event_id() for _ in chunks)
        try:
            frames = [
                gp.build_commentary(ident, delegation, chunk)
                for ident, chunk in zip(ids, chunks, strict=True)
            ]
        except ValueError:
            self._fail("no_delegation")
            return
        self._chunk_ids = ids
        for ident, frame in zip(ids, frames, strict=True):
            if not self._pout.put(frame, tag=ident):
                self._fail("overflow")
                return

    def _on_chunk_sent(self, tag: str) -> None:
        if self._state != _SPEAKING or tag not in self._chunk_ids:
            return
        self._chunks_sent.add(tag)
        if self._converse:
            return  # conversation stream stays open; ACK is metadata, not an audio gate
        self._maybe_gate_reply()
        everything_sent = len(self._chunks_sent) == len(self._chunk_ids)
        if everything_sent and len(self._acked) < len(self._chunk_ids):
            self._arm("ack", gs.ACK_WAIT_S)

    def _on_ack(self, event: gp.ProviderEvent) -> None:
        ident = event.client_event_id
        if (
            self._state != _SPEAKING
            or ident not in self._chunk_ids
            or ident in self._acked
        ):
            return  # foreign, old or repeated: never opens the audio epoch
        assert ident is not None
        self._acked.add(ident)
        if self._converse:
            return
        self._maybe_gate_reply()
        if len(self._acked) == len(self._chunk_ids):
            self._disarm("ack")

    def _maybe_gate_reply(self) -> None:
        """Release permission requires FIRST commentary commit AND its own ACK.

        The pre-ACK quarantine uses the same cap/token/epoch as normal output.
        A foreign/later-chunk ACK cannot release it; an early own ACK is cached
        but cannot release BEFORE successful local sending. No content provenance.
        """
        if (self._state != _SPEAKING or not self._commentary_committed
                or self._audio_token is None or not self._chunk_ids
                or self._chunk_ids[0] not in self._acked
                or self._gate_opened or self._gate_check_pending):
            return
        self._gate_check_pending = True
        epoch = self._reply_epoch
        self._submit("epoch_current", lambda: self._ops.epoch_current(epoch), "epoch_gate")

    def _on_epoch_gate(self, ok: bool, value: Any) -> None:
        self._gate_check_pending = False
        if self._state != _SPEAKING:
            return
        if not ok or value is not True:
            self._audio.close_and_drain()
            self._finish_reply("unavailable")
            self._begin_close(1000)
            return
        self._gate_opened = True
        # reply.started goes through the device FIFO (every earlier frame first); the
        # buffered audio is released only after that frame's send COMPLETED.
        self._emit(gs.build_reply_started(self._turn_str()), tag="started")

    def _device_sent(self, tag: str | None) -> None:
        """A device frame finished sending: only ``reply.started`` releases the audio."""
        if tag == "started":
            self._post(("started_sent",))
        elif tag == "converse_began":
            self._converse_audio_ready = True
            if self._audio_token is not None:
                self._audio.release(self._audio_token)

    def _on_started_sent(self) -> None:
        if self._state == _SPEAKING and self._audio_token is not None:
            self._audio.release(self._audio_token)  # a stale token is a no-op

    def _all_acked(self) -> bool:
        return bool(self._chunk_ids) and len(self._acked) == len(self._chunk_ids)

    # No speech-EOS signal is qualified for this PRIMARY stream.  Packet quiet,
    # captions, ACKs and a drained local queue cannot justify "completed".  Keep
    # accepting gated PCM through gaps until Stop, failure or the work deadline;
    # the latter reports unavailable, not successful speech completion.

    def _on_ack_timer(self) -> None:
        if self._state == _SPEAKING and not self._all_acked():
            self._audio.close_and_drain()
            self._finish_reply("unavailable")
            self._begin_close(1000)

    # -- timers ------------------------------------------------------------------------
    def _on_timer(self, name: str) -> None:
        if name == "close":
            if self._state == _CLOSING:
                self._finalize()  # the provider never confirmed within 15 s
            return
        if self._state in (_CLOSING, _CLOSED):
            return
        handler = {
            "await_start": self._on_await_start_timer,
            "start": self._on_start_timer,
            "cap": self._on_cap_timer,
            "settle": self._on_settle_timer,
            "poll": self._on_poll_timer,
            "ack": self._on_ack_timer,
            "silence": self._on_silence_timer,
            "converse_idle": self._converse_idle,
            "converse_limit": self._converse_limit,
            "converse_render": self._converse_render_tick,
            "converse_reply": self._converse_reply_timeout,
        }.get(name)
        if handler is not None:
            handler()

    def _on_await_start_timer(self) -> None:
        if self._state == _AWAIT_START:
            self._begin_close(1008)  # no frame: the device never sent session.start

    def _on_start_timer(self) -> None:
        if self._state == _STARTING:
            self._fail("deadline")  # provider connect + session.started took over 25 s

    def _on_cap_timer(self) -> None:
        if self._converse:
            return  # a queued timer from before mode selection is obsolete
        self._fail("deadline")  # 58 s work + up to 2 s cleanup, never extend the 60 s cap

    # -- provider events ---------------------------------------------------------------
    @staticmethod
    def _foreign_requested_close(event: gp.ProviderEvent, pinned: str | None) -> bool:
        """A pinned, started session cannot close under another session's identity."""
        return (
            pinned is not None
            and event.kind == "closed"
            and event.close_reason == "close_requested"
            and event.session_id != pinned
        )

    @classmethod
    def _provider_end_code(cls, event: gp.ProviderEvent, pinned: str | None) -> int:
        """Classify a validated provider end, not the success of any user request.

        OpenAI Live SessionClosedEvent (OpenAPI-generated SDK 3.51.0): expiry
        is a duration limit, content is a safety filter, remote_hangup is
        graceful, connection_lost is an unexpected primary/upstream disconnect,
        and close_requested is an application close or hangup request.
        A session identity must match before attributing a reason to this call.
        """
        if cls._foreign_requested_close(event, pinned):
            return 1002
        if pinned is None or event.session_id != pinned:
            return 1011
        if event.close_reason == "content":
            return 1008
        if event.close_reason in ("close_requested", "expired", "remote_hangup"):
            return 1000
        return 1011

    def _on_provider(
        self, event: gp.ProviderEvent, eligible: int | None = None,
        window: tuple[int, str, bool, bool] | None = None,
        close_eligible: bool = False,
    ) -> None:
        kind = event.kind
        if kind == "closed":
            if self._state == _CLOSING:
                if (not self._fatal and close_eligible
                        and event.session_id == self._provider_session_id
                        and event.close_reason == "close_requested"):
                    self._close_confirmed = True
                # Otherwise this is not confirmation of our requested close.
                self._finalize()
            elif self._state != _CLOSED:
                self._provider_closed_seen = True
                self._provider_fatal(self._provider_end_code(event, self._provider_session_id))
            return
        if kind == "audio":
            self._on_provider_audio(event, eligible, window)
            return
        if self._state in (_CLOSING, _CLOSED):
            return
        if kind == "started":
            self._on_provider_started(event)
        elif kind in ("error", "invalid", "invalid_audio", "invalid_transcript"):
            # A malformed frame is a definite protocol failure, not a transport
            # loss. A provider error event has no reliably classified cause.
            self._provider_fatal(
                1002 if kind in _INVALID_PROVIDER_KINDS else 1011
            )
        elif kind == "transcript":
            if self._converse:
                self._converse_transcript(event, window)
            else:
                self._on_transcript(event)
        elif kind == "delegation":
            if self._converse:
                self._converse_delegation(event, window)
            else:
                self._on_delegation(event)
        elif kind == "ack":
            self._on_ack(event)

    def _on_provider_audio(
        self, event: gp.ProviderEvent, eligible: int | None,
        window: tuple[int, str, bool, bool] | None = None,
    ) -> None:
        """Receive-stamped post-commit PCM; bounded hold before ACK, never early playback."""
        if event.pcm is None:
            return
        if self._converse and self._state not in (_CLOSING, _CLOSED):
            if eligible is None or eligible != self._audio_token:
                self._audio.discard(event.pcm)
                return
            try:
                self._audio.offer(event.pcm)
            except AudioOverflow:
                self._on_overflow()  # terminal, never reopen a corrupted stream
                return
            return
        if (self._state != _SPEAKING or eligible is None
                or eligible != self._audio_token or not self._audio.is_open):
            # Preserve aggregate byte accounting without queuing an ineligible frame.
            self._audio.discard(event.pcm)
            return
        try:
            self._audio.offer(event.pcm)
        except AudioOverflow:
            self._on_overflow()

    def _on_delegation(self, event: gp.ProviderEvent) -> None:
        """A hint only.  One valid id pins the turn; a second DIFFERENT one is ambiguous."""
        if self._converse:
            self._converse_delegation(event)
            return
        window = (_CAPTURING, _SETTLING, _PREVIEW)
        if self._state not in window or event.delegation_id is None:
            return  # outside the turn window (before begin, sending, replying, speaking)
        if self._delegation_id is None:
            self._delegation_id = event.delegation_id
        elif self._delegation_id != event.delegation_id:
            self._delegation_ambiguous = True  # permanent: never "last one wins"

    def _startup_mismatch(self, event: gp.ProviderEvent) -> bool:
        return (
            event.session_id is None
            or event.model != self._settings.model
            or (event.client_event_id is not None and event.client_event_id != self._provider_start_id)
            or (event.voice is not None and event.voice != self._settings.voice)
            or (event.sample_rate is not None and event.sample_rate != 24000)
            or (event.format_type is not None and event.format_type != "audio/pcm")
            or event.stored is True
            or (event.delegation_type is not None and event.delegation_type != "client")
        )

    def _on_provider_started(self, event: gp.ProviderEvent) -> None:
        if self._state != _STARTING or self._resume_submitted:
            return
        if self._startup_mismatch(event):
            self._provider_fatal(1002)
            return
        self._provider_session_id = event.session_id
        self._provider_started = True
        self._resume_submitted = True
        self._submit("resume", self._ops.resume, "resume")

    def _on_provider_closed(self, clean: bool) -> None:
        """Transport cleanup is not the API's ``session.closed`` finalization."""
        self._provider_closed_seen = True
        if self._state == _CLOSING:
            self._finalize()
        elif self._state != _CLOSED:
            self._provider_fatal()

    def _on_transcript(self, event: gp.ProviderEvent) -> None:
        if self._converse:
            self._converse_transcript(event)
            return
        state = self._state
        assert event.event_id is not None and event.text is not None
        assert event.start_ms is not None and event.end_ms is not None
        if state in (_SENDING, _AWAIT_REPLY):
            self._contaminate(event)
            return
        capturing = state == _CAPTURING or (state == _SETTLING and not self._sealing)
        if not capturing and state not in (_SETTLING, _PREVIEW):
            return  # READY / STARTING / SPEAKING: ignored
        try:
            result = self._stager.add(
                event.event_id, event.text, event.start_ms, event.end_ms
            )
        except gs.GatewayProtocolError as err:
            if capturing:
                self._fail(_STAGER_FAIL[err.code])
            else:
                self._late_input()
            return
        if result == "duplicate":
            return
        if not capturing:
            self._late_input()
            return
        self._last_delta = self._clock.now()
        if state == _SETTLING:
            self._arm_settle()

    def _contaminate(self, event: gp.ProviderEvent) -> None:
        """Input while sending / awaiting: never a resend; the reply can no longer be trusted."""
        assert event.event_id is not None and event.text is not None
        assert event.start_ms is not None and event.end_ms is not None
        try:
            result = self._stager.add(
                event.event_id, event.text, event.start_ms, event.end_ms
            )
        except gs.GatewayProtocolError:
            result = "late"
        if result == "duplicate":
            return
        self._contaminated = True
        if self._state == _AWAIT_REPLY:
            self._finish_reply("unavailable")
            self._begin_close(1000)

    def _late_input(self) -> None:
        if self._state == _PREVIEW and self._turn_id is not None:
            self._emit(gs.build_preview_invalidated(self._turn_id, "late_transcript"))
            self._fail("invalid_preview")
        else:
            self._fail("unstable_transcript")

    # -- running conversation (selected only by pet.converse.begin) --------------------
    def _input_behind(self, now: float) -> bool:
        return self._input_offset is not None and (
            now - (self._input_offset + self._input_bytes / gs.AUDIO_BYTES_PER_S)
            > gs.CONVERSE_INPUT_LAG_S + _EPS
            or now + _EPS < self._input_catchup_until
        )

    def _note_converse_audio(self, size: int) -> None:
        """Receive-time metadata only; never infer transcription completion.

        Continuous PCM (including device-substituted silence) advances at 48000
        B/s. The minimum arrival-minus-duration offset removes the arbitrary
        first-frame origin. Falling offsets also detect catch-up when the first
        frames themselves were delayed. Absolute initial delay, loss concealed by
        a discontinuous device timeline, and provider transcription lag are NOT
        measurable here; even a caught-up stream and empty outbox are not EOS.
        """
        now = self._clock.now()
        self._input_bytes += size
        offset = now - self._input_bytes / gs.AUDIO_BYTES_PER_S
        self._input_offset = offset if self._input_offset is None else min(
            self._input_offset, offset
        )
        if self._input_rate_offset is None or offset > self._input_rate_offset:
            self._input_rate_offset = offset
        elif self._input_rate_offset - offset > gs.CONVERSE_INPUT_GAIN_S + _EPS:
            self._input_catchup_until = now + gs.CONVERSE_INPUT_LAG_S
            self._input_rate_offset = offset
        if self._state == _SETTLING and not self._sealing and self._input_behind(now):
            if self._input_wait_t is None:
                self._input_wait_t = now
            self._arm_settle()

    def _refresh_converse_input(self) -> None:
        now = self._clock.now()
        # A timer has priority over the external FIFO. Audio already received but
        # not yet forwarded must not be mistaken for an empty provider outbox.
        queued = any(e[0] == "device" and e[1].kind == "audio" for e in self._inbox)
        if self._input_behind(now) or self._pout.pending_audio or queued:
            if self._input_wait_t is None:
                self._input_wait_t = now
        elif self._input_wait_t is not None:
            self._input_quiet_t = now
            self._input_wait_t = None

    def _on_audio_sent(self, pcm: bytes) -> None:
        # Observe only completed paced device sends, not provider arrival time:
        # queued/backpressured speech must not admit a new request too early.
        if (not self._converse or self._state != _SPEAKING
                or not self._converse_audio_ready or not self._commentary_committed):
            return
        if any(abs(int.from_bytes(pcm[i:i + 2], "little", signed=True)) > 256
               for i in range(0, len(pcm), 2)):
            self._render_audible = True
            self._last_audible = self._clock.now() + len(pcm) / gs.AUDIO_BYTES_PER_S

    def _on_converse_begin(self) -> None:
        if self._state != _READY or self._turn_used or self._begin_pending:
            self._fail("unknown_command")
            return
        self._converse = True
        self._turn_used = True
        self._last_speech = self._clock.now()
        self._disarm("cap")
        assert self._accepted_t is not None
        self._arm("converse_limit", max(
            0.0, self._accepted_t + gs.CONVERSE_WORK_S - self._clock.now()
        ))
        # Hold until began has actually reached the device. Audio received before
        # begin has no token and cannot retroactively enter this stream.
        self._audio_token = self._audio.open(hold=True)
        self._emit(gs.build_converse_began(
            self._settings.label, self._settings.short8
        ), tag="converse_began")
        self._converse_listen()

    def _converse_listen(self) -> None:
        if self._state in (_CLOSING, _CLOSED):
            return
        for timer in ("settle", "poll", "converse_reply", "converse_render"):
            self._disarm(timer)
        self._request_seq += 1
        self._state = _CAPTURING
        self._stager = gs.TranscriptStager()
        self._transcript_floor = self._transcript_end
        self._last_delta = -math.inf
        self._sealing = False
        self._input_wait_t = None
        self._input_quiet_t = -math.inf
        self._speak_pending = False
        self._reply_text = None
        self._poll_inflight = False
        # Pending work/rendering is not idle. Start a fresh listening interval,
        # rather than immediately expiring on the speech that began a long request.
        self._last_speech = self._clock.now()
        if not self._emit(gs.build_converse_state("listening")):
            return
        self._arm("converse_idle", max(
            0.0, self._last_speech + gs.CONVERSE_IDLE_S - self._clock.now()
        ))

    def _converse_idle(self) -> None:
        if self._state != _CAPTURING:
            return  # pending work/rendering does not expire for input quiet
        remaining = self._last_speech + gs.CONVERSE_IDLE_S - self._clock.now()
        if remaining > _EPS:
            self._arm("converse_idle", remaining)
            return
        self._converse_end = "idle_timeout"
        self._emit(gs.build_converse_notice("idle_timeout"))
        self._begin_close()

    def _converse_limit(self) -> None:
        self._converse_end = "time_limit"
        self._emit(gs.build_converse_notice("time_limit"))
        self._begin_close()

    def _converse_transcript(
        self, event: gp.ProviderEvent,
        window: tuple[int, str, bool, bool] | None = None,
    ) -> None:
        assert event.event_id is not None and event.text is not None
        assert event.start_ms is not None and event.end_ms is not None
        if event.event_id in self._seen_transcripts:
            # A conflicting replay within the current capture must fail closed,
            # not silently send the superseded words. Across request boundaries
            # only ids/timeline metadata survive and every replay is suppressed.
            if any(ident == event.event_id for ident, _ in self._stager.ordered()):
                try:
                    self._stager.add(
                        event.event_id, event.text, event.start_ms, event.end_ms
                    )
                except gs.GatewayProtocolError:
                    if self._state == _CAPTURING or (
                        self._state == _SETTLING and not self._sealing
                    ):
                        self._fail("unstable_transcript")
            return
        if len(self._seen_transcripts) >= 8192:
            self._fail("overflow")
            return
        self._seen_transcripts.add(event.event_id)
        self._transcript_end = max(self._transcript_end, event.end_ms)
        if event.text.strip():
            self._last_speech = self._clock.now()
        # No implicit backlog/steering: speech during an active send/reply is
        # consumed, not silently queued for a later delegation. Late old-timeline
        # input is likewise never spliced into a new request.
        capturing = self._state == _CAPTURING or (
            self._state == _SETTLING and not self._sealing
        )
        if window is not None:
            capturing = capturing and window[0] == self._request_seq and (
                window[1] == _CAPTURING or (window[1] == _SETTLING and not window[2])
            )
        if not capturing or event.start_ms < self._transcript_floor or not event.text:
            return
        try:
            self._stager.add(
                event.event_id, event.text, event.start_ms, event.end_ms
            )
        except gs.GatewayProtocolError as err:
            self._fail(_STAGER_FAIL[err.code])
            return
        self._last_delta = self._clock.now()
        if self._state == _SETTLING:
            self._arm_settle()
        elif event.text.strip():
            self._arm("converse_idle", gs.CONVERSE_IDLE_S)

    def _converse_delegation(
        self, event: gp.ProviderEvent,
        window: tuple[int, str, bool, bool] | None = None,
    ) -> None:
        ident = event.delegation_id
        if ident is None or ident in self._seen_delegations:
            return
        if len(self._seen_delegations) >= 1024:
            self._fail("overflow")
            return
        self._seen_delegations.add(ident)
        if self._state != _CAPTURING or (window is not None and (
            window[0] != self._request_seq or window[1] != _CAPTURING
        )):
            self._requests_refused += 1
            return  # never queued or replayed after the pending request
        self._turn_id = None
        self._send_state = None
        self._send_stage = ""
        self._send_accepted = False
        self._reply_done = None
        self._delegation_id = ident
        self._state = _SETTLING
        self._stop_t = self._clock.now()
        self._disarm("converse_idle")
        self._arm_settle()

    def _converse_settle(self) -> None:
        if self._state != _SETTLING or self._sealing:
            return
        self._refresh_converse_input()
        ready, _limit = self._settle_target()
        now = self._clock.now()
        if (self._input_wait_t is not None
                and now + _EPS >= self._input_wait_t + gs.CONVERSE_INPUT_MAX_S):
            self._known_unsent()
            self._emit(gs.build_converse_notice("not_sent"))
            self._converse_end = "not_sent"
            # Close instead of admitting the rejected utterance's delayed tail
            # as a new request. Nothing was submitted; never retry it.
            self._begin_close()
        elif self._input_wait_t is not None or now + _EPS < ready:
            self._arm_settle()
        elif now + _EPS >= ready and self._stager.text().strip():
            self._sealing = True  # freezes segmentation BEFORE async begin
            self._begin_pending = True
            previous = self._timed_out_turn
            if previous is None:
                self._submit("begin", self._ops.begin, "begin")
            else:
                self._submit(
                    "begin",
                    lambda: self._ops.begin_after_timeout(previous, self._closed_for_recovery),
                    "begin",
                )
        else:
            self._requests_refused += 1
            self._converse_notice("not_sent")  # delegation without recognised words

    def _converse_notice(self, code: str, *, timed_out: bool = False) -> None:
        if not self._emit(gs.build_converse_notice(code)):
            return
        self._notice_code = code
        self._reply_text = None
        self._state = _CONVERSE_RECOVERING
        self._disarm("poll")
        self._disarm("converse_reply")
        # Preflight/known-unsent refusals can leave a sealed capture in core.
        # Abandon ONLY that capture, never resolve an uncertain journal entry.
        if self._turn_id is not None:
            turn = self._turn_id
            action = self._ops.timeout_reply if timed_out else self._ops.abandon
            self._submit("abandon", lambda: action(turn), "converse_abandon")
        else:
            self._on_converse_abandon(True)

    def _on_converse_abandon(self, ok: bool) -> None:
        if self._state != _CONVERSE_RECOVERING:
            return
        if not ok:
            self._fail("busy")
            return
        lines = {
            "busy": "That conversation is busy.",
            "not_sent": "That was not sent.",
            "send_uncertain": "I could not confirm it was sent. I will not send it again.",
            "reply_unavailable": "No reply is available yet.",
        }
        self._converse_render(lines[self._notice_code or "not_sent"], reply=False)

    def _converse_render(self, text: str, *, reply: bool) -> None:
        delegation = self._delegation_id
        try:
            chunks = gs.chunk_commentary(text)
            ids = tuple(self._new_event_id() for _ in chunks)
            frames = [
                gp.build_commentary(ident, delegation or "", chunk)
                for ident, chunk in zip(ids, chunks, strict=True)
            ]
        except ValueError:
            if reply:
                self._converse_notice("reply_unavailable")
            else:
                self._fail("no_delegation")
            return
        self._disarm("converse_reply")
        self._state = _SPEAKING
        self._render_reply = reply
        self._render_audible = False
        self._render_start = self._clock.now()
        self._commentary_committed = False
        self._last_audible = -math.inf
        self._chunk_ids = ids
        self._chunks_sent.clear()
        self._acked.clear()
        if not self._emit(gs.build_converse_state("answering")):
            return
        for ident, frame in zip(ids, frames, strict=True):
            if not self._pout.put(frame, tag=ident):
                self._fail("overflow")
                return
        self._arm("converse_render", 0.1)

    def _converse_render_tick(self) -> None:
        if self._state != _SPEAKING:
            return
        now = self._clock.now()
        ready = (
            self._all_acked() and len(self._chunks_sent) == len(self._chunk_ids)
            and self._render_audible
            and now - self._last_audible >= gs.CONVERSE_SPEECH_QUIET_S - _EPS
            and self._audio.queued_bytes <= gs.AUDIO_CHUNK_BYTES
        )
        # ACK plus a pause in *audible samples*, not packets. The continuous
        # silence stream must not prevent another request. This is an explicitly
        # conservative display/admission heuristic, never a provider EOS claim.
        if ready:
            if self._render_reply:
                self._replies_rendered += 1
            self._converse_listen()
        elif now - self._render_start >= gs.CONVERSE_RENDER_MAX_S - _EPS:
            # Missing ACK/audio, or speech never ended. Do not admit a new request
            # into an unknown rendering boundary; close normally, never resend.
            self._emit(gs.build_converse_notice("reply_unavailable"))
            self._converse_end = "render_timeout"
            self._begin_close()
        else:
            self._arm("converse_render", 0.1)

    def _converse_timeout(self) -> None:
        self._timed_out_turn = self._turn_id
        self._converse_notice("reply_unavailable", timed_out=True)

    def _converse_reply_timeout(self) -> None:
        if (self._state == _AWAIT_REPLY
                and self._clock.now() + _EPS >= self._send_accept_t + gs.CONVERSE_REPLY_MAX_S):
            self._converse_timeout()


# --STAGE-MARKER--


__all__ = [
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
]
