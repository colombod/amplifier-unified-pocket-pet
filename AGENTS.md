# ESP32 pet project

## Current delivery direction

Ship a portable voice interface to **all authorized Amplifier Unified work**, over Tailscale including away from home: existing conversations/workspaces, status and attention, answering waiting questions, and starting new work/workspaces. The selected-session round trip is one working increment, not a permanent binding or the whole product.

Use the incremental delivery plan in `PROJECT-BRIEF.md` and promises in `contracts/pocket-assistant-delivery.v1.md`. Apply the user's requested Microsoft amplifier-converge conventions through these concise existing documents, not new enforcement machinery. Reuse existing device and Unified capabilities; do not replace the project with a new chatbot, manager runtime or orchestration framework. Each increment must produce a usable integrated outcome. An architectural change needs an evidenced obstacle to that outcome, not a preference for a new design.

Keep the normal pet UI free of diagnostic/test controls and extra Stop buttons. One clear voice control, honest state, context choice and useful Settings are sufficient. Cosmetic cleanup must not silently bypass explicit send/approval semantics. Source changes, builds, installation and physical acceptance are distinct; never claim the device changed before installation. Own ordinary repairs/checks and reuse valid evidence instead of returning the user to repeated component tests. Preserve actual Stop, paid/device limits, credentials, backups and uncertain outcomes.

This standalone project is authorized by the user. The prior mobile-capture experiment restrictions do not apply here. Work only in this project and its Docker resources; preserve unrelated projects and services.

Use Docker for ESP-IDF, compiler, CMake, Ninja and component dependencies. Keep the Mac free of a global ESP-IDF installation. A project-local Python environment for esptool/serial is allowed when needed. Do not read unrelated credentials or put secrets in source, logs, images, URLs or chats.

Corrected hardware identity (from original factory app strings and CO5300 driver): Waveshare ESP32-S3-Touch-AMOLED-1.8 V2, 368x448 AMOLED, 16 MB flash, 8 MB octal PSRAM, ES8311 microphone/speaker. The initial user-provided LCD-1.83 documentation was for a different board; do not reuse that pinout or ST7789 driver. Use waveshare/esp32_s3_touch_amoled_1_8 BSP 2.0.3 for the CO5300/CST820 revision. Start hardware work from the official Waveshare BSP/examples; verify pins and codec setup rather than borrowing another board's definitions.

The initial standalone bring-up delivered the pet, phone-friendly provisioning and GPT-Live experiments; it is not the current product destination. Reuse that work. Use current official OpenAI documentation: GPT-Live is not the same wire protocol as legacy Realtime. The user explicitly requested GPT-Live. Do not silently substitute a model.

The user subsequently requested reuse of their existing ChatGPT Tachikoma pet. Read-only download of that pet and deterministic embedded-asset conversion are authorized. Preserve its source sheet; do not change the user’s ChatGPT pet selection or artwork. The local export does not need the account's pet identifier; keep it out of source and generated reports. Keep the UI simple and readable on the physical display. Never start the microphone/API session before the user's action. Clearly show connecting, listening, speaking, disconnected and error states.

The upstream product goal is a portable voice manager across Amplifier Unified workspaces: new work, existing sessions and attention questions. Read docs/intent.md and docs/VISION.md. The older Raspberry Pi/OpenClaw proposal is historical, not the selected Unified integration. Keep the voice/backend seam replaceable; do not build Slack, Vercel, cloud queues or distributed orchestration now. See PROJECT-BRIEF.md.

Validate builds and hardware separately. Building is not proof that display, touch, microphone, speaker or live API access works. Back up existing board flash before replacing firmware; do not flash merely to test environment setup. Ask for deployment approval only after a concrete build and recovery plan exist.

## Project skills

Procedures for this project live in `.amplifier/skills/` and ship with the repository. Load the one that matches before working that way; register the folder first if your session does not list them (`load_skill(source="<this checkout>/.amplifier/skills")`).

| Skill | Use it when |
| --- | --- |
| `pet-voice-triage` | a spoken call failed or sounded wrong: find the cause from recorded counters before changing code |
| `pet-device-bench` | a firmware question needs a measurement on the physical pet, from USB, with nobody pressing the mic |
| `pet-unified-compat` | Unified was updated, another Unified release is targeted, or the gateway is refused |

Keep them true: when a procedure or a measured fact changes, change the skill in the same commit.

## Tracking and upstream boundaries

Use Beads (`bd`) for durable tracking in repositories that include it. Read `.agents/skills/beads/SKILL.md` or `~/.agents/skills/beads/SKILL.md`, then run `bd prime` when context is missing. Use `bd ready`, `bd show`, `bd update --claim` and `bd close` for issue operations. Use `bd remember` for persistent memory; do not create Markdown TODO queues or ad hoc memory files. No active Beads database was present at this firmware handoff. Preserve existing GitHub spike references as historical evidence; do not close them because an isolated device test passes.

