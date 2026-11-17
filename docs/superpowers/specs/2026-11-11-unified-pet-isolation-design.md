# Unified Pet Isolation Design

Status: separation approved with “ok”, corrected by the user to reuse existing TLS
and touch calibration. Implementation plan still requires review.

## Boundary

Keep the existing root conversation application supported with unchanged behavior.
Add `apps/unified-pet/` in the same repository with its own application sources and
`app_main`; no new repository or nested Git. Independent apps do not mean duplicated
infrastructure: extract existing TLS and touch calibration for both apps to link.
Only their narrow extraction, build/include wiring and test-path changes may
affect the root app; no unrelated refactor, algorithm rewrite or UI redesign.

The new app owns CMake, `main/`, `idf_component.yml`, configuration, partition-file
copy, dependency lock, managed-component directory, generated assets, scripts,
tests and build output. Never compile/include app behavior from root `main` by
relative path or symlink. Both apps use explicit shared-component dependencies;
pinned BSP/vendor components and source artwork remain shared read-only.
Generate assets under the new app's `PROJECT_DIR`.

## Reuse existing implementations

- `components/pet_tls/`: lift TLS I/O, verification, bounded HTTP framing and
  cleanup from `main/tailnet_tls.c` and `main/tailnet_http.h`, not a replacement TLS
  stack. Keep `tailnet_tls_probe(...)` as the app-owned compatibility adapter;
  its fixed `/api/connections/v1` request and credential/target selection stay
  outside common transport. Only a request-bytes seam is needed. Preserve chain,
  hostname, clock/expiry, deadlines/cancellation, borrowed-fd ownership, 4096-byte
  response bound and exact existing probe wire format. Provider WebSocket TLS and
  vendored TailTiny TLS remain their existing separate paths.
- `components/pet_touch_calibration/`: move `touch_calibration.c`,
  `touch_calibration.h`, `touch_alignment.h`; reuse the complete existing LVGL
  calibration flow, math and persistence. Its current input/screen parameters
  already suffice; keep `touch_calibration_init(...)` and
  `touch_calibration_begin(void)`, with no callback framework or axis/rotation
  changes. Preserve clamping and `pet_touch/map` version-1 record layout/values.
  Root keeps its current store; the new app reads legacy calibration as fallback
  and writes explicit new calibration only to `unified_pet/map`, using one private
  compile-time store selection. No automatic migration/reset.
- Both apps declare `REQUIRES pet_tls pet_touch_calibration`; the new app locates
  those repo components explicitly. Existing TailTiny/WireGuard, including the
  padding/security fixes, is reused unchanged rather than cloned or reinvented.

Use Docker ESP-IDF **5.5.4**, Waveshare AMOLED-1.8 V2 BSP **2.0.3**, and existing
locked dependencies without upgrades. The activation script's 5.5.6 is not the
baseline; LCD-1.83 hardware definitions are incorrect.

## Preservation

Before implementation, privately snapshot the current dirty/untracked source and
configuration with a hash manifest under workspace `working-files/`, outside the
child repository. HEAD alone is insufficient rollback. Retain previous evidence
and the known-working firmware with SHA-256
`ac208429d7d7ec10ca1dfa6552d989131b34b4c3ad3902de0ffbd70e3cc64044`
(ELF identifier `64fec68a4`). Verify all unrelated baseline files and retained
build artifacts remain hash-identical. For extracted files, review mapped logical
content/diffs and prove preserved APIs/behavior with regressions and both app
builds in separate output directories. Deliberate moves/linker changes mean neither
the whole source tree nor newly linked firmware is promised byte-identical.

Folders do not isolate device flash/NVS. Keep the same partition layout; any later
authorized deployment is app-only, without erase. Preserve legacy settings,
credentials, calibration and tailnet identity. Compatibility reads retain existing
protection and caller-wipe rules; new-app setting changes write only to
`unified_pet`, never legacy namespaces. Root calibration retains its original
explicit-save behavior. No automatic migration, enrollment or secret logging.

## Ordered delivery

1. **Milestone 2: microphone reliability first.** Measure queue occupancy, drainage
   gaps, captured/sent bytes and send duration. Reproduce the cause with offline
   slow/failing-consumer tests exercising production code before fixing it; do not
   blindly increase buffers/priorities or reduce animation. Normal bounded tests
   must not overflow; unsupported stalls must fail explicitly, without concealed
   PCM loss, and allow retry only after safe cleanup.
2. **Milestone 3: repeatable operation second.** Measure teardown, Back/Done,
   connection loss and memory/task baselines. Require ten bounded start/stop/retry
   cycles, acknowledged setup transitions, connection recovery, justified resource
   tolerances, no idle capture, no indefinite cleanup and no unsafe retry.
3. **Milestone 1: authenticated Unified roundtrip last, deferred from this plan.**
   Use supported authenticated workspace/session interfaces later; invent no
   endpoints, credentials, authority or replacement cloud service.

Existing conversation transport is only a temporary reliability fixture, not
Unified functionality. Mic/provider sessions require explicit user action and
separate authority. Hardware testing replaces the running application; preserve
the conversation binary for restoration. Never initialize capture at boot.

## Evidence and permissions

Run applicable native, Node and real-host-TLS regressions against the extracted
production components, calibration storage/coordinate tests, both app builds and
preservation checks. Stubbed lifecycle tests are not throughput or physical
voice proof; prior bounded tailnet/TLS HTTP 200 is not always-on, concurrency or
Unified qualification. Physical gates require deployment/recovery approval,
bounded provider authority and user-initiated tests. No Git commit or publication
permission is implied. Implementation waits for plan review and execution choice.