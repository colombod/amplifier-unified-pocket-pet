"""Bridge core: selected-session send gate, durable single-flight, exact reply once."""
# pyright: reportArgumentType=false, reportOptionalMemberAccess=false, reportOptionalSubscript=false
# (tests deliberately pass invalid types and read Optionals right after asserting them)

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from unittest import mock

import pet_bridge
from pet_bridge.core import Binding, Bridge, BridgeError, CommandResult, ReplyResult
from pet_bridge.journal import Journal, JournalError
from pet_bridge.reply import ReplyDecision

from .fakes import (
    CRED_1,
    CRED_2,
    CRED_9,
    OTHER_SESSION,
    SESSION,
    FakeClock,
    FakeTransport,
    IdFactory,
    OfflineTestCase,
    accepted_body,
    assistant_row,
    page,
    raised,
    user_row,
)

UTTER = "please summarize secret-marker-777"
ANSWER = "the answer secret-reply-888"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ok():
    return 200, accepted_body()


class CoreCase(OfflineTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.journal_root()
        self.clock = FakeClock()
        self.mono = FakeClock(5_000.0)
        self.ids = IdFactory()
        self.transport = FakeTransport()
        self.journal: Journal
        self.bridge: Bridge
        self.open_bridge()

    # -- helpers -------------------------------------------------------------------
    def open_bridge(
        self, max_records: int = 256, *, resume: bool = True, mono=None
    ) -> Bridge:
        """(Re)open the bridge on the same journal.

        An existing journal always starts DISARMED (stopped); ``resume=True`` (the
        default) performs the explicit resume so ordinary tests can carry on.
        Tests of the disarm itself pass ``resume=False``.
        """
        existing = getattr(self, "journal", None)
        if existing is not None:
            existing.close()
        self.journal = Journal(self.root, max_records=max_records)
        self.addCleanup(self.journal.close)
        extra = {} if mono is None else {"monotonic": mono}
        try:
            self.bridge = Bridge(
                self.journal, self.transport, clock=self.clock, new_id=self.ids, **extra
            )
        except TypeError as exc:
            if extra:
                self.fail(f"Bridge() does not accept the monotonic keyword: {exc}")
            raise
        if resume and self.bridge.status()["stopped"]:
            self.bridge.resume()
        return self.bridge

    def bind(self, session: str = SESSION, **kw) -> Binding:
        args = {
            "host_id": "host-1",
            "host_instance_id": "inst-1",
            "session_id": session,
            "client_id": "pet-1",
            "credential_ref": CRED_1,
        }
        args.update(kw)
        return self.bridge.select_session(**args)

    def sealed(self, text: str = UTTER, event: str = "ev-1"):
        turn = self.bridge.begin_turn()
        self.assertEqual(self.bridge.append_transcript(turn, text, event), "appended")
        return turn, self.bridge.seal_turn(turn)

    def send(
        self, text: str = UTTER, *, response=None, revision=None, event: str = "ev-1"
    ):
        if response is not None:
            self.transport.responses.append(response)
        turn, preview = self.sealed(text, event)
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256, expected_revision=revision
        )
        return turn, result

    def tick(self, seconds: float) -> None:
        """Advance the wall clock and the monotonic clock together."""
        self.clock.advance(seconds)
        self.mono.advance(seconds)

    def record(self, command_id: str):
        return self.journal.get(f"cmd-{command_id}.json")

    def assertCode(self, code: str, call, *args, **kwargs) -> BridgeError:
        with self.assertRaises(BridgeError) as ctx:
            call(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def complete(self, turn: str, text: str = ANSWER):
        return page([user_row(turn), assistant_row("gen-1", text)])

    def poll(self, turn: str, pg=None, *, coverage: bool = True, **kw) -> ReplyResult:
        return self.bridge.poll_reply(
            turn,
            pg if pg is not None else self.complete(turn),
            coverage_complete=coverage,
            **kw,
        )

    def fail_put(
        self,
        predicate,
        code: str = "journal_io_error",
        *,
        after_write: bool = False,
        once=False,
    ):
        real = self.journal.put
        state = {"armed": True}

        def put(name, record):
            if state["armed"] and predicate(name, record):
                if once:
                    state["armed"] = False
                if after_write:
                    real(name, record)
                raise JournalError(code)
            return real(name, record)

        self.journal.put = put

    def restart(self, **kw) -> Bridge:
        return self.open_bridge(**kw)

    def all_journal_bytes(self) -> bytes:
        return b"".join((self.root / n).read_bytes() for n in os.listdir(self.root))


# =================================================================================
class BindingTest(CoreCase):
    def test_no_binding_means_no_send(self):
        self.assertCode("no_binding", self.bridge.begin_turn)
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(self.bridge.status()["bound"])

    def test_select_returns_immutable_binding_with_opaque_credential_ref(self):
        binding = self.bind()
        self.assertEqual(
            (
                binding.host_id,
                binding.host_instance_id,
                binding.session_id,
                binding.client_id,
                binding.credential_ref,
            ),
            ("host-1", "inst-1", SESSION, "pet-1", CRED_1),
        )
        with self.assertRaises(AttributeError):
            binding.session_id = "x"  # type: ignore[misc]
        self.assertTrue(binding.binding_id)

    def test_invalid_binding_values_rejected(self):
        for bad in (
            {"session_id": "a/b"},
            {"session_id": ""},
            {"host_id": "bad id"},
            {"client_id": None},
            {"host_instance_id": "x" * 129},
            {"credential_ref": "t" * 65},
            {"credential_ref": ""},
        ):
            self.assertCode("invalid_binding", self.bind, **bad)
        self.assertFalse(self.bridge.status()["bound"])

    def test_binding_survives_restart_but_the_restart_is_disarmed(self):
        first = self.bind()
        self.restart(resume=False)
        status = self.bridge.status()
        self.assertTrue(status["bound"])
        self.assertEqual(status["binding_id"], first.binding_id)
        self.assertTrue(status["stopped"])
        self.assertCode("stopped", self.bridge.begin_turn)
        self.bridge.resume()
        self.assertTrue(self.bridge.begin_turn())

    def test_switching_selection_refuses_the_obsolete_preview_and_binds_the_next_turn(
        self,
    ):
        self.bind()
        turn, preview = self.sealed()
        self.assertEqual(preview.session_id, SESSION)
        second = self.bind(OTHER_SESSION, credential_ref=CRED_2)
        self.transport.responses.append(ok())
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256
        )
        # The frozen binding is never redirected, and the obsolete preview is not sent.
        self.assertEqual(
            (result.outcome, result.reason, result.persisted),
            ("refused", "epoch_stale", False),
        )
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.journal.names("cmd-"), [])
        # A later (even correctly digested) confirm stays refused for the same reason.
        again = self.bridge.confirm_send(turn, expected_text_sha256=preview.text_sha256)
        self.assertEqual((again.outcome, again.reason), ("refused", "epoch_stale"))
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.journal.names("cmd-"), [])
        self.assertFalse(self.bridge.status()["faulted"])
        # The next turn uses the new binding.
        turn2, preview2 = self.sealed(event="ev-2")
        self.assertEqual(preview2.session_id, OTHER_SESSION)
        self.assertEqual(preview2.binding_id, second.binding_id)
        self.transport.responses.clear()
        self.transport.responses.append((200, accepted_body(OTHER_SESSION)))
        sent = self.bridge.confirm_send(
            turn2, expected_text_sha256=preview2.text_sha256
        )
        self.assertEqual(sent.outcome, "accepted")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0][0], second)
        self.assertIn(OTHER_SESSION, self.transport.calls[0][2])

    def test_reselecting_the_same_session_keeps_the_voice_epoch(self):
        self.bind()
        before = self.bridge.status()["epoch"]
        self.bind(credential_ref=CRED_9)
        self.assertEqual(self.bridge.status()["epoch"], before)
        self.bind(OTHER_SESSION)
        self.assertEqual(self.bridge.status()["epoch"], before + 1)

    def test_clear_revokes_pending_turns_and_binding(self):
        self.bind()
        turn = self.bridge.begin_turn()
        self.assertEqual(self.bridge.append_transcript(turn, UTTER, "ev-1"), "appended")
        self.bridge.clear_session()
        self.assertEqual(
            self.bridge.append_transcript(turn, "more", "ev-2"), "binding_cleared"
        )
        self.assertCode("binding_cleared", self.bridge.seal_turn, turn)
        result = self.bridge.confirm_send(turn, expected_text_sha256=sha(UTTER))
        self.assertEqual(
            (result.outcome, result.reason), ("refused", "binding_cleared")
        )
        self.assertEqual(self.transport.calls, [])
        self.assertCode("no_binding", self.bridge.begin_turn)
        self.restart()
        self.assertFalse(self.bridge.status()["bound"])

    def test_clear_takes_effect_even_if_persistence_fails(self):
        self.bind()
        self.fail_put(lambda name, rec: name == "binding.json")
        self.bridge.clear_session()
        self.assertFalse(self.bridge.status()["bound"])
        self.assertTrue(self.bridge.status()["faulted"])

    def test_select_failure_changes_nothing(self):
        self.bind()
        self.fail_put(lambda name, rec: name == "binding.json")
        self.assertCode("journal_error", self.bind, OTHER_SESSION)
        self.assertEqual(self.bridge.status()["session_id"], SESSION)


# =================================================================================
class TurnCaptureTest(CoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.bind()

    def test_only_one_pending_turn(self):
        self.bridge.begin_turn()
        self.assertCode("turn_pending", self.bridge.begin_turn)

    def test_seal_preview_exposes_exact_text_and_digest_but_hides_repr(self):
        turn = self.bridge.begin_turn()
        self.bridge.append_transcript(turn, "pleas", "e1")
        self.bridge.append_transcript(turn, "e go", "e2")
        preview = self.bridge.seal_turn(turn)
        self.assertEqual(
            (preview.text, preview.chars, preview.text_sha256),
            ("please go", 9, sha("please go")),
        )
        self.assertNotIn("please go", repr(preview) + str(preview))
        self.assertEqual(self.bridge.seal_turn(turn), preview)  # idempotent
        self.assertEqual(self.bridge.append_transcript(turn, "x", "e3"), "sealed")

    def test_blank_or_invalid_text_cannot_be_sealed_or_sent(self):
        turn = self.bridge.begin_turn()
        self.assertCode("blank_text", self.bridge.seal_turn, turn)
        self.bridge.append_transcript(turn, "   ", "e1")
        self.assertCode("blank_text", self.bridge.seal_turn, turn)
        result = self.bridge.confirm_send(turn, expected_text_sha256=sha("   "))
        self.assertEqual((result.outcome, result.reason), ("refused", "not_sealed"))
        self.assertEqual(self.transport.calls, [])

    def test_append_rejects_bad_input_without_partial_effect(self):
        turn = self.bridge.begin_turn()
        self.assertEqual(self.bridge.append_transcript(turn, 5, "e1"), "invalid_text")  # type: ignore[arg-type]
        self.assertEqual(
            self.bridge.append_transcript(turn, "a\x00b", "e1"), "invalid_text"
        )
        self.assertEqual(
            self.bridge.append_transcript(turn, "ok", "bad id"), "invalid_event"
        )
        self.assertEqual(self.bridge.append_transcript(turn, "ok", 7), "invalid_event")  # type: ignore[arg-type]
        self.assertEqual(
            self.bridge.append_transcript("nope", "ok", "e1"), "unknown_turn"
        )
        self.assertEqual(
            self.bridge.append_transcript(turn, "a" * 4000, "e1"), "appended"
        )
        self.assertEqual(self.bridge.append_transcript(turn, "b", "e2"), "too_long")
        self.assertEqual(self.bridge.seal_turn(turn).chars, 4000)

    def test_event_count_is_bounded(self):
        turn = self.bridge.begin_turn()
        for i in range(128):
            self.assertEqual(
                self.bridge.append_transcript(turn, "a", f"e{i}"), "appended"
            )
        self.assertEqual(
            self.bridge.append_transcript(turn, "a", "e-last"), "too_many_events"
        )

    def test_duplicate_event_ids_are_dropped(self):
        turn = self.bridge.begin_turn()
        self.assertEqual(self.bridge.append_transcript(turn, "one", "e1"), "appended")
        self.assertEqual(
            self.bridge.append_transcript(turn, "one", "e1"), "duplicate_event"
        )
        self.assertEqual(self.bridge.seal_turn(turn).text, "one")

    def test_utterance_age_is_limited_to_60_seconds(self):
        turn = self.bridge.begin_turn()
        self.bridge.append_transcript(turn, UTTER, "e1")
        preview = self.bridge.seal_turn(turn)
        self.tick(60)
        self.assertEqual(
            self.bridge.append_transcript(turn, "x", "e2"), "sealed"
        )  # still alive at 60s
        self.tick(1)
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256
        )
        self.assertEqual((result.outcome, result.reason), ("refused", "expired"))
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(
            self.bridge.begin_turn() and "new turn allowed", "new turn allowed"
        )

    def test_expired_turn_cannot_be_appended_or_sealed(self):
        turn = self.bridge.begin_turn()
        self.tick(61)
        self.assertEqual(self.bridge.append_transcript(turn, "x", "e1"), "expired")
        self.assertCode("expired", self.bridge.seal_turn, turn)


