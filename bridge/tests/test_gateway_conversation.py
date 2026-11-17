"""Conversation mode, offline links and real core/journal flows; no live claims."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import threading
import unittest
from dataclasses import replace
from unittest import mock

from pet_bridge import companion, core
from pet_bridge import gateway_state as gs
from pet_bridge import gateway_provider as gp
from pet_bridge.companion import Preflight
from pet_bridge.journal import JournalError
from pet_bridge.transport import Response

from . import test_gateway_core as old
from . import test_gateway_flow as flow
from . import test_gateway_server as server
from .authfakes import INSTANCE_2
from .fakes import SESSION, IdFactory, assistant_row
from .test_companion import ATT
from .gatewayfakes import AUDIO_CANARY, REPLY_TEXT, TEXT_CANARY, TURN, OfflineAsyncCase

BEGIN = '{"type":"pet.converse.begin"}'
BEGAN = ('{"type":"pet.converse.began","safe_session_label":"Example conversation",'
         '"session_id_short":"a1b2c3d4"}')
STATES = (
    '{"type":"pet.converse.state","state":"listening"}',
    '{"type":"pet.converse.state","state":"asking"}',
    '{"type":"pet.converse.state","state":"answering"}',
)
NOTICES = (
    '{"type":"pet.converse.notice","code":"busy"}',
    '{"type":"pet.converse.notice","code":"not_sent"}',
    '{"type":"pet.converse.notice","code":"send_uncertain"}',
    '{"type":"pet.converse.notice","code":"reply_unavailable"}',
    '{"type":"pet.converse.notice","code":"idle_timeout"}',
    '{"type":"pet.converse.notice","code":"time_limit"}',
)


class GoldenTest(unittest.TestCase):
    def test_exact_design_golden_bytes(self):
        parsed = gs.parse_device_frame(BEGIN, model="gpt-live-1", voice="marin")
        self.assertEqual(parsed.kind, "converse_begin")
        self.assertEqual(gs.build_converse_began("Example conversation", "a1b2c3d4"), BEGAN)
        for raw in STATES:
            self.assertEqual(gs.build_converse_state(json.loads(raw)["state"]), raw)
        for raw in NOTICES:
            self.assertEqual(gs.build_converse_notice(json.loads(raw)["code"]), raw)

    def test_begin_rejects_every_extra_or_wrong_shape(self):
        for raw in ('{"type":"pet.converse.begin","extra":null}',
                    '{"type":"pet.converse.begin","state":"listening"}',
                    '{"type":"pet.converse.begin","type":"pet.converse.begin"}',
                    '{"type":["pet.converse.begin"]}', '"pet.converse.begin"'):
            with self.subTest(raw=raw), self.assertRaises(gs.GatewayProtocolError) as ctx:
                gs.parse_device_frame(raw, model="gpt-live-1", voice="marin")
            self.assertEqual(ctx.exception.code, "unknown_command")

    def test_builders_validate_without_echoing_values(self):
        for value in (None, [], 1, "PRIVATE-CANARY"):
            for builder in (gs.build_converse_state, gs.build_converse_notice):
                with self.assertRaises(ValueError) as ctx:
                    builder(value)
                self.assertNotIn("PRIVATE", str(ctx.exception))
        for label, short in (("", "a1b2c3d4"), ("OK", "SECRET")):
            with self.assertRaises(ValueError):
                gs.build_converse_began(label, short)


class ConversationCase(OfflineAsyncCase):
    async def asyncSetUp(self):
        self.h = old.Harness()
        self.addAsyncCleanup(self.h.aclose)
        await self.h.open()
        self.h.ops.results["begin"] = IdFactory(500)
        await self.h.boot()
        await self.h.feed(BEGIN)

    def states(self):
        return [f["state"] for f in self.h.device.of_type("pet.converse.state")]

    def notices(self):
        return [f["code"] for f in self.h.device.of_type("pet.converse.notice")]

    async def request(self, index=1):
        await self.h.push(old.delta(f"e{index}", f"request {index}", index * 1000, index * 1000 + 500))
        await self.h.push(old.delegation(f"dlg_{index}"))
        await self.h.clock.advance(1.5)

    async def render(self):
        # Audible output, then continuous digital silence, not packet quiet.
        self.h.provider.push_audio(AUDIO_CANARY)
        await self.h.quiesce()
        for _ in range(16):
            self.h.provider.push_audio(bytes(4800))
            await self.h.quiesce()
            await self.h.clock.advance(0.1)

    async def stop(self, frame='{"type":"pet.stop"}'):
        await self.h.feed(frame)
        result = await self.h.finish()
        await self.h.runner.wait_idle()
        self.assertEqual(result.final_state, "CLOSED")
        self.assertEqual(self.h.provider.close_calls, 1)
        self.assertEqual(self.h.device.close_codes, [1000])
        self.assertTrue(result.close_confirmed)
        self.assertFalse([t for t in self.h.session._tasks if not t.done()])
        self.assertEqual(self.h.session._timers, {})
        self.assertEqual(self.h.clock.pending, 0)
        self.assertEqual(self.h.ops.count("stop"), 1)
        return result


class ConversationTest(ConversationCase):
    async def test_provider_end_while_listening_has_expected_device_close(self):
        await self.h.push({"type": "session.closed", "reason": "expired"})
        result = await self.h.finish()
        self.assertEqual(self.h.device.close_codes, [1000])
        self.assertEqual(result.conversation_end, "provider_failure")
        self.assertFalse(result.close_confirmed)
        self.assertNotIn("session.closed", self.h.device.types())
        self.assertEqual(self.h.ops.count("confirm"), 0)

    async def test_safety_end_during_confirm_preserves_retained_outcome(self):
        gate = self.h.ops.hold("confirm")
        await self.request()
        await self.h.push({"type": "session.closed", "reason": "content"})
        result = await self.h.finish()
        self.assertEqual(self.h.device.close_codes, [1008])
        self.assertEqual(result.send_outcome, "inflight")
        self.assertEqual((result.requests_submitted, result.requests_sent), (1, 0))
        self.assertFalse(result.close_confirmed)
        self.assertNotIn("session.closed", self.h.device.types())
        gate.set()
        await self.h.runner.wait_idle()
        self.assertEqual(self.h.runner.take_result(result.confirm_job).outcome, "accepted")
        self.assertEqual(self.h.ops.count("confirm"), 1)

    async def test_provider_remote_hangup_during_speaking_never_replays(self):
        await self.request()
        await self.h.clock.advance(1)
        self.h.provider.push_audio(AUDIO_CANARY)
        await self.h.quiesce()
        await self.h.push({"type": "session.closed", "reason": "remote_hangup"})
        result = await self.h.finish()
        self.assertEqual(self.h.device.close_codes, [1000])
        self.assertEqual(result.requests_accepted, 1)
        self.assertEqual(self.h.ops.count("confirm"), 1)
        self.assertFalse(result.close_confirmed)
        self.assertNotIn("session.closed", self.h.device.types())

    async def test_batched_transcript_delegation_and_tail_share_the_settle_window(self):
        self.h.provider.push(old.delta("a", "hello ", 0, 500))
        self.h.provider.push(old.delegation("d"))
        self.h.provider.push(old.delta("b", "world", 500, 1000))
        await self.h.quiesce()
        await self.h.clock.advance(1.5)
        self.assertEqual(self.h.ops.staged, [("a", "hello "), ("b", "world")])
        self.assertEqual(self.h.ops.count("confirm"), 1)
        await self.stop()

    async def test_receive_stamped_pending_delegation_stays_unsent_after_render_timer(self):
        await self.request()
        await self.h.clock.advance(1)
        await self.render()
        seq = self.h.session._request_seq
        # A frame already received in the prior answering window, dispatched
        # after an internal render timer changed the state to listening.
        event = gp.ProviderEvent("delegation", delegation_id="queued_pending")
        self.h.session._post_external(("provider", event, None, (seq - 1, "SPEAKING", True, True)))
        await self.h.quiesce()
        await self.h.clock.advance(2)
        self.assertEqual(self.h.ops.count("confirm"), 1)
        self.assertEqual(self.states()[-1], "listening")
        await self.stop()

    async def test_conflicting_current_transcript_fails_closed_without_send(self):
        await self.h.push(old.delta("same", "hello", 0, 500))
        await self.h.push(old.delta("same", "different", 0, 500))
        result = await self.h.finish()
        self.assertEqual(result.error_code, "unstable_transcript")
        self.assertEqual(result.requests_sent, 0)

    async def test_long_utterance_never_forces_a_truncated_send_at_settle_limit(self):
        await self.h.push(old.delegation("d"))
        for index in range(6):
            await self.h.push(old.delta(f"e{index}", "words ", index * 1000, index * 1000 + 500))
            await self.h.clock.advance(1)
        self.assertEqual(self.h.ops.count("confirm"), 0)
        self.assertEqual(self.notices(), [])
        await self.h.clock.advance(0.5)
        self.assertEqual(self.h.ops.count("confirm"), 1)
        self.assertEqual("".join(t for _, t in self.h.ops.staged), "words " * 6)
        await self.stop()

    async def test_delegation_without_words_is_not_sent(self):
        await self.h.push(old.delegation("d"))
        await self.h.clock.advance(1.5)
        self.assertEqual(self.notices(), ["not_sent"])
        self.assertEqual(self.h.ops.count("begin"), 0)
        await self.render()
        await self.stop()

    async def test_two_requests_both_replies_rendered_without_preview_or_tap(self):
        for index in (1, 2):
            await self.request(index)
            self.assertEqual(self.states()[-1], "asking")
            await self.h.clock.advance(1.0)
            self.assertEqual(self.states()[-1], "answering")
            await self.render()
            self.assertEqual(self.states()[-1], "listening")
        result = await self.stop()
        self.assertEqual((result.requests_sent, result.requests_accepted,
                          result.requests_uncertain, result.requests_refused,
                          result.replies_rendered), (2, 2, 0, 0, 2))
        self.assertEqual(result.conversation_end, "stopped")
        self.assertEqual(self.h.ops.count("confirm"), 2)
        for op in ("begin", "stage", "seal", "preflight", "poll"):
            self.assertEqual(self.h.ops.count(op), 2)
        turns = [args[0] for name, args, _ in self.h.ops.calls if name == "confirm"]
        self.assertEqual(len(set(turns)), 2)
        staged = [args[1] for name, args, _ in self.h.ops.calls if name == "stage"]
        self.assertEqual(staged, [(("e1", "request 1"),), (("e2", "request 2"),)])
        self.assertEqual([f["content"] for f in self.h.provider.of_type("session.commentary.append")],
                         [REPLY_TEXT, REPLY_TEXT])
        self.assertFalse([t for t in self.h.device.types() if t.startswith(("pet.turn.", "pet.reply."))])

    async def test_busy_target_notice_no_send_then_new_request_can_work(self):
        self.h.ops.results["preflight"] = Preflight(SESSION, 9, False, "busy")
        await self.request()
        self.assertEqual(self.notices(), ["busy"])
        self.assertEqual(self.h.ops.count("confirm"), 0)
        self.assertEqual(self.h.ops.count("abandon"), 1)
        await self.render()
        self.h.ops.results.pop("preflight")
        await self.request(2)
        self.assertEqual(self.h.ops.count("confirm"), 1)
        result = await self.stop()
        self.assertEqual(result.requests_refused, 1)

    async def test_refused_send_fixed_notice_then_listen_never_replay(self):
        self.h.ops.results["confirm"] = core.CommandResult(TURN, "refused", "binding_mismatch", False)
        await self.request()
        self.assertEqual(self.notices(), ["not_sent"])
        await self.render()
        await self.h.push(old.delegation("dlg_1"))
        await self.h.clock.advance(2)
        self.assertEqual(self.h.ops.count("confirm"), 1)
        result = await self.stop()
        self.assertEqual((result.requests_sent, result.requests_refused), (0, 1))
        self.assertEqual(result.requests_submitted, 1)

    async def test_uncertain_send_notice_no_retry(self):
        self.h.ops.results["confirm"] = core.CommandResult(TURN, "uncertain", "transport_exception", False)
        await self.request()
        self.assertEqual(self.notices(), ["send_uncertain"])
        await self.render()
        await self.h.push(old.delta("e1", "request 1", 1000, 1500))
        await self.h.push(old.delegation("dlg_1"))
        await self.h.clock.advance(5)
        result = await self.stop()
        self.assertEqual((result.requests_uncertain, self.h.ops.count("confirm")), (1, 1))
        self.assertEqual(self.h.ops.count("poll"), 0)

    async def test_reply_unavailable_fixed_spoken_line_then_listening(self):
        self.h.ops.results["poll"] = lambda turn: core.ReplyResult(turn, "not_spoken", "reply_invalid", True, 3)
        await self.request()
        await self.h.clock.advance(1)
        self.assertEqual(self.notices(), ["reply_unavailable"])
        self.assertEqual(self.h.provider.of_type("session.commentary.append")[-1]["content"],
                         "No reply is available yet.")
        await self.render()
        self.assertEqual(self.states()[-1], "listening")
        result = await self.stop()
        self.assertEqual((result.requests_accepted, result.replies_rendered), (1, 0))

    async def test_delegation_pending_and_answering_never_queues_a_send(self):
        await self.request()
        await self.h.push(old.delta("ignored", "do something else", 2000, 2500))
        await self.h.push(old.delegation("dlg_pending"))
        await self.h.clock.advance(1)
        await self.h.push(old.delegation("dlg_answering"))
        await self.render()
        await self.h.push(old.delegation("dlg_pending"))
        await self.h.clock.advance(3)
        self.assertEqual(self.h.ops.count("confirm"), 1)
        result = await self.stop()
        self.assertEqual(result.requests_refused, 2)
        self.assertEqual(result.replies_rendered, 1)

    async def test_quiet_settle_includes_trailing_words_and_whitespace(self):
        await self.h.push(old.delta("a", "hello", 0, 500))
        await self.h.push(old.delegation("d"))
        await self.h.clock.advance(1)
        await self.h.push(old.delta("space", " ", 500, 510))
        await self.h.push(old.delta("b", "world", 510, 1000))
        await self.h.clock.advance(1.4)
        self.assertEqual(self.h.ops.count("confirm"), 0)
        await self.h.clock.advance(0.1)
        self.assertEqual(self.h.ops.staged, [("a", "hello"), ("space", " "), ("b", "world")])
        self.assertEqual(self.h.ops.count("confirm"), 1)
        await self.stop()

    async def test_idle_90_seconds_audio_and_silence_do_not_extend_it(self):
        await self.h.clock.advance(89)
        await self.h.feed(old.audio_frame(100))
        self.h.provider.push_audio(bytes(4800))
        await self.h.quiesce()
        self.assertFalse(self.h.task.done())
        await self.h.clock.advance(1)
        result = await self.h.finish()
        self.assertEqual(self.notices(), ["idle_timeout"])
        self.assertEqual(result.conversation_end, "idle_timeout")
        self.assertTrue(result.close_confirmed)
        self.assertEqual(self.h.device.close_codes, [1000])
        self.assertEqual(self.h.clock.pending, 0)

    async def test_recognised_speech_resets_idle_even_without_delegation(self):
        await self.h.clock.advance(80)
        await self.h.push(old.delta("e", "hello", 0, 100))
        await self.h.clock.advance(89.9)
        self.assertFalse(self.h.task.done())
        await self.h.clock.advance(0.1)
        self.assertEqual((await self.h.finish()).conversation_end, "idle_timeout")

    async def test_total_600_seconds_normal_close_despite_frequent_speech(self):
        for index in range(7):
            await self.h.clock.advance(80)
            await self.h.push(old.delta(f"e{index}", "hi", index * 1000, index * 1000 + 500))
        await self.h.clock.advance(37.9)
        self.assertFalse(self.h.task.done())
        await self.h.clock.advance(0.1)
        result = await self.h.finish()
        self.assertEqual(self.notices(), ["time_limit"])
        self.assertEqual(result.conversation_end, "time_limit")
        self.assertTrue(result.close_confirmed)
        self.assertEqual(self.h.device.close_codes, [1000])
        self.assertEqual(self.h.clock.pending, 0)

    async def test_total_cap_includes_unconfirmed_close_cleanup(self):
        self.h.provider.auto_close = False
        for index in range(7):
            await self.h.clock.advance(80)
            await self.h.push(old.delta(f"e{index}", "hi", index * 1000, index * 1000 + 500))
        await self.h.clock.advance(38)
        self.assertEqual(self.notices(), ["time_limit"])
        self.assertFalse(self.h.task.done())
        await self.h.clock.advance(2)
        result = await self.h.finish()
        self.assertFalse(result.close_confirmed)
        self.assertEqual(result.conversation_end, "time_limit")
        self.assertIn("close_unconfirmed", self.h.device.error_codes())
        self.assertEqual(self.h.clock.pending, 0)

    async def test_stop_listening(self):
        result = await self.stop()
        self.assertEqual(result.requests_sent, 0)

    async def test_session_close_is_mic_off(self):
        await self.stop('{"type":"session.close"}')

    async def test_stop_asking_never_cancels_or_replays(self):
        await self.request()
        result = await self.stop()
        self.assertEqual((result.requests_accepted, result.replies_rendered), (1, 0))
        self.assertEqual(self.h.ops.count("poll"), 0)
        self.assertNotIn("cancel", self.h.ops.names())

    async def test_stop_answering_suppresses_later_audio(self):
        await self.request()
        await self.h.clock.advance(1)
        self.h.provider.push_audio(AUDIO_CANARY)
        await self.h.quiesce()
        result = await self.stop()
        before = self.h.device.raw[:]
        self.h.provider.push_audio(AUDIO_CANARY)
        await self.h.clock.advance(3)
        self.assertEqual(before, self.h.device.raw)
        self.assertEqual(result.replies_rendered, 0)

    async def test_stop_during_confirm_preserves_retained_outcome_for_reconciliation(self):
        gate = self.h.ops.hold("confirm")
        await self.request()
        self.assertEqual(self.h.ops.count("confirm"), 1)
        await self.h.feed('{"type":"pet.stop"}')
        result = await self.h.finish()
        self.assertEqual(result.send_outcome, "inflight")
        self.assertEqual((result.requests_sent, result.requests_submitted), (0, 1))
        gate.set()
        await self.h.runner.wait_idle()
        settled = self.h.runner.take_result(result.confirm_job)
        self.assertEqual(settled.outcome, "accepted")
        self.assertEqual(self.h.ops.count("confirm"), 1)

    async def test_stop_during_preflight_proves_no_send(self):
        gate = self.h.ops.hold("preflight")
        await self.request()
        await self.h.feed('{"type":"pet.stop"}')
        result = await self.h.finish()
        gate.set()
        await self.h.quiesce()
        self.assertEqual((result.requests_refused, result.requests_sent), (1, 0))
        self.assertEqual(self.h.ops.count("confirm"), 0)

    async def test_time_limit_during_confirm_closes_without_cancelling_accepted_work(self):
        gate = self.h.ops.hold("confirm")
        await self.request()
        await self.h.clock.advance(598.5)
        result = await self.h.finish()
        self.assertEqual(result.conversation_end, "time_limit")
        self.assertEqual(result.send_outcome, "inflight")
        self.assertTrue(result.close_confirmed)
        self.assertEqual(self.h.device.close_codes, [1000])
        gate.set()
        await self.h.runner.wait_idle()
        self.assertEqual(self.h.runner.take_result(result.confirm_job).outcome, "accepted")
        self.assertEqual(self.h.ops.count("confirm"), 1)
        self.assertEqual(self.h.ops.count("stop"), 1)
        self.assertEqual(self.h.clock.pending, 0)

    async def test_provider_failure_mid_conversation_never_replays_or_claims_close(self):
        await self.request()
        await self.h.clock.advance(1)
        self.h.provider.push_closed(clean=False)
        result = await self.h.finish()
        self.assertEqual(result.conversation_end, "provider_failure")
        self.assertFalse(result.close_confirmed)
        self.assertEqual(self.h.device.close_codes, [1011])
        self.assertEqual(self.h.ops.count("confirm"), 1)
        self.assertEqual(self.h.clock.pending, 0)

    async def test_protocol_failure_mid_inflight_send_preserves_reconciliation(self):
        gate = self.h.ops.hold("confirm")
        await self.request()
        await self.h.push("{invalid provider frame")
        result = await self.h.finish()
        self.assertEqual(self.h.device.close_codes, [1002])
        self.assertFalse(result.close_confirmed)
        self.assertEqual(result.send_outcome, "inflight")
        self.assertEqual((result.requests_submitted, result.requests_sent), (1, 0))
        gate.set()
        await self.h.runner.wait_idle()
        self.assertEqual(self.h.runner.take_result(result.confirm_job).outcome, "accepted")
        self.assertEqual(self.h.ops.count("confirm"), 1)

    async def test_foreign_provider_close_mid_inflight_send_preserves_reconciliation(self):
        gate = self.h.ops.hold("confirm")
        await self.request()
        await self.h.push({
            "type": "session.closed", "reason": "close_requested",
            "session": {
                "id": "live_other", "expires_at": 2000000000,
                "model": "gpt-live-1", "status": "active",
            },
        })
        result = await self.h.finish()
        self.assertEqual(self.h.device.close_codes, [1002])
        self.assertFalse(result.close_confirmed)
        self.assertEqual(result.send_outcome, "inflight")
        self.assertEqual((result.requests_submitted, result.requests_sent), (1, 0))
        gate.set()
        await self.h.runner.wait_idle()
        self.assertEqual(self.h.runner.take_result(result.confirm_job).outcome, "accepted")
        self.assertEqual(self.h.ops.count("confirm"), 1)

    async def test_turn_frames_after_converse_are_protocol_error(self):
        await self.h.feed('{"type":"pet.turn.begin"}')
        result = await self.h.finish()
        self.assertEqual(result.error_code, "unknown_command")
        self.assertEqual(self.h.device.close_codes, [1008])
        self.assertEqual(self.h.ops.count("begin"), 0)

    async def test_capture_stop_after_converse_is_protocol_error(self):
        await self.h.feed({"type": "pet.turn.capture_stop", "turn_id": TURN})
        result = await self.h.finish()
        self.assertEqual(result.error_code, "unknown_command")
        self.assertEqual(self.h.device.close_codes, [1008])

    async def test_send_confirm_after_converse_is_protocol_error(self):
        await self.h.feed({"type": "pet.turn.send_confirm", "turn_id": TURN,
                           "text_sha256": "0" * 64, "preview_revision": 1})
        result = await self.h.finish()
        self.assertEqual(result.error_code, "unknown_command")
        self.assertEqual(self.h.ops.count("confirm"), 0)
        self.assertEqual(self.h.device.close_codes, [1008])

    async def test_second_converse_begin_never_reopens_or_creates_a_turn(self):
        await self.h.feed(BEGIN)
        self.assertEqual((await self.h.finish()).error_code, "unknown_command")
        self.assertEqual(self.h.ops.count("begin"), 0)
        self.assertEqual(self.h.factory.calls, 1)

    async def test_reply_deadline_survives_a_blocked_poll(self):
        gate = self.h.ops.hold("poll")
        await self.request()
        await self.h.clock.advance(179.9)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.states()[-1], "asking")
        await self.h.clock.advance(0.1)
        self.assertEqual(self.notices(), ["reply_unavailable"])
        self.assertEqual(self.h.ops.count("confirm"), 1)
        gate.set()
        await self.h.quiesce()
        await self.render()
        self.assertEqual(self.states()[-1], "listening")
        result = await self.stop()
        self.assertEqual(result.replies_rendered, 0)

    async def test_manager_reply_at_150_seconds_is_spoken_not_idle(self):
        accepted = self.h.clock.now() + 1.5
        self.h.ops.results["poll"] = lambda turn: core.ReplyResult(
            turn, "speak" if self.h.clock.now() >= accepted + 150 else "pending",
            "reply_complete", self.h.clock.now() >= accepted + 150, 3,
            REPLY_TEXT if self.h.clock.now() >= accepted + 150 else None,
        )
        await self.request()
        await self.h.clock.advance(149)
        self.assertEqual(self.states()[-1], "asking")
        self.assertEqual(self.notices(), [])
        self.assertFalse(self.h.task.done())
        await self.h.clock.advance(1)
        self.assertEqual(self.states()[-1], "answering")
        self.assertEqual(
            self.h.provider.of_type("session.commentary.append")[-1]["content"], REPLY_TEXT
        )
        await self.render()
        self.assertEqual(self.states()[-1], "listening")
        await self.h.clock.advance(10)
        self.assertFalse(self.h.task.done())  # fresh idle interval after the wait
        self.assertEqual((await self.stop()).replies_rendered, 1)

    async def test_no_reply_by_180_seconds_notices_then_returns_to_listening(self):
        self.h.ops.results["poll"] = lambda turn: core.ReplyResult(
            turn, "pending", "generation_running", False, 3
        )
        await self.request()
        await self.h.clock.advance(179.9)
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.states()[-1], "asking")
        await self.h.clock.advance(0.1)
        self.assertEqual(self.notices(), ["reply_unavailable"])
        polls = self.h.ops.count("poll")
        await self.render()
        await self.h.clock.advance(10)
        self.assertEqual(self.states()[-1], "listening")
        self.assertEqual(self.h.ops.count("poll"), polls)
        self.assertFalse(self.h.task.done())
        self.assertEqual((await self.stop()).replies_rendered, 0)

    async def test_total_cap_during_pending_reply_keeps_close_reservation(self):
        self.h.ops.results["poll"] = lambda turn: core.ReplyResult(
            turn, "pending", "generation_running", False, 3
        )
        self.h.provider.auto_close = False
        for index in range(6):
            await self.h.clock.advance(80)
            await self.h.push(old.delta(f"keep{index}", "hi", index * 1000, index * 1000 + 500))
        await self.request(10)  # accepted at 481.5, reply deadline would be 661.5
        await self.h.clock.advance(116.4)
        self.assertEqual(self.states()[-1], "asking")
        self.assertEqual(self.notices(), [])
        await self.h.clock.advance(0.1)  # work stops at 598
        self.assertEqual(self.notices(), ["time_limit"])
        self.assertFalse(self.h.task.done())
        await self.h.clock.advance(2)  # all close work ends at 600
        result = await self.h.finish()
        self.assertEqual(result.conversation_end, "time_limit")
        self.assertFalse(result.close_confirmed)
        self.assertEqual(result.replies_rendered, 0)
        self.assertEqual(self.h.clock.pending, 0)

    async def test_audio_send_backpressure_does_not_start_quiet_early(self):
        await self.request()
        await self.h.clock.advance(1)
        gate = self.h.device.send_block = asyncio.Event()
        self.h.provider.push_audio(AUDIO_CANARY)
        await self.h.quiesce()
        await self.h.clock.advance(2)
        self.assertEqual(self.states()[-1], "answering")
        self.assertEqual(self.h.session.outcome.replies_rendered, 0)
        gate.set()
        await self.h.quiesce()
        await self.h.clock.advance(1.4)
        self.assertEqual(self.states()[-1], "answering")
        await self.h.clock.advance(0.2)
        self.assertEqual(self.states()[-1], "listening")
        await self.stop()

    async def test_continuous_audio_in_both_directions_all_states_and_pacing(self):
        for phase in ("listening", "asking", "answering"):
            self.assertEqual(self.states()[-1], phase)
            await self.h.feed(old.audio_frame(100))
            self.h.provider.push_audio(AUDIO_CANARY)
            self.h.provider.push_audio(bytes(4800))
            await self.h.quiesce()
            await self.h.clock.advance(0.2)
            if phase == "listening":
                await self.h.push(old.delta("e1", "request 1", 1000, 1500))
                await self.h.push(old.delegation("dlg_1"))
                # This test used to send only isolated 4 ms frames across long
                # gaps. Supply the continuous device timeline while settling;
                # a stalled input is deliberately no longer safe to submit.
                for _ in range(15):
                    await self.h.feed(old.audio_frame(2400))
                    await self.h.clock.advance(0.1)
            elif phase == "asking":
                await self.h.clock.advance(1)
        result = await self.stop()
        self.assertEqual(result.audio_in_bytes, 600 + 15 * 4800)
        self.assertEqual(result.audio_out_bytes, 3 * (len(AUDIO_CANARY) + 4800))
        self.assertEqual(self.h.provider.types().count("session.input_audio.append"), 18)
        audio = self.h.device.of_type("session.output_audio.delta")
        self.assertEqual(b"".join(base64.b64decode(f["delta"]) for f in audio),
                         (AUDIO_CANARY + bytes(4800)) * 3)
        self.assertLess(self.h.device.types().index("pet.converse.began"),
                        self.h.device.types().index("session.output_audio.delta"))

    async def test_overflow_closes_stream_never_silently_reopens(self):
        self.h.session._audio._cap = 4800
        self.h.device.send_block = asyncio.Event()
        self.h.provider.push_audio(bytes(9600))
        await self.h.quiesce()
        self.assertEqual(self.h.session.outcome.error_code, "overflow")
        self.h.device.send_block.set()
        result = await self.h.finish()
        self.assertEqual(result.audio_out_bytes, 0)
        self.assertEqual(result.conversation_end, "overflow")
        self.assertIn("overflow", self.h.device.error_codes())
        self.assertFalse(self.h.session._audio.is_open)

    async def test_ack_without_audible_output_never_counts_rendered(self):
        await self.request()
        await self.h.clock.advance(1)
        for _ in range(6):
            self.h.provider.push_audio(bytes(4800))
            await self.h.quiesce()
            await self.h.clock.advance(10)
        result = await self.h.finish()
        self.assertEqual(result.replies_rendered, 0)
        self.assertEqual(result.conversation_end, "render_timeout")
        self.assertIn("reply_unavailable", self.notices())


class DelayedInputTest(ConversationCase):
    """Synthetic transport schedules through PetSession, not a model of settling."""

    async def audio(self, frames, interval=0.1):
        for _ in range(frames):
            await self.h.feed(old.audio_frame(2400))  # 100 ms of 24 kHz PCM16
            await self.h.clock.advance(interval)

    async def prefix(self):
        await self.audio(5)
        await self.h.push(old.delta("prefix", "please check ", 0, 400))
        await self.h.push(old.delegation("delayed"))

    async def test_delayed_device_audio_catches_up_before_sealing_full_request(self):
        await self.prefix()
        await self.h.clock.advance(1.6)  # baseline already sends the prefix here
        premature = self.h.ops.count("confirm")
        for index in range(32):
            await self.audio(1, 0.05)  # 2x real time, draining a 1.6 s device backlog
            if index == 20:
                await self.h.push(old.delta("tail", "the second workspace", 400, 1800))
        self.assertEqual(premature, 0, "submitted prefix before delayed audio arrived")
        self.assertEqual(self.h.ops.count("begin"), 0, "sealed during catch-up")
        await self.audio(24)  # continuous, now on-time silence/speech timeline
        self.assertEqual(self.h.ops.staged, [
            ("prefix", "please check "), ("tail", "the second workspace")
        ])
        self.assertEqual(self.h.ops.count("confirm"), 1)
        await self.stop()

    async def test_pending_provider_audio_defers_seal_and_restarts_quiet_on_drain(self):
        await self.prefix()
        gate = self.h.provider.send_block = asyncio.Event()
        await self.audio(16)  # 76800 bytes queued/in-flight, below the existing cap
        self.assertGreater(self.h.session._pout.pending_audio, 0)
        self.assertEqual(self.h.ops.count("begin"), 0)
        gate.set()
        await self.h.quiesce()
        await self.h.push(old.delta("tail", "the second workspace", 400, 1800))
        await self.audio(14)
        self.assertEqual(self.h.ops.count("confirm"), 0)
        await self.audio(1)
        self.assertEqual(self.h.ops.count("confirm"), 1)
        self.assertEqual("".join(t for _, t in self.h.ops.staged),
                         "please check the second workspace")
        await self.stop()

    async def test_stalled_device_input_fails_closed_without_submitting_prefix(self):
        await self.prefix()
        await self.h.clock.advance(8)
        self.assertEqual(self.h.ops.count("begin"), 0)
        self.assertEqual(self.h.ops.count("confirm"), 0)
        result = await self.h.finish()
        self.assertEqual(self.notices(), ["not_sent"])
        self.assertEqual((result.requests_submitted, result.requests_refused), (0, 1))
        self.assertTrue(result.close_confirmed)

    async def test_on_time_continuous_audio_keeps_original_settle_latency(self):
        await self.prefix()
        start = self.h.clock.now()
        await self.audio(14)
        self.assertEqual(self.h.ops.count("confirm"), 0)
        await self.audio(1)
        self.assertEqual(self.h.ops.count("confirm"), 1)
        self.assertAlmostEqual(self.h.ops.times("confirm")[0] - start, 1.5)
        await self.stop()

    async def test_provider_drain_without_transcript_still_requires_fresh_quiet(self):
        await self.prefix()
        gate = self.h.provider.send_block = asyncio.Event()
        await self.audio(16)
        self.assertEqual(self.h.ops.count("begin"), 0)
        gate.set()
        await self.h.quiesce()
        drained = self.h.clock.now()
        await self.audio(14)
        self.assertEqual(self.h.ops.count("confirm"), 0)
        await self.audio(1)
        self.assertAlmostEqual(self.h.ops.times("confirm")[0] - drained, 1.5)
        await self.stop()

    async def test_initial_catchup_cannot_rebaseline_into_a_prefix_send(self):
        # There is no earlier on-time frame to anchor absolute delay.
        await self.audio(2, 0.09)
        await self.h.push(old.delta("prefix", "please check ", 0, 100))
        await self.h.push(old.delegation("delayed"))
        await self.audio(20, 0.09)  # modest >1x, not just a 2x burst
        self.assertEqual(self.h.ops.count("begin"), 0)
        await self.h.push(old.delta("tail", "everything", 100, 2000))
        await self.audio(22)
        self.assertEqual(self.h.ops.count("confirm"), 1)
        self.assertEqual("".join(t for _, t in self.h.ops.staged), "please check everything")
        await self.stop()

    async def test_small_arrival_jitter_does_not_add_settle_latency(self):
        await self.prefix()
        start = self.h.clock.now()
        for _ in range(7):
            await self.audio(1, 0.08)
            await self.audio(1, 0.12)
        await self.audio(1)
        self.assertAlmostEqual(self.h.ops.times("confirm")[0] - start, 1.5)
        await self.stop()

    async def test_stop_while_input_is_behind_never_sends_or_resumes(self):
        await self.prefix()
        await self.h.clock.advance(1.6)
        result = await self.stop()
        self.assertEqual((result.requests_submitted, result.requests_refused), (0, 1))
        await self.h.feed(old.audio_frame(2400))
        await self.h.push(old.delta("tail", "never replay", 400, 1800))
        await self.h.clock.advance(10)
        self.assertEqual(self.h.ops.count("confirm"), 0)


class RealConversationFlowTest(flow.FlowCase):
    async def begin_conversation(self):
        fs = self.new_session()
        fs.go()
        await fs.feed(flow.ASSISTANT_START)
        await fs.feed(BEGIN)
        return fs

    async def request(self, fs, index):
        await fs.push(old.delta(f"e{index}", f"request {index}", index * 1000, index * 1000 + 500))
        await fs.push(old.delegation(f"d{index}"))
        await self.clock.advance(1.5)

    async def render(self, fs):
        fs.provider.push_audio(AUDIO_CANARY)
        await self.quiesce()
        for _ in range(16):
            fs.provider.push_audio(bytes(4800))
            await self.quiesce()
            await self.clock.advance(0.1)

    async def test_real_core_two_correlated_requests_both_complete(self):
        self.client.reply_text = REPLY_TEXT
        fs = await self.begin_conversation()
        for index in (1, 2):
            await self.request(fs, index)
            await self.clock.advance(1)
            await self.render(fs)
            self.assertEqual(fs.device.of_type("pet.converse.state")[-1]["state"], "listening")
        await fs.feed('{"type":"pet.stop"}')
        result = await fs.finish()
        self.assertEqual((result.requests_accepted, result.replies_rendered), (2, 2))
        self.assertEqual(len(self.client.posts), 2)
        for post in self.client.posts:
            command = json.loads(post["body"])["id"]
            self.assertEqual(self.record(command)["state"], "COMPLETE")
        self.assertEqual(len(set(json.loads(p["body"])["id"] for p in self.client.posts)), 2)

    async def test_real_core_delayed_audio_posts_complete_text_exactly_once(self):
        fs = await self.begin_conversation()

        async def audio(frames, interval):
            for _ in range(frames):
                await fs.feed(old.audio_frame(2400))
                await self.clock.advance(interval)

        await audio(5, 0.1)
        await fs.push(old.delta("prefix", "please check ", 0, 400))
        await fs.push(old.delegation("delayed"))
        await self.clock.advance(1.6)
        await audio(32, 0.05)
        self.assertEqual(self.client.posts, [])
        await fs.push(old.delta("tail", "the second workspace", 400, 1800))
        await audio(24, 0.1)
        self.assertEqual([json.loads(p["body"])["args"]["text"] for p in self.client.posts],
                         ["please check the second workspace"])
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()
        self.assertEqual(len(self.client.posts), 1)

    async def test_real_terminal_unspoken_reply_still_closes_before_next_request(self):
        self.client.reply_text = "x" * 4001
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        await self.clock.advance(1)
        self.assertEqual(fs.device.of_type("pet.converse.notice")[-1]["code"], "reply_unavailable")
        self.assertEqual(self.record(first)["state"], "COMPLETE")
        self.assertEqual(self.record(first)["reply_outcome"], "long_reply_not_spoken")
        await self.render(fs)
        self.client.reply_text = REPLY_TEXT
        await self.request(fs, 2)
        self.assertEqual(len(self.client.posts), 2)
        self.assertFalse(any(
            "x" * 100 in frame["content"]
            for frame in fs.provider.of_type("session.commentary.append")
        ))
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()

    async def test_real_long_history_incomplete_tail_speaks_and_completes_once(self):
        self.client.history_rows = [
            assistant_row("gen-old", "older", id=f"history-{i}") for i in range(639)
        ]
        self.client.reply_text = REPLY_TEXT
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        command = self.client.last_command
        # Inspect the genuine capped traversal (no mocked coverage or core result).
        window = await self.runner.run("poll", lambda: companion.detail_window(
            self.client, ATT, flow.SESSION_ID
        ))
        self.assertEqual((window.offset, window.total, window.pages, len(window.items)),
                         (401, 641, 4, 240))
        self.assertFalse(window.coverage_complete)
        before = len(self.client.named("detail"))
        await self.clock.advance(1)
        self.assertEqual(len(self.client.named("detail")) - before, 4)
        self.assertEqual(
            [f["content"] for f in fs.provider.of_type("session.commentary.append")],
            [REPLY_TEXT],
        )
        await self.render(fs)
        self.assertEqual(fs.device.of_type("pet.converse.state")[-1]["state"], "listening")
        self.assertTrue(fs.device.of_type("session.output_audio.delta"))
        self.assertEqual(self.record(command)["state"], "COMPLETE")
        self.assertTrue(self.record(command)["reply_consumed"])
        await self.clock.advance(2)
        self.assertEqual(len(self.client.named("detail")) - before, 4)  # no repeat
        await fs.feed('{"type":"pet.stop"}')
        self.assertEqual((await fs.finish()).replies_rendered, 1)
        self.assertEqual(len(self.client.posts), 1)

    async def test_real_lost_ack_reconciles_on_incomplete_tail_without_boundary(self):
        self.client.history_rows = [
            assistant_row(None, "older", id=f"history-{i}") for i in range(639)
        ]
        self.client.echo_sent_text = True
        self.client.post_response = Response(504, b"")
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        command = self.client.last_command
        self.assertEqual(len(self.client.named("detail")), 4)
        self.assertEqual(self.record(command)["state"], "ACCEPTED")
        self.assertEqual(self.record(command)["reason"], "observed_in_conversation")
        self.assertEqual(fs.device.of_type("pet.converse.notice"), [])
        await self.clock.advance(1)
        self.assertEqual(self.record(command)["state"], "ACCEPTED")  # speech needs boundary
        self.assertEqual(fs.provider.of_type("session.commentary.append"), [])
        await fs.feed('{"type":"pet.stop"}')
        self.assertEqual((await fs.finish()).requests_uncertain, 0)
        self.assertEqual(len(self.client.posts), 1)  # no resend

    async def test_real_reply_timeout_still_pending_stays_busy_next_call_suppresses(self):
        self.client.reply_pending = True
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        await self.clock.advance(180)
        self.assertEqual(fs.device.of_type("pet.converse.notice")[-1]["code"], "reply_unavailable")
        self.assertEqual(self.record(first)["state"], "ACCEPTED")
        reads = len(self.client.named("detail"))
        await self.render(fs)
        # A fresh read still shows working: retain the intentional busy fence.
        await self.request(fs, 2)
        self.assertEqual(fs.device.of_type("pet.converse.notice")[-1]["code"], "busy")
        self.assertGreater(len(self.client.named("detail")), reads)
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.record(first)["state"], "ACCEPTED")
        await self.render(fs)
        await self.request(fs, 3)
        self.assertEqual(fs.device.of_type("pet.converse.notice")[-1]["code"], "busy")
        self.assertEqual(self.record(first)["state"], "ACCEPTED")
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()
        await self.runner.wait_idle()
        fs2 = await self.begin_conversation()
        await self.request(fs2, 4)
        self.assertEqual(fs2.device.of_type("pet.converse.notice"), [])
        self.assertEqual(self.record(first)["state"], "SUPPRESSED")
        self.assertEqual(len(self.client.posts), 2)
        await fs2.feed('{"type":"pet.stop"}')
        await fs2.finish()

    async def test_real_poll_inflight_at_timeout_cannot_release_or_claim_speech(self):
        self.client.reply_text = REPLY_TEXT
        self.client.echo_sent_text = True
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        # Hold a genuine core poll beyond the timeout. Use the flow harness's
        # blocked-worker signal so its settle hook does not await that worker.
        gate = self.hold_posts()
        real_poll = self.ops.observe_reply
        def delayed_poll(turn):
            if not gate.wait(timeout=10):
                raise AssertionError("offline poll gate not released")
            return real_poll(turn)
        self.ops.observe_reply = delayed_poll
        await self.clock.advance(180)
        self.assertEqual(fs.device.of_type("pet.converse.notice")[-1]["code"], "reply_unavailable")
        self.assertEqual(self.record(first)["state"], "ACCEPTED")
        gate.set()
        await self.quiesce()
        # Observation never claims or exports the reply: fresh proof is needed.
        self.assertEqual(self.record(first)["state"], "ACCEPTED")
        self.assertFalse(self.record(first).get("reply_consumed", False))
        self.assertNotEqual(self.record(first).get("reply_outcome"), "claimed_for_render")
        self.assertEqual(
            [f["content"] for f in fs.provider.of_type("session.commentary.append")],
            ["No reply is available yet."],
        )
        await self.render(fs)
        await self.request(fs, 2)
        self.assertEqual(len(self.client.posts), 2)  # fresh proof, not the late poll
        self.assertEqual(self.record(first)["state"], "SUPPRESSED")
        self.assertEqual(self.record(first)["reply_outcome"], "late_reply_not_spoken")
        self.assertEqual(fs.device.of_type("pet.converse.notice")[-1]["code"], "reply_unavailable")
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()

    async def test_real_timeout_unproven_and_foreign_details_stay_blocked(self):
        self.client.reply_pending = True
        self.client.echo_sent_text = True
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        await self.clock.advance(180)
        await self.render(fs)
        self.client.reply_pending = False
        real_request = self.client.request
        cases = (
            "working", "wrong_input", "wrong_text", "ambiguous", "shared_generation",
            "foreign", "missing_status", "failed", "unavailable", "partial",
            "no_own", "wrong_generation", "later_unresolved", "truncated_input",
        )
        for index, case in enumerate(cases, 2):
            def request(origin, route, **kwargs):
                if route.name != "detail":
                    return real_request(origin, route, **kwargs)
                if case == "unavailable":
                    return Response(503, b"")
                if case == "partial":
                    self.client.partial = True
                    try:
                        return real_request(origin, route, **kwargs)
                    finally:
                        self.client.partial = False
                own = flow.user_row(first, text="request 1")
                rows = [own, flow.assistant_row("gen-1", REPLY_TEXT)]
                if case == "working":
                    own["inputStatus"].update(phase="working", basis="generation_started")
                elif case == "wrong_input":
                    own["inputStatus"]["inputId"] = "different"
                elif case == "wrong_text":
                    own["text"] = "different request"
                elif case == "ambiguous":
                    own["nativeInputAmbiguous"] = True
                elif case == "shared_generation":
                    rows.insert(1, flow.user_row("another-input"))
                elif case == "foreign":
                    own["sessionId"] = "foreign-session"
                elif case == "missing_status":
                    del own["inputStatus"]
                elif case == "failed":
                    own["inputStatus"].update(phase="failed", basis="generation_failed")
                elif case == "no_own":
                    rows.pop(0)
                elif case == "wrong_generation":
                    rows[-1]["generationId"] = "unrelated-generation"
                elif case == "later_unresolved":
                    rows.append(flow.user_row("later", generation=None))
                elif case == "truncated_input":
                    own["textDetail"] = {"truncated": True}
                return flow.json_response(flow.page(rows))
            self.client.request = request
            await self.request(fs, index)
            self.assertEqual(fs.device.of_type("pet.converse.notice")[-1]["code"], "busy", case)
            self.assertEqual(len(self.client.posts), 1, case)
            record = self.record(first)
            self.assertEqual(record["state"], "ACCEPTED", case)
            self.assertFalse(record.get("reply_consumed", False), case)
            self.assertNotEqual(record.get("reply_outcome"), "claimed_for_render", case)
            await self.render(fs)
        self.client.request = real_request
        await self.request(fs, len(cases) + 2)
        self.assertEqual(len(self.client.posts), 2)
        self.assertEqual(self.record(first)["reply_outcome"], "late_reply_not_spoken")
        self.assertNotIn(REPLY_TEXT, [
            frame["content"] for frame in fs.provider.of_type("session.commentary.append")
        ])
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()

    async def _late_unrenderable_reply_does_not_prevent_distinct_request(self, text):
        self.client.reply_pending = True
        self.client.echo_sent_text = True
        self.client.reply_text = text
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        await self.clock.advance(180)
        await self.render(fs)
        self.client.reply_pending = False
        await self.request(fs, 2)
        self.assertEqual(len(self.client.posts), 2)
        self.assertEqual(self.record(first)["reply_outcome"], "late_reply_not_spoken")
        self.assertEqual(
            [frame["content"] for frame in fs.provider.of_type("session.commentary.append")],
            ["No reply is available yet."],
        )
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()

    async def test_real_timeout_long_reply_proves_completion_without_speech(self):
        await self._late_unrenderable_reply_does_not_prevent_distinct_request("x" * 4001)

    async def test_real_timeout_empty_reply_proves_completion_without_speech(self):
        await self._late_unrenderable_reply_does_not_prevent_distinct_request("")

    async def test_real_timeout_invalid_reply_proves_completion_without_speech(self):
        await self._late_unrenderable_reply_does_not_prevent_distinct_request(None)

    async def test_real_timeout_validated_incomplete_tail_remains_busy(self):
        self.client.reply_pending = True
        self.client.echo_sent_text = True
        self.client.history_rows = [
            assistant_row("gen-old", "older", id=f"history-{i}") for i in range(639)
        ]
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        await self.clock.advance(180)
        await self.render(fs)
        self.client.reply_pending = False
        await self.request(fs, 2)
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.record(first)["state"], "ACCEPTED")
        self.assertEqual(fs.device.of_type("pet.converse.notice")[-1]["code"], "busy")
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()

    async def test_real_timeout_journal_write_failure_stays_closed(self):
        self.client.reply_pending = True
        self.client.echo_sent_text = True
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        await self.clock.advance(180)
        await self.render(fs)
        self.client.reply_pending = False
        with mock.patch.object(self.journal, "put", side_effect=JournalError("journal_io_error")):
            await self.request(fs, 2)
        self.assertTrue(self.bridge.status()["faulted"])
        self.assertEqual(self.record(first)["state"], "ACCEPTED")
        self.assertEqual(len(self.client.posts), 1)
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()

    async def test_real_timeout_recovery_does_not_bypass_busy_preflight(self):
        self.client.reply_pending = True
        self.client.echo_sent_text = True
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        await self.clock.advance(180)
        await self.render(fs)
        self.client.reply_pending = False
        self.client.listing_status = "working"
        self.client.listing_execution = True
        await self.request(fs, 2)
        self.assertEqual(self.record(first)["reply_outcome"], "late_reply_not_spoken")
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(fs.device.of_type("pet.converse.notice")[-1]["code"], "busy")
        await self.render(fs)
        self.client.listing_status = "idle"
        self.client.listing_execution = False
        await self.request(fs, 3)
        posted = [json.loads(post["body"])["args"]["text"] for post in self.client.posts]
        self.assertEqual(posted, ["request 1", "request 3"])
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()

    async def test_real_timeout_binding_or_epoch_change_during_detail_fails_closed(self):
        self.client.reply_pending = True
        self.client.echo_sent_text = True
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        await self.clock.advance(180)
        await self.render(fs)
        self.client.reply_pending = False
        real_request = self.client.request
        def changed_attachment(origin, route, **kwargs):
            response = real_request(origin, route, **kwargs)
            if route.name == "detail":
                attached = self.store.load_attachment(self.binding.credential_ref)
                self.store.save_attachment(replace(attached, host_instance_id=INSTANCE_2))
            return response
        self.client.request = changed_attachment
        await self.request(fs, 2)
        self.assertEqual(self.record(first)["state"], "ACCEPTED")
        self.assertEqual(len(self.client.posts), 1)
        await self.render(fs)
        attached = self.store.load_attachment(self.binding.credential_ref)
        self.store.save_attachment(replace(attached, host_instance_id=self.binding.host_instance_id))
        def changed_epoch(origin, route, **kwargs):
            response = real_request(origin, route, **kwargs)
            if route.name == "detail":
                self.bridge.stop()
                self.bridge.resume()
            return response
        self.client.request = changed_epoch
        await self.request(fs, 3)
        self.assertEqual(self.record(first)["state"], "ACCEPTED")
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(fs.device.of_type("pet.converse.notice")[-1]["code"], "busy")
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()

    async def test_observation_cache_is_exact_one_use_and_stop_safe(self):
        self.client.reply_text = REPLY_TEXT
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        turn = self.client.last_command
        observed = await self.runner.run("poll", lambda: self.ops.observe_reply(turn))
        self.assertEqual((observed.status, observed.text), ("ready", None))
        self.assertEqual(self.record(turn)["state"], "ACCEPTED")
        self.assertFalse(self.record(turn).get("reply_consumed", False))
        wrong = flow.IdFactory(501)()
        result = await self.runner.run("poll", lambda: self.ops.claim_observed_reply(wrong))
        self.assertIsNone(result.text)
        result = await self.runner.run("poll", lambda: self.ops.claim_observed_reply(turn))
        self.assertIsNone(result.text)  # wrong-id claim took the cache once
        observed = await self.runner.run("poll", lambda: self.ops.observe_reply(turn))
        self.assertEqual(observed.status, "ready")
        await self.runner.run("stop", self.ops.stop, urgent=True)
        result = await self.runner.run("poll", lambda: self.ops.claim_observed_reply(turn))
        self.assertIsNone(result.text)
        self.assertFalse(self.record(turn).get("reply_consumed", False))
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()

    async def _other_command_blocks_after_late_completion(self, state):
        self.client.reply_pending = True
        self.client.echo_sent_text = True
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        await self.clock.advance(180)
        await self.render(fs)
        self.client.reply_pending = False
        other = flow.IdFactory(900)()
        record = dict(self.record(first))
        record.update(command_id=other, state=state, event_ids=["other-e"])
        self.journal.put(f"cmd-{other}.json", record)
        before = self.record(other)
        await self.request(fs, 2)
        self.assertEqual(self.record(first)["reply_outcome"], "late_reply_not_spoken")
        self.assertEqual(self.record(other), before)
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(
            fs.device.of_type("pet.converse.notice")[-1]["code"],
            "send_uncertain" if state == "UNCERTAIN" else "busy",
        )
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()
        await self.runner.wait_idle()
        self.assertFalse(await self.runner.run("pre_dial_admission", self.ops.pre_dial_admission))
        self.assertEqual(self.record(other), before)
        self.assertEqual(len(self.client.posts), 1)

    async def test_other_prepared_command_blocks_late_recovery_and_predial(self):
        await self._other_command_blocks_after_late_completion("PREPARED")

    async def test_other_attempted_command_blocks_late_recovery_and_predial(self):
        await self._other_command_blocks_after_late_completion("ATTEMPTED")

    async def test_other_uncertain_command_blocks_late_recovery_and_predial(self):
        await self._other_command_blocks_after_late_completion("UNCERTAIN")

    async def test_real_timeout_unknown_journal_commit_faults_even_if_record_changed(self):
        self.client.reply_pending = True
        self.client.echo_sent_text = True
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        await self.clock.advance(180)
        await self.render(fs)
        self.client.reply_pending = False
        real_put = self.journal.put
        def unknown_commit(name, record):
            if record.get("reply_outcome") == "late_reply_not_spoken":
                real_put(name, record)
                raise JournalError("journal_persist_unknown")
            return real_put(name, record)
        with mock.patch.object(self.journal, "put", side_effect=unknown_commit):
            await self.request(fs, 2)
        self.assertEqual(self.record(first)["state"], "SUPPRESSED")
        self.assertTrue(self.bridge.status()["faulted"])
        self.assertEqual(len(self.client.posts), 1)
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()

    async def _close_during_timeout_proof(self, *, disconnect):
        self.client.reply_pending = True
        self.client.echo_sent_text = True
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        await self.clock.advance(180)
        await self.render(fs)
        self.client.reply_pending = False
        gate = self.hold_posts()
        entered = threading.Event()
        real_request = self.client.request
        def blocked_request(origin, route, **kwargs):
            if route.name == "detail":
                entered.set()
                if not gate.wait(timeout=10):
                    raise AssertionError("offline read gate not released")
            return real_request(origin, route, **kwargs)
        self.client.request = blocked_request
        await self.request(fs, 2)
        self.assertTrue(entered.wait(timeout=2))
        if disconnect:
            fs.device.disconnect()
        else:
            await fs.feed('{"type":"pet.stop"}')
        result = await fs.finish()
        gate.set()
        await self.runner.wait_idle()
        self.assertEqual(len(self.client.posts), 1)
        self.assertTrue(self.bridge.status()["stopped"])
        self.assertEqual(result.replies_rendered, 0)
        record = self.record(first)
        self.assertEqual(record["state"], "ACCEPTED")
        self.assertFalse(record.get("reply_consumed", False))
        self.assertNotEqual(record.get("reply_outcome"), "claimed_for_render")
        self.assertNotIn(REPLY_TEXT, [
            frame["content"] for frame in fs.provider.of_type("session.commentary.append")
        ])

    async def test_real_timeout_stop_during_proof_never_sends_new_input(self):
        await self._close_during_timeout_proof(disconnect=False)

    async def test_real_timeout_disconnect_during_proof_never_sends_new_input(self):
        await self._close_during_timeout_proof(disconnect=True)

    async def test_real_claim_crossing_timeout_is_not_spoken_and_requires_new_proof(self):
        self.client.reply_text = REPLY_TEXT
        self.client.echo_sent_text = True
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        gate = self.hold_posts()
        entered = threading.Event()
        claim = self.ops.claim_observed_reply
        def blocked_claim(turn):
            entered.set()
            if not gate.wait(timeout=10):
                raise AssertionError("offline claim gate not released")
            return claim(turn)
        self.ops.claim_observed_reply = blocked_claim
        await self.clock.advance(180)
        self.assertTrue(entered.is_set())
        self.assertEqual(fs.device.of_type("pet.converse.notice")[-1]["code"], "reply_unavailable")
        gate.set()
        await self.quiesce()
        self.assertEqual(self.record(first)["state"], "COMPLETE")
        self.assertEqual(self.record(first)["reply_outcome"], "claimed_for_render")
        await self.render(fs)
        await self.request(fs, 2)
        self.assertEqual(len(self.client.posts), 2)
        self.assertEqual(self.record(first)["state"], "SUPPRESSED")
        self.assertEqual(self.record(first)["reply_outcome"], "late_reply_not_spoken")
        self.assertNotIn(REPLY_TEXT, [
            frame["content"] for frame in fs.provider.of_type("session.commentary.append")
        ])
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()

    async def test_terminal_long_reply_claim_crossing_timeout_recovers(self):
        self.client.echo_sent_text = True
        self.client.reply_text = "x" * 4001
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        gate = self.hold_posts()
        entered = threading.Event()
        claim = self.ops.claim_observed_reply
        def blocked_claim(turn):
            entered.set()
            if not gate.wait(timeout=10):
                raise AssertionError("offline claim gate not released")
            return claim(turn)
        self.ops.claim_observed_reply = blocked_claim
        await self.clock.advance(180)
        self.assertTrue(entered.is_set())
        gate.set()
        await self.quiesce()
        self.assertEqual(self.record(first)["reply_outcome"], "long_reply_not_spoken")
        await self.render(fs)
        await self.request(fs, 2)
        self.assertEqual(len(self.client.posts), 2)
        self.assertEqual(self.record(first)["reply_outcome"], "late_reply_not_spoken")
        self.assertFalse(any(
            "x" * 100 in frame["content"]
            for frame in fs.provider.of_type("session.commentary.append")
        ))
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()

    async def test_legacy_and_new_terminal_journals_never_replay_speech_after_restart(self):
        self.client.reply_pending = True
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()
        await self.runner.wait_idle()
        original = self.record(first)
        for state, outcome, consumed in (
            ("COMPLETE", "spoken", True),
            ("COMPLETE", "claimed_for_render", True),
            ("SUPPRESSED", "late_reply_not_spoken", False),
        ):
            with self.subTest(outcome=outcome):
                record = dict(original)
                record.update(state=state, reply_outcome=outcome, reply_consumed=consumed)
                self.journal.put(f"cmd-{first}.json", record)
                restarted = core.Bridge(
                    self.journal,
                    flow.transport.HttpsTransport(self.store, lambda: self.client),
                    clock=lambda: flow.WALL0 + self.clock.now(),
                    new_id=flow.IdFactory(3000), monotonic=self.clock.now,
                )
                result = restarted.poll_reply(
                    first, flow.page([flow.user_row(first), assistant_row("gen-1", REPLY_TEXT)]),
                    coverage_complete=True, session_id=flow.SESSION_ID,
                )
                self.assertEqual(result.status, "not_spoken")
                self.assertIsNone(result.text)
                self.assertEqual(len(self.client.posts), 1)

    async def test_new_input_racing_late_proof_is_not_queued_or_duplicated(self):
        self.client.reply_pending = True
        self.client.echo_sent_text = True
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        await self.clock.advance(180)
        await self.render(fs)
        self.client.reply_pending = False
        gate = self.hold_posts()
        entered = threading.Event()
        real_request = self.client.request
        def blocked_request(origin, route, **kwargs):
            if route.name == "detail":
                entered.set()
                if not gate.wait(timeout=10):
                    raise AssertionError("offline read gate not released")
            return real_request(origin, route, **kwargs)
        self.client.request = blocked_request
        await self.request(fs, 2)
        self.assertTrue(entered.wait(timeout=2))
        await fs.push(old.delta("e3", "request 3", 3000, 3500))
        await fs.push(old.delegation("d3"))
        gate.set()
        await self.quiesce()
        posted = [json.loads(post["body"])["args"]["text"] for post in self.client.posts]
        self.assertEqual(posted, ["request 1", "request 2"])
        self.assertEqual(self.record(first)["reply_outcome"], "late_reply_not_spoken")
        self.assertNotIn(REPLY_TEXT, [
            frame["content"] for frame in fs.provider.of_type("session.commentary.append")
        ])
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()

    async def test_real_uncertain_journal_stays_uncertain_and_blocks_next_send(self):
        self.client.post_response = Response(504, b"")
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        command = self.client.last_command
        self.assertEqual(self.record(command)["state"], "UNCERTAIN")
        self.assertEqual(fs.device.of_type("pet.converse.notice")[-1]["code"], "send_uncertain")
        await self.render(fs)
        await self.request(fs, 2)
        # The unresolved earlier send is what blocks this one: named as such, not "busy".
        self.assertEqual(
            fs.device.of_type("pet.converse.notice")[-1]["code"], "send_uncertain"
        )
        self.assertEqual(len(self.client.posts), 1)
        await fs.feed('{"type":"pet.stop"}')
        result = await fs.finish()
        self.assertEqual(result.requests_uncertain, 1)
        self.assertEqual(self.record(command)["state"], "UNCERTAIN")

    async def test_real_lost_acknowledgement_is_reconciled_and_the_reply_spoken(self):
        # The host took the input but its acknowledgement never arrived (a timeout or
        # a 504).  The conversation shows that exact input, so it is not uncertain.
        self.client.post_response = Response(504, b"")
        self.client.echo_sent_text = True
        self.client.reply_text = REPLY_TEXT
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        command = self.client.last_command
        self.assertEqual(fs.device.of_type("pet.converse.notice"), [])
        self.assertEqual(fs.device.of_type("pet.converse.state")[-1]["state"], "asking")
        await self.clock.advance(1)
        await self.render(fs)
        self.assertEqual(fs.device.of_type("pet.converse.state")[-1]["state"], "listening")
        await fs.feed('{"type":"pet.stop"}')
        result = await fs.finish()
        self.assertEqual(
            (result.requests_accepted, result.requests_uncertain, result.replies_rendered),
            (1, 0, 1),
        )
        self.assertEqual(len(self.client.posts), 1)  # observed, never re-sent
        self.assertEqual(self.record(command)["state"], "COMPLETE")

    async def test_real_earlier_uncertain_send_is_reconciled_and_stops_blocking(self):
        self.client.post_response = Response(504, b"")
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        first = self.client.last_command
        self.assertEqual(self.record(first)["state"], "UNCERTAIN")
        await self.render(fs)
        # Later the conversation does show that input.  In the SAME call its reply is
        # then simply pending (reported as busy); the input is never sent again.
        self.client.post_response = None
        self.client.echo_sent_text = True
        self.client.reply_text = REPLY_TEXT
        await self.request(fs, 2)
        self.assertEqual(self.record(first)["state"], "ACCEPTED")
        self.assertEqual(fs.device.of_type("pet.converse.notice")[-1]["code"], "busy")
        self.assertEqual(len(self.client.posts), 1)
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()
        # In the NEXT call the old send no longer blocks: a new request goes through.
        fs2 = await self.begin_conversation()
        await self.request(fs2, 3)
        self.assertEqual(fs2.device.of_type("pet.converse.notice"), [])
        self.assertEqual(fs2.device.of_type("pet.converse.state")[-1]["state"], "asking")
        self.assertEqual(len(self.client.posts), 2)
        self.assertNotEqual(self.client.last_command, first)
        self.assertEqual(self.record(first)["state"], "SUPPRESSED")
        await fs2.feed('{"type":"pet.stop"}')
        await fs2.finish()

    async def test_real_busy_then_idle_abandons_only_unsent_capture(self):
        self.client.listing_status = "working"
        self.client.listing_execution = True
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        self.assertEqual(self.client.posts, [])
        self.assertFalse(self.bridge.status()["open_turn"])
        await self.render(fs)
        self.client.listing_status = "idle"
        self.client.listing_execution = False
        await self.request(fs, 2)
        self.assertEqual(len(self.client.posts), 1)
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()

    async def test_real_attachment_mismatch_between_requests_never_sends_again(self):
        self.client.reply_text = REPLY_TEXT
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        await self.clock.advance(1)
        await self.render(fs)
        attached = self.store.load_attachment(self.binding.credential_ref)
        self.store.save_attachment(replace(attached, host_instance_id=INSTANCE_2))
        await self.request(fs, 2)
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(fs.device.of_type("pet.converse.notice")[-1]["code"], "busy")
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()

    async def test_real_attachment_mismatch_at_confirm_sends_nothing(self):
        real = self.ops.preflight
        def preflight_then_change_attachment():
            result = real()
            attached = self.store.load_attachment(self.binding.credential_ref)
            self.store.save_attachment(replace(attached, host_instance_id=INSTANCE_2))
            return result
        self.ops.preflight = preflight_then_change_attachment
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        self.assertEqual(self.client.posts, [])
        self.assertEqual(fs.device.of_type("pet.converse.notice")[-1]["code"], "not_sent")
        self.assertFalse(self.bridge.status()["open_turn"])
        await fs.feed('{"type":"pet.stop"}')
        result = await fs.finish()
        self.assertEqual((result.requests_sent, result.requests_refused), (0, 1))

    async def test_real_attachment_mismatch_at_poll_never_releases_reply_text(self):
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        attached = self.store.load_attachment(self.binding.credential_ref)
        self.store.save_attachment(replace(attached, host_instance_id=INSTANCE_2))
        await self.clock.advance(1)
        self.assertEqual(self.client.named("detail"), [])
        self.assertEqual(fs.device.of_type("pet.converse.notice")[-1]["code"], "reply_unavailable")
        self.assertNotIn(self.client.reply_text, [f["content"] for f in fs.provider.of_type("session.commentary.append")])
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()

    async def test_real_abandon_cannot_rearm_stop_or_touch_accepted_journal(self):
        fs = await self.begin_conversation()
        await self.request(fs, 1)
        command = self.client.last_command
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()
        await self.runner.wait_idle()
        record = self.record(command)
        await self.runner.run("abandon", lambda: self.ops.abandon(command))
        self.assertTrue(self.bridge.status()["stopped"])
        self.assertEqual(self.record(command), record)

    async def test_real_metadata_and_journal_never_leak_content(self):
        self.client.reply_text = REPLY_TEXT
        fs = await self.begin_conversation()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            await fs.push(old.delta("secret_event", TEXT_CANARY, 0, 500))
            await fs.push(old.delegation("secret_delegation"))
            await self.clock.advance(2.5)
            await self.render(fs)
            await fs.feed('{"type":"pet.stop"}')
            result = await fs.finish()
        views = repr(result) + repr(self.runner.retained()) + repr(fs.session) + out.getvalue() + err.getvalue()
        for secret in (flow.SESSION_ID, "secret_event", "secret_delegation"):
            self.assertNotIn(secret, views)
        for name in self.journal.names("cmd-"):
            views += repr(self.journal.get(name))
        # The private journal intentionally stores binding/event ids and digests;
        # it must not store the content itself. Outcomes/logs above store no ids.
        for secret in (TEXT_CANARY, REPLY_TEXT, base64.b64encode(AUDIO_CANARY).decode()):
            self.assertNotIn(secret, views)


class ConversationServerTest(server.GatewayCase):
    async def start_conversation(self):
        task = asyncio.create_task(self.gw.handle(self.request()))
        await self.h.feed(server.ASSISTANT_START)
        await self.h.feed(BEGIN)
        return task

    async def test_shutdown_after_legacy_cap_closes_normally_and_keeps_metadata(self):
        task = await self.start_conversation()
        await self.h.clock.advance(80)
        await self.gw.shutdown()
        await task
        self.assertFalse(self.gw.forced_cancel)
        self.assertFalse(self.gw.busy)
        result = self.gw.last_outcome.conversation
        self.assertEqual(result.conversation_end, "stopped")
        self.assertEqual(result.requests_sent, 0)
        self.assertTrue(result.close_confirmed)

    async def test_late_confirm_reconciles_counts_once_without_resend(self):
        task = await self.start_conversation()
        gate = self.h.ops.hold("confirm")
        await self.h.push(old.delta("e", "hello", 0, 500))
        await self.h.push(old.delegation("d"))
        await self.h.clock.advance(1.5)
        await self.h.feed('{"type":"pet.stop"}')
        await task
        self.assertTrue(self.gw.busy)
        gate.set()
        await old.until(lambda: not self.gw.busy)
        summary = self.gw.last_outcome.conversation
        self.assertEqual((summary.requests_submitted, summary.requests_sent,
                          summary.requests_accepted, summary.requests_uncertain), (1, 1, 1, 0))
        self.assertEqual(summary.send_outcome, "accepted")
        self.assertIsNone(summary.confirm_job)
        self.assertEqual(self.h.runner.held_results(), ())
        self.assertEqual(self.h.ops.count("confirm"), 1)


class PublicLateRecoveryControl(flow.FlowCase):
    async def test_public_baseline_timeout_exact_completion_allows_only_new_request(self):
        self.client.reply_pending = True
        self.client.echo_sent_text = True
        self.client.reply_text = REPLY_TEXT
        fs = self.new_session()
        fs.go()
        await fs.feed(flow.ASSISTANT_START)
        await fs.feed(BEGIN)
        await fs.push(old.delta("e1", "request 1", 1000, 1500))
        await fs.push(old.delegation("d1"))
        await self.clock.advance(1.5)
        first = self.client.last_command
        await self.clock.advance(180)
        self.assertEqual(self.record(first)["state"], "ACCEPTED")
        fs.provider.push_audio(AUDIO_CANARY)
        await self.quiesce()
        for _ in range(16):
            fs.provider.push_audio(bytes(4800))
            await self.quiesce()
            await self.clock.advance(0.1)
        self.client.reply_pending = False
        await fs.push(old.delta("e2", "request 2", 2000, 2500))
        await fs.push(old.delegation("d2"))
        await self.clock.advance(1.5)
        self.assertEqual(len(self.client.posts), 2)
        self.assertNotEqual(self.client.last_command, first)
        self.assertEqual(self.record(first)["reply_outcome"], "late_reply_not_spoken")
        self.assertNotIn(REPLY_TEXT, [
            frame["content"] for frame in fs.provider.of_type("session.commentary.append")
        ])
        await fs.feed('{"type":"pet.stop"}')
        await fs.finish()
