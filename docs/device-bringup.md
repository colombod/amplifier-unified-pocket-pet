# Device bring-up record — 10 November 2026

This records the work in the Mac device chat. Statements about earlier attempts
are historical; the current capabilities and limits are in the root README.
Original repository intent, issue history and Linux toolkit evidence are preserved.
No private flash dump, credentials, raw serial capture or recorded speech is included.

## Environment and artwork

The user authorized `/Users/colombod/workspaces/esp32-pet` and Docker-isolated
tooling. ESP-IDF 5.5.4 runs in the official Linux ARM64 image pinned to
`sha256:f3bc9daebfd8cb4e0afbdced7caac4431c7a862326a5e5587d65c31439549bb5`.
CMake, Ninja, Xtensa compiler and managed dependencies stay in Docker. Pillow
11.3.0 is container-only. The Mac `.host-tools` venv contains esptool 4.11.0 and
pyserial 3.5 for USB access; no global ESP-IDF install was made.

The existing ChatGPT Tachikoma (`pet_6abe2e4dd1f48191bbbc9389b55e641e`) was
downloaded read-only. Its selection and artwork were not changed. Source PNG
SHA256: `51264950be47d773608a5b521309626a462fd3b0c1bf2a76daee261e1bccc103`.
The source is 1536×2288 with 192×208 cells. Decoding the whole RGBA sheet would
require 14,057,472 bytes, exceeding 8 MiB PSRAM. `scripts/export_pet.py` instead
composites 73 existing frames deterministically onto the display background.
The 5,830,656-byte RGB565 pack remains aligned in mapped flash; each frame is
79,872 bytes. Generated output is rebuilt rather than committed.

## Hardware correction and recovery

The first implementation followed the user-supplied LCD-1.83 reference and
ST7789 mapping. The old factory image remained frozen, followed by a completely
dark screen in the display probe. UI-ready logs, advancing LVGL timers and
returning flush calls proved software activity but did not prove visible output.
This was an incorrect hardware profile, not an accepted display result.

Before replacing firmware, the complete 16 MiB original flash was backed up and
verified against the board with esptool digest verification. Its factory app
identified `ESP32-S3-Touch-AMOLED-1.8`, the corresponding BSP and CO5300 driver.
The full factory image was restored and verified; the user confirmed factory
screen and touch worked again. The corrected official **AMOLED 1.8 V2 BSP 2.0.3**
uses 368×448 CO5300 and CST820 (detected by its CST816S-compatible driver at 0x15).
The user then confirmed the pet's Connect your phone QR setup screen was visible.

The correct ES8311 audio wiring comes from the pinned AMOLED BSP: I2S BCLK9,
MCLK16, LRCK45, DOUT8, DIN10 and active-high amplifier GPIO46; shared I2C14/15.
The audio adapter follows the official Waveshare Brookesia duplex helper rather
than mixing incompatible BSP speaker/microphone convenience handles.