# =================================================================================
class ConfirmGateTest(CoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.bind()

    def test_confirm_requires_a_sealed_turn(self):
        turn = self.bridge.begin_turn()
        self.bridge.append_transcript(turn, UTTER, "e1")
        result = self.bridge.confirm_send(turn, expected_text_sha256=sha(UTTER))
        self.assertEqual((result.outcome, result.reason), ("refused", "not_sealed"))
        self.assertEqual(self.transport.calls, [])

    def test_confirm_requires_the_exact_sealed_digest(self):
        turn, preview = self.sealed()
        for bad in (
            sha("other"),
            preview.text_sha256.upper(),
            "",
            None,
            5,
            preview.text_sha256[:-1],
        ):
            result = self.bridge.confirm_send(turn, expected_text_sha256=bad)  # type: ignore[arg-type]
            self.assertEqual(
                (result.outcome, result.reason), ("refused", "digest_mismatch"), bad
            )
        self.assertEqual(self.transport.calls, [])
        self.transport.responses.append(ok())
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256
        )
        self.assertEqual(result.outcome, "accepted")
        self.assertEqual(len(self.transport.calls), 1)

    def test_unknown_turn_is_refused(self):
        result = self.bridge.confirm_send(
            "00000000-0000-4000-8000-0000000000aa", expected_text_sha256=sha("x")
        )
        self.assertEqual((result.outcome, result.reason), ("refused", "unknown_turn"))
        self.assertEqual(self.transport.calls, [])

    def test_confirm_twice_sends_once(self):
        turn, _result = self.send(response=ok())
        again = self.bridge.confirm_send(turn, expected_text_sha256=sha(UTTER))
        self.assertEqual(
            (again.outcome, again.reason), ("refused", "duplicate_confirm")
        )
        self.assertEqual(len(self.transport.calls), 1)

    def test_duplicate_confirm_across_restart_sends_at_most_once(self):
        turn, _ = self.send(response=ok())
        self.restart()
        again = self.bridge.confirm_send(turn, expected_text_sha256=sha(UTTER))
        self.assertEqual(
            (again.outcome, again.reason), ("refused", "duplicate_confirm")
        )
        self.assertEqual(len(self.transport.calls), 1)

    def test_duplicate_events_in_the_same_process_do_not_become_a_second_send(self):
        self.send(response=(409, b"{}"), event="ev-dup")
        turn = self.bridge.begin_turn()
        self.assertEqual(
            self.bridge.append_transcript(turn, UTTER, "ev-dup"), "duplicate_event"
        )

    def test_duplicate_events_after_restart_do_not_become_a_second_send(self):
        self.send(response=(409, b"{}"), event="ev-dup")
        self.restart()
        turn = self.bridge.begin_turn()
        self.assertEqual(
            self.bridge.append_transcript(turn, UTTER, "ev-dup"), "duplicate_event"
        )

    def test_expected_revision_is_a_top_level_request_field(self):
        self.send(response=ok(), revision=12)
        body = json.loads(self.transport.calls[0][3])
        self.assertEqual(body["expectedRevision"], 12)
        self.assertNotIn("expectedRevision", body["args"])
        self.assertEqual(self.transport.calls[0][1], "POST")
        self.assertEqual(
            self.transport.calls[0][2], f"/api/sessions/{SESSION}/commands"
        )

    def test_invalid_revision_refused_and_turn_kept(self):
        turn, preview = self.sealed()
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256, expected_revision=-1
        )
        self.assertEqual(
            (result.outcome, result.reason), ("refused", "invalid_revision")
        )
        self.assertEqual(self.transport.calls, [])
        self.transport.responses.append(ok())
        self.assertEqual(
            self.bridge.confirm_send(
                turn, expected_text_sha256=preview.text_sha256
            ).outcome,
            "accepted",
        )

    def test_command_id_is_the_turn_id_and_sent_in_the_body(self):
        turn, result = self.send(response=ok())
        self.assertEqual(result.command_id, turn)
        self.assertEqual(json.loads(self.transport.calls[0][3])["id"], turn)


class ModelEventsNeverDispatchTest(CoreCase):
    def test_live_events_preview_only_and_never_send(self):
        self.bind()
        turn = self.bridge.begin_turn()
        events = [
            {
                "type": "session.input_transcript.delta",
                "event_id": "e1",
                "delta": "please ",
                "start_ms": 0,
                "end_ms": 10,
            },
            {
                "type": "session.input_transcript.delta",
                "event_id": "e2",
                "delta": "send it",
                "start_ms": 10,
                "end_ms": 20,
            },
            {
                "type": "session.delegation.created",
                "offset_ms": 30,
                "delegation": {"id": "d1", "type": "delegation", "target": "client"},
            },
            {
                "type": "session.commentary.append",
                "event_id": "c1",
                "delegation_id": "d1",
                "content": "I am sending that now.",
            },
        ]
        outcomes = [self.bridge.on_live_event(turn, e) for e in events]
        self.assertEqual(
            outcomes,
            [
                "appended",
                "appended",
                "delegation_hint_ignored",
                "commentary_not_forwarded",
            ],
        )
        self.assertEqual(
            self.bridge.on_live_event(turn, {"type": "response.done"}), "ignored"
        )
        self.assertEqual(
            self.bridge.on_live_event(turn, {"type": "session.input_transcript.delta"}),
            "invalid_event",
        )
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.journal.names("cmd-"), [])
        self.assertEqual(self.bridge.seal_turn(turn).text, "please send it")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.journal.names("cmd-"), [])


# =================================================================================
class PersistenceBeforeSendTest(CoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.bind()

    def test_full_private_prepared_then_attempted_before_transport_then_scrubbed_outcome(
        self,
    ):
        log: list[tuple] = []
        real_put = self.journal.put

        def spy(name, record):
            if name != "binding.json":
                log.append(("put", record["state"], record.get("text")))
            return real_put(name, record)

        self.journal.put = spy
        turn, preview = self.sealed()

        def during_send():
            log.append(("send",))
            on_disk = self.record(turn)
            self.assertEqual((on_disk["state"], on_disk["text"]), ("ATTEMPTED", UTTER))

        self.transport.on_send = during_send
        self.transport.responses.append(ok())
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256
        )
        self.assertEqual(result.outcome, "accepted")
        self.assertEqual(
            log,
            [
                ("put", "PREPARED", UTTER),
                ("put", "ATTEMPTED", UTTER),
                ("send",),
                ("put", "ACCEPTED", None),
            ],
        )
        self.assertNotIn(UTTER.encode(), self.all_journal_bytes())
        self.assertEqual(
            oct(os.stat(self.root / f"cmd-{turn}.json").st_mode & 0o777), "0o600"
        )

    def test_prepared_write_failure_means_no_send_and_a_clean_retry(self):
        self.fail_put(lambda n, r: r.get("state") == "PREPARED", once=True)
        turn, preview = self.sealed()
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256
        )
        self.assertEqual((result.outcome, result.reason), ("refused", "journal_error"))
        self.assertEqual(self.transport.calls, [])
        self.assertIsNone(self.record(turn))
        self.assertFalse(self.bridge.status()["faulted"])
        self.transport.responses.append(ok())
        self.assertEqual(
            self.bridge.confirm_send(
                turn, expected_text_sha256=preview.text_sha256
            ).outcome,
            "accepted",
        )

    def test_attempted_write_failure_means_no_send_and_abandoned(self):
        self.fail_put(lambda n, r: r.get("state") == "ATTEMPTED")
        turn, preview = self.sealed()
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256
        )
        self.assertEqual((result.outcome, result.reason), ("refused", "journal_error"))
        self.assertEqual(self.transport.calls, [])
        rec = self.record(turn)
        self.assertEqual(rec["state"], "ABANDONED")
        self.assertNotIn("text", rec)
        self.assertNotIn(UTTER.encode(), self.all_journal_bytes())
        self.assertTrue(self.bridge.begin_turn())  # the gate is not wedged

    def test_failure_after_rename_is_unknown_state_and_faults_the_bridge(self):
        self.fail_put(
            lambda n, r: r.get("state") == "PREPARED",
            "journal_persist_unknown",
            after_write=True,
        )
        turn, preview = self.sealed()
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256
        )
        self.assertEqual((result.outcome, result.reason), ("refused", "journal_error"))
        self.assertEqual(self.transport.calls, [])
        self.assertTrue(self.bridge.status()["faulted"])
        self.assertCode("bridge_faulted", self.bridge.begin_turn)

    def test_outcome_write_failure_faults_and_recovery_makes_attempted_uncertain(self):
        self.fail_put(lambda n, r: r.get("state") == "ACCEPTED")
        turn, result = self.send(response=ok())
        self.assertEqual((result.outcome, result.persisted), ("accepted", False))
        self.assertTrue(self.bridge.status()["faulted"])
        self.assertCode("bridge_faulted", self.bridge.begin_turn)
        self.assertEqual(self.record(turn)["state"], "ATTEMPTED")
        self.restart()
        self.assertEqual(self.record(turn)["state"], "UNCERTAIN")
        self.assertNotIn("text", self.record(turn))
        self.assertEqual(len(self.transport.calls), 1)

    def test_faulted_bridge_refuses_confirm_and_poll(self):
        turn, preview = self.sealed()
        self.fail_put(
            lambda n, r: r.get("state") == "PREPARED",
            "journal_persist_unknown",
            after_write=True,
        )
        self.bridge.confirm_send(turn, expected_text_sha256=preview.text_sha256)
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256
        )
        self.assertEqual(result.reason, "bridge_faulted")
        self.assertEqual(self.poll(turn).reason, "bridge_faulted")


