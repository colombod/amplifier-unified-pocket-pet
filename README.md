# Amplifier Unified Pocket Pet

A portable Wi-Fi voice interface to a personal manager across Amplifier Unified:
start work, continue existing sessions, and answer things waiting on the human.
The animated Tachikoma, microphone, speaker and touch display provide a screen-off
companion. [Original intent](docs/intent.md) and the [draft vision](docs/VISION.md)
retain the distinction between the goal and a proven integration.

## Milestone 1 — user-confirmed spoken conversation

On 10 November 2026 the user confirmed they can talk with the assistant. Firmware
**0.3.5-amoled-v2-live** is the milestone baseline. This is a standalone voice
conversation: it has no persistent memory or Amplifier Unified access yet.
[Decisions, achievements and device lessons](docs/milestone-1.md).

## Current implementation — 10 November 2026

| Capability | Actual evidence |
| --- | --- |
| Correct display and touch profile | Connected board is **Waveshare ESP32-S3-Touch-AMOLED-1.8 V2**, 368×448 CO5300/CST820. Original LCD-1.83 link was mismatched. User confirmed factory recovery and corrected pet QR screen. |
| Animated Tachikoma | Existing user-owned ChatGPT artwork preserved; 73 RGB565 frames mapped in flash. Idle, tap-wave and thinking animations implemented. |
| Phone provisioning | Password-protected device Wi-Fi, QR join, phone page at `http://192.168.4.1`, 2.4 GHz scan, required OpenAI key, checked persistence, retry feedback and installed-version labels implemented. Successful save and restart reconnection observed; the user confirmed reconfiguration edits stay intact and the saved key is recognized. iPhone automatic captive-page opening remains unconfirmed. |
| Wi-Fi status and touch alignment | Home/Settings show link status and RSSI separately from voice state. Four-point affine touch alignment is implemented with independent center validation and NVS persistence; physical accuracy is pending. |
| Local audio and volume | User confirmed both speaker tone and recorded playback audible at about 70%. Volume persists. Microphone playback is quieter than the tone, with occasional clipped peaks; gain/fidelity still needs tuning. |
| GPT-Live voice baseline | `0.3.5-amoled-v2-live` builds with Talk/Stop, secure WebSocket streaming, visible states and managed Responses reasoning. Build and protocol checks pass. The first runtime trial authenticated and received audio, then overflowed its playback buffer. The user has confirmed working spoken conversation. Fresh sessions do not retain prior conversations; long-duration reliability and Unified integration remain unverified. See [voice test](docs/voice-test.md). |
| Unified / remote access | No Unified connector, Tailscale enrollment, cross-workspace operations or attention-answer round trip implemented. The direct OpenAI voice test is an isolated hardware spike. |

The [bring-up record](docs/device-bringup.md) includes the failed display route,
factory recovery, pairing fixes, audio changes and validation boundaries.
[Sanitized evidence](docs/device-evidence.json) records artifact identities.
Historical Linux toolkit evidence remains in [development-toolkit.md](docs/development-toolkit.md);
the Mac firmware workspace uses the Docker workflow below.

## Use the device

1. Open the gear icon, choose **Wi-Fi & OpenAI key**, and scan the QR to join its password-protected `Tachikoma-…-Setup` network.
2. If no page opens automatically, browse to `http://192.168.4.1` while still on that Wi-Fi.
3. Select a 2.4 GHz network and enter its password and your required OpenAI API key.
4. Wait until settings are confirmed saved, then choose Done. Subsequent network changes can retain the saved key.
5. If touch positions are offset, open **Settings → Touch alignment**, tap each ring center and lift, then check the fourth center point.
6. In **Settings → Audio / volume**, adjust speaker level and run the local tone or three-second record/playback test.
7. On the pet screen, the round **microphone** starts the voice session; the square **Stop** ends it.

The setup page is reachable only through the device AP; it never returns the saved
key. Provisioning changes require a per-boot token. Failed joins keep setup
available; an interrupted response is shown as uncertain until a confirmed save.
The prototype uses standard NVS, without encrypted flash or secure boot. Do not
share flash dumps. No API call or microphone capture starts on boot.

## Build on the Mac

Requires Docker Desktop. No global ESP-IDF install is used.

```sh
docker compose build
sh scripts/build.sh
```

ESP-IDF **5.5.4** is pinned by image digest. Managed versions are locked in
`dependencies.lock`: Waveshare AMOLED BSP **2.0.3**, LVGL **9.3.0**,
esp_lvgl_port **2.6.2**, esp_websocket_client **1.6.1**. Pillow **11.3.0** lives
only in the image. The exporter recreates ignored generated assets from the
unchanged source sheet; the output is `build-amoled/esp32_pet.bin`.

```sh
node tests/setup_page_test.mjs
docker compose run --rm idf bash -c 'cc -Wall -Wextra -Werror -I main tests/setup_address_test.c -o /tmp/setup_address_test && /tmp/setup_address_test'
docker compose run --rm idf bash -c 'cc -Wall -Wextra -Werror -I main tests/live_protocol_test.c -o /tmp/live_protocol_test && /tmp/live_protocol_test'
docker compose run --rm idf bash -c 'cc -Wall -Wextra -Werror -I main tests/touch_alignment_test.c -lm -o /tmp/touch_alignment_test && /tmp/touch_alignment_test'
```

These checks cover setup retry/save behavior, AP address checks including mapped
IPv6, and bounded WebSocket reassembly across TCP splits and continuation frames.
Touch-transform native tests also cover offset, scale/swap and rejected degenerate points. These checks do not call OpenAI or prove physical audio quality.

For macOS USB flashing, reproduce the allowed project-local tools with:

```sh
python3 -m venv .host-tools
.host-tools/bin/pip install esptool==4.11.0 pyserial==3.5
.host-tools/bin/python -m serial.tools.list_ports
```

Identify the actual Espressif device before opening it. Docker Desktop does not
directly expose macOS `/dev/cu.*` ports. A verified complete factory backup and
restore script exist locally; see [recovery](docs/device-bringup.md#recovery-and-installation).
Backups, secrets, raw logs, binaries and generated outputs are excluded from Git.

A later restart exposed speech crackles; the installed health candidate is
`0.3.8-amoled-v2-health`, with physical acceptance pending. The original milestone
tag remains the first spoken baseline, not a certificate of long-term audio health.

The post-restart health/review record is [PR #6 review](docs/pr-6-review.md).
The next implementation context is [Unified handoff](docs/unified-handoff.md);
the user chose the shared LAN for the first connection.

## Experiments toward the Unified goal

The existing questions remain open; none is closed by the standalone voice test.

| Spike | Remaining qualification |
| --- | --- |
| [#1 Native Tailscale](https://github.com/colombod/amplifier-unified-pocket-pet/issues/1) | Runtime reachability, reconnect, direct/DERP behavior and resource measurements on the corrected board. |
| [#2 Unified integration](https://github.com/colombod/amplifier-unified-pocket-pet/issues/2) | Installed-host capabilities, scoped authentication, exact-session continuity and broader manager operations through supported contracts. |
| [#3 Voice and concurrency](https://github.com/colombod/amplifier-unified-pocket-pet/issues/3) | First live speech reply, latency/dropout/resource measurements, touch/animation under load, then voice through the intended Unified session. |
| [#4 Attention round trip](https://github.com/colombod/amplifier-unified-pocket-pet/issues/4) | Real waiting-question cue and human answer to the correct revision, including stale/duplicate/reconnect cases. |

Current implementation scope and authorization are recorded in [PROJECT-BRIEF.md](PROJECT-BRIEF.md).