Preserve original quotations and evidence in docs/intent.md. Current direction is in docs/VISION.md; the delivery contract distinguishes explicit requirements from proposed checks. Pocket develops, tests and ships against supported Amplifier Unified releases with their shipped runtime. Do not modify Unified, replace its dependencies, or open patches for it. Report feature gaps or bugs through Unified feedback with user impact, versioned findings and suggested behavior. Use supported interfaces only: do not invent endpoints from action names, forge human provenance, bypass ownership, silently take over sessions, or replay uncertain commands. No server changes or Tailscale enrollment are implied by device work. The isolated Responses voice test is historical and has no Unified access.

Keep flash backups, credentials, enrollment keys, raw serial logs, raw audio and build outputs out of Git. Inspect staged content before every push. Record exact versions, failures and validation limits in sanitized evidence. Historical Linux toolkit work in docs/development-toolkit.md remains a separate record; this Mac uses Docker instead.

## Setup credential invariants

The existing AP-local phone page also saves an optional Tailscale auth key through token-protected `/configure` with `action: "tailscale_key"`. This branch is independent of station connection, Wi-Fi/OpenAI entry and Wi-Fi save generations; it never enrolls or connects Tailscale. `pet_setup/ts_auth_key` is bounded to 255 bytes, blank/omitted means keep, and HTTP returns only `tailscale_key_saved`. Device code may call `network_copy_tailscale_auth_key` after `network_init`; the caller owns and wipes its buffer, as with the OpenAI accessor. Ordinary NVS/HTTP-over-WPA2 limitations remain; failed writes/commits are not acknowledged, and rollback is best-effort, not a flash transaction.

Run `node tests/setup_page_test.mjs` for independent save/pending/edit behavior. In the IDF Docker environment, also run `cc -Wall -Wextra -Werror -I main -I "$IDF_PATH/components/json/cJSON" tests/setup_tailscale_test.c "$IDF_PATH/components/json/cJSON/cJSON.c" -o /tmp/setup_tailscale_test && /tmp/setup_tailscale_test`; this executes the production handler/storage helpers with fault-injected NVS, not a second persistence implementation.

## Existing native connectivity spike

Bounded physical Tailscale/TLS/HTTP success and persisted identity reuse are recorded in the workspace evidence. This does not qualify continuous audio over Tailscale, off-site operation or coexistence with the UI. The restrictions below describe the current spike implementation, not the final product architecture.

Do not run the diagnostic tunnel until its shutdown race is repaired and verified.
Source review found that a task-done event can wake destruction while the signaling
task still accesses the event group. The earlier bounded probe did not prove safe
teardown; retain resources whenever task exit has not been established.

`tailnet_spike` is boot-idle and starts only from Settings or USB Serial/JTAG commands `tailnet start`, `tailnet cancel`, `tailnet status` (newline terminated, 31-byte input cap, unknown input discarded without echo). Init creates one idle test worker plus the USB receiver; saving any phone setting never connects. Voice/local audio and the spike share atomic exclusive admission. Failed teardown retains the runtime and blocks retry/audio until restart; never free it or restart it to conceal missing task-exit proof.

Phone `action: "unified_target"` saves one checked versioned nonsecret `pet_setup/unified_target` record (blank host, default port 8941). Only canonical lowercase FQDNs, not IPs/URLs, are accepted; user edits survive late prefill/save responses. Before any TailTiny start the network worker must acknowledge actual HTTP/DNS listener stop and AP pause, not merely queue AP closure. Restoration follows proven tunnel teardown.

Vendored TailTiny v3.1.0 provenance/patches are in `components/tailtiny/source-manifest.json`. Its `microlink/identity_v1` key now holds a v2 record validating all three keypairs and enrollment phase: PREPARED (never attempted), ATTEMPTED (possibly sent), REGISTERED (authorized response). PREPARED may retry credential copy without replacing identity; checked commit/reopen/readback of ATTEMPTED must precede the first enrollment send. Ambiguous/restarted ATTEMPTED and REGISTERED reuse identity without AuthKey. Unknown v1 phase, corruption, partial records and I/O failure never regenerate/erase or authorize a send. Only the runtime-owned 256-byte bootstrap buffer is a credential source; legacy config/settings and caller auth pointers are ignored. Scrub transient JSON/H2 copies and the bootstrap buffer after consumption or proven task exit, never free under active tasks. `tests/run_enrollment.sh` compiles exact production constructor/registration bodies with hardware/NVS/transport stubs and send counters; its `alloc_fault_auth` (helper) and `alloc_fault_reg` (full caller) scenarios pin the auth-construction allocation-failure invariant (credentials wiped, zero transport sends) using actual cJSON allocation faults. The immutable exact-peer/TCP-port plaintext fence is local defense, not tailnet ACL enforcement. Application TLS requires trusted chain, hostname and clock; the public bundle does not establish trust in Unified's private CA.

