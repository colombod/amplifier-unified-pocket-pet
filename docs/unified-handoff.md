# Handoff: turn the working voice pet into a Unified assistant

## User direction

The user has a working standalone voice pet and wants a useful portable interface
to Amplifier Unified, with durable conversation and access to their real work.
They explicitly paused integration to check post-restart crackling and review
PR #6, then requested a handoff after satisfactory health/review and merge.

**Use the shared local network first.** The user states device and server are
currently on the same network; Tailscale is for later remote reachability, not a
prerequisite for this first LAN integration. Do not enroll a tailnet client,
change a router, enable public exposure or replace the working voice baseline
merely to start integration.

## Baseline and repository

Repository: https://github.com/colombod/amplifier-unified-pocket-pet

- `milestone-1-voice` / fdd783e preserves the first user-confirmed spoken baseline,
  firmware 0.3.5. Decisions, achievements and lessons: [milestone-1.md](milestone-1.md).
- PR #6 contains the device implementation plus subsequent health/review fixes.
  Consult its actual merge state and the current main commit before continuing.
- The accepted health baseline is **0.3.8-amoled-v2-health**: the user confirmed
  two clean starts/stops with clean speech. `milestone-1-reviewed` preserves the
  reviewed baseline. Latest physical verdict,
  artifact hash, installation status and limits are in [device-evidence.json](device-evidence.json).
  Do not infer acceptance from the version number alone.
- [Bring-up history](device-bringup.md) and [PR review](pr-6-review.md) retain
  observed failures and the distinction between build, flash, logs and audibility.

Hardware is the actual **Waveshare ESP32-S3-Touch-AMOLED-1.8 V2**, 368×448
CO5300/CST820, ES8311, 16 MiB flash and 8 MiB octal PSRAM. The original LCD-1.83
link is wrong for this connected unit. Use the pinned official AMOLED BSP 2.0.3;
never reuse the LCD/ST7789 mapping. SDK/compiler/dependencies run in Docker.

## What exists

Animated original Tachikoma pixels are flash-mapped. Phone QR/AP setup saves
2.4 GHz Wi-Fi and a required initial OpenAI key independently. Blank replacement
key retains the existing key. Current edits survive old saved-state polls and
new edits invalidate prior pending save confirmations. Saved networks retry
with bounded backoff without interrupting recent provisioning activity.

Settings holds Wi-Fi/key, persistent volume/local tests and four-point measured
touch alignment. Home and Settings distinguish Wi-Fi/RSSI from voice status.
Talk explicitly starts capture/API; Stop ends capture and closes the session.
The voice model is **gpt-live-1**, Marin, PCM16 mono 24 kHz. The current isolated
backend is managed **gpt-6-luna Responses**. There is no Unified connector and no
persistent memory across fresh voice sessions. Its spoken answers do not run
work in Unified.

`main/assistant_config.h` is the voice/backend configuration seam. `live_voice.c`
handles verified TLS, startup/close, bounded streams and separate capture/playback.
It distinguishes digital silence from audible output. Speaker playback gates
input until AEC is qualified; barge-in/wakeword and long-duration robustness are
not certified. Boot and cleanup explicitly disable the amplifier. The health
update adds a 320 ms playback prebuffer and underrun diagnostics, and disables
Wi-Fi modem sleep only during voice, restoring the previous setting afterward.

## Supported Unified integration facts

Public source documentation was read at commit
`2788dda66f748b90fcc9968ab80467df3aded8ba` on 10 November 2026. This is a source
reference, **not** proof of the installed service version or capabilities:

- [Live sessions v1](https://github.com/microsoft/amplifier-unified/blob/2788dda66f748b90fcc9968ab80467df3aded8ba/docs/clients/live-sessions.md)
  documents authenticated client attachment, session discovery/snapshots, commands
  with stable IDs, SSE and action-schema discovery. Attachment does not transfer
  Foundation execution ownership.
- [Native/TUI handoff](https://github.com/microsoft/amplifier-unified/blob/2788dda66f748b90fcc9968ab80467df3aded8ba/docs/clients/tui-handoff.md)
  describes the existing control bearer credential as broad service control, not
  restricted per-device access. Keep that credential server-side; do not put it
  on the ESP32 or expose it through the phone page.
- [Observations v1](https://github.com/microsoft/amplifier-unified/blob/2788dda66f748b90fcc9968ab80467df3aded8ba/docs/clients/observations.md)
  is optional capability-discovered functionality. Do not assume support from the
  product name or replace the shipped HTTP/SSE interface with draft AHP/ACP.

The user supplied their existing HTTPS/Tailscale service address in the device
chat. A non-authenticated Mac probe reached it with valid TLS: root returned 307
and `/api/actions` returned **401**. No credential was obtained, no authenticated
attachment or workspace/session operation was performed, and no server was changed.
The actual LAN endpoint, installed capabilities and owner-authorized credentials
still need qualification; a Mac tailnet connection does not prove ESP32 reachability.
The private endpoint is deliberately not copied into this committed handoff.

## Intended first useful proof

Use a supported authenticated connection to the running host, discover its actual
action schemas and bind voice to an explicitly chosen persistent Unified
conversation. A spoken input and its returned answer must belong to that same
host-owned conversation, with prior context retained across a device voice-session
restart. Retain stable command IDs/receipts; uncertainty must not automatically
resubmit an input. Reconnection or session selection does not start work, acquire
ownership, warm a runtime or forge human provenance.

The broader user goal includes managing workspaces, creating new work, continuing
existing sessions and answering genuine waiting questions. The global-manager /
direct-focus interaction in VISION.md is still a proposal, not a newly ratified
architecture. Do not quietly substitute a new isolated chatbot for existing-session
continuity. Waiting-question answers require exact identity, revision and actual
human input; a general spoken instruction is not an invented approval receipt.

The old Raspberry Pi/OpenClaw/Slack/Vercel proposal is historical. No such service,
cloud queue or distributed orchestration is required or implemented here.

## Reproduction and recovery

```sh
docker compose build
sh scripts/build.sh
node tests/setup_page_test.mjs
docker compose run --rm idf bash -c 'set -e; for name in setup_address live_protocol touch_alignment network_retry; do cc -Wall -Wextra -Werror -I main tests/${name}_test.c -lm -o /tmp/${name}_test; /tmp/${name}_test; done'
```

Host USB tools are project-local; the actual port must be identified before use.
App-only writes use 0x10000 and preserve NVS. A complete private factory backup
was digest-verified and restored successfully; `scripts/restore_factory.sh`
requires that original local backup and the actual port. Never commit or transmit
it: it can contain credentials. Read the local AGENTS.md before work.

Opening USB serial can reset this board even with nominal no-reset flags. Start
one bounded observer before a physical test and keep it open. Do not reopen it
mid-conversation or pairing. Logs retain aggregate state/levels only; no speech
content or key should be logged or committed.

## Remaining evidence limits

The reported startup/speaking crackle regression passed the user’s final two-start
check on 0.3.8. Preserve that accepted baseline when extending the system.
Router outage/return, long-running voice stability, measured latency/CPU envelope,
physical touch accuracy after calibration and AEC/barge-in are not automatically
passed by unit tests or milestone acceptance. Existing spikes #1–#4 remain open;
this handoff does not claim their integrated outcomes. Preserve factory recovery,
source evidence and these limits when extending the system.