Sources: [official board documentation](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-1.8),
[factory V2 profile](https://github.com/waveshareteam/ESP32-S3-Touch-AMOLED-1.8/blob/main/firmware/brookesia/README.md),
[official audio helper](https://github.com/waveshareteam/ESP32-S3-Touch-AMOLED-1.8/blob/main/firmware/brookesia/components/bsp_extra/src/bsp_board_extra.c).
The exact runtime dependency versions are locked in `dependencies.lock`.

## Pairing changes and observations

| Build / step | Change and evidence |
| --- | --- |
| Corrected AMOLED baseline | Pet and QR setup displayed; factory recovery already user-confirmed. |
| 0.1.1 pairing address fix | The joined iPhone received “Open setup using the device Wi-Fi.” HTTPD used a dual-stack socket while the address check truncated it to IPv4. `sockaddr_storage` plus explicit native/mapped AP checks fixed this. Native regression tests reject foreign and truncated addresses. |
| 0.1.2 pairing reliability | User reported save failure and page loss; exact error was not recalled. Delayed radio transition lets the HTTP acknowledgement leave first. Page retains masked inputs across lost acknowledgements, reports uncertainty and server errors, and gates Done on connection plus committed settings. AP remains on failure; stable AP password avoids cached-password mismatch. The precise original failure was not proven. |
| 0.1.3 version visibility | Running version is server-rendered into phone HTML and returned by status; device setup also shows it. NVS free entries ruled out observed storage exhaustion. Private status evidence includes request accepted, station acquired IP and settings committed. |
| 0.1.4 foreground fix | User could not see the device version; labels now use explicit light foregrounds on the dark panel. Saved Wi-Fi reconnect was observed across later boots. |

Setup uses an AP-only HTTP server, per-boot mutation token, no-store responses,
text-rendered SSIDs, required first API key, checked NVS commits and no key echo.
The phone can retain a saved key when changing networks. Wildcard DNS and DHCP
captive hints are implemented, but iPhone automatic opening is not confirmed;
manual `http://192.168.4.1` works when joined to device Wi-Fi. Enterprise Wi-Fi,
5 GHz and router captive-login flows are not implemented.

Opening this USB serial port can reset the board even with nominal no-reset
flags. A later serial read could therefore destroy the state being investigated.
Start one bounded observer before a user test and keep it open; do not reopen
serial during pairing or voice use. Logs stay in ignored private storage.

## Local audio changes and acceptance

Version 0.2.0 introduced explicitly initiated local tests: speaker tone and
three-second PCM16/mono/24 kHz capture to 144 KB PSRAM, playback, then wipe/free.
The first software-successful test was silent to the user. Success return codes
were not treated as physical audio acceptance.

Version **0.2.1-amoled-v2-audio** added a persistent 0–100% volume slider, default
60%; a one-second 440 Hz tone at amplitude 8000 with fades; and a 120 ms playback
DMA drain before codec release. Speaker-only tests open DAC only. A single worker
owns the codec, I2S channels and cleanup; the shared display/touch I2C bus remains.
The user confirmed **both tone and recorded playback audible** near 70% volume.
They subsequently reported voice playback was much quieter than the tone.

Two private test summaries reported RMS 1874 / 1663, DC 24 / 26, peak 32768,
and 185 / 158 clipped samples out of 72000. These aggregate measurements contain
no speech. They show why blindly increasing the existing 24 dB microphone gain
is not justified. Speech fidelity and input conditioning remain to be tuned.
No raw audio was saved or uploaded by the local tests.

## GPT-Live implementation

Version **0.3.0-amoled-v2-live** adds explicit Talk/Stop, secure WebSocket transport,
24 kHz PCM16 streaming, bounded PSRAM playback buffers, fragmented-message checks,
visible connecting/listening/thinking/speaking/error/disconnected states, managed
Responses reasoning, and graceful `session.close` / `session.closed` handling.
The initial candidate had a three-minute test limit, removed in 0.3.1 at the
user’s request. A Talk-initiated conversation stays open until Stop or a failure;
there is no automatic reconnect.
Microphone transmission becomes silence during speaker playback and a short tail;
full-duplex echo cancellation and barge-in are not validated. See [voice-test.md](voice-test.md).

The direct OpenAI test has **no Amplifier Unified connector**. Build and native
protocol checks passed. A successful spoken API round trip, touch/animation under
voice load, latency, dropouts, CPU envelope and live memory headroom remain to be
measured. Existing spike #3 remains open, as do connectivity, Unified integration
and attention-round-trip spikes #1/#2/#4.

## Recovery and installation

The complete original backup remains private at
`private-backups/factory-20261110/factory-flash.bin` with its local digest manifest.
It can contain credentials and is deliberately excluded from Git. Recovery was
exercised successfully. `scripts/restore_factory.sh ACTUAL_PORT` restores it at
0x0; do not run it as an environment test. Verify the actual port/device first.
The last user-accepted audio app is also retained privately for app-only rollback.

Current incremental installations write only the app at **0x10000** with the
build's DIO / 16 MB / 80 MHz flash settings, retaining the partition table and NVS.
The app partition is 14 MiB; the live candidate is about 7 MiB and fits. No OTA
layout is included. Build success, flash hash verification, clean startup and
user/API acceptance are recorded separately in [device-evidence.json](device-evidence.json).

## Alignment with upstream

On 10 November the user supplied the upstream repository, asked for all work and
steps to be pushed, and asked this chat to take over the work. Its original intent,
draft vision, prior toolkit evidence and Git history are preserved. The broader
goal includes new Unified workspaces/work and exact existing-session continuity;
the direct voice spike does not replace either. No server, Tailscale, workspace,
session or waiting-question operation was performed by this device work.


### First live runtime result and correction

The installed 0.3.0 booted cleanly and rejoined saved Wi-Fi. Two user-initiated
sessions reached `session.started`, opened the audio stream and received output
audio. Internal free heap at stream startup was approximately 96 KB and free
PSRAM approximately 7.94 MB. Both trials overflowed the original 192 KiB (about
4.1 seconds) playback queue; the first also confirmed `session.closed` afterward.
An in-flight worker update briefly overwrote the error display. These are observed
failures, not a successful voice acceptance result.

Version **0.3.1-amoled-v2-live** increases the still-bounded queue to 1 MiB
(about 21.8 seconds of PCM), latches errors against concurrent worker updates,
and adds aggregate received/played byte counts and output peak without logging
content. It explicitly unmutes at first live playback, matching the accepted
local playback path. The user heard no speech in 0.3.0, despite returned audio. A full
spoken reply and Stop flow remain unconfirmed pending the corrected trial.


### Settings and voice controls, 0.3.2

At the user’s request, Wi-Fi/API-key setup and Audio / volume now live inside a
Settings page opened by a small gear icon. The home screen keeps the pet and a
round microphone control, which changes to a square Stop icon while the session
is active. Labels retain explicit Start voice / Stop listening wording. Firmware
version is also shown in Settings. These LVGL changes build successfully; their
physical appearance and touch navigation still require a user check.


### Silent-output diagnosis and stream separation, 0.3.3

The user confirmed Settings navigation worked in 0.3.2 but heard no speech. The
runtime summary showed 1,152,000 output bytes received, 459,840 submitted for
playback, **output peak 0**, and finalization confirmed on Stop, at volume 100%.
Those returned bytes were digital silence. The original code incorrectly treated
any output packet as audible speech and suppressed microphone transmission;
continuous silence therefore prevented the model hearing the user. Capture,
network sends and playback in one loop also failed to keep pace with output.

Version **0.3.3-amoled-v2-live** uses independent bounded capture/playback workers
and sends input in approximately 100 ms batches. Only playback with peak at least
250 suppresses mic transmission, with the same short tail. A two-second capture
queue and the existing 1 MiB playback queue fail explicitly on overflow. Stop
joins both workers before releasing the codec. Aggregate input/output peaks and
byte counts support diagnosis without retaining speech. The new build passes;
physical voice acceptance remains pending.

The user also reported difficult taps, possibly an offset. The official BSP uses
368×448 touch bounds with no swap/mirror, consistent with display rotation; no
unmeasured calibration transform was invented. Expanded gear, voice and button
hit areas are included. Whether large Settings buttons are offset as well is
still awaiting a user observation; touch accuracy is not claimed resolved.