class OutcomeHandlingTest(CoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.bind()

    def test_transport_exception_becomes_uncertain_and_is_never_retried(self):
        turn, result = self.send(response=ConnectionError("SECRET-" + UTTER))
        self.assertEqual(
            (result.outcome, result.reason), ("uncertain", "transport_exception")
        )
        self.assertNotIn("secret-marker-777", repr(result) + str(result.metadata))
        again = self.bridge.confirm_send(turn, expected_text_sha256=sha(UTTER))
        self.assertEqual(again.reason, "duplicate_confirm")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.record(turn)["state"], "UNCERTAIN")
        self.assertNotIn(b"secret-marker-777", self.all_journal_bytes())

    def test_an_uncertain_send_seen_in_the_conversation_becomes_accepted(self):
        turn, _ = self.send(response=ConnectionError("x"))
        self.assertCode("uncertain_unresolved", self.bridge.begin_turn)
        calls = len(self.transport.calls)
        shown = page([user_row(turn, text=UTTER, phase="working")])
        self.assertEqual(
            self.bridge.reconcile_delivered(shown, session_id=SESSION), (turn,)
        )
        record = self.record(turn)
        self.assertEqual(
            (record["state"], record["reason"]), ("ACCEPTED", "observed_in_conversation")
        )
        self.assertEqual(len(self.transport.calls), calls)  # observed, never sent
        self.assertCode("reply_pending", self.bridge.begin_turn)  # the normal gate now
        self.assertEqual(self.bridge.reconcile_delivered(shown, session_id=SESSION), ())

    def test_an_uncertain_send_is_never_reconciled_without_exact_proof(self):
        turn, _ = self.send(response=ConnectionError("x"))
        other = "12345678-1234-4234-8234-123456789abc"
        not_proof = {
            "absent": page([]),
            "other id": page([user_row(other, text=UTTER)]),
            "other text": page([user_row(turn, text=UTTER + "!")]),
            "cut text": page([user_row(turn, text=UTTER, textDetail={"length": 9000})]),
            "assistant row": page([{**user_row(turn, text=UTTER), "role": "assistant"}]),
            "twice": page([user_row(turn, text=UTTER), user_row(turn, text=UTTER)]),
            "ambiguous": page([user_row(turn, text=UTTER, nativeInputAmbiguous=True)]),
            "native differs": page([user_row(turn, text=UTTER, nativeInputId=other)]),
            "not a page": {"items": "x"},
        }
        for name, shown in not_proof.items():
            with self.subTest(case=name):
                self.assertEqual(
                    self.bridge.reconcile_delivered(shown, session_id=SESSION), ()
                )
        proof = page([user_row(turn, text=UTTER)])
        self.assertEqual(self.bridge.reconcile_delivered(proof, session_id="other"), ())
        self.assertEqual(self.record(turn)["state"], "UNCERTAIN")
        self.assertCode("uncertain_unresolved", self.bridge.begin_turn)

    def test_a_tombstoned_uncertain_send_is_left_to_the_operator(self):
        turn, _ = self.send(response=ConnectionError("x"))
        self.clock.advance(24 * 3600)
        self.bridge.cleanup()
        self.assertIs(self.record(turn).get("tombstone"), True)
        proof = page([user_row(turn, text=UTTER)])
        self.assertEqual(self.bridge.reconcile_delivered(proof, session_id=SESSION), ())
        self.assertEqual(self.record(turn)["state"], "UNCERTAIN")

    def test_bad_transport_return_is_uncertain(self):
        for bad in ("200", None, (200,), (200, "text"), ("200", b"{}")):
            self.transport = FakeTransport(bad)
            self.restart()
            self.bridge.select_session(
                host_id="host-1",
                host_instance_id="inst-1",
                session_id=SESSION,
                client_id="pet-1",
                credential_ref=CRED_1,
            )
            turn, preview = self.sealed(event=f"e{self.ids.n}")
            result = self.bridge.confirm_send(
                turn, expected_text_sha256=preview.text_sha256
            )
            self.assertEqual(
                (result.outcome, result.reason),
                ("uncertain", "transport_bad_return"),
                bad,
            )
            self.bridge.resolve_uncertain(turn, operator_ack=f"ack-uncertain:{turn}")

    def test_uncertain_receipts_hold_the_gate_with_no_retry(self):
        for response in (
            (504, json.dumps({"code": "runtime_pending"}).encode()),
            (200, accepted_body(delivery="sending")),
            (200, b"{}"),
            (500, b""),
        ):
            self.transport = FakeTransport()
            self.restart()
            self.bridge.select_session(
                host_id="host-1",
                host_instance_id="inst-1",
                session_id=SESSION,
                client_id="pet-1",
                credential_ref=CRED_1,
            )
            turn, result = self.send(response=response, event=f"u{self.ids.n}")
            self.assertEqual(result.outcome, "uncertain", response)
            self.assertCode("uncertain_unresolved", self.bridge.begin_turn)
            self.assertEqual(len(self.transport.calls), 1)
            self.bridge.resolve_uncertain(turn, operator_ack=f"ack-uncertain:{turn}")

    def test_http_200_with_failure_flags_is_not_success_and_speaks_nothing(self):
        turn, result = self.send(
            response=(200, accepted_body(accepted=False, delivery="held"))
        )
        self.assertEqual(result.outcome, "uncertain")
        self.assertEqual(self.poll(turn).status, "not_spoken")

    def test_409_cas_rejection_scrubs_and_never_speaks(self):
        turn, result = self.send(
            response=(409, b'{"accepted":false,"error":"stale"}'), revision=5
        )
        self.assertEqual((result.outcome, result.reason), ("rejected", "http_409"))
        self.assertEqual(json.loads(self.transport.calls[0][3])["expectedRevision"], 5)
        rec = self.record(turn)
        self.assertEqual(rec["state"], "REJECTED")
        self.assertNotIn("text", rec)
        self.assertNotIn(b"secret-marker-777", self.all_journal_bytes())
        reply = self.poll(turn)
        self.assertEqual((reply.status, reply.text), ("not_spoken", None))
        self.assertTrue(self.bridge.begin_turn())  # rejected releases the gate

    def test_foreign_receipt_is_uncertain(self):
        _turn, result = self.send(response=(200, accepted_body(OTHER_SESSION)))
        self.assertEqual(
            (result.outcome, result.reason), ("uncertain", "foreign_session")
        )

    def test_success_without_host_instance_id_is_accepted_with_reply_waiting(self):
        turn, result = self.send(response=(200, accepted_body()))
        self.assertEqual((result.outcome, result.persisted), ("accepted", True))
        self.assertNotIn("host_instance_id", result.metadata)
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")
        self.assertCode("reply_pending", self.bridge.begin_turn)

    def test_result_metadata_is_scalar_and_text_free(self):
        _turn, result = self.send(
            response=(200, accepted_body(hostInstanceId="host-9", stateRevision=3))
        )
        self.assertEqual(result.metadata["host_instance_id"], "host-9")
        self.assertNotIn("secret-marker-777", repr(result))


