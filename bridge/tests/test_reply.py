"""Reply correlation: only the exact finished generation of our own input is spoken."""
# pyright: reportArgumentType=false, reportOptionalMemberAccess=false, reportOptionalSubscript=false
# (tests deliberately pass invalid types and read Optionals right after asserting them)

from __future__ import annotations

import hashlib
from dataclasses import replace

from pet_bridge.companion import detail_window
from pet_bridge.reply import MAX_REPLY_CHARS, ReplyDecision, evaluate_reply, input_delivered

from .test_companion import ATT, DetailServer

from .fakes import (
    OTHER_SESSION,
    SESSION,
    OfflineTestCase,
    assistant_row,
    page,
    user_row,
)

CMD = "00000000-0000-4000-8000-000000000001"
OTHER_CMD = "00000000-0000-4000-8000-000000000002"


class ReplyCase(OfflineTestCase):
    def evaluate(
        self, pg, *, coverage=True, session=SESSION, command=CMD
    ) -> ReplyDecision:
        return evaluate_reply(
            pg, command_id=command, session_id=session, coverage_complete=coverage
        )

    def assertNotSpoken(
        self, decision: ReplyDecision, status: str, reason: str, terminal=None
    ):
        self.assertEqual((decision.status, decision.reason), (status, reason))
        self.assertIsNone(decision.text)
        if terminal is not None:
            self.assertEqual(decision.terminal, terminal)


class CompletedReplyTest(ReplyCase):
    def test_completed_exact_reply_is_spoken(self):
        d = self.evaluate(page([user_row(CMD), assistant_row("gen-1", "The answer.")]))
        self.assertEqual(
            (d.status, d.reason, d.text, d.terminal, d.generation_id),
            ("speak", "reply_complete", "The answer.", True, "gen-1"),
        )

    def test_text_is_hidden_from_repr(self):
        d = self.evaluate(
            page([user_row(CMD), assistant_row("gen-1", "private answer")])
        )
        self.assertNotIn("private answer", repr(d) + str(d))

    def test_generation_may_come_from_row_when_status_lacks_it(self):
        own = user_row(CMD, generation=None, generationId="gen-1")
        d = self.evaluate(page([own, assistant_row("gen-1", "ok")]))
        self.assertEqual(d.status, "speak")

    def test_multiple_assistant_rows_of_the_generation_are_joined_in_page_order(self):
        d = self.evaluate(
            page(
                [
                    user_row(CMD),
                    assistant_row("gen-1", "One."),
                    assistant_row("gen-1", "  "),
                    assistant_row("gen-1", "Two."),
                ]
            )
        )
        self.assertEqual(d.text, "One.\n\nTwo.")

    def test_length_boundary(self):
        d = self.evaluate(
            page([user_row(CMD), assistant_row("gen-1", "a" * MAX_REPLY_CHARS)])
        )
        self.assertEqual(d.status, "speak")
        d = self.evaluate(
            page([user_row(CMD), assistant_row("gen-1", "a" * (MAX_REPLY_CHARS + 1))])
        )
        self.assertNotSpoken(d, "not_spoken", "long_reply_not_spoken", True)
        parts = [assistant_row("gen-1", "a" * 2500), assistant_row("gen-1", "b" * 2500)]
        self.assertEqual(
            self.evaluate(page([user_row(CMD), *parts])).reason, "long_reply_not_spoken"
        )


