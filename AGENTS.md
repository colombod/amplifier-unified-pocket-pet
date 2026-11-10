# ESP32 pet project

This standalone project is authorized by the user. The prior mobile-capture experiment restrictions do not apply here. Work only in this project and its Docker resources; preserve unrelated projects and services.

Use Docker for ESP-IDF, compiler, CMake, Ninja and component dependencies. Keep the Mac free of a global ESP-IDF installation. A project-local Python environment for esptool/serial is allowed when needed. Do not read unrelated credentials or put secrets in source, logs, images, URLs or chats.

Corrected hardware identity (from original factory app strings and CO5300 driver): Waveshare ESP32-S3-Touch-AMOLED-1.8 V2, 368x448 AMOLED, 16 MB flash, 8 MB octal PSRAM, ES8311 microphone/speaker. The initial user-provided LCD-1.83 documentation was for a different board; do not reuse that pinout or ST7789 driver. Use waveshare/esp32_s3_touch_amoled_1_8 BSP 2.0.3 for the CO5300/CST820 revision. Start hardware work from the official Waveshare BSP/examples; verify pins and codec setup rather than borrowing another board's definitions.

First deliverable: a simple animated pet; easy Wi-Fi setup and OpenAI API-key entry through a device-hosted phone-friendly provisioning page; GPT-Live voice conversation. Use current official OpenAI documentation: GPT-Live is not the same wire protocol as legacy Realtime. The user explicitly requested GPT-Live. Do not silently substitute a model.

The user subsequently requested reuse of their existing ChatGPT Tachikoma pet. Its stable ID is pet_6abe2e4dd1f48191bbbc9389b55e641e. Read-only download of that pet and deterministic embedded-asset conversion are authorized. Preserve its source sheet; do not change the user’s ChatGPT pet selection or artwork. Keep the UI simple and readable on the physical display. Never start the microphone/API session before the user's action. Clearly show connecting, listening, speaking, disconnected and error states.

The upstream product goal is a portable voice manager across Amplifier Unified workspaces: new work, existing sessions and attention questions. Read docs/intent.md and docs/VISION.md. The older Raspberry Pi/OpenClaw proposal is historical, not the selected Unified integration. Keep the voice/backend seam replaceable; do not build Slack, Vercel, cloud queues or distributed orchestration now. See PROJECT-BRIEF.md.

Validate builds and hardware separately. Building is not proof that display, touch, microphone, speaker or live API access works. Back up existing board flash before replacing firmware; do not flash merely to test environment setup. Ask for deployment approval only after a concrete build and recovery plan exist.

## Tracking and upstream boundaries

Use Beads (`bd`) for durable tracking in repositories that include it. Read `.agents/skills/beads/SKILL.md` or `~/.agents/skills/beads/SKILL.md`, then run `bd prime` when context is missing. Use `bd ready`, `bd show`, `bd update --claim` and `bd close` for issue operations. Use `bd remember` for persistent memory; do not create Markdown TODO queues or ad hoc memory files. No active Beads database was present at this firmware handoff. Preserve existing GitHub spike references as historical evidence; do not close them because an isolated device test passes.

Preserve original quotations and evidence in docs/intent.md. The interaction design in docs/VISION.md remains a draft. Use supported Unified interfaces only: do not invent endpoints from action names, forge human provenance, bypass ownership, silently take over sessions, or replay uncertain commands. No server changes or Tailscale enrollment are implied by device work. The current managed Responses voice test has no Unified access.

Keep flash backups, credentials, enrollment keys, raw serial logs, raw audio and build outputs out of Git. Inspect staged content before every push. Record exact versions, failures and validation limits in sanitized evidence. Historical Linux toolkit work in docs/development-toolkit.md remains a separate record; this Mac uses Docker instead.