class RecoveryTest(CoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.bind()

    def plant(self, turn: str, state: str, **extra):
        binding = self.bridge.status()
        record = {
            "state": state,
            "binding": {
                "binding_id": binding["binding_id"],
                "host_id": "host-1",
                "host_instance_id": "inst-1",
                "session_id": SESSION,
                "client_id": "pet-1",
                "credential_ref": CRED_1,
            },
            "epoch": binding["epoch"],
            "created_at": self.clock.now,
            "updated_at": self.clock.now,
            "event_ids": ["ev-x"],
            "text": UTTER,
            "text_sha256": sha(UTTER),
            "chars": len(UTTER),
            "expected_revision": None,
        }
        record.update(extra)
        self.journal.put(f"cmd-{turn}.json", record)

    def test_prepared_becomes_abandoned_and_attempted_becomes_uncertain_without_transport(
        self,
    ):
        t1, t2 = (
            "00000000-0000-4000-8000-0000000000b1",
            "00000000-0000-4000-8000-0000000000b2",
        )
        self.plant(t1, "PREPARED")
        self.plant(t2, "ATTEMPTED", attempted_at=self.clock.now)
        self.restart()
        self.assertEqual(self.record(t1)["state"], "ABANDONED")
        self.assertEqual(self.record(t2)["state"], "UNCERTAIN")
        self.assertNotIn("text", self.record(t1))
        self.assertNotIn("text", self.record(t2))
        self.assertNotIn(b"secret-marker-777", self.all_journal_bytes())
        self.assertEqual(self.transport.calls, [])

    def test_recovered_commands_are_never_dispatched_again(self):
        t1 = "00000000-0000-4000-8000-0000000000b1"
        self.plant(t1, "PREPARED")
        self.restart()
        result = self.bridge.confirm_send(t1, expected_text_sha256=sha(UTTER))
        self.assertEqual(
            (result.outcome, result.reason), ("refused", "duplicate_confirm")
        )
        self.assertEqual(self.transport.calls, [])
        self.assertTrue(self.bridge.begin_turn())  # abandoned does not hold the gate

    def test_uncertain_holds_gate_until_explicit_exact_ack_and_never_sends(self):
        t2 = "00000000-0000-4000-8000-0000000000b2"
        self.plant(t2, "ATTEMPTED")
        self.restart()
        self.assertCode("uncertain_unresolved", self.bridge.begin_turn)
        self.assertCode(
            "ack_mismatch", self.bridge.resolve_uncertain, t2, operator_ack="yes"
        )
        self.assertCode(
            "ack_mismatch", self.bridge.resolve_uncertain, t2, operator_ack=True
        )  # type: ignore[arg-type]
        self.assertCode(
            "not_uncertain",
            self.bridge.resolve_uncertain,
            "00000000-0000-4000-8000-0000000000ff",
            operator_ack="ack-uncertain:00000000-0000-4000-8000-0000000000ff",
        )
        self.bridge.resolve_uncertain(t2, operator_ack=f"ack-uncertain:{t2}")
        self.assertEqual(self.record(t2)["state"], "RESOLVED")
        self.assertTrue(self.bridge.begin_turn())
        self.assertEqual(self.transport.calls, [])

    def test_corrupt_or_foreign_records_fail_closed_at_startup(self):
        t = "00000000-0000-4000-8000-0000000000c1"
        self.journal.put(f"cmd-{t}.json", {"state": "BOGUS"})
        self.assertRaises(BridgeError, self.restart)
        try:
            self.restart()
        except BridgeError as err:
            self.assertEqual(err.code, "journal_corrupt")

    def test_corrupt_binding_fails_closed(self):
        self.journal.put("binding.json", {"epoch": "x"})
        with self.assertRaises(BridgeError) as ctx:
            self.restart()
        self.assertEqual(ctx.exception.code, "journal_corrupt")


class RetentionTest(CoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.bind()

    def test_terminal_records_are_forgotten_after_24_hours_not_before(self):
        turn, _ = self.send(response=(409, b"{}"))
        self.clock.advance(24 * 3600 - 1)
        self.assertEqual(self.bridge.cleanup(), 0)
        self.assertIsNotNone(self.record(turn))
        self.clock.advance(1)
        self.assertEqual(self.bridge.cleanup(), 1)
        self.assertIsNone(self.record(turn))

    def test_accepted_waiting_for_reply_is_kept_until_complete_or_expiry(self):
        turn, _ = self.send(response=ok())
        self.clock.advance(24 * 3600 - 1)
        self.assertEqual(self.bridge.cleanup(), 0)
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")
        self.assertCode("reply_pending", self.bridge.begin_turn)
        self.clock.advance(1)
        self.assertEqual(self.bridge.cleanup(), 1)
        self.assertTrue(self.bridge.begin_turn())

    def test_uncertain_is_never_age_deleted_and_never_releases_the_gate(self):
        # (Replaces the earlier test that wrongly expected UNCERTAIN to expire.)
        turn, _ = self.send(response=ConnectionError("x"))
        self.clock.advance(24 * 3600 - 1)
        self.assertCode("uncertain_unresolved", self.bridge.begin_turn)
        self.clock.advance(1)
        self.assertEqual(self.bridge.cleanup(), 0)
        self.assertCode("uncertain_unresolved", self.bridge.begin_turn)
        self.assertEqual(self.record(turn)["state"], "UNCERTAIN")
        self.clock.advance(10 * 365 * 24 * 3600)  # a large forward clock jump
        self.assertEqual(self.bridge.cleanup(), 0)
        self.assertCode("uncertain_unresolved", self.bridge.begin_turn)
        self.assertEqual(self.record(turn)["state"], "UNCERTAIN")

    def test_after_24_hours_uncertain_shrinks_to_a_minimal_unresolved_tombstone(self):
        turn, _ = self.send(
            response=(200, accepted_body(delivery="sending", stateRevision=3)),
            revision=9,
        )
        full = self.record(turn)
        self.assertEqual(full["state"], "UNCERTAIN")
        for sensitive in ("text_sha256", "chars", "expected_revision", "http"):
            self.assertIn(sensitive, full)
        self.clock.advance(24 * 3600 - 1)
        self.bridge.cleanup()
        self.assertEqual(self.record(turn), full)  # untouched before 24h
        self.clock.advance(1)
        self.bridge.cleanup()
        tomb = self.record(turn)
        self.assertIsNotNone(tomb, "the UNCERTAIN record was age-deleted")
        self.assertEqual(tomb["state"], "UNCERTAIN")
        self.assertIs(tomb.get("tombstone"), True)
        self.assertEqual(tomb["command_id"], turn)
        self.assertEqual(tomb["epoch"], full["epoch"])
        self.assertEqual(tomb["created_at"], full["created_at"])
        self.assertEqual(tomb["updated_at"], full["updated_at"])
        self.assertEqual(tomb["binding"], full["binding"])
        self.assertEqual(tomb["event_ids"], [])
        for gone in (
            "text",
            "text_sha256",
            "chars",
            "expected_revision",
            "http",
            "attempted_at",
        ):
            self.assertNotIn(gone, tomb)
        self.assertCode("uncertain_unresolved", self.bridge.begin_turn)
        before = self.record(turn)
        writes: list[str] = []
        real_put = self.journal.put
        self.journal.put = lambda name, record: (
            writes.append(name),
            real_put(name, record),
        )[1]
        self.clock.advance(1)
        self.bridge.cleanup()  # idempotent: no rewrite, tombstoned_at not refreshed
        self.assertEqual(writes, [])
        self.assertEqual(self.record(turn), before)

    def test_tombstone_survives_restart_after_25_hours_and_only_an_exact_ack_releases(
        self,
    ):
        turn, _ = self.send(response=ConnectionError("x"))
        self.clock.advance(25 * 3600)
        self.restart()
        self.assertIsNotNone(self.record(turn), "the UNCERTAIN record was age-deleted")
        self.assertIs(self.record(turn).get("tombstone"), True)
        self.assertCode("uncertain_unresolved", self.bridge.begin_turn)
        self.assertCode(
            "ack_mismatch", self.bridge.resolve_uncertain, turn, operator_ack="yes"
        )
        self.assertCode("uncertain_unresolved", self.bridge.begin_turn)
        self.bridge.resolve_uncertain(turn, operator_ack=f"ack-uncertain:{turn}")
        self.assertEqual(self.record(turn)["state"], "RESOLVED")
        self.assertTrue(self.bridge.begin_turn())
        self.assertEqual(len(self.transport.calls), 1)  # nothing was ever re-sent

    def test_a_fault_while_scrubbing_fails_closed_and_keeps_the_gate(self):
        turn, _ = self.send(response=ConnectionError("x"))
        full = self.record(turn)
        self.clock.advance(25 * 3600)
        self.fail_put(lambda n, r: r.get("tombstone") is True)
        self.assertCode("journal_error", self.bridge.begin_turn)
        self.assertCode("journal_error", self.bridge.cleanup)
        self.assertEqual(self.record(turn), full)  # neither shrunk nor deleted
        self.restart()  # a healthy restart finishes the scrub; still gated
        self.assertIs(self.record(turn).get("tombstone"), True)
        self.assertCode("uncertain_unresolved", self.bridge.begin_turn)

    def test_unknown_persistence_during_the_scrub_faults_the_bridge(self):
        self.send(response=ConnectionError("x"))
        self.clock.advance(25 * 3600)
        self.fail_put(
            lambda n, r: r.get("tombstone") is True,
            "journal_persist_unknown",
            after_write=True,
        )
        self.assertCode("journal_error", self.bridge.begin_turn)
        self.assertTrue(self.bridge.status()["faulted"])
        self.assertCode("bridge_faulted", self.bridge.begin_turn)

    def test_the_default_journal_is_bounded_to_256_records(self):
        self.assertEqual(self.journal.max_records, 256)

    def test_a_resolved_uncertain_record_then_ages_out_like_any_terminal(self):
        turn, _ = self.send(response=ConnectionError("x"))
        self.bridge.resolve_uncertain(turn, operator_ack=f"ack-uncertain:{turn}")
        self.clock.advance(24 * 3600)
        self.assertEqual(self.bridge.cleanup(), 1)
        self.assertIsNone(self.record(turn))

    def test_full_journal_evicts_oldest_terminal_but_refuses_when_nothing_can_go(self):
        self.open_bridge(max_records=3)  # binding + 2 commands
        first, _ = self.send(response=(409, b"{}"), event="a1")
        second, _ = self.send(response=(409, b"{}"), event="a2")
        self.clock.advance(5)
        self.bridge.begin_turn()
        self.assertIsNone(self.record(first))
        self.assertIsNotNone(self.record(second))
        self.open_bridge(max_records=1)
        self.assertCode("journal_full", self.bridge.begin_turn)

    def test_journal_failures_during_retention_surface_as_bridge_errors(self):
        self.send(response=(409, b"{}"))
        self.clock.advance(24 * 3600)

        def boom(name):
            raise JournalError("journal_io_error")

        self.journal.delete = boom
        self.assertCode("journal_error", self.bridge.begin_turn)
        self.assertCode("journal_error", self.bridge.cleanup)

    def test_eviction_failure_refuses_the_send_instead_of_raising(self):
        self.open_bridge(max_records=3)
        self.send(response=(409, b"{}"), event="a1")
        self.send(response=(409, b"{}"), event="a2")
        turn = self.bridge.begin_turn()  # evicts one terminal record to make room
        self.bridge.append_transcript(turn, UTTER, "a3")
        preview = self.bridge.seal_turn(turn)

        def boom(name):
            raise JournalError("journal_io_error")

        self.journal.delete = boom
        self.journal.put(
            "cmd-00000000-0000-4000-8000-0000000000f1.json",
            self._filler(),
        )
        sent_before = len(self.transport.calls)
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256
        )
        self.assertEqual((result.outcome, result.reason), ("refused", "journal_error"))
        self.assertEqual(len(self.transport.calls), sent_before)

    def _filler(self):
        binding = self.bridge.status()
        return {
            "state": "REJECTED",
            "binding": {
                "binding_id": binding["binding_id"],
                "host_id": "host-1",
                "host_instance_id": "inst-1",
                "session_id": SESSION,
                "client_id": "pet-1",
                "credential_ref": CRED_1,
            },
            "epoch": binding["epoch"],
            "created_at": self.clock.now,
            "updated_at": self.clock.now,
            "event_ids": [],
        }

    def test_nothing_unresolved_is_ever_evicted(self):
        self.open_bridge(max_records=2)  # binding + 1 command
        turn, _ = self.send(response=ok())
        self.assertCode("reply_pending", self.bridge.begin_turn)
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")


# =================================================================================
class StopAndEpochTest(CoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.bind()

    def test_stop_is_local_only_and_abandons_unsent_capture(self):
        turn, preview = self.sealed()
        self.bridge.stop()
        self.assertEqual(self.transport.calls, [])
        self.assertTrue(self.bridge.status()["stopped"])
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256
        )
        self.assertEqual((result.outcome, result.reason), ("refused", "stopped"))
        self.assertEqual(self.bridge.append_transcript(turn, "x", "e9"), "stopped")
        self.assertCode("stopped", self.bridge.begin_turn)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.journal.names("cmd-"), [])

    def test_stop_persists_and_resume_reopens(self):
        self.bridge.stop()
        self.restart(resume=False)
        self.assertTrue(self.bridge.status()["stopped"])
        self.bridge.resume()
        self.assertFalse(self.bridge.status()["stopped"])
        self.assertTrue(self.bridge.begin_turn())

    def test_stop_takes_effect_even_if_persistence_fails(self):
        self.fail_put(lambda n, r: n == "binding.json")
        self.bridge.stop()
        self.assertTrue(self.bridge.status()["stopped"])
        self.assertTrue(self.bridge.status()["faulted"])
        self.assertCode("bridge_faulted", self.bridge.resume)

    def test_stop_while_reply_waits_suppresses_it_even_after_resume(self):
        turn, _ = self.send(response=ok())
        self.bridge.stop()
        self.assertEqual(self.poll(turn).reason, "stopped")
        self.bridge.resume()
        old = self.poll(turn)
        self.assertEqual(
            (old.status, old.reason, old.text), ("not_spoken", "epoch_stale", None)
        )
        self.assertEqual(self.record(turn)["state"], "SUPPRESSED")
        self.assertTrue(self.bridge.begin_turn())  # the gate was released locally

    def test_stop_during_transport_callback_records_outcome_but_suppresses_reply(self):
        self.transport.on_send = self.bridge.stop
        turn, result = self.send(response=ok())
        self.assertEqual((result.outcome, result.persisted), ("accepted", True))
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")
        self.assertEqual(self.poll(turn).reason, "stopped")
        self.bridge.resume()
        reply = self.poll(turn)
        self.assertEqual(
            (reply.status, reply.reason, reply.text),
            ("not_spoken", "epoch_stale", None),
        )
        self.assertEqual(len(self.transport.calls), 1)

    def test_reentrant_calls_during_flight_see_the_gate(self):
        seen = []
        self.transport.on_send = lambda: seen.append(self._try_begin())
        self.send(response=ok())
        self.assertEqual(seen, ["command_inflight"])

    def _try_begin(self) -> str:
        try:
            self.bridge.begin_turn()
        except BridgeError as err:
            return err.code
        return "allowed"

    def test_clear_and_select_elsewhere_do_not_release_old_reply(self):
        turn, _ = self.send(response=ok())
        self.bridge.clear_session()
        self.bind()
        self.assertEqual(self.poll(turn).reason, "epoch_stale")

    def test_epoch_current_lets_a_speaker_recheck_before_playing(self):
        turn, _ = self.send(response=ok())
        reply = self.poll(turn)
        self.assertEqual(reply.status, "speak")
        self.assertTrue(self.bridge.epoch_current(reply.epoch))
        self.bridge.stop()
        self.assertFalse(self.bridge.epoch_current(reply.epoch))
        self.assertFalse(self.bridge.epoch_current("x"))  # type: ignore[arg-type]


