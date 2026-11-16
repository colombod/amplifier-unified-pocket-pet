"""Selected-session send gate with durable single-flight and exact reply-once.

The core is transport-agnostic: it is handed a ``Transport`` and a ``Journal`` and
never opens a socket.  It enforces:

* an explicit, immutable ``Binding`` captured at ``begin_turn``;
* one pending turn / in-flight command at a time, including across restart;
* send only after the exact sealed text digest is confirmed, and only while the
  turn's captured voice epoch is still current: a changed target refuses the
  confirm (``epoch_stale``) before PREPARED, any command or transport use;
* the full private record is persisted PREPARED then ATTEMPTED *before* the
  transport call; an ambiguous outcome is UNCERTAIN and is never retried;
* text is scrubbed from the journal at every transition that follows the attempt;
* Stop / clear / selecting another session bump a persisted voice *epoch*, and a
  reply is spoken only if its command's epoch is still current, at most once, with
  the "consumed" claim persisted before the text is returned;
* every restart of an existing journal is a fresh, DISARMED audio epoch above the
  binding epoch and every command epoch, so a recovered reply can never be spoken
  and capture needs an explicit ``resume()``.  A brand-new journal starts active;
* an UNCERTAIN command is never aged out: after 24 h it shrinks to a minimal
  unresolved tombstone and keeps the single-flight gate until an operator gives the
  exact ``resolve_uncertain`` acknowledgement;
* the main lock is NOT held while the transport call is in flight: the durable
  ATTEMPTED record and the in-flight claim are taken under the lock, the lock is
  released for the HTTP round trip and re-taken only to record the outcome.

Nothing here claims Unified is idle: ``expected_revision`` comes from a future
validated preflight and is only forwarded as the request's top-level CAS field.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from . import wire
from .companion import DetailWindow
from .journal import BINDING_NAME, Journal, JournalError
from .reply import MAX_REPLY_CHARS, evaluate_reply, input_delivered

MAX_TURN_AGE_S = 60.0
RETENTION_S = 24 * 3600.0
MAX_EVENTS_PER_TURN = 128
MAX_TURN_CHARS = wire.MAX_TEXT_CHARS
_REVOKED_MEMORY = 8
_DISCARDED_EVENT_CAP = 1024  # in-memory replay memory for events of discarded turns

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")  # event ids
_CRED_RE = re.compile(r"device-[0-9a-f]{32}")  # opaque credential identifier ONLY
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

_STATES = frozenset(
    {
        "PREPARED",
        "ATTEMPTED",
        "ACCEPTED",
        "REJECTED",
        "UNCERTAIN",
        "ABANDONED",
        "COMPLETE",
        "SUPPRESSED",
        "RESOLVED",
    }
)
_TERMINAL = frozenset({"REJECTED", "ABANDONED", "COMPLETE", "SUPPRESSED", "RESOLVED"})
_EXPIRING = _TERMINAL | {"ACCEPTED"}  # UNCERTAIN never ages out; see _tombstone
_TOMBSTONE_KEEP = ("state", "binding", "epoch", "created_at", "updated_at", "reason")
_BINDING_FIELDS = (
    "binding_id",
    "host_id",
    "host_instance_id",
    "session_id",
    "client_id",
    "credential_ref",
)
_NOT_REPLYING = {
    "COMPLETE": "reply_closed",
    "SUPPRESSED": "suppressed",
    "UNCERTAIN": "delivery_uncertain",
    "REJECTED": "delivery_rejected",
    "PREPARED": "delivery_in_flight",
    "ATTEMPTED": "delivery_in_flight",
    "ABANDONED": "abandoned",
    "RESOLVED": "resolved",
}


class BridgeError(Exception):
    """A refusal or failure identified only by a stable ``code``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class Binding:
    """Explicit target captured at ``begin_turn``.  ``credential_ref`` is an opaque
    identifier (exactly ``device-`` + 32 lowercase hex characters) of a credential
    held elsewhere, never the credential itself; it is hidden from ``repr``."""

    binding_id: str
    host_id: str
    host_instance_id: str
    session_id: str
    client_id: str
    credential_ref: str = field(repr=False)


@dataclass(frozen=True)
class TranscriptPreview:
    """What the human confirms: the exact sealed text, its digest and target."""

    turn_id: str
    binding_id: str
    session_id: str
    text: str = field(repr=False)
    text_sha256: str
    chars: int


@dataclass(frozen=True)
class CommandResult:
    """``outcome`` is accepted, rejected, uncertain or refused (nothing was sent)."""

    command_id: str
    outcome: str
    reason: str
    persisted: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplyResult:
    """``status`` is speak, pending or not_spoken.  ``text`` only when speak."""

    command_id: str
    status: str
    reason: str
    terminal: bool
    epoch: int
    text: str | None = field(default=None, repr=False)


class Transport(Protocol):
    def send(
        self, binding: Binding, method: str, path: str, body: bytes
    ) -> tuple[int, bytes]: ...


