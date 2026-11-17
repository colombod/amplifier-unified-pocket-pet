"""Host bridge core: speak to ONE explicitly selected existing Unified session.

Offline, stdlib-only core.  It opens no socket, holds no credential and talks to no
device.  A later authenticated transport and a speech gateway plug into the seams
below.  Unit tests here are offline evidence only: they are not a real network,
Unified, hardware or provider qualification.

Production public interfaces (``pet_bridge.<module>``)
-----------------------------------------------------
``journal``
    ``Journal(root: Path, max_records: int = 256)`` - private (0700/0600), bounded,
    single-process (flock) store of schema-1 JSON records, <= 64 KiB each, named
    ``binding.json`` or ``cmd-<uuid4>.json``.  ``put(name, record)``,
    ``get(name) -> dict | None``, ``names(prefix="") -> list[str]``,
    ``delete(name)``, ``count()``, ``max_records``, ``close()``, context manager.
    ``JournalError(code)``: ``journal_*`` codes only; ``journal_persist_unknown``
    means the rename happened and durability is unknown (never a rollback claim).

``wire``
    ``send_command_request(session_id, command_id, text, *, expected_revision=None)
    -> (method, path, body: bytes)`` - exact ``POST /api/sessions/{id}/commands``
    ``conversation.send``; ``expectedRevision`` is top level, never inside ``args``.
    ``parse_send_receipt(status, body, *, session_id, command_id=None) -> Receipt``
    with ``outcome`` accepted | rejected | uncertain.  ``parse_live_event(event)
    -> LiveEvent | None`` for ``session.input_transcript.delta``,
    ``session.delegation.created`` and ``session.commentary.append`` only.
    ``WireError(code)``.

``reply``
    ``evaluate_reply(page, *, command_id, session_id, coverage_complete, newest_tail=None)
    -> ReplyDecision`` (pure): speak only the exact finished generation of our own
    input, from whole history or a companion-validated newest tail with proof
    of the generation boundary. Arbitrary partial pages are never sufficient.

``core``
    ``Bridge(journal, transport, *, clock, new_id, monotonic=time.monotonic)``;
    ``Transport.send(binding, method, path, body) -> (status: int, body: bytes)``;
    ``Binding`` (``credential_ref`` is exactly ``device-`` + 32 lowercase hex, an
    opaque identifier, hidden from ``repr``);
    ``select_session(...) -> Binding``; ``clear_session()``; ``begin_turn() ->
    turn_id``; ``append_transcript(turn_id, text, event_id) -> code``;
    ``on_live_event(turn_id, event) -> code`` (preview only, never sends);
    ``seal_turn(turn_id) -> TranscriptPreview``; ``confirm_send(turn_id, *,
    expected_text_sha256, expected_revision=None) -> CommandResult``;
    ``poll_reply(command_id, page, *, coverage_complete, session_id=None, newest_tail=None) ->
    ReplyResult``; ``resolve_uncertain(command_id, *, operator_ack)``; ``stop()``;
    ``resume()``; ``epoch_current(epoch) -> bool``; ``cleanup() -> int``;
    ``status() -> metadata``.  ``BridgeError(code)``.

    Lifecycle invariants (round-1 repair):

    * A brand-new journal starts active and unbound.  EVERY restart of an existing
      journal is a fresh DISARMED audio epoch above the binding epoch and every
      command epoch (persisted before the constructor returns; a failure raises, so
      no sender exists).  The gateway must call ``resume()`` explicitly; a recovered
      ACCEPTED reply is never spoken.
    * ``stop()``/``clear_session()`` close the memory gate first; if the write
      fails the bridge is faulted and only the binding record is unlinked as a
      fallback.  If every write fails the on-disk state is uncertified, and the next
      start is still a fresh disarmed epoch.
    * A turn is sent only to its frozen binding and only while that binding's voice
      epoch is still current.  If the target changed after the preview (including
      A -> B -> A) ``confirm_send`` returns ``refused`` / ``epoch_stale`` before
      anything is persisted or sent; the preview is invalidated, never redirected.
      Re-selecting the same host/instance/session keeps the epoch and the original
      frozen binding sends.  A command already ATTEMPTED keeps its recorded outcome
      and its reply is suppressed.  The next turn uses the new selection.
    * ``Journal`` startup removes only exact generated ``.tmp-<pid>-<uuid4 hex>``
      temps, after the exclusive lock and through the directory fd; all matches are
      validated and count-capped (``MAX_STALE_TEMPS``) before any deletion.  An
      unsafe/over-cap/unremovable match fails the open with ``journal_temp_unsafe``,
      ``journal_temp_overflow`` or ``journal_temp_cleanup_failed`` and is preserved.
      Records, ``.lock``, near-miss and unknown names are never touched.
    * ``confirm_send`` releases the main lock around ``transport.send`` once the
      durable ATTEMPTED record and in-flight claim exist.  ``stop``/``status``/
      ``epoch_current`` stay responsive; a late accept is recorded but never spoken
      after a Stop.  A ``BaseException`` from the transport (cancellation, exit)
      is recorded UNCERTAIN and re-raised; nothing is retried.
    * UNCERTAIN never ages out: after 24 h it shrinks to a minimal unresolved
      tombstone and holds the gate until the exact ``resolve_uncertain`` ack.
    * A turn is fresh only while BOTH the injected wall clock and ``monotonic``
      age are within [0, 60] s; a non-finite/boolean reading is ``invalid_clock``.
    * Duplicate-event memory is rebuilt from retained records on cleanup (bounded
      by ``max_records`` x 128).  Events of discarded unsent turns are also
      remembered in a fixed in-memory FIFO (1024); it resets on process restart, so
      a gateway session must use event ids that are unique per session.
    * ``cleanup() -> int`` returns the number of records forgotten (tombstoning
      is not forgetting).

Future seams (not implemented here)
-----------------------------------
* Authenticated transport: implements ``Transport.send``; resolves the secret from
  ``binding.credential_ref`` itself, binds the connection to the host and
  ``host_instance_id`` it authenticated, never retries a POST, and returns
  ``(status, body)`` or raises.  Any raise or malformed return is recorded UNCERTAIN.
* Read-only page fetch for ``poll_reply``: supplies the window of the bound session
  and passes ``coverage_complete=True`` only for whole history, or passes
  ``newest_tail`` from ``companion.detail_window`` for an exact validated tail.
  An incomplete tail also needs an older different generation before our input.
* Speech gateway: after every process start calls ``resume()`` only on an explicit
  human/operator action; forwards model events to ``on_live_event``, shows the
  ``TranscriptPreview`` to the human, calls ``confirm_send`` only after an explicit
  confirmation of ``preview.text_sha256``, polls the reply, and calls
  ``epoch_current(result.epoch)`` immediately before playing ``result.text``.
* ``expected_revision`` must come from a future validated preflight; this core does
  not claim Unified was idle.

Stop is local only: it never claims a Unified stop and sends nothing.

Gateway modules (gateway_state, gateway_provider, gateway_core, gateway_server) are
imported explicitly; this package never imports them, so the stdlib core imports
without aiohttp.
"""

from .core import (
    Binding,
    Bridge,
    BridgeError,
    CommandResult,
    ReplyResult,
    TranscriptPreview,
    Transport,
)
from .journal import Journal, JournalError
from .reply import ReplyDecision, evaluate_reply
from .wire import (
    LiveEvent,
    Receipt,
    WireError,
    parse_live_event,
    parse_send_receipt,
    send_command_request,
)

__all__ = [
    "Binding",
    "Bridge",
    "BridgeError",
    "CommandResult",
    "Journal",
    "JournalError",
    "LiveEvent",
    "Receipt",
    "ReplyDecision",
    "ReplyResult",
    "TranscriptPreview",
    "Transport",
    "WireError",
    "evaluate_reply",
    "parse_live_event",
    "parse_send_receipt",
    "send_command_request",
]