class DecoyAndCorrelationTest(ReplyCase):
    def test_assistant_input_id_equal_to_command_id_is_not_used(self):
        decoy = assistant_row("gen-other", "decoy", inputId=CMD)
        d = self.evaluate(page([user_row(CMD), decoy]))
        self.assertNotSpoken(d, "pending", "reply_row_not_visible", False)

    def test_assistant_without_generation_or_with_other_generation_is_ignored(self):
        rows = [
            user_row(CMD),
            assistant_row(None, "no gen"),
            assistant_row("gen-2", "other run"),
            assistant_row("gen-1", "real"),
        ]
        self.assertEqual(self.evaluate(page(rows)).text, "real")

    def test_latest_assistant_is_never_a_fallback(self):
        d = self.evaluate(page([user_row(CMD), assistant_row("gen-2", "latest")]))
        self.assertIsNone(d.text)
        self.assertEqual(d.status, "pending")

    def test_text_and_order_matching_never_bind(self):
        d = self.evaluate(
            page(
                [
                    assistant_row("gen-1", "hello"),
                    user_row(OTHER_CMD, generation="gen-9"),
                ]
            )
        )
        self.assertNotSpoken(d, "pending", "user_row_not_visible", False)

    def test_other_users_row_with_same_inputid_but_assistant_role_is_not_ours(self):
        d = self.evaluate(page([assistant_row("gen-1", "x", inputId=CMD)]))
        self.assertEqual(d.reason, "user_row_not_visible")

    def test_missing_own_row_is_pending_without_fallback(self):
        d = self.evaluate(page([assistant_row("gen-1", "orphan")]))
        self.assertNotSpoken(d, "pending", "user_row_not_visible", False)

    def test_duplicate_own_rows_are_ambiguous(self):
        d = self.evaluate(
            page([user_row(CMD), user_row(CMD), assistant_row("gen-1", "x")])
        )
        self.assertNotSpoken(d, "not_spoken", "ambiguous_user_row", True)

    def test_shared_generation_by_status_or_by_row_generation(self):
        steering_peer = user_row(OTHER_CMD, generation="gen-1")
        d = self.evaluate(
            page([user_row(CMD), steering_peer, assistant_row("gen-1", "mixed")])
        )
        self.assertNotSpoken(d, "not_spoken", "shared_generation", True)
        peer = user_row(OTHER_CMD, generation=None, generationId="gen-1")
        d = self.evaluate(page([user_row(CMD), peer, assistant_row("gen-1", "mixed")]))
        self.assertEqual(d.reason, "shared_generation")
        no_id = {
            "id": "u2",
            "role": "user",
            "text": "x",
            "inputStatus": {
                "version": 1,
                "inputId": "z",
                "phase": "done",
                "basis": "generation_finished",
                "generationId": "gen-1",
            },
        }
        d = self.evaluate(page([user_row(CMD), no_id, assistant_row("gen-1", "mixed")]))
        self.assertEqual(d.reason, "shared_generation")

    def test_user_row_of_a_different_generation_does_not_block(self):
        d = self.evaluate(
            page(
                [
                    user_row(OTHER_CMD, generation="gen-0"),
                    user_row(CMD),
                    assistant_row("gen-1", "ok"),
                ]
            )
        )
        self.assertEqual(d.status, "speak")

    def test_steering_row_is_never_spoken(self):
        own = user_row(
            CMD, steering={"generationId": "gen-1", "disposition": "applied"}
        )
        d = self.evaluate(page([own, assistant_row("gen-1", "x")]))
        self.assertNotSpoken(d, "not_spoken", "steering_not_spoken", True)

    def test_foreign_session_page_or_row(self):
        d = self.evaluate(
            page([user_row(CMD), assistant_row("gen-1", "x")], sessionId=OTHER_SESSION)
        )
        self.assertNotSpoken(d, "not_spoken", "foreign_session", False)
        rows = [user_row(CMD), assistant_row("gen-1", "x", sessionId=OTHER_SESSION)]
        self.assertEqual(self.evaluate(page(rows)).reason, "foreign_session")
        rows = [user_row(CMD), assistant_row("gen-1", "x", sessionId=SESSION)]
        self.assertEqual(self.evaluate(page(rows, sessionId=SESSION)).status, "speak")