# =================================================================================
class ReplyOnceTest(CoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.bind()
        self.turn, _ = self.send(response=ok())

    def test_completed_exact_reply_is_spoken_exactly_once(self):
        first = self.poll(self.turn)
        self.assertEqual(
            (first.status, first.reason, first.text, first.terminal),
            ("speak", "reply_complete", ANSWER, True),
        )
        second = self.poll(self.turn)
        self.assertEqual(
            (second.status, second.reason, second.text),
            ("not_spoken", "already_consumed", None),
        )
        self.assertTrue(self.bridge.begin_turn())

    def test_consumption_is_persisted_before_text_is_returned_and_survives_restart(
        self,
    ):
        seen = []
        real = self.journal.put

        def spy(name, record):
            if record.get("state") == "COMPLETE":
                seen.append(("claim", record.get("reply_consumed")))
            return real(name, record)

        self.journal.put = spy
        reply = self.poll(self.turn)
        self.assertEqual((seen, reply.status), ([("claim", True)], "speak"))
        self.restart()
        again = self.poll(self.turn)
        self.assertEqual(
            (again.status, again.reason, again.text),
            ("not_spoken", "already_consumed", None),
        )

    def test_failed_claim_write_never_releases_text(self):
        self.fail_put(lambda n, r: r.get("state") == "COMPLETE")
        reply = self.poll(self.turn)
        self.assertEqual(
            (reply.status, reply.reason, reply.text),
            ("not_spoken", "journal_fault", None),
        )
        self.assertTrue(self.bridge.status()["faulted"])
        self.assertEqual(self.poll(self.turn).reason, "bridge_faulted")

    def test_crash_with_unknown_claim_does_not_repeat_speech(self):
        self.fail_put(
            lambda n, r: r.get("state") == "COMPLETE",
            "journal_persist_unknown",
            after_write=True,
        )
        reply = self.poll(self.turn)
        self.assertIsNone(reply.text)
        self.restart()
        self.assertIsNone(self.poll(self.turn).text)

    def test_pending_states_do_not_consume_or_close(self):
        running = page(
            [
                user_row(self.turn, phase="working", basis="generation_started"),
                assistant_row("gen-1", "partial"),
            ]
        )
        reply = self.poll(self.turn, running)
        self.assertEqual(
            (reply.status, reply.reason, reply.terminal, reply.text),
            ("pending", "generation_running", False, None),
        )
        self.assertEqual(self.record(self.turn)["state"], "ACCEPTED")
        self.assertEqual(self.poll(self.turn).status, "speak")

    def test_partial_page_is_not_spoken(self):
        partial = page(
            [user_row(self.turn), assistant_row("gen-1", "x")], offset=3, total=5
        )
        reply = self.poll(self.turn, partial)
        self.assertEqual(
            (reply.status, reply.text, reply.reason),
            ("pending", None, "pagination_required"),
        )
        self.assertEqual(
            self.poll(self.turn, self.complete(self.turn), coverage=False).reason,
            "coverage_incomplete",
        )
        self.assertEqual(self.record(self.turn)["state"], "ACCEPTED")

    def test_steered_shared_generation_and_decoys_are_not_spoken_and_close_the_command(
        self,
    ):
        cases = {
            "steering_not_spoken": page(
                [
                    user_row(self.turn, steering={"disposition": "applied"}),
                    assistant_row("gen-1", "x"),
                ]
            ),
            "shared_generation": page(
                [
                    user_row(self.turn),
                    user_row(
                        "00000000-0000-4000-8000-0000000000dd", generation="gen-1"
                    ),
                    assistant_row("gen-1", "mixed"),
                ]
            ),
        }
        for reason, pg in cases.items():
            self.setUp()
            reply = self.poll(self.turn, pg)
            self.assertEqual(
                (reply.status, reply.reason, reply.text, reply.terminal),
                ("not_spoken", reason, None, True),
                reason,
            )
            self.assertEqual(self.record(self.turn)["state"], "COMPLETE")
            self.assertTrue(self.bridge.begin_turn())

    def test_decoy_assistant_rows_stay_pending(self):
        decoys = page(
            [
                user_row(self.turn),
                assistant_row("gen-2", "decoy", inputId=self.turn),
                assistant_row(None, "x"),
            ]
        )
        reply = self.poll(self.turn, decoys)
        self.assertEqual((reply.status, reply.text), ("pending", None))

    def test_truncated_reply_is_never_spoken(self):
        long_row = assistant_row("gen-1", "a" * 4096, textDetail={"length": 9000})
        reply = self.poll(self.turn, page([user_row(self.turn), long_row]))
        self.assertEqual(
            (reply.status, reply.reason, reply.text),
            ("not_spoken", "long_reply_not_spoken", None),
        )

    def test_foreign_session_page_is_not_spoken(self):
        reply = self.poll(self.turn, session_id=OTHER_SESSION)
        self.assertEqual(
            (reply.status, reply.reason, reply.text),
            ("not_spoken", "foreign_session", None),
        )
        self.assertEqual(self.poll(self.turn, session_id=SESSION).status, "speak")

    def test_unknown_and_invalid_command_ids(self):
        for bad in ("00000000-0000-4000-8000-0000000000ee", "x", None, 5):
            reply = self.bridge.poll_reply(
                bad, self.complete(self.turn), coverage_complete=True
            )  # type: ignore[arg-type]
            self.assertEqual(
                (reply.status, reply.reason, reply.text),
                ("not_spoken", "unknown_command", None),
            )

    def test_uncertain_and_rejected_commands_are_not_pollable_for_speech(self):
        self.bridge.stop()
        self.bridge.resume()
        self.poll(self.turn)  # closes the suppressed one
        turn2, _ = self.send(response=ConnectionError("x"), event="u2")
        reply = self.poll(turn2)
        self.assertEqual(
            (reply.status, reply.reason, reply.text),
            ("not_spoken", "delivery_uncertain", None),
        )


# =================================================================================
class MetadataOnlyTest(CoreCase):
    def test_no_text_in_status_repr_errors_or_journal_after_terminal(self):
        self.bind()
        turn, result = self.send(response=ok())
        reply = self.poll(turn)
        self.assertEqual(reply.text, ANSWER)
        everything = " ".join(
            [
                repr(self.bridge),
                str(self.bridge.status()),
                repr(result),
                repr(reply),
                str(self.bridge.status()),
                repr(self.journal),
            ]
        )
        for secret in ("secret-marker-777", "secret-reply-888"):
            self.assertNotIn(secret, everything)
            self.assertNotIn(secret.encode(), self.all_journal_bytes())
        try:
            self.bridge.begin_turn()
            self.bridge.append_transcript("nope", UTTER, "e")
        except BridgeError as err:
            self.assertNotIn("secret", str(err) + repr(err))

    def test_status_is_metadata_only(self):
        self.bind()
        self.sealed()
        status = self.bridge.status()
        self.assertEqual(status["open_turn"], True)
        self.assertEqual(status["open_turn_chars"], len(UTTER))
        flat = json.dumps(status)
        self.assertNotIn("secret-marker-777", flat)
        self.assertEqual(
            set(status),
            {
                "bound",
                "binding_id",
                "host_id",
                "session_id",
                "epoch",
                "stopped",
                "faulted",
                "open_turn",
                "open_turn_chars",
                "records",
                "journal_records",
            },
        )


class OfflineAndStdlibTest(CoreCase):
    def test_network_guard_is_active(self):
        import socket

        with self.assertRaises(AssertionError):
            socket.create_connection(("127.0.0.1", 9))
        with socket.socket() as sock, self.assertRaises(AssertionError):
            sock.connect(("127.0.0.1", 9))

    def test_production_imports_are_standard_library_only(self):
        package = Path(pet_bridge.__file__).parent
        files = sorted(package.glob("*.py"))
        self.assertGreaterEqual(len(files), 5)
        # U3: the ONE explicit third-party allowance.  A module root is accepted iff it is
        # in the standard library or named for that exact file here; no other file, no
        # other third-party name, no generic allowance.
        third_party_allowed = {"gateway_server.py": frozenset({"aiohttp"})}
        used: dict[str, set[str]] = {}
        for path in files:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif (
                    isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                ):
                    modules = [node.module]
                for module in modules:
                    root = module.split(".")[0]
                    if root in third_party_allowed.get(path.name, frozenset()):
                        used.setdefault(path.name, set()).add(root)
                        continue
                    self.assertIn(
                        root,
                        sys.stdlib_module_names,
                        f"{path.name}: {module}",
                    )
        # the allowance is used (not dormant), and only by its one file
        self.assertEqual(used, {"gateway_server.py": {"aiohttp"}})
        self.assertEqual(set(third_party_allowed), {"gateway_server.py"})

    def test_no_socket_or_http_modules_in_production(self):
        package = Path(pet_bridge.__file__).parent
        forbidden = {"socket", "ssl", "http", "urllib.request", "asyncio", "subprocess"}
        # Pure core only: the later transport modules are asserted separately in
        # test_transport (network/subprocess only in transport.py + _https_worker.py).
        # The pure gateway modules (state and provider wire) are held to the same rule.
        core_files = (
            "__init__.py",
            "core.py",
            "journal.py",
            "reply.py",
            "wire.py",
            "gateway_state.py",
            "gateway_provider.py",
        )
        for path in (package / name for name in core_files):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif (
                    isinstance(node, ast.ImportFrom) and node.module and node.level == 0
                ):
                    names = [node.module]
                for name in names:
                    self.assertNotIn(name, forbidden, path.name)

    def test_result_types_exist(self):
        self.assertTrue(all((CommandResult, ReplyResult)))


# =================================================================================
# Round-1 repair tests (R1..R11).  Written BEFORE the implementation changes.
# =================================================================================
def _plant_command(case: CoreCase, command_id: str, state: str, epoch: int, **extra):
    binding = case.bridge.status()
    record = {
        "state": state,
        "binding": {
            "binding_id": binding["binding_id"]
            or "00000000-0000-4000-8000-0000000000aa",
            "host_id": "host-1",
            "host_instance_id": "inst-1",
            "session_id": SESSION,
            "client_id": "pet-1",
            "credential_ref": CRED_1,
        },
        "epoch": epoch,
        "created_at": case.clock.now,
        "updated_at": case.clock.now,
        "event_ids": ["ev-planted"],
    }
    record.update(extra)
    case.journal.put(f"cmd-{command_id}.json", record)


class PreDialSafetyTest(CoreCase):
    COMMAND = "00000000-0000-4000-8000-0000000000b2"

    def setUp(self):
        super().setUp()
        self.binding = self.bind()

    def test_record_matrix_is_read_only_and_no_send(self):
        for state in (
            "PREPARED", "ATTEMPTED", "UNCERTAIN", "ACCEPTED", "REJECTED",
            "ABANDONED", "COMPLETE", "SUPPRESSED", "RESOLVED",
        ):
            with self.subTest(state=state):
                _plant_command(self, self.COMMAND, state, self.bridge.status()["epoch"])
                before = self.all_journal_bytes()
                self.assertEqual(
                    self.bridge.pre_dial_safe(self.binding),
                    state not in ("PREPARED", "ATTEMPTED", "UNCERTAIN"),
                )
                self.assertEqual(self.all_journal_bytes(), before)
        self.assertEqual(self.transport.calls, [])

    def test_restart_recovers_attempted_as_unresolved_and_prepared_as_unsent(self):
        _plant_command(self, self.COMMAND, "ATTEMPTED", self.bridge.status()["epoch"])
        self.restart(resume=False)
        self.assertEqual(self.record(self.COMMAND)["state"], "UNCERTAIN")
        before = self.all_journal_bytes()
        self.assertFalse(self.bridge.pre_dial_safe(self.binding))
        self.assertEqual(self.all_journal_bytes(), before)
        self.bridge.resolve_uncertain(
            self.COMMAND, operator_ack=f"ack-uncertain:{self.COMMAND}"
        )
        self.assertTrue(self.bridge.pre_dial_safe(self.binding))
        _plant_command(self, self.COMMAND, "PREPARED", self.bridge.status()["epoch"])
        self.restart(resume=False)
        self.assertEqual(self.record(self.COMMAND)["state"], "ABANDONED")
        self.assertTrue(self.bridge.pre_dial_safe(self.binding))
        self.assertEqual(self.transport.calls, [])

    def test_faulted_unbound_inconsistent_and_corrupt_state_refuse(self):
        self.assertFalse(self.bridge.pre_dial_safe(None))
        wrong = self.bind(session=OTHER_SESSION)
        self.assertFalse(self.bridge.pre_dial_safe(self.binding))
        self.assertTrue(self.bridge.pre_dial_safe(wrong))
        state = self.journal.get("binding.json")
        for key, value in (("epoch", True), ("stopped", 0), ("binding", None)):
            with self.subTest(key=key):
                self.journal.put("binding.json", {**state, key: value})
                before = self.all_journal_bytes()
                self.assertFalse(self.bridge.pre_dial_safe(wrong))
                self.assertEqual(self.all_journal_bytes(), before)
        self.journal.put("binding.json", state)
        _plant_command(self, self.COMMAND, "ACCEPTED", state["epoch"])
        name = f"cmd-{self.COMMAND}.json"
        record = self.journal.get(name)
        for invalid in ("UNKNOWN", [], None):
            with self.subTest(invalid=invalid):
                self.journal.put(name, {**record, "state": invalid})
                self.assertFalse(self.bridge.pre_dial_safe(wrong))
        self.journal.put(name, record)
        with mock.patch.object(self.journal, "get", side_effect=JournalError("journal_io_error")):
            self.assertFalse(self.bridge.pre_dial_safe(wrong))
        with mock.patch.object(self.bridge, "_faulted", True):
            self.assertFalse(self.bridge.pre_dial_safe(wrong))
        self.assertEqual(self.transport.calls, [])

    def test_open_turn_and_lost_listed_record_fail_closed(self):
        turn = self.bridge.begin_turn()
        self.assertFalse(self.bridge.pre_dial_safe(self.binding))
        self.bridge.abandon_turn(turn)
        _plant_command(self, self.COMMAND, "ACCEPTED", self.bridge.status()["epoch"])
        actual = self.journal.get
        with mock.patch.object(
            self.journal, "get",
            side_effect=lambda name: None if name.startswith("cmd-") else actual(name),
        ):
            self.assertFalse(self.bridge.pre_dial_safe(self.binding))

    def test_listing_or_count_error_and_mixed_uncertainty_fail_closed(self):
        second = "00000000-0000-4000-8000-0000000000b3"
        epoch = self.bridge.status()["epoch"]
        _plant_command(self, self.COMMAND, "ACCEPTED", epoch)
        _plant_command(self, second, "UNCERTAIN", epoch)
        before = self.all_journal_bytes()
        self.assertFalse(self.bridge.pre_dial_safe(self.binding))
        self.assertEqual(self.all_journal_bytes(), before)
        self.journal.delete(f"cmd-{second}.json")
        for method in ("names", "count"):
            with self.subTest(method=method), mock.patch.object(
                self.journal, method, side_effect=JournalError("journal_io_error")
            ):
                self.assertFalse(self.bridge.pre_dial_safe(self.binding))
        with mock.patch.object(self.journal, "count", return_value=42):
            self.assertFalse(self.bridge.pre_dial_safe(self.binding))


class CredentialRefTest(CoreCase):
    """R4: only the exact opaque identifier 'device-' + 32 lowercase hex."""

    TOKEN_SHAPES = (
        "sk-" + "a" * 40,
        "sk-proj-" + "A1" * 20,
        "tskey-auth-kABC123CNTRL-" + "x" * 20,
        "ghp_" + "A" * 36,
        "amc_" + "0" * 32 + "." + "A" * 64,
        "amt_" + "0" * 32 + "." + "A" * 64,
        "amd_" + "0" * 32 + "." + "A" * 64,
        "device-" + "a" * 31,
        "device-" + "a" * 33,
        "device-" + "A" * 32,
        "device-" + "g" * 32,
        "DEVICE-" + "a" * 32,
        "device-" + "a" * 32 + "\n",
        " device-" + "a" * 32,
        "device_" + "a" * 32,
        "cred-1",
        "",
    )

    def test_exact_opaque_identifier_is_accepted(self):
        self.assertEqual(self.bind().credential_ref, CRED_1)
        self.assertEqual(
            self.bind(credential_ref="device-" + "0123456789abcdef" * 2).credential_ref,
            "device-" + "0123456789abcdef" * 2,
        )

    def test_token_shaped_and_near_miss_values_are_rejected(self):
        for bad in self.TOKEN_SHAPES:
            err = raised(self.bind, credential_ref=bad)
            self.assertIsInstance(err, BridgeError, repr(bad))
            self.assertEqual(getattr(err, "code", None), "invalid_binding", repr(bad))
            if bad:
                self.assertNotIn(bad, str(err) + repr(err))
        self.assertFalse(self.bridge.status()["bound"])
        self.assertEqual(self.transport.calls, [])

    def test_non_string_values_are_rejected(self):
        for bad in (None, 5, b"device-" + b"a" * 32, ["device-" + "a" * 32]):
            err = raised(self.bind, credential_ref=bad)
            self.assertEqual(getattr(err, "code", None), "invalid_binding", repr(bad))

    def test_binding_repr_and_str_never_show_the_credential_reference(self):
        binding = self.bind()
        self.assertNotIn(CRED_1, repr(binding))
        self.assertNotIn(CRED_1, str(binding))
        self.assertNotIn("credential", repr(binding))
        self.assertEqual(
            binding.credential_ref, CRED_1
        )  # still readable by a transport

    def test_a_persisted_binding_with_a_token_shaped_reference_fails_closed(self):
        self.bind()
        state = self.journal.get("binding.json")
        state["binding"]["credential_ref"] = "sk-" + "a" * 40
        self.journal.put("binding.json", state)
        err = raised(self.restart)
        self.assertIsInstance(err, BridgeError)
        self.assertEqual(getattr(err, "code", None), "journal_corrupt")


class ClockTest(CoreCase):
    """R7: a turn is fresh only if BOTH the wall and the monotonic age are in [0, 60]."""

    def setUp(self) -> None:
        super().setUp()
        self.open_bridge(mono=self.mono)
        self.bind()
        self.turn, self.preview = self.sealed()

    def confirm(self):
        self.transport.responses.append(ok())
        return self.bridge.confirm_send(
            self.turn, expected_text_sha256=self.preview.text_sha256
        )

    def assertExpired(self, result):
        self.assertEqual((result.outcome, result.reason), ("refused", "expired"))
        self.assertEqual(self.transport.calls, [])

    def test_both_clocks_within_sixty_seconds_send(self):
        self.clock.advance(30)
        self.mono.advance(30)
        self.assertEqual(self.confirm().outcome, "accepted")

    def test_boundary_sixty_is_alive_and_just_over_is_expired(self):
        self.clock.advance(60)
        self.mono.advance(60)
        self.assertEqual(self.confirm().outcome, "accepted")

    def test_just_over_sixty_on_either_clock_is_expired(self):
        for wall, mono in ((60.001, 10), (10, 60.001)):
            self.setUp()
            self.clock.advance(wall)
            self.mono.advance(mono)
            self.assertExpired(self.confirm())

    def test_partial_rollback_wall_40_but_monotonic_70_does_not_send(self):
        self.clock.advance(40)
        self.mono.advance(70)
        self.assertExpired(self.confirm())

    def test_partial_rollback_monotonic_40_but_wall_70_does_not_send(self):
        self.clock.advance(70)
        self.mono.advance(40)
        self.assertExpired(self.confirm())

    def test_large_wall_clock_rollback_does_not_extend_the_age(self):
        self.clock.advance(-3600)
        self.mono.advance(5)
        self.assertExpired(self.confirm())

    def test_small_wall_clock_rollback_is_expired_as_well(self):
        self.clock.advance(-1)
        self.mono.advance(5)
        self.assertExpired(self.confirm())

    def test_monotonic_rollback_is_expired(self):
        self.clock.advance(5)
        self.mono.advance(-5)
        self.assertExpired(self.confirm())

    def test_forward_wall_jump_is_expired(self):
        self.clock.advance(3600)
        self.mono.advance(1)
        self.assertExpired(self.confirm())

    def unsealed(self):
        self.bridge.clear_session()
        self.bind()
        turn = self.bridge.begin_turn()
        self.assertEqual(self.bridge.append_transcript(turn, "x", "e0"), "appended")
        return turn

    def test_append_and_seal_are_subject_to_the_same_age_rule(self):
        for wall, mono in ((-1, 5), (5, -1), (61, 5), (5, 61)):
            self.setUp()
            turn = self.unsealed()
            self.clock.advance(wall)
            self.mono.advance(mono)
            self.assertEqual(
                self.bridge.append_transcript(turn, "y", "e1"), "expired", (wall, mono)
            )
            self.assertCode("expired", self.bridge.seal_turn, turn)

    def test_non_finite_or_boolean_clock_readings_are_refused_never_trusted(self):
        for bad in (float("nan"), float("inf"), float("-inf"), True, None, "1"):
            for which in ("clock", "mono"):
                self.setUp()
                getattr(self, which).now = bad
                try:
                    result = self.confirm()
                except Exception as exc:  # noqa: BLE001
                    self.fail(
                        f"confirm raised {type(exc).__name__} for {which}={bad!r}"
                    )
                self.assertEqual(result.outcome, "refused", (which, bad))
                self.assertIn(result.reason, ("expired", "invalid_clock"), (which, bad))
                self.assertEqual(self.transport.calls, [], (which, bad))

    def test_begin_turn_refuses_a_non_finite_clock(self):
        self.bridge.clear_session()
        self.bind()
        for bad in (float("nan"), float("inf"), True):
            self.clock.now = bad
            err = raised(self.bridge.begin_turn)
            self.assertIsInstance(err, BridgeError, repr(bad))
            self.assertEqual(getattr(err, "code", None), "invalid_clock", repr(bad))
            self.clock.now = 1_000_000.0
        self.assertTrue(self.bridge.begin_turn())

    def test_monotonic_is_optional_and_defaults_to_the_real_clock(self):
        self.restart()  # open_bridge() passes no monotonic
        self.assertFalse(self.bridge.status()["stopped"])
        self.assertTrue(self.bridge.begin_turn())

    def test_retention_uses_the_wall_clock_conservatively(self):
        # A rolled-back wall clock never makes a record look older (so never forgets).
        self.bridge.clear_session()
        self.bind()
        turn, _ = self.send(response=(409, b"{}"), event="r1")
        self.clock.advance(-48 * 3600)
        self.assertEqual(self.bridge.cleanup(), 0)
        self.assertIsNotNone(self.record(turn))


class SeenBoundTest(CoreCase):
    """R8: duplicate-event memory is rebuilt from retained records and bounded."""

    def setUp(self) -> None:
        super().setUp()
        self.bind()

    def test_event_ids_of_forgotten_records_are_forgotten_too(self):
        self.send(response=(409, b"{}"), event="ev-old")
        turn = self.bridge.begin_turn()
        self.assertEqual(
            self.bridge.append_transcript(turn, "x", "ev-old"), "duplicate_event"
        )
        self.bridge.stop()
        self.bridge.resume()
        self.clock.advance(24 * 3600)
        self.assertEqual(self.bridge.cleanup(), 1)
        fresh = self.bridge.begin_turn()
        self.assertEqual(
            self.bridge.append_transcript(fresh, "x", "ev-old"), "appended"
        )

    def test_event_ids_of_capacity_evicted_records_are_forgotten_too(self):
        self.open_bridge(max_records=3)
        self.send(response=(409, b"{}"), event="ev-a")
        self.send(response=(409, b"{}"), event="ev-b")
        self.clock.advance(5)
        turn = self.bridge.begin_turn()  # evicts the oldest terminal record (ev-a)
        self.assertEqual(self.bridge.append_transcript(turn, "x", "ev-a"), "appended")
        self.assertEqual(
            self.bridge.append_transcript(turn, "y", "ev-b"), "duplicate_event"
        )

    def test_retained_records_keep_their_event_ids_across_cleanup_and_restart(self):
        self.send(response=(409, b"{}"), event="ev-term")
        self.bridge.cleanup()
        self.restart()
        turn = self.bridge.begin_turn()
        self.assertEqual(
            self.bridge.append_transcript(turn, "z", "ev-term"), "duplicate_event"
        )
        self.bridge.cleanup()
        self.assertEqual(
            self.bridge.append_transcript(turn, "z", "ev-term"), "duplicate_event"
        )

    def test_replayed_events_of_a_discarded_unsent_turn_are_dropped_in_memory(self):
        turn = self.bridge.begin_turn()
        self.assertEqual(
            self.bridge.append_transcript(turn, "hello", "ev-d1"), "appended"
        )
        self.bridge.stop()
        self.bridge.resume()
        again = self.bridge.begin_turn()
        self.assertEqual(
            self.bridge.append_transcript(again, "hello", "ev-d1"), "duplicate_event"
        )
        self.assertEqual(self.transport.calls, [])  # a replay can never dispatch
        self.assertEqual(self.journal.names("cmd-"), [])

    def test_clear_also_remembers_discarded_events(self):
        turn = self.bridge.begin_turn()
        self.bridge.append_transcript(turn, "hello", "ev-c1")
        self.bridge.clear_session()
        self.bind()
        again = self.bridge.begin_turn()
        self.assertEqual(
            self.bridge.append_transcript(again, "hello", "ev-c1"), "duplicate_event"
        )

    def test_the_discard_memory_is_bounded_and_forgets_the_oldest(self):
        first = last = ""
        for n in range(9):  # 9 turns x 128 events = 1152 > the 1024-event cap
            turn = self.bridge.begin_turn()
            for i in range(128):
                event = f"d{n}-{i}"
                first = first or event
                last = event
                self.assertEqual(
                    self.bridge.append_transcript(turn, "a", event), "appended"
                )
            self.bridge.stop()
            self.bridge.resume()
        turn = self.bridge.begin_turn()
        self.assertEqual(self.bridge.append_transcript(turn, "a", first), "appended")
        self.assertEqual(
            self.bridge.append_transcript(turn, "a", last), "duplicate_event"
        )
        self.assertEqual(
            self.bridge.append_transcript(turn, "a", "d1-0"), "duplicate_event"
        )

    def test_the_discard_memory_is_in_process_only_and_resets_on_restart(self):
        turn = self.bridge.begin_turn()
        self.bridge.append_transcript(turn, "hello", "ev-r1")
        self.bridge.stop()
        self.restart()
        again = self.bridge.begin_turn()
        self.assertEqual(
            self.bridge.append_transcript(again, "hello", "ev-r1"), "appended"
        )


class UnresolvedClaimTest(CoreCase):
    """R10: a terminal 'reply_invalid' decision must really close the record."""

    def setUp(self) -> None:
        super().setUp()
        self.bind()
        self.turn, _ = self.send(response=ok())

    def force(self, text):
        decision = ReplyDecision("speak", "reply_complete", True, text, "gen-1")
        return mock.patch("pet_bridge.core.evaluate_reply", return_value=decision)

    def test_oversize_text_closes_the_record_and_does_not_wedge_the_gate(self):
        with self.force("x" * 4001):
            reply = self.poll(self.turn)
        self.assertEqual(
            (reply.status, reply.reason, reply.text, reply.terminal),
            ("not_spoken", "reply_invalid", None, True),
        )
        self.assertEqual(self.record(self.turn)["state"], "COMPLETE")
        self.assertTrue(self.bridge.begin_turn())

    def test_missing_text_closes_the_record_too(self):
        with self.force(None):
            reply = self.poll(self.turn)
        self.assertEqual((reply.status, reply.reason), ("not_spoken", "reply_invalid"))
        self.assertEqual(self.record(self.turn)["state"], "COMPLETE")
        self.assertTrue(self.bridge.begin_turn())
        self.assertEqual(self.poll(self.turn).reason, "reply_closed")


class FrozenTargetEpochTest(CoreCase):
    """A turn is sent to its frozen binding only while that binding's epoch is current."""

    def setUp(self) -> None:
        super().setUp()
        self.bind()

    def test_unchanged_target_reselect_sends_the_original_frozen_binding(self):
        turn, preview = self.sealed()
        before = self.bridge.status()["epoch"]
        reselected = self.bind(credential_ref=CRED_9)
        self.assertEqual(self.bridge.status()["epoch"], before)
        self.transport.responses.append(ok())
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256
        )
        self.assertEqual(result.outcome, "accepted")
        self.assertEqual(len(self.transport.calls), 1)
        sent_binding = self.transport.calls[0][0]
        self.assertEqual(sent_binding.credential_ref, CRED_1)
        self.assertEqual(sent_binding.binding_id, preview.binding_id)
        self.assertNotEqual(sent_binding, reselected)

    def test_a_b_a_remains_stale(self):
        turn, preview = self.sealed()
        before = self.bridge.status()["epoch"]
        self.bind(OTHER_SESSION, credential_ref=CRED_2)
        self.bind()
        self.assertEqual(self.bridge.status()["epoch"], before + 2)
        self.transport.responses.append(ok())
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256
        )
        self.assertEqual((result.outcome, result.reason), ("refused", "epoch_stale"))
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.journal.names("cmd-"), [])

    def test_a_host_or_instance_change_is_stale_too(self):
        for change in ({"host_id": "host-2"}, {"host_instance_id": "inst-2"}):
            with self.subTest(change=change):
                turn, preview = self.sealed(event=f"ev-{self.ids.n}")
                self.bind(**change)
                self.transport.responses.append(ok())
                result = self.bridge.confirm_send(
                    turn, expected_text_sha256=preview.text_sha256
                )
                self.assertEqual(
                    (result.outcome, result.reason), ("refused", "epoch_stale")
                )
                self.assertEqual(self.transport.calls, [])
                self.assertEqual(self.journal.names("cmd-"), [])
                self.transport.responses.clear()
                self.bind()

    def test_staleness_is_checked_before_any_persistence_or_transport(self):
        turn, preview = self.sealed()
        self.bind(OTHER_SESSION)
        self.transport.responses.append(ok())
        puts: list[str] = []
        real = self.journal.put

        def spy(name, record):
            puts.append(name)
            return real(name, record)

        self.journal.put = spy
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256
        )
        self.assertEqual(result.reason, "epoch_stale")
        self.assertEqual(puts, [])
        self.assertEqual(self.transport.calls, [])

    def test_switching_during_the_send_keeps_the_outcome_and_suppresses_the_reply(
        self,
    ):

        def switch() -> None:
            self.bind(OTHER_SESSION, credential_ref=CRED_2)

        self.transport.on_send = switch
        turn, result = self.send(response=ok())
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual((result.outcome, result.persisted), ("accepted", True))
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")
        self.assertEqual(self.record(turn)["binding"]["session_id"], SESSION)
        reply = self.poll(turn)
        self.assertEqual(
            (reply.status, reply.reason, reply.text),
            ("not_spoken", "epoch_stale", None),
        )
        self.assertEqual(len(self.transport.calls), 1)
        # The next turn uses the new selection.
        _t, preview = self.sealed(event="ev-next")
        self.assertEqual(preview.session_id, OTHER_SESSION)


