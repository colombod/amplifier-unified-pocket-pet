# Milestone 1: a working voice pet

**Accepted by the user on 10 November 2026.** The user confirmed “we can talk with
assistant” and named this milestone 1. Firmware **0.3.5-amoled-v2-live**, tagged
`milestone-1-voice`, is the baseline. Its app was installed at 0x10000 with esptool
hash verification. The exact binary SHA256 and size are in
[device-evidence.json](device-evidence.json). Binaries and private recovery data
remain local; the source, dependency lock, Docker image pin and exporter are committed.

The acceptance is a working standalone spoken conversation. The user also
confirmed its current limitation: it is a private chat with no persistent memory.
It is not yet a useful Amplifier Unified connector. No existing Unified session,
workspace operation, waiting-question answer or away-from-home connectivity is
claimed. Long-duration stability, measured latency, AEC/barge-in and physical
accuracy after touch calibration are not certified by this milestone.

## Achievements and evidence

| Achievement | Evidence and practical boundary |
| --- | --- |
| Isolated development environment | Docker ESP-IDF 5.5.4, pinned image digest, container compiler/CMake/Ninja and Pillow; only esptool/serial in a project-local Mac venv. No global ESP-IDF installation. |
| Correct hardware and exercised recovery | Factory image identified AMOLED 1.8 V2; original 16 MiB dump verified against board, full restore exercised, factory display/touch user-confirmed. |
| Existing Tachikoma embedded | Original ChatGPT sheet preserved and selection unchanged; deterministic 73-frame conversion mapped in flash, avoiding a whole-sheet RAM load. |
| Phone-first provisioning | QR-protected AP, AP-only page, required first OpenAI key, persistent Wi-Fi/key, checked saves and explicit retries. New settings commit and restart reconnect observed. User confirmed fields now survive editing and the saved key is recognized. |
| Local audio and persistent volume | User heard tone and recorded playback around 70%. Voice is quieter than the tone; occasional clipped input peaks prohibit an unmeasured blanket gain boost. |
| Standalone GPT-Live speech | User confirmed talking with the assistant after the installed 0.3.5 baseline. Prior session-start/close events and aggregate diagnostics remain separately recorded. |
| Small-screen controls | Gear opens Settings, separating Wi-Fi/key and audio controls from the pet. Round microphone becomes square Stop. Wi-Fi/RSSI and voice state are separate indicators. |
| Touch correction path | Three raw taps fit an affine transform, fourth center tap checks it before saving to its own NVS namespace. Native math checks pass; physical accuracy acceptance is still pending. |
| Recoverable upstream record | Original intent, draft vision and Linux toolkit records preserved; firmware, tests and sanitized bring-up evidence pushed in PR #6. No private dump, credentials, raw audio or logs committed. |

## Core decisions at this milestone

| Decision | Rationale and status |
| --- | --- |
| Use the verified AMOLED V2 identity | The initial LCD-1.83 reference was wrong for the actual connected unit. Use Waveshare AMOLED BSP 2.0.3, CO5300/CST820, 368×448, ES8311 audio, 16 MiB flash and 8 MiB octal PSRAM. Hardware finding, not an interchangeable pinout choice. |
| Keep the Mac toolchain isolated in Docker | User requested isolated tooling. Project-local USB tools are a necessary macOS Docker Desktop exception; no global SDK installation. |
| Reuse the user's existing pet pixels | Preserve artwork and ChatGPT selection. Convert deterministically into flash-mapped RGB565; do not load the 14 MB RGBA sheet into 8 MiB PSRAM. |
| Configure on the phone | The touch display is too small for network passwords and API keys. The device provides QR join and a simple setup page. |
| Require a key on first setup and retain it across network changes | Wi-Fi and API credentials are independent settings. Blank replacement-key input keeps the existing key; the page never returns its value. Standard NVS is a prototype limitation, not encrypted credential storage. |
| Start capture/API only by explicit user action | No microphone or billed voice session on boot. Once started, conversation remains open until Stop or failure; the temporary three-minute cap was removed at the user's request. |
| Use the actual GPT-Live protocol | `gpt-live-1` at `/v1/live/sessions`, session.start/started, PCM append/delta, graceful close/closed. Do not silently substitute legacy Realtime. |
| Keep voice frontend separate from reasoning and future Unified transport | Managed gpt-6-luna Responses delegation is an isolated spike backend. The product destination is Amplifier Unified, not this private chat or the historical OpenClaw alternative. |
| Separate capture, network send and playback scheduling | Continuous duplex traffic cannot share one blocking audio loop. Queues are bounded; WebSocket separate TX locking is enabled to avoid receive/send contention. |
| Detect audible PCM before suppressing microphone transmission | Live can return continuous digital silence. Packet arrival is not speech and must not silence the microphone. The current speaker gate is a temporary no-AEC tradeoff, not barge-in support. |
| Verify a new save by its own generation | Polling an existing saved configuration must not clear what a user is typing. Only the current submitted generation can clear fields after commit. |
| Measure touch alignment | No guessed global offset. Preserve the vendor callback, collect raw points, verify independently and persist only the checked correction. |
| Keep recovery and evidence separate from product claims | A passing build, successful flash, clean log and user-heard reply prove different things. Keep the full private backup and last audible local-audio app available for rollback. |