class CoverageTest(ReplyCase):
    def test_caller_must_assert_coverage(self):
        rows = [user_row(CMD), assistant_row("gen-1", "x")]
        for claim in (False, None, 1, "yes"):
            self.assertNotSpoken(
                self.evaluate(page(rows), coverage=claim),
                "pending",
                "coverage_incomplete",
                False,
            )

    def test_page_must_start_at_zero_and_be_complete(self):
        rows = [user_row(CMD), assistant_row("gen-1", "x")]
        self.assertEqual(
            self.evaluate(page(rows, offset=5, total=7)).reason, "pagination_required"
        )
        self.assertEqual(
            self.evaluate(page(rows, offset=0, total=9)).reason, "pagination_required"
        )
        self.assertEqual(
            self.evaluate(page(rows, total=1)).reason, "pagination_required"
        )

    def test_partial_window_with_competing_user_row_off_window_cannot_be_misjudged(
        self,
    ):
        d = self.evaluate(
            page([user_row(CMD), assistant_row("gen-1", "x")], offset=0, total=3)
        )
        self.assertEqual((d.status, d.text), ("pending", None))

    def test_malformed_pages(self):
        for bad in (
            None,
            [],
            "x",
            {},
            {"items": "x", "offset": 0, "total": 0},
            {"items": [], "offset": True, "total": 0},
            {"items": [], "offset": 0, "total": "0"},
            {"items": [1], "offset": 0, "total": 1},
        ):
            d = self.evaluate(bad)
            self.assertNotSpoken(d, "not_spoken", "page_invalid", False)