Run both Node suites (`setup_page_test.mjs`, `setup_unified_page_test.mjs`) and `sh tests/run_native.sh <output-dir>` inside the pinned SDK image; native suites use actual production helpers, cJSON/X25519 and ASan/UBSan. `tailnet_tls_test.py` drives the production TLS implementation with real host mbedTLS and synthetic certificates over socketpairs; `tls_host` stubs only hardware seams. Offline build receipts, exact USB path, caps and unexercised physical/lifecycle boundaries belong to `working-files/tailnet-connectivity` in the workspace, not tracked source. Passing these checks is not enrollment, reset persistence, measured device memory, direct/DERP delivery or device TLS trust.

## Independent app/shared components

Root remains the conversation app. `apps/unified-pet` owns its own behavior,
config/assets/output and writes only `unified_pet`; never import root `main`.
TLS/HTTP and full calibration live once in `components/pet_tls` and
`components/pet_touch_calibration`. Keep root calibration's `pet_touch/map` and
probe API unchanged; only new-app builds select the private touch store define.
Run canonical `sh tests/run_shared.sh <output-dir>` in the pinned SDK with
`PET_TLS_HOST_LIB` set to the existing real-host mbedTLS library cache, in addition
to root/app native and Node suites. Preserve vendor security fixes read-only.

## Host bridge (`bridge/`)

- `bridge/` is a host-side adapter, not another assistant/runtime. Each interaction has one explicit native Unified destination; the current fixed-session prototype must not become the product's scope ceiling. Broader routing uses supported host discovery/actions and existing authority, not multicast or silent retargeting.
- Companion-v1 credentials and the provider key stay host-side, outside Git, mode 0600; never in source, firmware, logs or chats.
- Model delegation is not authority to auto-send. Send a transcript once; never auto-replay an uncertain command.
- Stop audio only does not cancel the Unified run.
- Offline core test target (not a claim that it currently passes): `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s bridge/tests -t bridge -v`
- Hardware and physical calls remain separately gated.

### What Unified allows a paired device, checked against Unified 0.114.5 and 0.116.0

Recheck `amplifier_web/device_pairing.py` `permits_companion` in the Unified release you target before changing any request; anything else answers 403. Unified can update itself and restart (0.114.5 to 0.116.0 on 21 November 2026, device-facing files unchanged): find the running release before a test.

- Session listing is `GET /api/sessions?view=observation` only. `GET /api/companion` answered 200 until Unified changed and 403 afterwards; every spoken request was then refused.
- Also allowed, in exactly these shapes: `POST /api/clients/attach` (`kind: native`, `protocolVersion: 1`), `GET /api/conversation/detail?part=messages`, `POST /api/sessions/<id>/commands` with `conversation.send` (`via: chat`, `preserveDraft: true`, `attachmentIds: []`).
- Older deployed gateways say "That conversation is busy" for any failed idle check. This checkout distinguishes positive activity from an inaccessible, unlisted or unknown state in running conversation mode; the latter reports "not sent". Neither case submits work.
- The bound conversation is listed only while its workspace folder is a registered Unified workspace.
- A dormant conversation can take more than a minute to accept the first request (67 s measured). The gateway speaks fixed progress lines meanwhile; the device must stay in `asking`, never `answering`, until the real reply.
- The gateway follows a Unified restart by itself (`host_restart_follow` in the event log). Its credential store has a single owner: a second process gets `auth_locked`.
- A spoken test must need Unified ("what is this chat's title"). The voice model answers general knowledge itself, and then nothing is sent.

### Tailscale: what exists

- Works: the gateway host reaches Unified over Tailscale. The pet reaches the gateway on the same LAN.
- Not built: the pet away from the gateway's LAN. The firmware accepts only private LAN addresses (10/8, 172.16/12, 192.168/16) for the gateway, voice does not use the device's own Tailscale, and that device-side code is a connectivity spike with an unrepaired shutdown race (see above). The gateway's optional Tailnet listener has no device counterpart yet.

## Current promises and historical prototype references

- `contracts/pocket-assistant-delivery.v1.md`: product promises and agreement
  status; `PROJECT-BRIEF.md`: current design and delivery sequence.
- `PINS.md` and the earlier milestone/wire documents retain prototype facts and
  behavior references. Read relevant wire rules before changing the bridge
  protocol; current user corrections supersede stale product scope, not actual
  send/permission boundaries. Do not auto-load archives or evidence.
- Each promise has one home; link rather than restate. Requirements stated by
  the user are not permission to claim their approval of newly drafted wording.
- No conformance ledger, kit, pre-push guard or Converge manager is installed.
  An unrun check is "Can't check", never a pass.
- Current corrections must reach affected contributors; do not resume historical
  workers or replay old operations merely because a document was updated.
