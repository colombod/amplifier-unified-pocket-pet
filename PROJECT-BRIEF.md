# Pocket assistant implementation brief

## Product destination

The upstream [original intent](docs/intent.md) is a portable Wi-Fi voice manager
across Amplifier Unified workspaces: create workspaces, start new work, continue
selected existing sessions and attend to genuine waiting questions. The user
asked this chat on 10 November 2026 to take over this part of the work and publish
the firmware and implementation record to `colombod/amplifier-unified-pocket-pet`.
The [vision](docs/VISION.md) remains a draft, not a ratified interaction design.

## Immediate authorized work

The user authorized a separate Mac workspace, Docker-isolated ESP32 tools,
read-only reuse of their ChatGPT Tachikoma pet, phone-based Wi-Fi and required
OpenAI-key entry, board backup/firmware installation, audio acceptance tests and
GPT-Live integration. That authorization supersedes the initial records-only
and no-hardware repository setup boundaries for this device work. It does not
authorize unrelated server modifications, arbitrary workspace operations or
Tailscale enrollment.

The actual connected board is **ESP32-S3-Touch-AMOLED-1.8 V2**, 368×448,
CO5300/CST820, ES8311 audio, 16 MiB flash and 8 MiB octal PSRAM. The original
LCD-1.83 reference led to an incompatible display build; factory recovery was
exercised and the corrected profile is user-confirmed. Use official AMOLED BSP
2.0.3, not the LCD/ST7789 pinout.

## Voice/backend boundary

The first isolated voice test uses **`gpt-live-1`**, PCM16 mono 24 kHz over the
documented Live primary WebSocket, and **`gpt-6-luna`** as a separate managed
Responses reasoning backend. It has no Unified tools or session access. It must
never claim that a generic spoken answer ran work in Unified. The replaceable
configuration seam is `main/assistant_config.h`.

Microphone and API start only on Talk. Stop releases audio and requests graceful
session close. No automatic reconnect starts another billed session. The user requested continuous conversation after Talk; input is replaced with silence during speaker
playback and a short tail until echo cancellation is tested. This is not proven
barge-in, wakeword support or a final interaction policy.

For Unified, qualify the documented installed-host live-session and observation
contracts cited in docs/intent.md. Preserve genuine human provenance, exact
session/question identity, revisions and receipts. Do not invent HTTP endpoints
from action names or replay uncertain commands. A scoped server-side adapter may
be needed; its design and deployment are not supplied by this voice spike.

## Historical alternative

An earlier pasted proposal suggested local Raspberry Pi/OpenClaw task delegation
and later Slack/cloud transport. It was background to the initial narrowed
pet/setup/voice implementation. After reading the upstream goal, that alternative
is not treated as the selected destination. No Pi, OpenClaw, Slack, Vercel, cloud
queue or distributed orchestration was implemented.

## Evidence standard

Keep build success, physical behavior, API access and Unified integration
separate. See docs/device-bringup.md and docs/device-evidence.json. The factory
backup, raw serial logs and local binaries remain private and uncommitted.
