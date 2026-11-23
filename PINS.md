# Pins — hard facts for this repository

Read before your first command. Facts only; rules live in `AGENTS.md`, promises
in `docs/CONTRACT.md`. Fix a line the moment it drifts.

## Where things are

| Thing | Exact location |
| --- | --- |
| Standing rules | `AGENTS.md`; device app also `apps/unified-pet/AGENTS.md` |
| Product vision | `docs/VISION.md` |
| Original user words | `docs/intent.md` |
| Architecture | `docs/ARCHITECTURE.md` |
| Product promises and delivery sequence | `docs/CONTRACT.md`, `PROJECT-BRIEF.md` |
| Historical protocol references | `contracts/README.md` |
| Host bridge source / tests | `bridge/pet_bridge/`, `bridge/tests/` |
| Device app | `apps/unified-pet/`; `sh scripts/build.sh` builds it from the repository root. Root `main/` is retained for historical regression tests, not installation. |
| Conformance ledger, kit, pre-push guard | none exist |
| Published product source | `main` and release tags in the GitHub-connected repository; see `PROJECT-BRIEF.md` for the installed versions |
| Historical two-answer comparator | Firmware `1004293` (Build 11) and gateway `8857705`; see the dated physical evidence in `PROJECT-BRIEF.md`. Not a supported release. |
| Unified LAN preview evidence | Stock 0.119.1: four companion request shapes allowed by local package policy; real LAN call with gateway `c3349b4` and normal `0.3.19-amoled-boot`. Stock 0.119.0 was used in the prior LAN preview. Check other releases before pairing; this is not a general compatibility promise. |
| Provider-free acoustic check | `bridge/tools/pet_link_test_host.py` and its loopback test beside it. Device and Unified voice acceptance are separate. |

## Hardware

- Board: Waveshare ESP32-S3-Touch-AMOLED-1.8 V2; 368×448 AMOLED CO5300, touch
  CST820, audio ES8311; 16 MiB flash; 8 MiB octal PSRAM. Not the LCD-1.83 board.
- BSP `waveshare/esp32_s3_touch_amoled_1_8` 2.0.3; ESP-IDF 5.5.4 pinned by Docker
  image digest; versions locked in `apps/unified-pet/dependencies.lock`.
- Device NVS namespace for the new app: `unified_pet`. Never restore old NVS over
  the current Wi-Fi settings.

## Boundaries

- Host bridge binds one explicitly selected existing Unified session; Unified and
  provider credentials stay host-side, outside Git, mode 0600.
- Device ↔ bridge: TLS WebSocket `/v1/pet/live`, RFC 1918 IPv4 dial, FQDN
  certificate verification, per-pet bearer token. See `docs/ARCHITECTURE.md`
  for the released path; the wire drafts in `contracts/` are historical.
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
sh scripts/build.sh --offline-cache                                                # Pocket Pet build, offline tools image and component cache
```
