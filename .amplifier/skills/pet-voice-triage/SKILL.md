---
name: pet-voice-triage
description: Find why a spoken call on the pocket pet failed or sounded wrong, from recorded counters, before changing any code. Use for "That conversation is busy", silence after Listening, a mic that turns itself off, "Not sent", Reconnecting, crackling or choppy replies, long silence before an answer, or a request that never reached Amplifier Unified.
---

# Pet voice triage

One symptom has several causes here. Read the records of the failed call first;
change nothing until a counter names the cause. Do not ask the person to repeat
the call, toggle the mic or reboot.

## Evidence, in this order

1. **Gateway event log**: the file given to `pet_bridge serve --event-log`. One
   `session_end` line per call, counts only. Compute `audio_in_bytes / 48000`
   against the call length (from the `device_connect` line before it). This is
   stream duration, not microphone content. Where present, `input_frames_zero`
   and `input_frames_nonzero` count admitted silent and non-silent frames, not
   recognized speech. Older records have no signal counters; do not infer them.
2. **Unified's access log** for the gateway's requests: status of
   `GET /api/sessions`, `GET /api/conversation/detail`, `POST .../commands`, and
   how long the POST took.
3. **The pet's own report** (bench build only): `pet_bench.py last` from the
   `pet-device-bench` skill. It survives until the pet restarts.
4. **Signal**: the report's `wifi` record, or `pet_bench.py scan`.

## Reading the symptom

| Reported | Counter | Cause |
| --- | --- | --- |
| Request refused at preflight | `refused_preflight` > 0 | Older gateways said “busy” for any failed idle check. In this source, known activity reports busy; inability to read or find the conversation reports not sent. Inspect the listing/access result before attributing the cause. |
| Stays on Listening, nothing happens | microphone under about 90% of the call length | Starved microphone. Check signal first (weak access point after an unclean restart), then the pet report for late sends. |
| Stays on Listening, nothing happens | microphone near 100%, `transcript_segments` 0 | Nothing was recognised. Not explained yet: record call time, signal and whether the person spoke. |
| Answer comes from the voice model, not Unified | `transcript_segments` > 0, `delegations` 0 | The question did not need Unified. Test with one that does ("what is this chat's title"). |
| Mic turns itself off, "Not sent" | `refused_input_late` 1, `conversation_end` `not_sent` | The gateway waited 6 s for late microphone audio and gave up. Same cause as a starved microphone. |
| Long silence, then an answer | `requests_accepted` 1, POST took tens of seconds | Unified was starting a dormant conversation. With the progress-line gateway `progress_rendered` should be above 0. |
| Reconnecting, call ended by itself | pet report `exit_reason` 2, or `conversation_end` `device_lost` | Microphone buffer overflow (old firmware) or a stall longer than about 5 s. Check signal and the report's `send_us_max`. |
| Crackling | pet report `play_write_us_max` above 60000 | The speaker task ran late (codec DMA is 60 ms). Check audio task priority and core. |
| Short gaps in replies | pet report `playback` underruns with `arrival_gap_ms_max` above the prebuffer | Reply audio arrived late over the network. |
| `overflow`, then `device_connect` refused 409 | first call starved, pet redialled | The gateway was still closing the first call. Treat as a starved microphone. |

## Rules

- The first call after a restart is the one that has failed most. Check it explicitly.
- The normal build keeps no report. If the cause is not in the gateway log, install
  the bench build and have the person make one call, then read `last`.
- Record an unexplained case in the project's findings; do not fold it into a
  neighbouring cause.

Done when: the counter that names the cause is quoted, with the call's time, and
what remains unexplained is stated.