@dataclass
class _Turn:
    turn_id: str
    binding: Binding
    epoch: int
    started_at: float
    started_mono: float
    parts: list[str] = field(default_factory=list, repr=False)
    events: list[str] = field(default_factory=list, repr=False)
    chars: int = 0
    sealed: bool = False
    text: str = field(default="", repr=False)
    sha: str = ""


def _number(value: Any) -> bool:
    return type(value) in (int, float)


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _valid_binding_dict(value: Any) -> Binding | None:
    if not isinstance(value, dict) or set(value) != set(_BINDING_FIELDS):
        return None
    if not wire.is_canonical_uuid4(value["binding_id"]):
        return None
    ok = (
        wire.valid_session_id(value["session_id"])
        and all(
            isinstance(value[k], str) and _ID_RE.fullmatch(value[k])
            for k in ("host_id", "host_instance_id", "client_id")
        )
        and isinstance(value["credential_ref"], str)
        and _CRED_RE.fullmatch(value["credential_ref"])
    )
    return Binding(**value) if ok else None


def _validate_cmd(record: dict[str, Any]) -> dict[str, Any]:
    if (
        record.get("state") not in _STATES
        or _valid_binding_dict(record.get("binding")) is None
        or type(record.get("epoch")) is not int
        or not _number(record.get("updated_at"))
        or not _number(record.get("created_at"))
        or not isinstance(record.get("event_ids"), list)
        or not all(isinstance(e, str) for e in record["event_ids"])
        or ("text" in record and not isinstance(record["text"], str))
    ):
        raise BridgeError("journal_corrupt")
    return record


def _journal_code(err: JournalError) -> str:
    return {"journal_full": "journal_full", "journal_io_error": "journal_error"}.get(
        err.code,
        "journal_corrupt"
        if err.code.startswith("journal_record") or err.code == "journal_unsafe_record"
        else "journal_error",
    )