class CallbackAndReplayTest(CoreCase):
    """R11: callbacks, replay gates and error paths."""

    def setUp(self) -> None:
        super().setUp()
        self.bind()

    def test_clear_during_the_transport_callback_suppresses_the_reply(self):
        self.transport.on_send = self.bridge.clear_session
        turn, result = self.send(response=ok())
        self.assertEqual((result.outcome, result.persisted), ("accepted", True))
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")
        self.bind()
        reply = self.poll(turn)
        self.assertEqual(
            (reply.status, reply.reason, reply.text),
            ("not_spoken", "epoch_stale", None),
        )

    def test_selecting_another_session_during_the_callback_suppresses_the_reply(self):

        def select_elsewhere() -> None:
            self.bind(OTHER_SESSION)

        self.transport.on_send = select_elsewhere
        turn, result = self.send(response=ok())
        self.assertEqual(result.outcome, "accepted")
        reply = self.poll(turn)
        self.assertEqual(
            (reply.status, reply.reason, reply.text),
            ("not_spoken", "epoch_stale", None),
        )

    def test_eight_concurrent_polls_consume_the_reply_exactly_once(self):
        turn, _ = self.send(response=ok())
        start = threading.Barrier(8)
        results: list = []
        guard = threading.Lock()

        def worker():
            start.wait(5)
            reply = self.poll(turn)
            with guard:
                results.append(reply)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(results), 8)
        self.assertEqual([r.status for r in results].count("speak"), 1)
        self.assertEqual(
            sorted(r.reason for r in results if r.status != "speak"),
            ["already_consumed"] * 7,
        )
        self.assertEqual(sum(1 for r in results if r.text is not None), 1)

    def test_an_unknown_claim_write_releases_no_speech_even_after_restart(self):
        turn, _ = self.send(response=ok())
        self.fail_put(
            lambda n, r: r.get("state") == "COMPLETE",
            "journal_persist_unknown",
            after_write=True,
        )
        reply = self.poll(turn)
        self.assertEqual((reply.status, reply.text), ("not_spoken", None))
        self.restart()
        again = self.poll(turn)
        self.assertEqual((again.status, again.text), ("not_spoken", None))

    def test_failed_resume_write_stays_disarmed(self):
        self.bridge.stop()
        self.fail_put(lambda n, r: n == "binding.json" and r.get("stopped") is False)
        self.assertCode("journal_error", self.bridge.resume)
        self.assertTrue(self.bridge.status()["stopped"])
        self.assertCode("stopped", self.bridge.begin_turn)

    def test_unknown_resume_write_stays_disarmed_and_faults(self):
        self.bridge.stop()
        self.fail_put(
            lambda n, r: n == "binding.json" and r.get("stopped") is False,
            "journal_persist_unknown",
            after_write=True,
        )
        self.assertCode("journal_error", self.bridge.resume)
        self.assertTrue(self.bridge.status()["stopped"])
        self.assertTrue(self.bridge.status()["faulted"])
        self.assertCode("bridge_faulted", self.bridge.begin_turn)

    def test_a_failed_recovery_transition_fails_closed_and_sends_nothing(self):
        t = "00000000-0000-4000-8000-0000000000b2"
        _plant_command(self, t, "ATTEMPTED", 0, text=UTTER, attempted_at=1.0)
        self.journal.close()
        journal = Journal(self.root)
        self.addCleanup(journal.close)
        real = journal.put

        def put(name, record):
            if record.get("state") == "UNCERTAIN":
                raise JournalError("journal_io_error")
            return real(name, record)

        journal.put = put
        err = raised(Bridge, journal, self.transport, clock=self.clock, new_id=self.ids)
        self.assertIsInstance(err, BridgeError)
        self.assertEqual(getattr(err, "code", None), "journal_error")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(journal.get(f"cmd-{t}.json")["state"], "ATTEMPTED")

    def test_events_of_abandoned_and_uncertain_commands_stay_a_replay_gate(self):
        t1 = "00000000-0000-4000-8000-0000000000b1"
        t2 = "00000000-0000-4000-8000-0000000000b2"
        _plant_command(self, t1, "PREPARED", 0, text=UTTER, event_ids=["ev-p"])
        _plant_command(self, t2, "ATTEMPTED", 0, text=UTTER, event_ids=["ev-a"])
        self.restart()
        self.assertEqual(self.record(t1)["state"], "ABANDONED")
        self.assertEqual(self.record(t2)["state"], "UNCERTAIN")
        self.bridge.resolve_uncertain(t2, operator_ack=f"ack-uncertain:{t2}")
        turn = self.bridge.begin_turn()
        for event in ("ev-p", "ev-a"):
            self.assertEqual(
                self.bridge.append_transcript(turn, "again", event), "duplicate_event"
            )
        self.assertEqual(self.transport.calls, [])


