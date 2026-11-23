"""Real-stack arrival replay; synthetic transcripts/provider, no live connection.

rig_arrivals.json is the exact arrival-times/PCM-sizes-only projection of call 1
in all 14 working-files/rig-traces/<build>--<scenario>.json inputs supplied for
manager review, 17 November 2026 (4889 frames). The production firmware voice
owner + pinned lwIP/mbedTLS/WebSocket stack ran against an impaired scripted
host, NOT this gateway or a provider. No audio, statuses or private state copied.
Answers occupied 4--9 s (4--7 s for blackout-600ms); listen-rtt160 had none.

The two model--late-600* traces are the first 100 s of the microphone message
times of apps/unified-pet/tests/bridge_converse_test.c cases late-600 and
late-600-heavy (CONVERSE_APPEND_TRACE): the production voice owner under that
fixture's model of the real pet's routinely late loop (every iteration 8-20 ms
or 20-40 ms late, 100 ms messages, an answer at 20-26 s and 80-86 s). Fixture
time with a 2 ms synthetic send, no network: device-side cadence only. A request
on such a device must not be delayed or refused. (A fresh replay session that
has heard nothing reaches the gateway's 90 s idle limit, so points stop at 87 s.)
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

from . import test_gateway_core as old
from .gatewayfakes import OfflineAsyncCase

ARRIVALS = json.loads(Path(__file__).with_name("rig_arrivals.json").read_text())
# Test instants avoid simultaneous timer/frame arrivals. Each request has its own
# real PetSession, receiving the ENTIRE preceding trace to preserve its baseline.
POINTS = {
    "base--clean": (1.0005, 5.0005, 9.0505, 11.0005),
    "cand--clean": (1.0005, 5.0005, 9.0505, 11.0005),
    "cand--talk-rtt60": (1.0005, 5.0005, 9.0505, 11.0005),
    "cand--loss-0.5pct": (1.0005, 5.0005, 5.5005, 9.0505, 14.0005),
    "cand--talk-rtt160": (0.5005, 1.0005, 3.2005, 6.0005, 9.0505, 11.0005),
    "cand--listen-rtt160": (0.5005, 1.0005, 4.5005, 7.0005, 8.0005),
    "base--talk-rtt60": (1.0005, 3.0005, 4.1005, 4.6005, 9.0505),
    "base--loss-0.5pct": (1.0005, 4.5005, 6.1005, 6.8005, 9.0505),
    "base--blackout-600ms": (1.0005, 7.0505, 10.9005, 11.5005, 13.0005),
    "cand--blackout-600ms": (1.0005, 7.0505, 10.9005, 11.5005, 13.0005),
    "base--blackout-2500ms": (1.0005, 3.0005, 5.2005, 6.0005, 9.0505),
    "cand--blackout-2500ms": (1.0005, 3.0005, 5.2005, 6.0005, 9.0505),
    "base--talk-rtt160": (0.5005, 2.2005, 4.1005, 9.0505),
    "base--listen-rtt160": (0.5005, 2.2005, 4.1005, 7.0005),
    "model--late-600": (1.0005, 12.0005, 21.5005, 27.0005, 50.0005, 81.0005, 87.0005),
    "model--late-600-heavy": (1.0005, 12.0005, 21.5005, 27.0005, 50.0005, 81.0005, 87.0005),
}
EXACT = {"base--clean", "cand--clean", "cand--talk-rtt60", "cand--loss-0.5pct",
         "model--late-600", "model--late-600-heavy"}
JITTER = {"cand--talk-rtt160", "cand--listen-rtt160"}
# Points with no recoverable continuous input remaining, rather than an EOS
# inference from packet quiet. The replay deliberately withholds disconnect:
# PetSession must fail closed itself even if TCP never reports the dead link.
FAIL_FROM = {
    "base--talk-rtt60": 3,
    "base--loss-0.5pct": 6,
    "base--blackout-600ms": 10,
    "cand--blackout-600ms": 10,
    "base--blackout-2500ms": 5,
    "cand--blackout-2500ms": 5,
    "base--talk-rtt160": 0,
    "base--listen-rtt160": 0,
}


class TraceHarness(old.Harness):
    async def quiesce(self):
        # Real reader/dispatcher/outbox/worker tasks, less redundant settling
        # than the general-purpose harness for thousands of 20 ms arrivals.
        for _ in range(3):
            await self.runner.wait_idle()
            for _ in range(5):
                await asyncio.sleep(0)


class ArrivalReplayTest(OfflineAsyncCase):
    rows = []

    async def replay(self, name, point):
        h = TraceHarness()
        await h.open()
        try:
            await h.boot()
            await h.feed('{"type":"pet.converse.begin"}')
            origin = h.clock.now()
            schedule = [(ms / 1000, size) for ms, size in ARRIVALS[name]]
            schedule.append((point, None))
            for at, size in sorted(schedule):
                await h.clock.advance(max(0, origin + at - h.clock.now()))
                if size is None:
                    await h.push(old.delta("prefix", "please check everything", 0, 100))
                    await h.push(old.delegation("trace"))
                else:
                    await h.feed({
                        "type": "session.input_audio.append",
                        "audio": base64.b64encode(bytes(size)).decode("ascii"),
                    })
                if h.ops.count("begin") or h.task.done():
                    break
            # Recorded audio ends here, never synthesize replacement audio.
            if not h.ops.count("begin") and not h.task.done():
                await h.clock.advance(10)
            began = h.ops.times("begin")
            at = None if not began else began[0] - origin
            self.rows.append((name, point, at, h.session.outcome.conversation_end))
            if point >= FAIL_FROM.get(name, float("inf")):
                self.assertIsNone(at, "submitted a prefix on irrecoverably late input")
                self.assertEqual(h.session.outcome.conversation_end, "not_sent")
                self.assertEqual(h.session.outcome.requests_submitted, 0)
                # At these late injection points no words have arrived by the
                # 12-second recognition hint. It is informational, not a send.
                expected = ["not_sent"]
                if name.endswith("--blackout-600ms") and point == 13.0005:
                    expected.insert(0, "no_words_yet")
                self.assertEqual([notice["code"] for notice in
                                  h.device.of_type("pet.converse.notice")], expected)
            else:
                self.assertIsNotNone(at, "working-link settle missed before input ended")
                if name in EXACT:
                    self.assertAlmostEqual(at, point + 1.5, places=7)
                else:
                    self.assertLessEqual(at, point + 1.5 + 0.5 + 1e-7)
                self.assertEqual(h.ops.count("confirm"), 1)
                # Independent trace arithmetic at the actual begin, not a second
                # settle implementation: no send while known >300 ms behind.
                received = [(ms / 1000, n) for ms, n in ARRIVALS[name] if ms / 1000 <= at]
                total, minimum = 0, float("inf")
                for arrived, n in received:
                    total += n
                    minimum = min(minimum, arrived - total / 48000)
                self.assertLessEqual(at - minimum - total / 48000, 0.3 + 1e-7)
                self.assertEqual(h.session._pout.pending_audio, 0)
        finally:
            await h.aclose()

    @classmethod
    def tearDownClass(cls):
        if os.environ.get("PET_TRACE_REPORT"):
            for name, point, at, end in cls.rows:
                result = end if at is None else f"added={at - point - 1.5:.6f}s"
                print(f"TRACE {name} point={point:.4f} {result}", flush=True)
        super().tearDownClass()


class AccumulatedInputWaitTest(OfflineAsyncCase):
    async def asyncSetUp(self):
        self.h = TraceHarness()
        self.addAsyncCleanup(self.h.aclose)
        await self.h.open()
        await self.h.boot()
        await self.h.feed('{"type":"pet.converse.begin"}')
        await self.audio(5, 0.1)
        await self.h.push(old.delta("prefix", "please check everything", 0, 100))
        await self.h.push(old.delegation("flapping"))

    async def audio(self, count, interval):
        for _ in range(count):
            await self.h.feed(old.audio_frame(2400))
            await self.h.clock.advance(interval)

    async def flap(self):
        await self.h.clock.advance(0.6)  # genuine stall beyond the 300 ms tolerance
        await self.audio(12, 0.05)  # 2x to restore the known timeline
        await self.audio(6, 0.1)  # caught-up interval, shorter than transcript quiet

    async def test_short_stalls_accumulate_and_fail_closed_at_six_seconds(self):
        recovered = []
        for _ in range(12):
            await self.flap()
            if self.h.task.done():
                break
            self.assertIsNone(self.h.session._input_wait_t, "flap did not recover")
            recovered.append(self.h.session._input_wait_s)
            self.assertEqual(self.h.ops.count("begin"), 0)
        self.assertGreater(len(recovered), 2)  # multiple individually recovered waits
        self.assertTrue(all(0 < b - a < 1.5 for a, b in zip(recovered, recovered[1:])))
        self.assertTrue(self.h.task.done(), "flapping deferred the request without limit")
        # Recorded sum + last open interval is exactly the budget, not a fresh
        # six-second allowance for the final flap.
        session = self.h.session
        spent = session._input_wait_s
        if session._input_wait_t is not None:
            # Close's first entry time is the timer at which the budget expired.
            spent += session._close_hard - 15 - session._input_wait_t
        self.assertAlmostEqual(spent, 6.0, places=7)
        self.assertEqual(session.outcome.conversation_end, "not_sent")
        self.assertEqual(session.outcome.requests_submitted, 0)
        self.assertEqual(self.h.ops.count("confirm"), 0)

    async def test_finite_flaps_can_recover_and_send_once(self):
        for _ in range(3):
            await self.flap()
        self.assertGreater(self.h.session._input_wait_s, 0)
        self.assertLess(self.h.session._input_wait_s, 6)
        self.assertEqual(self.h.ops.count("begin"), 0)
        await self.audio(20, 0.1)
        self.assertEqual(self.h.ops.count("confirm"), 1)
        self.assertEqual(self.h.ops.staged, [("prefix", "please check everything")])

    async def test_repeated_provider_pending_intervals_share_the_same_budget(self):
        recovered = []
        for _ in range(14):
            gate = self.h.provider.send_block = asyncio.Event()
            await self.audio(6, 0.1)
            gate.set()
            await self.h.quiesce()
            if self.h.task.done():
                break
            await self.audio(5, 0.1)
            if self.h.task.done():
                break
            recovered.append(self.h.session._input_wait_s)
            self.assertIsNone(self.h.session._input_wait_t)
            self.assertEqual(self.h.ops.count("begin"), 0)
        self.assertGreater(len(recovered), 2)
        self.assertTrue(all(b > a for a, b in zip(recovered, recovered[1:])))
        self.assertTrue(self.h.task.done(), "provider backlog flaps reset the budget")
        self.assertEqual(self.h.session.outcome.conversation_end, "not_sent")
        self.assertEqual(self.h.ops.count("confirm"), 0)


def _trace_test(name):
    async def test(self):
        for point in POINTS[name]:
            with self.subTest(trace=name, point=point):
                await self.replay(name, point)
    return test


for _name in POINTS:
    setattr(ArrivalReplayTest, "test_" + _name.replace("-", "_").replace(".", "_"),
            _trace_test(_name))