# Pins — hard facts for this repository

Read before your first command. Facts only; rules live in `AGENTS.md`, promises in
`contracts/`. Fix a line the moment it drifts.

## Where things are

| Thing | Exact location |
| --- | --- |
| Standing rules | `AGENTS.md`; device app also `apps/unified-pet/AGENTS.md` |
| Vision (DRAFT) | `docs/VISION.md` |
| Original user words | `docs/intent.md` |
| Contracts (DRAFT) | `contracts/README.md`, `contracts/<name>.v1.md` |
| Product promises and delivery sequence | `contracts/pocket-assistant-delivery.v1.md`, `PROJECT-BRIEF.md` (earlier single-session milestone is historical) |
| Historical standalone voice milestone | `docs/milestone-1.md` |
| Host bridge source / tests | `bridge/pet_bridge/`, `bridge/tests/` |
| Device app | `apps/unified-pet/` (root `main/` is the older conversation app) |
| Conformance ledger, kit, pre-push guard | none exist |
| Current local product integration | `product/integration` in the GitHub-connected repository; `main` is the published base, not the installed product |
| Historical two-answer comparator | Firmware `1004293` (Build 11) and gateway `8857705`; see the dated physical evidence in `PROJECT-BRIEF.md`. Not a supported release. |
| Unified versions checked during recovery | 0.116.0: listing/detail reads observed. 0.117.0: installed at the gateway switch; the four required request shapes pass the local package-policy check. Neither is a released voice compatibility pin. |
| Provider-free acoustic check | `bridge/tools/pet_link_test_host.py` and its loopback test beside it. Device and Unified voice acceptance are separate. |

## Hardware

- Board: Waveshare ESP32-S3-Touch-AMOLED-1.8 V2; 368×448 AMOLED CO5300, touch
  CST820, audio ES8311; 16 MiB flash; 8 MiB octal PSRAM. Not the LCD-1.83 board.
- BSP `waveshare/esp32_s3_touch_amoled_1_8` 2.0.3; ESP-IDF 5.5.4 pinned by Docker
  image digest; versions locked in `dependencies.lock`.
- Device NVS namespace for the new app: `unified_pet`. Never restore old NVS over
  the current Wi-Fi settings.

## Boundaries

- Host bridge binds one explicitly selected existing Unified session; Unified and
  provider credentials stay host-side, outside Git, mode 0600.
- Device ↔ bridge: TLS WebSocket `/v1/pet/live`, RFC 1918 IPv4 dial, FQDN
  certificate verification, per-pet bearer token. Details:
  `contracts/pet-bridge-wire.v1.md`.
- Never in Git: credentials, tokens, session IDs of real sessions, flash backups,
  raw serial logs, raw audio, binaries, build outputs.
- Builds and offline tests are not physical qualification.

## Existing check commands

These exist in the repository; listing them is not a claim that they pass.

```
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s bridge/tests -t bridge -v   # host bridge offline suite
node tests/setup_page_test.mjs                                                      # root phone setup page
node tests/setup_unified_page_test.mjs                                              # Unified target setup page
sh tests/run_native.sh <output-dir>                                                 # root native suites, inside pinned SDK
sh tests/run_shared.sh <output-dir>                                                 # shared TLS/touch components, inside pinned SDK
sh apps/unified-pet/scripts/build.sh                                                # device app build, offline cached image
```