class InflightLockTest(CoreCase):
    """R3: the main lock is NOT held across transport.send."""

    def setUp(self) -> None:
        super().setUp()
        self.bind()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)

        def block():
            self.entered.set()
            if not self.release.wait(10):
                raise AssertionError("transport never released by the test")

        self.transport.on_send = block
        self.box: dict = {}

    def start_send(self):
        turn, preview = self.sealed()

        def run():
            try:
                self.box["result"] = self.bridge.confirm_send(
                    turn, expected_text_sha256=preview.text_sha256
                )
            except BaseException as exc:  # noqa: BLE001
                self.box["error"] = exc

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self.assertTrue(self.entered.wait(5), "transport was never entered")
        return turn, preview

    def other_thread(self, fn, timeout: float = 2.0):
        out: dict = {}

        def run():
            try:
                out["value"] = fn()
            except BaseException as exc:  # noqa: BLE001
                out["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout)
        self.assertFalse(
            thread.is_alive(), "call blocked behind the in-flight transport"
        )
        if "error" in out:
            raise out["error"]
        return out.get("value")

    def finish(self):
        self.release.set()
        self.thread.join(10)
        self.assertFalse(self.thread.is_alive())

    def test_status_and_epoch_check_answer_while_the_transport_waits(self):
        self.transport.responses.append(ok())
        turn, _ = self.start_send()
        status = self.other_thread(self.bridge.status)
        self.assertEqual(status["records"], {"ATTEMPTED": 1})
        self.assertTrue(
            self.other_thread(lambda: self.bridge.epoch_current(status["epoch"]))
        )
        self.finish()
        self.assertEqual(self.box["result"].outcome, "accepted")
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")

    def test_pre_dial_refuses_while_send_is_inflight_then_allows_known_acceptance(self):
        binding = self.bridge._binding
        self.transport.responses.append(ok())
        turn, _ = self.start_send()
        self.assertFalse(self.other_thread(lambda: self.bridge.pre_dial_safe(binding)))
        self.finish()
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")
        self.assertTrue(self.bridge.pre_dial_safe(binding))

    def test_stop_from_a_second_thread_does_not_wait_and_the_late_accept_is_not_spoken(
        self,
    ):
        self.transport.responses.append(ok())
        turn, preview = self.start_send()
        self.assertEqual(
            getattr(raised(self.other_thread, self.bridge.begin_turn), "code", None),
            "command_inflight",
        )
        self.other_thread(self.bridge.stop)
        self.assertTrue(self.other_thread(lambda: self.bridge.status()["stopped"]))
        again = self.other_thread(
            lambda: self.bridge.confirm_send(
                turn, expected_text_sha256=preview.text_sha256
            )
        )
        self.assertEqual(
            (again.outcome, again.reason), ("refused", "duplicate_confirm")
        )
        self.assertEqual(len(self.transport.calls), 1)  # no second send
        self.finish()
        result = self.box["result"]
        self.assertEqual((result.outcome, result.persisted), ("accepted", True))
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")
        self.assertEqual(self.poll(turn).reason, "stopped")
        self.bridge.resume()
        late = self.poll(turn)
        self.assertEqual(
            (late.status, late.reason, late.text), ("not_spoken", "epoch_stale", None)
        )
        self.assertEqual(len(self.transport.calls), 1)

    def test_a_new_turn_cannot_start_or_send_while_one_is_in_flight(self):
        self.transport.responses.append(ok())
        self.start_send()
        err = raised(self.other_thread, self.bridge.begin_turn)
        self.assertEqual(getattr(err, "code", None), "command_inflight")
        self.finish()
        self.assertEqual(len(self.transport.calls), 1)

    def test_reentrant_stop_during_the_callback_still_works(self):
        self.transport.on_send = self.bridge.stop
        self.transport.responses.append(ok())
        turn, result = self.send()
        self.assertEqual(result.outcome, "accepted")
        self.assertTrue(self.bridge.status()["stopped"])
        self.assertEqual(self.poll(turn).reason, "stopped")


class InterruptedTransportTest(CoreCase):
    """R3: a BaseException from the transport is recorded UNCERTAIN, then re-raised."""

    def setUp(self) -> None:
        super().setUp()
        self.bind()

    def test_non_exception_interrupts_persist_uncertain_and_are_reraised(self):
        for make in (
            lambda: SystemExit(3),
            KeyboardInterrupt,
            asyncio.CancelledError,
        ):
            self.setUp()
            exc = make()
            turn, preview = self.sealed()
            self.transport.responses.append(exc)
            err = raised(
                self.bridge.confirm_send,
                turn,
                expected_text_sha256=preview.text_sha256,
            )
            self.assertIs(err, exc)
            self.assertEqual(self.record(turn)["state"], "UNCERTAIN")
            self.assertNotIn("text", self.record(turn))
            self.assertEqual(len(self.transport.calls), 1)  # no retry
            status = self.bridge.status()
            self.assertEqual(status["records"], {"UNCERTAIN": 1})
            self.assertCode("uncertain_unresolved", self.bridge.begin_turn)
            again = self.bridge.confirm_send(
                turn, expected_text_sha256=preview.text_sha256
            )
            self.assertEqual(again.reason, "duplicate_confirm")
            self.assertEqual(len(self.transport.calls), 1)

    def test_an_interrupt_with_a_failing_outcome_write_still_settles_inflight(self):
        turn, preview = self.sealed()
        self.fail_put(lambda n, r: r.get("state") == "UNCERTAIN")
        self.transport.responses.append(SystemExit(1))
        err = raised(
            self.bridge.confirm_send, turn, expected_text_sha256=preview.text_sha256
        )
        self.assertIsInstance(err, SystemExit)
        self.assertTrue(self.bridge.status()["faulted"])
        self.assertEqual(self.record(turn)["state"], "ATTEMPTED")
        self.restart()
        self.assertEqual(self.record(turn)["state"], "UNCERTAIN")  # recovery
        self.assertEqual(len(self.transport.calls), 1)


class RestartDisarmTest(CoreCase):
    """R2: every restart of an existing journal is a fresh, disarmed audio epoch."""

    def test_a_brand_new_journal_starts_active_and_unbound(self):
        status = self.bridge.status()
        self.assertEqual((status["bound"], status["stopped"]), (False, False))
        self.assertCode("no_binding", self.bridge.begin_turn)
        self.bind()
        self.assertTrue(self.bridge.begin_turn())

    def test_an_existing_journal_restarts_disarmed_until_an_explicit_resume(self):
        self.bind()
        self.restart(resume=False)
        status = self.bridge.status()
        self.assertTrue(status["bound"] and status["stopped"])
        self.assertCode("stopped", self.bridge.begin_turn)
        self.assertEqual(self.journal.get("binding.json")["stopped"], True)
        self.restart(resume=False)  # a second restart stays disarmed
        self.assertTrue(self.bridge.status()["stopped"])
        self.bridge.resume()
        self.assertFalse(self.bridge.status()["stopped"])
        self.assertFalse(self.journal.get("binding.json")["stopped"])
        self.assertTrue(self.bridge.begin_turn())

    def test_a_restarted_bridge_cannot_confirm_or_poll_until_resumed(self):
        self.bind()
        turn, preview = self.sealed()
        self.restart(resume=False)
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256
        )
        self.assertEqual(result.outcome, "refused")
        self.assertEqual(self.transport.calls, [])

    def test_the_new_epoch_exceeds_the_binding_and_every_command_epoch(self):
        self.bind()
        turn, _ = self.send(response=ok())
        old = self.bridge.status()["epoch"]
        _plant_command(
            self, "00000000-0000-4000-8000-000000000f01", "REJECTED", old + 40
        )
        self.restart(resume=False)
        self.assertGreater(self.bridge.status()["epoch"], old + 40)
        first = self.bridge.status()["epoch"]
        self.restart(resume=False)
        self.assertGreater(self.bridge.status()["epoch"], first)
        self.assertEqual(self.record(turn)["epoch"], old)

    def test_a_recovered_accepted_reply_is_never_spoken(self):
        self.bind()
        turn, _ = self.send(response=ok())
        self.restart()  # resumed
        reply = self.poll(turn)
        self.assertEqual(
            (reply.status, reply.reason, reply.text),
            ("not_spoken", "epoch_stale", None),
        )
        self.assertEqual(self.record(turn)["state"], "SUPPRESSED")
        self.assertEqual(len(self.transport.calls), 1)
        self.assertTrue(self.bridge.begin_turn())  # the gate is released locally

    def test_a_recovered_accepted_reply_is_not_spoken_while_disarmed_either(self):
        self.bind()
        turn, _ = self.send(response=ok())
        self.restart(resume=False)
        reply = self.poll(turn)
        self.assertEqual((reply.status, reply.text), ("not_spoken", None))
        self.assertEqual(reply.reason, "stopped")

    def test_recovery_never_calls_the_transport(self):
        self.bind()
        _plant_command(
            self, "00000000-0000-4000-8000-0000000000b1", "PREPARED", 0, text=UTTER
        )
        _plant_command(
            self, "00000000-0000-4000-8000-0000000000b2", "ATTEMPTED", 0, text=UTTER
        )
        self.restart(resume=False)
        self.restart()
        self.assertEqual(self.transport.calls, [])

    def test_startup_persist_failure_is_a_constructor_failure_and_nothing_can_send(
        self,
    ):
        self.bind()
        self.send(response=ok())
        self.journal.close()
        journal = Journal(self.root)
        self.addCleanup(journal.close)
        real = journal.put

        def put(name, record):
            if name == "binding.json":
                raise JournalError("journal_io_error")
            return real(name, record)

        journal.put = put
        sent = len(self.transport.calls)
        err = raised(Bridge, journal, self.transport, clock=self.clock, new_id=self.ids)
        self.assertIsInstance(err, BridgeError)
        self.assertEqual(len(self.transport.calls), sent)

    # -- failed Stop / clear persistence ------------------------------------------
    def fail_all_binding_writes(self, *, unlink_fails: bool):
        real_put, real_delete = self.journal.put, self.journal.delete
        self.deletes: list[str] = []

        def put(name, record):
            if name == "binding.json":
                raise JournalError("journal_io_error")
            return real_put(name, record)

        def delete(name):
            self.deletes.append(name)
            if unlink_fails:
                raise JournalError("journal_io_error")
            return real_delete(name)

        self.journal.put, self.journal.delete = put, delete

    def accepted_waiting(self):
        self.bind()
        turn, _ = self.send(response=ok())
        return turn

    def test_failed_stop_write_closes_the_gate_now_and_restart_never_speaks_the_old_reply(
        self,
    ):
        for unlink_fails in (False, True):
            self.setUp()
            turn = self.accepted_waiting()
            self.fail_all_binding_writes(unlink_fails=unlink_fails)
            self.bridge.stop()
            status = self.bridge.status()
            self.assertEqual((status["stopped"], status["faulted"]), (True, True))
            self.assertEqual(self.poll(turn).status, "not_spoken")
            self.assertEqual(self.deletes, ["binding.json"])  # only that one record
            self.restart(resume=False)
            self.assertTrue(self.bridge.status()["stopped"])
            old = self.poll(turn)
            self.assertEqual((old.status, old.text), ("not_spoken", None), unlink_fails)
            self.restart()  # even explicitly resumed: the epoch is fresh
            old = self.poll(turn)
            self.assertEqual((old.status, old.text), ("not_spoken", None), unlink_fails)
            self.assertEqual(old.reason, "epoch_stale", unlink_fails)
            self.assertEqual(len(self.transport.calls), 1)

    def test_failed_clear_write_closes_the_gate_now_and_restart_never_speaks_the_old_reply(
        self,
    ):
        for unlink_fails in (False, True):
            self.setUp()
            turn = self.accepted_waiting()
            self.fail_all_binding_writes(unlink_fails=unlink_fails)
            self.bridge.clear_session()
            status = self.bridge.status()
            self.assertEqual((status["bound"], status["faulted"]), (False, True))
            self.assertCode("bridge_faulted", self.bridge.begin_turn)
            self.assertEqual(self.deletes, ["binding.json"])
            self.restart(resume=False)
            self.assertTrue(self.bridge.status()["stopped"])
            self.assertEqual(self.bridge.status()["bound"], unlink_fails)
            self.restart()
            old = self.poll(turn)
            self.assertEqual(
                (old.status, old.reason, old.text), ("not_spoken", "epoch_stale", None)
            )

    def test_the_unlink_fallback_removes_only_the_binding_record(self):
        turn = self.accepted_waiting()
        self.fail_all_binding_writes(unlink_fails=False)
        self.bridge.stop()
        self.assertEqual(self.deletes, ["binding.json"])
        self.assertIsNone(self.journal.get("binding.json"))
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")

    def test_a_successful_stop_or_clear_never_unlinks_anything(self):
        self.accepted_waiting()
        deleted: list[str] = []
        real_delete = self.journal.delete
        self.journal.delete = lambda name: (deleted.append(name), real_delete(name))[1]
        self.bridge.stop()
        self.bridge.resume()
        self.bridge.clear_session()
        self.assertEqual(deleted, [])