class NewestTailReplyTest(ReplyCase):
    def window(self, rows, *, complete=False):
        # Four capped pages; no earlier row is silently added to the requested
        # test window. Padding is strictly AFTER the supplied rows.
        rows = [*rows]
        while len(rows) % 4:
            rows.append({"id": f"padding-{len(rows)}", "role": "tool"})
        prefix = [] if complete else [
            {"id": f"old-{i}", "role": "tool"} for i in range(400)
        ]
        return detail_window(
            DetailServer(prefix + rows, limit=len(rows) // 4).client(), ATT, SESSION
        )

    def decide(self, rows, *, complete=False):
        window = self.window(rows, complete=complete)
        self.assertEqual(window.coverage_complete, complete)
        return evaluate_reply(
            window.as_page(), command_id=CMD, session_id=SESSION,
            coverage_complete=window.coverage_complete, newest_tail=window,
        )

    def bounded(self, *rows):
        return [assistant_row("gen-0", "older"), user_row(CMD), *rows]

    def test_older_row_or_user_status_generation_bounds_the_run(self):
        for older in (
            assistant_row("gen-0", "old"),
            user_row(OTHER_CMD, generation="gen-0"),
            {"id": "old-tool", "role": "tool", "generationId": "gen-0"},
        ):
            with self.subTest(older=older):
                d = self.decide([older, user_row(CMD), assistant_row("gen-1", "answer")])
                self.assertEqual((d.status, d.text), ("speak", "answer"))

    def test_offset_zero_needs_no_older_generation(self):
        rows = [user_row(CMD), assistant_row("gen-1", "answer")]
        self.assertEqual(self.decide(rows, complete=True).status, "speak")
        window = self.window(rows, complete=True)
        # Tail evidence is independently sufficient at offset zero.
        d = evaluate_reply(
            window.as_page(), command_id=CMD, session_id=SESSION,
            coverage_complete=False, newest_tail=window,
        )
        self.assertEqual(d.status, "speak")

    def test_own_row_oldest_stays_pending_even_with_later_other_generation(self):
        d = self.decide([
            user_row(CMD), assistant_row("gen-1", "answer"), assistant_row("gen-0", "later")
        ])
        self.assertNotSpoken(d, "pending", "generation_boundary_not_visible", False)

    def test_only_same_generation_or_generationless_older_rows_stay_pending(self):
        for generation in ("gen-1", None, "", 1, "g" * 201):
            for older in (
                assistant_row(generation, "old"),
                user_row(OTHER_CMD, generation=generation),
                # Only a USER's inputStatus can prove a boundary.
                {"id": "not-user", "role": "tool",
                 "inputStatus": {"generationId": "gen-0"}},
            ):
                with self.subTest(older=older):
                    d = self.decide([older, user_row(CMD), assistant_row("gen-1", "x")])
                    self.assertNotSpoken(d, "pending", "generation_boundary_not_visible", False)

    def test_shared_generation_inside_window_still_rejects(self):
        for peer in (
            user_row(OTHER_CMD),
            user_row(OTHER_CMD, generation=None, generationId="gen-1"),
        ):
            for rows in (
                [peer, user_row(CMD)],
                [user_row(CMD), peer],
            ):
                d = self.decide([
                    assistant_row("gen-0", "old"), *rows, assistant_row("gen-1", "mixed")
                ])
                self.assertNotSpoken(d, "not_spoken", "shared_generation", True)

    def test_ambiguous_or_unresolved_later_input_still_blocks(self):
        for later, status, reason in (
            (user_row(OTHER_CMD, generation="gen-2", nativeInputAmbiguous=True),
             "not_spoken", "ambiguous_later_input"),
            (user_row(OTHER_CMD, generation=None), "pending", "later_input_unresolved"),
            (user_row(OTHER_CMD, generation="gen-2", generationId="gen-3"),
             "pending", "later_input_unresolved"),
        ):
            d = self.decide(self.bounded(assistant_row("gen-1", "x"), later))
            self.assertNotSpoken(d, status, reason)

    def test_own_row_absent_never_uses_latest_assistant(self):
        d = self.decide([user_row(OTHER_CMD), assistant_row("gen-1", "orphan")])
        self.assertNotSpoken(d, "pending", "user_row_not_visible", False)

    def test_own_identity_and_finished_status_still_required(self):
        for own, status, reason in (
            (user_row(CMD, nativeInputAmbiguous=True), "not_spoken", "ambiguous_input_identity"),
            (user_row(CMD, nativeInputId=OTHER_CMD), "not_spoken", "ambiguous_input_identity"),
            (user_row(CMD, phase="working"), "pending", "generation_running"),
            (user_row(CMD, basis="human_decision"), "not_spoken", "completion_ambiguous"),
            (user_row(CMD, steering={}), "not_spoken", "steering_not_spoken"),
        ):
            d = self.decide([assistant_row("gen-0", "old"), own, assistant_row("gen-1", "x")])
            self.assertNotSpoken(d, status, reason)
        d = self.decide(self.bounded(user_row(CMD, id="duplicate")))
        self.assertNotSpoken(d, "not_spoken", "ambiguous_user_row", True)

    def test_text_and_foreign_session_rules_still_apply(self):
        for row, reason in (
            (assistant_row("gen-1", "a" * 4001), "long_reply_not_spoken"),
            (assistant_row("gen-1", "short", textDetail={}), "long_reply_not_spoken"),
            (assistant_row("gen-1", 5), "reply_invalid"),
            (assistant_row("gen-1", " "), "no_reply_text"),
            (assistant_row("gen-1", "foreign", sessionId=OTHER_SESSION), "foreign_session"),
        ):
            d = self.decide(self.bounded(row))
            self.assertEqual(d.reason, reason)
            self.assertIsNone(d.text)
        d = self.decide(self.bounded(
            assistant_row("gen-1", "a" * 2500), assistant_row("gen-1", "b" * 2500)
        ))
        self.assertEqual(d.reason, "long_reply_not_spoken")
        d = self.decide(self.bounded(assistant_row("gen-other", "decoy", inputId=CMD)))
        self.assertNotSpoken(d, "pending", "reply_row_not_visible", False)

    def test_tail_reply_parts_join_without_latest_fallback_at_4000_char_boundary(self):
        d = self.decide(self.bounded(
            assistant_row("gen-1", "a" * 1999),
            assistant_row("gen-1", " "),
            assistant_row("gen-1", "b" * 1999),
            assistant_row("gen-other", "latest decoy"),
        ))
        self.assertEqual(d.status, "speak")
        self.assertEqual(d.text, "a" * 1999 + "\n\n" + "b" * 1999)
        self.assertEqual(len(d.text), 4000)

    def test_tail_evidence_cannot_be_asserted_copied_to_another_page_or_mutated(self):
        rows = self.bounded(assistant_row("gen-1", "answer"))
        window = self.window(rows)
        for evidence in (None, True, {"newest_tail": True}, replace(window)):
            d = evaluate_reply(
                window.as_page(), command_id=CMD, session_id=SESSION,
                coverage_complete=False, newest_tail=evidence,
            )
            self.assertNotSpoken(d, "pending", "coverage_incomplete", False)
        pg = window.as_page()
        pg["items"][1]["inputStatus"]["generationId"] = "tampered"
        self.assertFalse(window.proves_newest_tail(pg, SESSION))
        self.assertFalse(window.proves_newest_tail(window.as_page(), OTHER_SESSION))
        self.assertNotIn("answer", repr(window))

    def test_tail_evidence_is_bound_to_metadata_order_and_nested_content(self):
        for mutate in (
            lambda pg: pg.update(offset=0),
            lambda pg: pg.update(total=999),
            lambda pg: pg["items"].reverse(),
            lambda pg: pg["items"].pop(),
            lambda pg: pg["items"][-1].update(text="substituted"),
        ):
            window = self.window(self.bounded(assistant_row("gen-1", "answer")))
            pg = window.as_page()
            mutate(pg)
            d = evaluate_reply(
                pg, command_id=CMD, session_id=SESSION,
                coverage_complete=False, newest_tail=window,
            )
            self.assertNotSpoken(d, "pending", "coverage_incomplete", False)

    def test_tail_evidence_from_another_session_cannot_be_used(self):
        window = self.window(self.bounded(assistant_row("gen-1", "answer")))
        d = evaluate_reply(
            window.as_page(), command_id=CMD, session_id=OTHER_SESSION,
            coverage_complete=False, newest_tail=window,
        )
        self.assertNotSpoken(d, "pending", "coverage_incomplete", False)

    def test_delivery_positive_proof_needs_neither_full_history_nor_generation_boundary(self):
        window = self.window([user_row(CMD), assistant_row("gen-1", "answer")])
        self.assertFalse(window.coverage_complete)
        self.assertTrue(input_delivered(
            window.as_page(), command_id=CMD,
            text_sha256=hashlib.sha256(b"hello").hexdigest(),
        ))


class GenerationStateTest(ReplyCase):
    def decide(self, **status):
        rows = [user_row(CMD, **status), assistant_row("gen-1", "partial")]
        return self.evaluate(page(rows))

    def test_running_states_wait(self):
        for phase, basis in (
            ("queued", "steering_queued"),
            ("included", "steering_context"),
            ("working", "generation_started"),
            ("working", "execution_observed"),
        ):
            d = self.decide(phase=phase, basis=basis)
            self.assertNotSpoken(d, "pending", "generation_running", False)

    def test_waiting_workers_waits_and_is_not_success(self):
        d = self.decide(phase="waiting", basis="waiting_workers")
        self.assertNotSpoken(d, "pending", "waiting_workers", False)

    def test_failed_is_terminal_failure_without_speech(self):
        d = self.decide(phase="failed", basis="generation_failed")
        self.assertNotSpoken(d, "not_spoken", "generation_failed", True)

    def test_unknown_states(self):
        d = self.decide(phase="unknown", basis="no_evidence")
        self.assertNotSpoken(d, "pending", "no_evidence", False)
        d = self.decide(phase="unknown", basis="generation_detached")
        self.assertNotSpoken(d, "not_spoken", "generation_unknown", True)

    def test_done_requires_generation_finished_basis(self):
        d = self.decide(phase="done", basis="human_decision")
        self.assertNotSpoken(d, "not_spoken", "completion_ambiguous", True)

    def test_invalid_status_shapes(self):
        for status in (
            None,
            "done",
            {},
            {
                "version": 2,
                "inputId": CMD,
                "phase": "done",
                "basis": "generation_finished",
                "generationId": "g",
            },
            {
                "version": 1,
                "inputId": OTHER_CMD,
                "phase": "done",
                "basis": "generation_finished",
                "generationId": "g",
            },
            {
                "version": True,
                "inputId": CMD,
                "phase": "done",
                "basis": "generation_finished",
                "generationId": "g",
            },
            {
                "version": 1,
                "inputId": CMD,
                "phase": "bogus",
                "basis": "generation_finished",
                "generationId": "g",
            },
            {"version": 1, "inputId": CMD, "phase": 5, "basis": "x"},
        ):
            own = user_row(CMD)
            own["inputStatus"] = status
            d = self.evaluate(page([own, assistant_row("gen-1", "x")]))
            self.assertNotSpoken(d, "not_spoken", "status_invalid", True)

    def test_missing_or_conflicting_generation(self):
        own = user_row(CMD, generation=None)
        self.assertNotSpoken(
            self.evaluate(page([own, assistant_row("gen-1", "x")])),
            "not_spoken",
            "no_generation",
            True,
        )
        own = user_row(CMD, generation="gen-1", generationId="gen-2")
        self.assertEqual(
            self.evaluate(page([own, assistant_row("gen-1", "x")])).reason,
            "ambiguous_generation",
        )
        own = user_row(CMD, generation="")
        self.assertEqual(self.evaluate(page([own])).reason, "no_generation")


class ReplyTextTest(ReplyCase):
    def test_truncated_text_detail_is_not_spoken(self):
        row = assistant_row(
            "gen-1",
            "a" * 4096,
            textDetail={"sessionId": SESSION, "field": "text", "length": 9000},
        )
        d = self.evaluate(page([user_row(CMD), row]))
        self.assertNotSpoken(d, "not_spoken", "long_reply_not_spoken", True)

    def test_text_detail_marker_alone_blocks_speech_even_for_short_text(self):
        row = assistant_row("gen-1", "short but marked", textDetail={"length": 5000})
        d = self.evaluate(page([user_row(CMD), row]))
        self.assertNotSpoken(d, "not_spoken", "long_reply_not_spoken", True)

    def test_missing_blank_or_non_string_reply_text(self):
        d = self.evaluate(page([user_row(CMD), assistant_row("gen-1", "   ")]))
        self.assertNotSpoken(d, "not_spoken", "no_reply_text", True)
        row = assistant_row("gen-1", "x")
        row["text"] = 5
        self.assertEqual(
            self.evaluate(page([user_row(CMD), row])).reason, "reply_invalid"
        )
        row = assistant_row("gen-1", "x")
        del row["text"]
        self.assertEqual(
            self.evaluate(page([user_row(CMD), row])).reason, "reply_invalid"
        )
        d = self.evaluate(page([user_row(CMD), assistant_row("gen-1", "\ud800")]))
        self.assertEqual(d.reason, "reply_invalid")


OLDER_CMD = "00000000-0000-4000-8000-000000000003"
LATER_CMD = "00000000-0000-4000-8000-000000000004"


def bare_user(**row):
    """A user row with no inputStatus, like an input the host has not annotated."""
    base = {"id": "u-bare", "role": "user", "text": "x"}
    base.update(row)
    return base


class OwnIdentityTest(ReplyCase):
    """R6: our own user row must have an unambiguous native identity."""

    def own_page(self, **extra):
        return page([user_row(CMD, **extra), assistant_row("gen-1", "ok")])

    def test_native_input_ambiguous_on_the_own_row_rejects_even_with_a_valid_status(
        self,
    ):
        for flag in (True, 1, "true", "yes", [1]):
            d = self.evaluate(self.own_page(nativeInputAmbiguous=flag))
            self.assertNotSpoken(d, "not_spoken", "ambiguous_input_identity", True)

    def test_false_ambiguity_flag_is_the_same_as_absent(self):
        self.assertEqual(
            self.evaluate(self.own_page(nativeInputAmbiguous=False)).status, "speak"
        )

    def test_conflicting_native_input_id_on_the_own_row_rejects(self):
        for native in (OTHER_CMD, "x", 5, ""):
            d = self.evaluate(self.own_page(nativeInputId=native))
            self.assertNotSpoken(d, "not_spoken", "ambiguous_input_identity", True)

    def test_matching_native_input_id_is_fine(self):
        self.assertEqual(
            self.evaluate(self.own_page(nativeInputId=CMD)).status, "speak"
        )

    def test_a_decoy_row_claiming_our_command_as_its_native_id_rejects(self):
        decoy = bare_user(inputId=OTHER_CMD, nativeInputId=CMD)
        d = self.evaluate(page([user_row(CMD), decoy, assistant_row("gen-1", "ok")]))
        self.assertNotSpoken(d, "not_spoken", "ambiguous_input_identity", True)


class LaterInputTest(ReplyCase):
    """R6: a user row AFTER ours with an unresolved generation may mix replies."""

    def evaluate_with(self, later, *before):
        rows = [*before, user_row(CMD), assistant_row("gen-1", "ok"), later]
        return self.evaluate(page(rows))

    def test_a_later_user_row_without_any_generation_blocks_speech(self):
        for later in (
            bare_user(),
            bare_user(inputId=LATER_CMD),
            user_row(LATER_CMD, generation=None, phase="queued", basis="no_evidence"),
            user_row(
                LATER_CMD, generation=None, phase="working", basis="generation_started"
            ),
        ):
            d = self.evaluate_with(later)
            self.assertNotSpoken(d, "pending", "later_input_unresolved", False)

    def test_a_later_row_with_two_different_generations_is_unresolved(self):
        later = user_row(LATER_CMD, generation="gen-2", generationId="gen-3")
        d = self.evaluate_with(later)
        self.assertNotSpoken(d, "pending", "later_input_unresolved", False)

    def test_a_later_row_with_ambiguous_identity_blocks_speech_terminally(self):
        for later in (
            user_row(LATER_CMD, generation="gen-2", nativeInputAmbiguous=True),
            user_row(LATER_CMD, generation="gen-2", nativeInputId=OTHER_CMD),
            bare_user(inputId=LATER_CMD, nativeInputId=OTHER_CMD, generationId="gen-2"),
        ):
            d = self.evaluate_with(later)
            self.assertNotSpoken(d, "not_spoken", "ambiguous_later_input", True)

    def test_a_later_row_with_a_resolved_different_generation_does_not_block(self):
        later = user_row(LATER_CMD, generation="gen-2")
        self.assertEqual(self.evaluate_with(later).status, "speak")

    def test_a_later_row_sharing_the_generation_still_rejects(self):
        later = user_row(LATER_CMD, generation="gen-1")
        self.assertEqual(self.evaluate_with(later).reason, "shared_generation")

    def test_older_history_with_unknown_generation_does_not_block(self):
        for older in (
            bare_user(),
            user_row(OLDER_CMD, generation=None, phase="unknown", basis="no_evidence"),
            user_row(OLDER_CMD, generation="gen-0", nativeInputAmbiguous=True),
        ):
            rows = [older, user_row(CMD), assistant_row("gen-1", "ok")]
            self.assertEqual(self.evaluate(page(rows)).status, "speak")

    def test_only_rows_after_ours_count_even_when_the_assistant_row_comes_last(self):
        rows = [user_row(CMD), bare_user(), assistant_row("gen-1", "ok")]
        d = self.evaluate(page(rows))
        self.assertEqual(d.reason, "later_input_unresolved")

    def test_done_with_no_assistant_row_yet_stays_pending_not_terminal(self):
        d = self.evaluate(page([user_row(CMD)]))
        self.assertNotSpoken(d, "pending", "reply_row_not_visible", False)
        d = self.evaluate(page([user_row(CMD), assistant_row("gen-2", "other")]))
        self.assertNotSpoken(d, "pending", "reply_row_not_visible", False)

    def test_text_schema_cap_and_detail_remain_fail_closed(self):
        bad_text = assistant_row("gen-1", "x")
        bad_text["text"] = 5
        self.assertEqual(
            self.evaluate(page([user_row(CMD), bad_text])).reason, "reply_invalid"
        )
        long_text = assistant_row("gen-1", "a" * (MAX_REPLY_CHARS + 1))
        self.assertEqual(
            self.evaluate(page([user_row(CMD), long_text])).reason,
            "long_reply_not_spoken",
        )
        marked = assistant_row("gen-1", "short", textDetail={"length": 9000})
        self.assertEqual(
            self.evaluate(page([user_row(CMD), marked])).reason, "long_reply_not_spoken"
        )