class Bridge:
    """Owns one selected-session voice-to-Unified relay.  Thread-safe via one RLock;
    transport callbacks may re-enter ``stop`` and friends on the calling thread."""

    def __init__(
        self,
        journal: Journal,
        transport: Transport,
        *,
        clock: Callable[[], float],
        new_id: Callable[[], str],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._journal = journal
        self._transport = transport
        self._clock = clock
        self._monotonic = monotonic
        self._new_id = new_id
        self._lock = threading.RLock()
        self._binding: Binding | None = None
        self._epoch = 0
        self._stopped = False
        self._faulted = False
        self._turn: _Turn | None = None
        self._inflight: str | None = None
        self._revoked: dict[str, str] = {}
        self._seen: set[str] = set()
        self._discarded: dict[str, None] = {}
        with self._lock:
            self._start()

    def _start(self) -> None:
        """Load, recover, then (for an existing journal) persist a fresh DISARMED epoch.

        Any failure raises, so no ``Bridge`` exists that could send.
        """
        known = self._load_state()
        commands = self._commands()
        floor = max([self._epoch, *(record["epoch"] for _n, record in commands)])
        self._recover()
        self.cleanup()
        if known or commands:
            self._epoch, self._stopped = floor + 1, True
            self._persist_state()

    def _wall(self) -> float:
        now = self._clock()
        if not _finite(now):
            raise BridgeError("invalid_clock")
        return now

    def _mono(self) -> float:
        now = self._monotonic()
        if not _finite(now):
            raise BridgeError("invalid_clock")
        return now

    # -- journal plumbing ----------------------------------------------------------
    @staticmethod
    def _name(command_id: str) -> str:
        return f"cmd-{command_id}.json"

    def _put(self, name: str, record: dict[str, Any]) -> None:
        try:
            self._journal.put(name, record)
        except JournalError as err:
            if err.code == "journal_persist_unknown":
                self._faulted = True
            raise BridgeError(_journal_code(err)) from None

    def _read(self, name: str) -> dict[str, Any] | None:
        try:
            return self._journal.get(name)
        except JournalError as err:
            raise BridgeError(_journal_code(err)) from None

    def _delete(self, name: str) -> None:
        try:
            self._journal.delete(name)
        except JournalError as err:
            if err.code == "journal_persist_unknown":
                self._faulted = True
            raise BridgeError(_journal_code(err)) from None

    def _count(self) -> int:
        try:
            return self._journal.count()
        except JournalError as err:
            raise BridgeError(_journal_code(err)) from None

    def _cmd(self, command_id: Any) -> dict[str, Any] | None:
        if not wire.is_canonical_uuid4(command_id):
            return None
        record = self._read(self._name(command_id))
        return None if record is None else _validate_cmd(record)

    def _commands(self) -> list[tuple[str, dict[str, Any]]]:
        try:
            names = self._journal.names("cmd-")
        except JournalError as err:
            raise BridgeError(_journal_code(err)) from None
        found = []
        for name in names:
            record = self._read(name)
            if record is not None:
                found.append((name, _validate_cmd(record)))
        return found

    def pre_dial_state(self, expected_binding: Binding | None) -> str:
        """Read-only admission classification: safe, reconcilable uncertainty, or blocked.

        This is not the turn gate: it does not clean up, suppress, reconcile, or
        resume anything. The journal's exclusive-owner and durable ATTEMPTED-before-
        send invariants are required, as is gateway custody across sessions.
        """
        with self._lock:
            if (
                self._faulted
                or self._inflight is not None
                or self._turn is not None
                or expected_binding is None
                or self._binding != expected_binding
            ):
                return "blocked"
            try:
                # Check the persisted binding without mutating the loaded state.
                state = self._read(BINDING_NAME)
                if (
                    state is None
                    or set(state) != {"binding", "epoch", "stopped"}
                    or _valid_binding_dict(state["binding"]) != self._binding
                    or type(state["epoch"]) is not int
                    or state["epoch"] != self._epoch
                    or type(state["stopped"]) is not bool
                    or state["stopped"] != self._stopped
                ):
                    return "blocked"
                names = self._journal.names("cmd-")
                uncertain = False
                for name in names:
                    record = self._read(name)
                    if record is None:
                        return "blocked"
                    state_name = _validate_cmd(record)["state"]
                    if state_name in {"PREPARED", "ATTEMPTED"}:
                        return "blocked"
                    if state_name == "UNCERTAIN":
                        binding = _valid_binding_dict(record["binding"])
                        digest = record.get("text_sha256")
                        updated = record["updated_at"]
                        if (
                            "tombstone" in record
                            or not _finite(updated)
                            or not 0 <= self._wall() - updated < RETENTION_S
                            or not isinstance(digest, str)
                            or _SHA_RE.fullmatch(digest) is None
                            or binding is None
                            or (
                                binding.host_id, binding.client_id, binding.credential_ref,
                                binding.session_id,
                            ) != (
                                expected_binding.host_id, expected_binding.client_id,
                                expected_binding.credential_ref, expected_binding.session_id,
                            )
                        ):
                            return "blocked"
                        uncertain = True
                if self._count() != len(names) + 1:
                    return "blocked"
                return "uncertain" if uncertain else "safe"
            except Exception:  # noqa: BLE001 - any unreadable state is unsafe, never logged
                return "blocked"

    def pre_dial_safe(self, expected_binding: Binding | None) -> bool:
        """The unchanged, non-mutating final gate used immediately before a dial."""
        return self.pre_dial_state(expected_binding) == "safe"

    def _transition(
        self, name: str, record: dict[str, Any], state: str, **extra: Any
    ) -> dict[str, Any]:
        updated = {k: v for k, v in record.items() if k != "text"}
        updated.update(state=state, updated_at=self._wall(), **extra)
        self._put(name, updated)
        return updated

    def _state_record(
        self, binding: Binding | None, epoch: int, stopped: bool
    ) -> dict[str, Any]:
        return {
            "binding": None if binding is None else asdict(binding),
            "epoch": epoch,
            "stopped": stopped,
        }

    def _persist_state(self) -> None:
        self._put(
            BINDING_NAME, self._state_record(self._binding, self._epoch, self._stopped)
        )

    def _load_state(self) -> bool:
        """Load the persisted binding/epoch; True when a binding record existed."""
        record = self._read(BINDING_NAME)
        if record is None:
            return False
        raw = record.get("binding")
        binding = None if raw is None else _valid_binding_dict(raw)
        if (
            set(record) != {"binding", "epoch", "stopped"}
            or (raw is not None and binding is None)
            or type(record["epoch"]) is not int
            or record["epoch"] < 0
            or type(record["stopped"]) is not bool
        ):
            raise BridgeError("journal_corrupt")
        self._binding, self._epoch, self._stopped = (
            binding,
            record["epoch"],
            record["stopped"],
        )
        return True

    def _recover(self) -> None:
        """Startup recovery.  Never contacts the transport and never re-dispatches."""
        for name, record in self._commands():
            self._seen.update(record["event_ids"])
            if record["state"] == "PREPARED":
                self._transition(name, record, "ABANDONED", reason="recovered_unsent")
            elif record["state"] == "ATTEMPTED":
                self._transition(
                    name, record, "UNCERTAIN", reason="recovered_attempted"
                )

    # -- retention / capacity ------------------------------------------------------
    def cleanup(self) -> int:
        """Forget expired terminal and accepted-and-unanswered records (returns how many).

        An UNCERTAIN record is never forgotten and never releases the gate: after 24 h
        it is shrunk to a minimal unresolved tombstone (no digest, counters, revision,
        HTTP metadata or event ids) until ``resolve_uncertain``.  The duplicate-event
        memory is rebuilt from what is retained.
        """
        with self._lock:
            now, removed, retained = self._wall(), 0, []
            for name, record in self._commands():
                aged = now - record["updated_at"] >= RETENTION_S
                if aged and record["state"] in _EXPIRING:
                    self._delete(name)
                    removed += 1
                    continue
                if (
                    aged
                    and record["state"] == "UNCERTAIN"
                    and "tombstone" not in record
                ):
                    record = self._tombstone(name, record, now)
                retained.append(record)
            self._rebuild_seen(retained)
            return removed

    def _tombstone(
        self, name: str, record: dict[str, Any], now: float
    ) -> dict[str, Any]:
        tomb = {key: record[key] for key in _TOMBSTONE_KEEP if key in record}
        tomb.update(
            command_id=name[len("cmd-") : -len(".json")],
            event_ids=[],
            tombstone=True,
            tombstoned_at=now,
        )
        self._put(name, tomb)
        return tomb

    def _rebuild_seen(self, records: Any) -> None:
        """Bounded by construction: at most ``max_records`` x 128 retained event ids."""
        self._seen = {event for record in records for event in record["event_ids"]}

    def _ensure_capacity(self) -> None:
        count = self._count()
        if count + 1 <= self._journal.max_records:
            return
        terminal = sorted(
            (record["updated_at"], name)
            for name, record in self._commands()
            if record["state"] in _TERMINAL
        )
        evicted = False
        while terminal and count + 1 > self._journal.max_records:
            self._delete(terminal.pop(0)[1])
            evicted = True
            count -= 1
        if evicted:
            self._rebuild_seen(record for _n, record in self._commands())
        if count + 1 > self._journal.max_records:
            raise BridgeError("journal_full")

    def _gate(self) -> None:
        """Refuse a new command while another is in flight, uncertain or awaiting reply."""
        flags: set[str] = set()
        if self._inflight is not None:
            flags.add("command_inflight")
        for name, record in self._commands():
            state = record["state"]
            if state in ("PREPARED", "ATTEMPTED"):
                flags.add("command_inflight")
            elif state == "UNCERTAIN":
                flags.add("uncertain_unresolved")
            elif state == "ACCEPTED":
                if record["epoch"] != self._epoch:
                    self._transition(name, record, "SUPPRESSED", reason="epoch_stale")
                else:
                    flags.add("reply_pending")
        for code in ("command_inflight", "uncertain_unresolved", "reply_pending"):
            if code in flags:
                raise BridgeError(code)

    # -- binding -------------------------------------------------------------------
    def select_session(
        self,
        *,
        host_id: str,
        host_instance_id: str,
        session_id: str,
        client_id: str,
        credential_ref: str,
    ) -> Binding:
        """Select the session for FUTURE turns.

        A turn already begun keeps its frozen binding but is never redirected: when
        the target changes, its confirm is refused ``epoch_stale`` before anything is
        persisted or sent (A -> B -> A stays stale).  Re-selecting the SAME
        host/instance/session leaves the epoch alone, so the original frozen binding
        still sends.  A command already ATTEMPTED keeps its outcome; its reply is
        suppressed.
        """
        with self._lock:
            if self._faulted:
                raise BridgeError("bridge_faulted")
            new_id = self._new_id()
            if not wire.is_canonical_uuid4(new_id):
                raise BridgeError("invalid_id")
            binding = _valid_binding_dict(
                {
                    "binding_id": new_id,
                    "host_id": host_id,
                    "host_instance_id": host_instance_id,
                    "session_id": session_id,
                    "client_id": client_id,
                    "credential_ref": credential_ref,
                }
            )
            if binding is None:
                raise BridgeError("invalid_binding")
            old = self._binding
            moved = old is not None and (
                old.host_id,
                old.host_instance_id,
                old.session_id,
            ) != (
                binding.host_id,
                binding.host_instance_id,
                binding.session_id,
            )
            epoch = self._epoch + (1 if moved else 0)
            self._put(BINDING_NAME, self._state_record(binding, epoch, self._stopped))
            self._binding, self._epoch = binding, epoch
            return binding

    def clear_session(self) -> None:
        """Revoke the binding and any pending turn.  Takes effect even if persistence fails."""
        with self._lock:
            self._binding = None
            self._epoch += 1
            self._discard("binding_cleared")
            self._persist_best_effort()

    def _persist_best_effort(self) -> None:
        """Persist Stop/clear; on failure the memory gate is already closed.

        If the write fails, fault the bridge and try to unlink ONLY the binding
        record so a restart cannot restore a stale armed state.  If every write
        fails the on-disk state cannot be certified; a restart still starts a
        fresh disarmed epoch that suppresses every older reply.
        """
        try:
            self._persist_state()
        except BridgeError:
            self._faulted = True
            with contextlib.suppress(JournalError):
                self._journal.delete(BINDING_NAME)

    def _discard(self, reason: str) -> None:
        turn, self._turn = self._turn, None
        if turn is not None:
            self._revoked[turn.turn_id] = reason
            while len(self._revoked) > _REVOKED_MEMORY:
                self._revoked.pop(next(iter(self._revoked)))
            for event in turn.events:
                self._discarded.pop(event, None)
                self._discarded[event] = None
            while len(self._discarded) > _DISCARDED_EVENT_CAP:
                self._discarded.pop(next(iter(self._discarded)))

    # -- turn capture --------------------------------------------------------------
    def abandon_turn(self, turn_id: str) -> None:
        """Discard ONLY this unsent capture; never change a command's journal state.

        Used when a conversational request fails preflight. Unlike stop/resume it
        cannot re-arm a stopped bridge, advance an epoch or suppress accepted work.
        """
        with self._lock:
            if self._turn is not None and self._turn.turn_id == turn_id:
                self._discard("abandoned")

    def begin_turn(self) -> str:
        with self._lock:
            if self._faulted:
                raise BridgeError("bridge_faulted")
            if self._stopped:
                raise BridgeError("stopped")
            if self._binding is None:
                raise BridgeError("no_binding")
            if self._turn is not None:
                raise BridgeError("turn_pending")
            self.cleanup()
            self._gate()
            self._ensure_capacity()
            turn_id = self._new_id()
            if not wire.is_canonical_uuid4(turn_id):
                raise BridgeError("invalid_id")
            if self._read(self._name(turn_id)) is not None:
                raise BridgeError("id_collision")
            self._turn = _Turn(
                turn_id, self._binding, self._epoch, self._wall(), self._mono()
            )
            return turn_id

    def _open_turn(self, turn_id: str) -> tuple[_Turn | None, str | None]:
        turn = self._turn
        if turn is not None and turn.turn_id == turn_id:
            try:
                wall_age = self._wall() - turn.started_at
                mono_age = self._mono() - turn.started_mono
            except BridgeError:
                self._discard("invalid_clock")
                return None, "invalid_clock"
            # Fresh only if BOTH clocks agree: a rolled-back or jumped clock never
            # extends the age, whichever one moved.
            if not (
                0 <= wall_age <= MAX_TURN_AGE_S and 0 <= mono_age <= MAX_TURN_AGE_S
            ):
                self._discard("expired")
                return None, "expired"
            return turn, None
        return None, self._revoked.get(turn_id, "unknown_turn")

    def append_transcript(self, turn_id: str, text: str, event_id: str) -> str:
        """Add a transcript fragment.  Returns an outcome code; never raises for bad input."""
        with self._lock:
            turn, code = self._open_turn(turn_id)
            if turn is None:
                return code or "unknown_turn"
            if turn.sealed:
                return "sealed"
            if not isinstance(text, str) or "\x00" in text or not self._encodable(text):
                return "invalid_text"
            if not isinstance(event_id, str) or not _REF_RE.fullmatch(event_id):
                return "invalid_event"
            if (
                event_id in turn.events
                or event_id in self._seen
                or event_id in self._discarded
            ):
                return "duplicate_event"
            if len(turn.events) >= MAX_EVENTS_PER_TURN:
                return "too_many_events"
            if turn.chars + len(text) > MAX_TURN_CHARS:
                return "too_long"
            turn.parts.append(text)
            turn.events.append(event_id)
            turn.chars += len(text)
            return "appended"

    @staticmethod
    def _encodable(text: str) -> bool:
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True

    def on_live_event(self, turn_id: str, event: Any) -> str:
        """Feed a model event.  It can only add transcript preview; it never dispatches."""
        try:
            parsed = wire.parse_live_event(event)
        except wire.WireError:
            return "invalid_event"
        if parsed is None:
            return "ignored"
        if parsed.kind == "transcript_delta":
            return self.append_transcript(
                turn_id, parsed.text or "", parsed.event_id or ""
            )
        if parsed.kind == "delegation_hint":
            return "delegation_hint_ignored"
        return "commentary_not_forwarded"

    def seal_turn(self, turn_id: str) -> TranscriptPreview:
        with self._lock:
            turn, code = self._open_turn(turn_id)
            if turn is None:
                raise BridgeError(code or "unknown_turn")
            if not turn.sealed:
                text = "".join(turn.parts)
                if not text.strip():
                    raise BridgeError("blank_text")
                try:
                    wire.check_text(text)
                except wire.WireError as err:
                    raise BridgeError(err.code) from None
                turn.text = text
                turn.sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                turn.sealed = True
            return TranscriptPreview(
                turn.turn_id,
                turn.binding.binding_id,
                turn.binding.session_id,
                turn.text,
                turn.sha,
                len(turn.text),
            )

    # -- send ----------------------------------------------------------------------
    def _refuse(self, turn_id: str, reason: str) -> CommandResult:
        return CommandResult(turn_id, "refused", reason, False)

    def _confirmable(
        self, turn_id: str, digest: Any, revision: Any
    ) -> tuple[_Turn | None, CommandResult | None]:
        if self._faulted:
            return None, self._refuse(turn_id, "bridge_faulted")
        if self._turn is None or self._turn.turn_id != turn_id:
            try:
                existing = self._cmd(turn_id)
            except BridgeError:
                return None, self._refuse(turn_id, "journal_error")
            reason = (
                "duplicate_confirm"
                if existing is not None
                else self._revoked.get(turn_id, "unknown_turn")
            )
            return None, self._refuse(turn_id, reason)
        turn, code = self._open_turn(turn_id)
        if turn is None:
            return None, self._refuse(turn_id, code or "unknown_turn")
        if turn.epoch != self._epoch:
            # The target changed (or was left and re-selected: A -> B -> A stays
            # stale) after this preview was frozen.  The frozen binding is never
            # redirected and the obsolete preview is not sent: nothing is
            # persisted and the transport is never reached.
            self._discard("epoch_stale")
            return None, self._refuse(turn_id, "epoch_stale")
        if not turn.sealed:
            return None, self._refuse(turn_id, "not_sealed")
        if not (
            isinstance(digest, str)
            and _SHA_RE.fullmatch(digest)
            and hmac.compare_digest(digest, turn.sha)
        ):
            return None, self._refuse(turn_id, "digest_mismatch")
        if revision is not None and (type(revision) is not int or revision < 0):
            return None, self._refuse(turn_id, "invalid_revision")
        return turn, None

    def confirm_send(
        self,
        turn_id: str,
        *,
        expected_text_sha256: str,
        expected_revision: int | None = None,
    ) -> CommandResult:
        """Send the sealed turn once, only if the caller confirms its exact digest.

        The lock is held only to claim the turn (durable ATTEMPTED + in-flight
        claim) and again to record the outcome; it is NOT held across the transport
        call, so ``stop``/``status``/``epoch_current`` stay responsive and no
        second send can start meanwhile.
        """
        with self._lock:
            claim = self._claim_send(turn_id, expected_text_sha256, expected_revision)
        if isinstance(claim, CommandResult):
            return claim
        turn, attempted, method, path, body = claim
        return self._send_and_record(turn, attempted, method, path, body)

    def _claim_send(
        self, turn_id: str, digest: Any, revision: Any
    ) -> CommandResult | tuple[_Turn, dict[str, Any], str, str, bytes]:
        turn, refused = self._confirmable(turn_id, digest, revision)
        if turn is None:
            return refused or self._refuse(turn_id, "unknown_turn")
        try:
            method, path, body = wire.send_command_request(
                turn.binding.session_id, turn_id, turn.text, expected_revision=revision
            )
            self._gate()
            self._ensure_capacity()
            attempted = self._persist_before_send(turn, revision)
        except wire.WireError:
            return self._refuse(turn_id, "invalid_request")
        except BridgeError as err:
            return self._refuse(turn_id, err.code)
        if isinstance(attempted, CommandResult):
            return attempted
        # Claim BEFORE the lock is released: later confirms see the durable ATTEMPTED
        # record, begin_turn sees the in-flight claim.
        self._turn, self._inflight = None, turn.turn_id
        return turn, attempted, method, path, body

    def _persist_before_send(
        self, turn: _Turn, revision: int | None
    ) -> dict[str, Any] | CommandResult:
        name, now = self._name(turn.turn_id), self._wall()
        prepared = {
            "state": "PREPARED",
            "binding": asdict(turn.binding),
            "epoch": turn.epoch,
            "created_at": turn.started_at,
            "updated_at": now,
            "event_ids": list(turn.events),
            "text": turn.text,
            "text_sha256": turn.sha,
            "chars": len(turn.text),
            "expected_revision": revision,
        }
        try:
            self._put(name, prepared)
        except BridgeError as err:
            return self._refuse(turn.turn_id, err.code)
        self._seen.update(turn.events)
        attempted = {**prepared, "state": "ATTEMPTED", "attempted_at": now}
        try:
            self._put(name, attempted)
        except BridgeError as err:
            self._turn = None  # a record exists now; this id can never be sent again
            try:
                self._transition(
                    name, prepared, "ABANDONED", reason="attempt_not_persisted"
                )
            except BridgeError:
                self._faulted = True
            return self._refuse(turn.turn_id, err.code)
        return attempted

    def _send_and_record(
        self,
        turn: _Turn,
        attempted: dict[str, Any],
        method: str,
        path: str,
        body: bytes,
    ) -> CommandResult:
        """Run the transport call WITHOUT the lock, then record the outcome under it."""
        interrupted: BaseException | None = None
        try:
            receipt = self._classify(
                self._transport.send(turn.binding, method, path, body), turn
            )
        except Exception:  # noqa: BLE001 - injected transport boundary: any failure is uncertain
            receipt = wire.Receipt("uncertain", "transport_exception", {})
        except BaseException as exc:  # noqa: BLE001 - cancellation/exit: settle, then re-raise
            receipt = wire.Receipt("uncertain", "transport_interrupted", {})
            interrupted = exc
        try:
            with self._lock:
                result = self._record_outcome(turn, attempted, receipt)
        finally:
            self._inflight = None
        if interrupted is not None:
            raise interrupted
        return result

    def _record_outcome(
        self, turn: _Turn, attempted: dict[str, Any], receipt: wire.Receipt
    ) -> CommandResult:
        self._inflight = None
        state = {"accepted": "ACCEPTED", "rejected": "REJECTED"}.get(
            receipt.outcome, "UNCERTAIN"
        )
        persisted = True
        try:
            self._transition(
                self._name(turn.turn_id),
                attempted,
                state,
                reason=receipt.reason,
                http=dict(receipt.metadata),
            )
        except BridgeError:
            self._faulted, persisted = True, False
        return CommandResult(
            turn.turn_id,
            receipt.outcome,
            receipt.reason,
            persisted,
            dict(receipt.metadata),
        )

    @staticmethod
    def _classify(reply: Any, turn: _Turn) -> wire.Receipt:
        if (
            not isinstance(reply, tuple)
            or len(reply) != 2
            or type(reply[0]) is not int
            or not isinstance(reply[1], bytes)
        ):
            return wire.Receipt("uncertain", "transport_bad_return", {})
        return wire.parse_send_receipt(
            reply[0],
            reply[1],
            session_id=turn.binding.session_id,
            command_id=turn.turn_id,
        )

    def resolve_uncertain(self, command_id: str, *, operator_ack: str) -> None:
        """Operator closes an UNCERTAIN command.  Never sends anything."""
        with self._lock:
            record = self._cmd(command_id)
            if record is None or record["state"] != "UNCERTAIN":
                raise BridgeError("not_uncertain")
            if (
                not isinstance(operator_ack, str)
                or operator_ack != f"ack-uncertain:{command_id}"
            ):
                raise BridgeError("ack_mismatch")
            self._transition(
                self._name(command_id), record, "RESOLVED", reason="operator_ack"
            )

    def reconcile_delivered(self, page: Any, *, session_id: str) -> tuple[str, ...]:
        """Promote UNCERTAIN commands the conversation itself proves were delivered.

        A send whose acknowledgement was lost (a timeout, say) is recorded UNCERTAIN and
        blocks every later send.  When the conversation shows that exact input (same
        command id, same text digest) the delivery is no longer uncertain: the record
        becomes ACCEPTED, exactly as if the acknowledgement had arrived.  Never sends,
        never resolves on absence, and leaves tombstones to the operator.
        """
        promoted: list[str] = []
        with self._lock:
            if self._faulted:
                return ()
            for name, record in self._commands():
                if record["state"] != "UNCERTAIN" or "tombstone" in record:
                    continue
                binding, digest = record.get("binding"), record.get("text_sha256")
                if (
                    not isinstance(binding, dict)
                    or binding.get("session_id") != session_id
                    or not isinstance(digest, str)
                ):
                    continue
                command_id = name[len("cmd-") : -len(".json")]
                if not input_delivered(page, command_id=command_id, text_sha256=digest):
                    continue
                try:
                    self._transition(
                        name, record, "ACCEPTED", reason="observed_in_conversation"
                    )
                except BridgeError:
                    self._faulted = True
                    break
                promoted.append(command_id)
        return tuple(promoted)

    def reconcile_pre_dial(
        self, expected_binding: Binding | None, page: Any, *, session_id: str
    ) -> tuple[str, ...]:
        """Recheck eligibility before applying existing positive delivery proof."""
        with self._lock:
            if (
                expected_binding is None
                or expected_binding.session_id != session_id
                or self.pre_dial_state(expected_binding) != "uncertain"
                or not isinstance(page, dict)
                or not isinstance(page.get("items"), list)
                or type(page.get("offset")) is not int
                or page["offset"] != 0
                or type(page.get("total")) is not int
                or page["total"] != len(page["items"])
                or ("sessionId" in page and page["sessionId"] != session_id)
                or any(
                    not isinstance(row, dict)
                    or ("sessionId" in row and row["sessionId"] != session_id)
                    for row in page["items"]
                )
            ):
                return ()
            # Conflicting native IDs elsewhere in the snapshot cannot be treated
            # as an unambiguous command identity, even if one row matches.
            for name, record in self._commands():
                if record["state"] != "UNCERTAIN":
                    continue
                command_id = name[len("cmd-") : -len(".json")]
                rows = [
                    row for row in page["items"]
                    if row.get("role") == "user"
                    and (row.get("inputId") == command_id or row.get("nativeInputId") == command_id)
                ]
                if len(rows) > 1:
                    return ()
            return self.reconcile_delivered(page, session_id=session_id)

    # -- reply ---------------------------------------------------------------------
    @staticmethod
    def _reply(
        command_id: Any,
        status: str,
        reason: str,
        terminal: bool = False,
        epoch: int = -1,
        text: str | None = None,
    ) -> ReplyResult:
        ident = command_id if isinstance(command_id, str) else ""
        return ReplyResult(ident, status, reason, terminal, epoch, text)

    def _poll_precheck(
        self, command_id: Any, session_id: str | None
    ) -> ReplyResult | dict[str, Any]:
        """Everything decidable without the page.  A dict result means 'evaluate'."""
        if self._faulted:
            return self._reply(command_id, "not_spoken", "bridge_faulted")
        try:
            record = self._cmd(command_id)
        except BridgeError:
            return self._reply(command_id, "not_spoken", "journal_error")
        if record is None:
            return self._reply(command_id, "not_spoken", "unknown_command")
        epoch = record["epoch"]
        if record["state"] != "ACCEPTED":
            reason = (
                "already_consumed"
                if record.get("reply_consumed") is True
                else _NOT_REPLYING[record["state"]]
            )
            return self._reply(command_id, "not_spoken", reason, True, epoch)
        if self._stopped:
            return self._reply(command_id, "not_spoken", "stopped", False, epoch)
        if epoch != self._epoch:
            return self._close(command_id, record, "SUPPRESSED", "epoch_stale")
        if session_id is not None and session_id != record["binding"]["session_id"]:
            return self._reply(
                command_id, "not_spoken", "foreign_session", False, epoch
            )
        return record

    def poll_reply(
        self,
        command_id: str,
        page: dict[str, Any],
        *,
        coverage_complete: bool,
        session_id: str | None = None,
        newest_tail: DetailWindow | None = None,
    ) -> ReplyResult:
        """Release the exact finished reply of ``command_id`` at most once."""
        with self._lock:
            record = self._poll_precheck(command_id, session_id)
            if isinstance(record, ReplyResult):
                return record
            decision = evaluate_reply(
                page,
                command_id=command_id,
                session_id=record["binding"]["session_id"],
                coverage_complete=coverage_complete,
                newest_tail=newest_tail,
            )
            if decision.status == "speak":
                return self._claim(command_id, record, decision.text)
            if decision.terminal:
                return self._close(
                    command_id,
                    record,
                    "COMPLETE",
                    decision.reason,
                    reply_consumed=False,
                )
            return self._reply(
                command_id, decision.status, decision.reason, False, record["epoch"]
            )

    def _close(
        self,
        command_id: str,
        record: dict[str, Any],
        state: str,
        reason: str,
        **extra: Any,
    ) -> ReplyResult:
        try:
            self._transition(
                self._name(command_id), record, state, reply_outcome=reason, **extra
            )
        except BridgeError:
            self._faulted = True
        return self._reply(command_id, "not_spoken", reason, True, record["epoch"])

    def _claim(
        self, command_id: str, record: dict[str, Any], text: str | None
    ) -> ReplyResult:
        """Persist 'consumed' BEFORE the text leaves; an unknown claim never repeats speech."""
        epoch = record["epoch"]
        if text is None or len(text) > MAX_REPLY_CHARS:
            return self._close(
                command_id, record, "COMPLETE", "reply_invalid", reply_consumed=False
            )
        try:
            self._transition(
                self._name(command_id),
                record,
                "COMPLETE",
                reply_outcome="spoken",
                reply_consumed=True,
            )
        except BridgeError:
            self._faulted = True
            return self._reply(command_id, "not_spoken", "journal_fault", False, epoch)
        return self._reply(command_id, "speak", "reply_complete", True, epoch, text)

    # -- control -------------------------------------------------------------------
    def stop(self) -> None:
        """Local stop only: abandon the unsent capture and mute older-epoch replies.

        No Unified stop is claimed and no command is sent.  Takes effect even if
        persistence fails (the bridge is then marked faulted)."""
        with self._lock:
            self._stopped = True
            self._epoch += 1
            self._discard("stopped")
            self._persist_best_effort()

    def resume(self) -> None:
        with self._lock:
            if self._faulted:
                raise BridgeError("bridge_faulted")
            if not self._stopped:
                return
            self._put(
                BINDING_NAME, self._state_record(self._binding, self._epoch, False)
            )
            self._stopped = False

    def epoch_current(self, epoch: int) -> bool:
        """A speaker re-checks this just before playing a released reply."""
        with self._lock:
            return type(epoch) is int and epoch == self._epoch and not self._stopped

    def status(self) -> dict[str, Any]:
        """Metadata only: ids, flags and counts.  Never any text."""
        with self._lock:
            records: dict[str, int] = {}
            total = 0
            try:
                for _name, record in self._commands():
                    records[record["state"]] = records.get(record["state"], 0) + 1
                total = self._journal.count()
            except (BridgeError, JournalError):
                records, total = {}, -1
            binding = self._binding
            return {
                "bound": binding is not None,
                "binding_id": None if binding is None else binding.binding_id,
                "host_id": None if binding is None else binding.host_id,
                "session_id": None if binding is None else binding.session_id,
                "epoch": self._epoch,
                "stopped": self._stopped,
                "faulted": self._faulted,
                "open_turn": self._turn is not None,
                "open_turn_chars": 0 if self._turn is None else self._turn.chars,
                "records": records,
                "journal_records": total,
            }
