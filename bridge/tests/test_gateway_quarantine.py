"""Offline post-commentary quarantine policy, not word provenance or live qualification."""
from __future__ import annotations

import asyncio
import base64
from unittest import mock

from pet_bridge import gateway_core as gc, gateway_state as gs
from .test_gateway_core import SpeakCase, ack_of, LONG_REPLY, until

PREFIX = b"\x11\x22" * 14400  # 28,800-byte measured-window-sized synthetic prefix
SUFFIX = b"\x33\x44" * 2400
OLD = b"\x55\x66" * 2400


class QuarantineTest(SpeakCase):
    @property
    def session(self) -> gc.PetSession:
        assert self.h.session is not None
        return self.h.session

    def setUp(self):
        super().setUp()
        self.attempts = []
        self.addCleanup(lambda: self.assertEqual(self.attempts, []))
        def refuse(*a, **kw):
            self.attempts.append("forbidden")
            raise AssertionError("external call")
        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "socket.create_connection", "socket.getaddrinfo",
                       "socket.socket.bind", "socket.socket.listen", "subprocess.Popen"):
            p = mock.patch(target, side_effect=refuse)
            p.start()
            self.addCleanup(p.stop)

    def pcm(self):
        return b"".join(base64.b64decode(f["delta"])
                        for f in self.device.of_type("session.output_audio.delta"))

    async def waiting_ack(self):
        self.provider.auto_ack = False
        await self.flow()
        await self.clock.advance(1)
        frames = self.provider.of_type("session.commentary.append")
        self.assertTrue(frames)
        return frames

    async def stop(self):
        await self.h.feed({"type": "pet.stop"})
        result = await self.h.finish()
        self.assertEqual(self.ops.count("confirm"), 1)
        self.assertFalse(result.reply_done == "completed")
        self.assertEqual(self.provider.types().count("session.close"), 1)
        return result

    async def test_post_commit_prefix_waits_for_ack_then_prefix_suffix_order(self):
        frames = await self.waiting_ack()
        self.provider.push_audio(PREFIX)
        await self.h.quiesce()
        self.assertEqual(self.pcm(), b"")
        self.assertEqual(self.session._audio.queued_bytes, len(PREFIX))
        await self.h.push(ack_of(frames[0]))
        self.provider.push_audio(SUFFIX)
        await self.h.quiesce()
        await self.clock.advance(1)
        self.assertEqual(self.pcm(), PREFIX + SUFFIX)
        await self.stop()

    async def test_foreign_and_later_chunk_ack_do_not_release_first_prefix(self):
        self.ops.results["poll"] = lambda turn: gc.core.ReplyResult(
            turn, "speak", "reply_complete", True, 3, LONG_REPLY)
        frames = await self.waiting_ack()
        self.assertGreater(len(frames), 1)
        self.provider.push_audio(PREFIX)
        await self.h.push({"type": "session.commentary.appended", "client_event_id": "foreign"})
        for frame in frames[1:]:
            await self.h.push(ack_of(frame))
        await self.clock.advance(.5)
        self.assertEqual(self.pcm(), b"")
        self.assertNotIn("pet.reply.started", self.device.types())
        await self.h.push(ack_of(frames[0]))
        await self.clock.advance(1)
        self.assertEqual(self.pcm(), PREFIX)
        await self.stop()

    async def test_receive_stamp_excludes_old_fifo_pcm_when_completion_overtakes_it(self):
        self.provider.auto_ack = False
        session = self.session
        original_send = self.provider.send_text
        original_post = session._post_external
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        queued = []
        def retain_pcm(event):
            if event[0] == "provider" and event[1].kind == "audio":
                queued.append(event)
            else:
                original_post(event)
        async def blocked(text):
            import json
            if json.loads(text)["type"] == "session.commentary.append":
                entered.set()
                await release.wait()
            await original_send(text)
        session._post_external = retain_pcm
        self.provider.send_text = blocked
        await self.flow()
        await self.clock.advance(1)
        await until(entered.is_set)
        self.provider.push_audio(OLD)
        await self.h.quiesce()
        self.assertEqual(len(queued), 1)
        self.assertIsNone(queued[0][2])
        release.set()
        await self.h.quiesce()  # prioritized send completion opens the held queue
        self.assertTrue(session._commentary_committed)
        session._post_external = original_post
        original_post(queued[0])  # earlier receive must NOT acquire the new eligibility
        self.provider.push_audio(PREFIX)
        await self.h.push(ack_of(self.provider.of_type("session.commentary.append")[0]))
        await self.clock.advance(1)
        self.assertEqual(self.pcm(), PREFIX)
        self.assertGreaterEqual(session.outcome.dropped_audio_bytes, len(OLD))
        await self.stop()

    async def test_ack_before_send_returns_cannot_capture_or_release_early_audio(self):
        self.provider.auto_ack = False
        original_send = self.provider.send_text
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        async def blocked(text):
            import json
            frame = json.loads(text)
            if frame["type"] == "session.commentary.append":
                self.provider.push(ack_of(frame))
                self.provider.push_audio(OLD)
                entered.set()
                await release.wait()
            await original_send(text)
        self.provider.send_text = blocked
        await self.flow()
        await self.clock.advance(1)
        await until(entered.is_set)
        await self.h.quiesce()
        self.assertEqual(self.pcm(), b"")
        self.assertNotIn("pet.reply.started", self.device.types())
        release.set()
        await self.h.quiesce()
        self.provider.push_audio(PREFIX)
        await self.h.quiesce()
        await self.clock.advance(1)
        self.assertEqual(self.pcm(), PREFIX)
        await self.stop()

    async def test_stop_discards_quarantine_and_late_ack_cannot_reopen(self):
        frames = await self.waiting_ack()
        self.provider.push_audio(PREFIX)
        await self.h.quiesce()
        self.assertGreater(self.session._audio.queued_bytes, 0)
        await self.h.feed({"type": "pet.stop"})
        await self.h.push(ack_of(frames[0]))
        self.provider.push_audio(SUFFIX)
        await self.h.quiesce()
        result = await self.h.finish()
        self.assertEqual(self.pcm(), b"")
        self.assertEqual(result.reply_done, "stopped")
        self.assertFalse(self.session._audio.is_open)

    async def test_missing_ack_timeout_discards_without_playing(self):
        await self.waiting_ack()
        self.provider.push_audio(PREFIX)
        await self.h.quiesce()
        await self.clock.advance(10)
        result = await self.h.finish()
        self.assertEqual(self.pcm(), b"")
        self.assertEqual(result.reply_done, "unavailable")
        self.assertGreaterEqual(result.dropped_audio_bytes, len(PREFIX))

    async def test_held_epoch_and_started_frame_do_not_lose_prefix(self):
        frames = await self.waiting_ack()
        gate = self.ops.hold("epoch_current")
        self.provider.push_audio(PREFIX)
        await self.h.push(ack_of(frames[0]))
        self.provider.push_audio(SUFFIX)
        await self.h.quiesce()
        self.assertEqual(self.pcm(), b"")
        self.assertEqual(self.session._audio.queued_bytes, len(PREFIX + SUFFIX))
        self.device.send_block = asyncio.Event()
        gate.set()
        await self.h.quiesce()
        self.assertEqual(self.pcm(), b"")
        self.device.send_block.set()
        await self.h.quiesce()
        await self.clock.advance(1)
        self.assertEqual(self.pcm(), PREFIX + SUFFIX)
        await self.stop()

    async def test_stale_epoch_discards_quarantine(self):
        frames = await self.waiting_ack()
        self.ops.results["epoch_current"] = False
        self.provider.push_audio(PREFIX)
        await self.h.push(ack_of(frames[0]))
        result = await self.h.finish()
        self.assertEqual(self.pcm(), b"")
        self.assertEqual(result.reply_done, "unavailable")
        self.assertFalse(self.session._audio.is_open)

    async def test_exact_cap_then_overflow_clears_quarantine_no_partial_success(self):
        await self.waiting_ack()
        # Actual provider frames remain within the existing per-frame cap.
        remain = gs.AUDIO_QUEUE_CAP
        while remain:
            size = min(remain, 4800)
            self.provider.push_audio(b"\x11\x22" * (size // 2))
            remain -= size
            await self.h.quiesce()
        self.assertEqual(self.session._audio.queued_bytes, gs.AUDIO_QUEUE_CAP)
        before = self.session.outcome.dropped_audio_bytes
        self.provider.push_audio(b"\x11\x22")
        await self.h.quiesce()
        result = await self.h.finish()
        self.assertEqual(result.error_code, "overflow")
        self.assertEqual(result.reply_done, "unavailable")
        self.assertEqual(self.pcm(), b"")
        self.assertEqual(result.dropped_audio_bytes - before, gs.AUDIO_QUEUE_CAP + 2)
        self.assertEqual(result.audio_out_bytes, 0)
        self.assertEqual(self.session._audio.queued_bytes, 0)
        self.assertFalse(self.session._audio.is_open)

    async def test_no_quarantine_while_commentary_send_is_blocked(self):
        self.provider.auto_ack = False
        original = self.provider.send_text
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        async def blocked(text):
            import json
            if json.loads(text)["type"] == "session.commentary.append":
                entered.set()
                await release.wait()
            await original(text)
        self.provider.send_text = blocked
        await self.flow()
        await self.clock.advance(1)
        await until(entered.is_set)
        self.provider.push_audio(OLD)
        await self.h.quiesce()
        self.assertEqual(self.pcm(), b"")
        self.assertEqual(self.session._audio.queued_bytes, 0)
        release.set()
        await self.h.quiesce()
        frames = self.provider.of_type("session.commentary.append")
        self.provider.push_audio(PREFIX)
        await self.h.push(ack_of(frames[0]))
        await self.clock.advance(1)
        self.assertEqual(self.pcm(), PREFIX)
        await self.stop()

    async def test_real_fifo_old_audio_cannot_gain_eligibility_from_internal_completion(self):
        self.provider.auto_ack = False
        session = self.session
        original_send = self.provider.send_text
        entered, release = asyncio.Event(), asyncio.Event()
        self.addCleanup(release.set)
        async def blocked(text):
            import json
            if json.loads(text)["type"] == "session.commentary.append":
                entered.set()
                await release.wait()
            await original_send(text)
        self.provider.send_text = blocked
        await self.flow()
        await self.clock.advance(1)
        await until(entered.is_set)
        # Hold only dispatcher wakeups, not the provider reader or real bounded FIFO.
        original_wake = session._wake.set
        session._wake.set = lambda: None
        self.addCleanup(setattr, session._wake, "set", original_wake)
        self.provider.push_audio(OLD)
        await self.h.quiesce()
        self.assertTrue(any(e[0] == "provider" and e[1].kind == "audio"
                            and e[2] is None for e in session._inbox))
        release.set()
        await self.h.quiesce()
        self.assertTrue(session._internal)  # "sent" will run before earlier FIFO PCM
        session._wake.set = original_wake
        original_wake()
        await self.h.quiesce()
        self.assertEqual(session._audio.queued_bytes, 0)
        self.provider.push_audio(PREFIX)
        await self.h.push(ack_of(self.provider.of_type("session.commentary.append")[0]))
        await self.clock.advance(1)
        self.assertEqual(self.pcm(), PREFIX)
        await self.stop()

    async def test_stop_and_provider_error_during_held_release_discard_without_reopen(self):
        for boundary in ("epoch", "started"):
            for terminal in ("stop", "error"):
                with self.subTest(boundary=boundary, terminal=terminal):
                    await self.fresh_harness()
                    frames = await self.waiting_ack()
                    release = None
                    if boundary == "epoch":
                        release = self.ops.hold("epoch_current")
                    else:
                        self.device.send_block = asyncio.Event()
                    self.provider.push_audio(PREFIX)
                    await self.h.push(ack_of(frames[0]))
                    self.assertEqual(self.pcm(), b"")
                    if terminal == "stop":
                        await self.h.feed({"type": "pet.stop"})
                    else:
                        await self.h.push({"type": "error", "error": {
                            "code": "synthetic", "message": "synthetic", "type": "synthetic"}})
                    if release:
                        release.set()
                    if self.device.send_block:
                        self.device.send_block.set()
                    await self.h.quiesce()
                    result = await self.h.finish()
                    self.assertEqual(result.reply_done, "stopped" if terminal == "stop" else "unavailable")
                    self.assertEqual(self.pcm(), b"")
                    self.assertFalse(self.session._audio.is_open)
                    self.assertEqual(self.provider.types().count("session.close"), 1)

    async def test_provider_error_with_populated_quarantine_counts_all_discarded(self):
        await self.waiting_ack()
        self.provider.push_audio(PREFIX)
        await self.h.quiesce()
        before = self.session.outcome.dropped_audio_bytes
        await self.h.push({"type": "error", "error": {
            "code": "synthetic", "message": "synthetic", "type": "synthetic"}})
        result = await self.h.finish()
        self.assertEqual(result.dropped_audio_bytes - before, len(PREFIX))
        self.assertEqual(result.reply_done, "unavailable")
        self.assertEqual(self.pcm(), b"")