## Lessons from this device

1. **Identify the physical board before trusting a product link.** Closely named
   Waveshare boards have different panel drivers and pin mappings. Software
   UI-ready/flush counters did not make the wrong panel work.
2. **Back up first and exercise recovery.** The original app was useful identity
   evidence, and a verified factory restore provided a known-good screen/touch baseline.
3. **USB serial observation can reset the board.** Nominal no-reset flags did not
   prevent USB_UART_CHIP_RESET on this setup. Open a bounded observer before a
   test and keep it open; do not reopen it during pairing or a live conversation.
4. **Return codes are not audibility.** The first tone test completed silently.
   Volume, amplifier setup, codec lifecycle and playback drain needed physical checks.
5. **Use the official exact-board audio example.** Shared I2C and duplex codec
   ownership matter. Keep the bus alive for display/touch and release only owned
   audio resources. Local tests and live audio must not compete for the codec.
6. **Continuous incoming audio includes silence.** Treating every chunk as speaking
   suppressed the microphone indefinitely. Peak-based audibility checks now have
   a regression test; transcript/audio content stays unlogged.
7. **Blocking work in a real-time path creates a queue failure.** Separate capture,
   playback and sending; inspect transport locking as well as buffer sizes. Larger
   buffers alone did not diagnose the microphone problem.
8. **A queue overflow does not prove Wi-Fi disconnected.** Show Wi-Fi/RSSI,
   API-session state and specific stream errors separately.
9. **An already configured device is a distinct provisioning case.** Saved-status
   polling cleared replacement credentials while typing. Test old-save/new-edit,
   lost acknowledgements and generation-confirmed commits, not only first setup.
10. **Inspect actual socket address families.** HTTPD's dual-stack mapped IPv6
    address was truncated by an IPv4-only AP check, rejecting legitimate iPhone requests.
11. **Input level needs evidence.** Quiet playback and clipped peaks can coexist.
    Tune speech conditioning with measurements rather than simply raising gain.
12. **Small visible icons still need generous hit areas and measured mapping.**
    The user reported offset even on large buttons, which is distinct from icon size.
13. **Voice connectivity is only the first product milestone.** A new private
    conversation with no memory does not manage the user's existing work.

## Post-acceptance health finding

A subsequent restart produced crackling at idle and in playback, reported by the
user. The original milestone tag remains immutable as the first spoken baseline;
audio health was reopened before merge. Firmware 0.3.6 adds explicit boot/cleanup
amplifier-off control and buffered playback. Physical restart acceptance is
pending; see the bring-up and device-evidence records.

## Direction after the baseline

The user explicitly wants to move forward with **Amplifier Unified integration**.
The next useful outcome is voice attached to persistent host-owned conversations
and the intended workspace/session, using supported authenticated contracts.
Existing-session continuity remains required alongside the broader ability to
manage workspaces and start new work. Waiting-question answers must preserve
question identity, revision and genuine human provenance. No uncertain command
should be replayed automatically.

The persistent global-manager/direct-focus interaction in VISION.md is still a
proposal, not newly ratified by milestone 1. Native ESP32 Tailscale remains a
connectivity spike, not a selected production route. The next implementation
must qualify installed-host capabilities and scoped access before deployment;
this milestone neither changes servers nor enrolls anything into a tailnet.

References: [original intent](intent.md), [draft vision](VISION.md),
[bring-up chronology](device-bringup.md), [voice test](voice-test.md),
[PR #6](https://github.com/colombod/amplifier-unified-pocket-pet/pull/6).


## Reviewed audio baseline

Firmware **0.3.8-amoled-v2-health** subsequently passed the user’s two-start/stop
check: the initial crackle was gone and speech remained clean. The first milestone
tag remains immutable; `milestone-1-reviewed` identifies the reviewed health
baseline. Saved-network retry and pending-save/new-edit review findings were
fixed and regression-tested before merge.

Two additional device lessons: keep the amplifier off while silent DMA and DAC
references settle, and treat Wi-Fi modem sleep as a real-time audio latency
tradeoff. The current code uses a 320 ms jitter buffer and disables modem sleep
only for a voice session, restoring the prior policy afterward. These changes
passed the stated user check; they do not certify every router, long-duration
workload or battery envelope.