class FlakyClock(FakeClock):
    """A wall clock that turns invalid (NaN) after a number of readings."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.invalid_after: int | None = None

    def __call__(self) -> float:
        self.calls += 1
        if self.invalid_after is not None and self.calls > self.invalid_after:
            return float("nan")
        return self.now


class ClockFaultPathTest(CoreCase):
    """R7 follow-up: an invalid clock mid-send fails closed on every path."""

    def setUp(self) -> None:
        super().setUp()
        self.clock = FlakyClock()
        self.restart()
        self.bind()

    def test_clock_going_invalid_between_the_age_check_and_the_write_refuses(self):
        turn, preview = self.sealed()
        self.clock.invalid_after = self.clock.calls + 1  # the age check passes
        result = self.bridge.confirm_send(
            turn, expected_text_sha256=preview.text_sha256
        )
        self.assertEqual((result.outcome, result.reason), ("refused", "invalid_clock"))
        self.assertEqual(self.transport.calls, [])
        self.assertIsNone(self.record(turn))  # nothing was persisted
        self.assertEqual(self.journal.names("cmd-"), [])

    def test_clock_going_invalid_while_the_transport_runs_faults_and_recovers(self):
        self.transport.on_send = lambda: setattr(self.clock, "now", float("nan"))
        turn, result = self.send(response=ok())
        self.assertEqual((result.outcome, result.persisted), ("accepted", False))
        self.assertTrue(self.bridge.status()["faulted"])
        self.assertEqual(self.record(turn)["state"], "ATTEMPTED")
        self.clock.now = 1_000_000.0
        self.restart()
        self.assertEqual(self.record(turn)["state"], "UNCERTAIN")
        self.assertEqual(len(self.transport.calls), 1)


class ReceiptsThroughTheBridgeTest(CoreCase):
    """R5/R6/R9 end to end: hostile receipts and pages never raise or speak."""

    def setUp(self) -> None:
        super().setUp()
        self.bind()

    def test_malformed_and_contradictory_receipts_are_uncertain_and_hold_the_gate(self):
        hostile = (
            (
                200,
                json.dumps(
                    {"accepted": True, "delivery": ["accepted"], "sessionId": SESSION}
                ).encode(),
            ),
            (
                200,
                json.dumps(
                    {"accepted": True, "delivery": {"a": 1}, "sessionId": SESSION}
                ).encode(),
            ),
            (
                409,
                json.dumps(
                    {"accepted": True, "delivery": "accepted", "sessionId": SESSION}
                ).encode(),
            ),
            (409, json.dumps({"accepted": False, "delivery": "held"}).encode()),
            (
                200,
                json.dumps(
                    {
                        "accepted": True,
                        "delivery": "accepted",
                        "sessionId": SESSION,
                        "receipt": {"delivery": "failed"},
                    }
                ).encode(),
            ),
        )
        for response in hostile:
            self.setUp()
            try:
                turn, result = self.send(response=response)
            except Exception as exc:  # noqa: BLE001
                self.fail(f"confirm_send raised {type(exc).__name__} for {response!r}")
            self.assertEqual(result.outcome, "uncertain", response)
            self.assertCode("uncertain_unresolved", self.bridge.begin_turn)
            self.assertEqual(self.poll(turn).status, "not_spoken")
            self.assertEqual(len(self.transport.calls), 1)

    def test_a_genuine_409_rejection_still_releases_the_gate(self):
        _turn, result = self.send(response=(409, b'{"accepted":false,"error":"stale"}'))
        self.assertEqual((result.outcome, result.reason), ("rejected", "http_409"))
        self.assertTrue(self.bridge.begin_turn())

    def test_an_ambiguous_own_row_closes_the_command_without_speech(self):
        turn, _ = self.send(response=ok())
        pg = page(
            [user_row(turn, nativeInputAmbiguous=True), assistant_row("gen-1", ANSWER)]
        )
        reply = self.poll(turn, pg)
        self.assertEqual(
            (reply.status, reply.reason, reply.text, reply.terminal),
            ("not_spoken", "ambiguous_input_identity", None, True),
        )
        self.assertEqual(self.record(turn)["state"], "COMPLETE")
        self.assertTrue(self.bridge.begin_turn())

    def test_an_unresolved_later_input_keeps_the_command_pending_then_it_can_speak(
        self,
    ):
        turn, _ = self.send(response=ok())
        later = {"id": "u9", "role": "user", "text": "next"}
        blocked = page([user_row(turn), assistant_row("gen-1", ANSWER), later])
        reply = self.poll(turn, blocked)
        self.assertEqual(
            (reply.status, reply.reason, reply.terminal, reply.text),
            ("pending", "later_input_unresolved", False, None),
        )
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")
        resolved = page(
            [
                user_row(turn),
                assistant_row("gen-1", ANSWER),
                user_row("00000000-0000-4000-8000-0000000000dd", generation="gen-2"),
            ]
        )
        spoken = self.poll(turn, resolved)
        self.assertEqual((spoken.status, spoken.text), ("speak", ANSWER))

    def test_a_failing_directory_listing_is_a_bridge_error_not_a_raw_oserror(self):
        marker = "/private/path-marker-4410"
        with mock.patch(
            "pet_bridge.journal.os.listdir", side_effect=OSError(5, marker)
        ):
            err = raised(self.bridge.begin_turn)
            self.assertIsInstance(err, BridgeError)
            self.assertEqual(getattr(err, "code", None), "journal_error")
            self.assertNotIn(marker, str(err) + repr(err))
            self.assertIsInstance(raised(self.bridge.cleanup), BridgeError)
            status = self.bridge.status()  # never raises, reports no records
        self.assertEqual(status["journal_records"], -1)
