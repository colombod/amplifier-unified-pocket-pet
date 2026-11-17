# Independent Unified Pet application

The deliverable is the pocket voice interface to Unified, not a diagnostic app.
Keep the pet, one primary voice control, honest connection/voice state and useful
Settings. Do not add an extra Stop button or expose Tailscale probes, numeric
transport counters, speaker-tone/recording tests or test instructions in normal
screens. Preserve cancellation and exact send authorization without making
cosmetic cleanup an automatic-send change. Reuse test code off the user path.
Report source-only, built, installed and physically demonstrated changes separately.

Own app behavior, configuration, generated assets and output here. Never include
or symlink root `main/`. Only named repository components (`pet_tls`,
`pet_touch_calibration`, TailTiny/WireGuard) and pinned managed/SDK dependencies
are shared; vendor security fixes are read-only.

Keep partitions and legacy record layouts compatible. Read legacy setup/audio
only read-only and only fall back on NOT_FOUND; writes go to `unified_pet`.
Shared touch defaults to root `pet_touch`; this project's component definition
selects `unified_pet` with read-only legacy-map fallback, no migration.

Use `sh scripts/build.sh` from any directory (offline cached Docker image only).
Run `sh tests/run_native.sh <absolute-output>` inside the pinned SDK, and both
local Node setup suites. Canonical TLS/HTTP/touch tests are repository
`tests/run_shared.sh <absolute-output>` with `PET_TLS_HOST_LIB` pointing to the
approved host mbedTLS cache. `sh tests/link_rig/run_link_rig.sh <fresh-absolute-output> sdkconfig all` runs the
production voice owner and transport on the pinned lwIP/TLS/WebSocket stack
over an impaired link (network-less container with a tap device); rerun it when
`bridge_voice.c`, `bridge_transport.c` or TCP/TLS/WebSocket settings change.
Run `python3 tests/isolation_test.py --baseline
<private-baseline>` before completion. Builds are not physical qualification;
microphone/repeatability work and authenticated Unified integration are separate.