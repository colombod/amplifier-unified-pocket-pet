# Unified Pet Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task, after plan review and execution-method approval. Steps use checkbox syntax.

**Goal:** Add an independent app preserving conversation-pet behavior and reusing existing TLS/calibration, then qualify microphone reliability before repeatable operation; defer authenticated Unified integration.

**Architecture:** `apps/unified-pet/` owns app behavior/config/output; both apps link the existing TLS and touch-calibration implementations through narrow shared components. Vendor/BSP/artwork remain shared read-only. Conversation transport is a temporary reliability fixture, not Unified functionality; production-path tests precede separately authorized physical gates.

**Tech Stack:** C, ESP-IDF 5.5.4 in Docker, Waveshare AMOLED-1.8 V2 BSP 2.0.3, existing locked LVGL/WebSocket/TailTiny dependencies, Python/Node host tests, ASan/UBSan and real host mbedTLS.

**Spec:** [Approved isolation design](../specs/2026-11-11-unified-pet-isolation-design.md).

## Global Constraints

- Repository: this repository; workspace root is a container, not a source repository.
- Preserve root conversation behavior and all dirty/untracked work. Only planned TLS/calibration extraction,
  necessary CMake/includes/thin probe adapter and test-path changes may affect root; no unrelated refactor or nested Git.
- New app owns behavior/config/output; no imports/symlinks to root `main` implementation.
  Share `pet_tls`, `pet_touch_calibration` and existing pinned vendor/BSP/artwork, not duplicated infrastructure.
- Docker ESP-IDF **5.5.4**, Waveshare AMOLED-1.8 V2 BSP **2.0.3**, existing locked dependencies without upgrades;
  do not adopt activation-script 5.5.6.
- Before implementation, privately snapshot current dirty/untracked source/configuration plus hashes outside the child repository; HEAD is not rollback.
- Same partition layout; later approved flashing is app-only, without erase. Preserve legacy settings/credentials/calibration/tailnet identity;
  new-app writes target only `unified_pet`; root's explicit `pet_touch/map` save remains unchanged.
- Keep TLS security/timeouts/framing/bounds, touch transform/axes/clamping and version-1 calibration bytes;
  no automatic migration/reset, generic wrapper framework, API churn or dependency upgrade.
- Compatibility reads preserve existing protection/caller wiping; no automatic migration, reenrollment,
  secret/audio/transcript/provider-body logging or boot/idle capture.
- Order: milestone **2 microphone**, then **3 repeatability**, then **1 Unified**.
  Unified auth/endpoints/roundtrip, new backend services and concurrency qualification are out of scope.
- No implementation, Git, USB, network/provider calls, containers, installation or resources are authorized by writing this plan.
  Execution must respect current permissions; commits/publication need separate authorization.
- No DTU: this is an ESP firmware app. Offline tests, firmware builds and physical acceptance are distinct evidence.

## Review Focus

1. Extraction regresses TLS verification or stored calibration: Task 1 real-TLS rejection/wire tests and `test_shared_calibration`.
2. Dirty files/builds or blank/failed saves overwrite preserved state: Task 1 `test_preservation_boundary`, `test_project_boundary`, `test_storage_compatibility`.
3. Bursty, partial or stalled sends silently lose PCM or poison retry: Task 2 `test_capture_backpressure` / `test_retry_after_failure`.
4. Queue-full or late setup completion falsely reports Back/Done success: Task 3 `test_setup_exit_ack` / phone lost-response cases.
5. Disconnect/blocked worker permits use-after-free, endless cleanup or accumulating allocations: Task 3 `test_teardown_fence` / `test_ten_cycles`.

## Source map and evidence baseline

- Read `AGENTS.md`; preserve `docs/intent.md` and draft status of `docs/VISION.md`. C LSP is unavailable here; source inspection is not a verified semantic call graph.
- Build references: root `CMakeLists.txt:1-5`, `main/CMakeLists.txt:1-10`, `main/pet_asset.S.in:5`, `scripts/export_pet.py`, `scripts/build.sh`, `compose.yaml`, `Dockerfile`, `dependencies.lock`, `sdkconfig.defaults`, `sdkconfig`, `partitions.csv`.
- Runtime references: `main/live_voice.c:79-98,197-343`; `main/audio_test.c:40-159,183-221`; `main/network.c:59-98,203-273,362-481`; `main/main.c:49-113`; `main/tailnet_tls.{c,h}`, `main/tailnet_http.h`, `main/touch_calibration.{c,h}`, `main/touch_alignment.h`.
- Test references: `tests/run_native.sh`, `tests/run_audio_lifecycle.sh`, `tests/audio_lifecycle_test.c`, both `tests/setup*_page_test.mjs`, `tests/tailnet_tls_host.c`, `tests/tailnet_tls_test.py`, `tests/tls_host/`, `tests/touch_alignment_test.c`.
- Working firmware: retained privately outside this repository; SHA-256 `ac208429d7d7ec10ca1dfa6552d989131b34b4c3ad3902de0ffbd70e3cc64044`, ELF identifier `64fec68a4`.
- Reported handoff HEAD `694f1a22a675f8e52c279333f67c143bc9630151`, branch `feature/tailscale-setup-key`, 15 modified/104 untracked/no staged: historical context, not ownership or a freshly verified status.

---

### Task 1: Shared TLS/calibration plus independently buildable app and preservation

**Files:** Create under `apps/unified-pet/`:
- `CMakeLists.txt`, `sdkconfig.defaults`, `sdkconfig`, `partitions.csv`, `dependencies.lock`.
- Own copies of app-owned root `main/` files, excluding shared `touch_calibration.{c,h}`, `touch_alignment.h`, `tailnet_http.h`. Keep a local thin `tailnet_tls.c/.h` probe adapter, not a second TLS engine.
- `scripts/build.sh`, `scripts/export_pet.py`; generated `assets/generated/`, `managed_components/`, `build/` stay local and unpublished.
- `tests/isolation_test.py`, `tests/storage_compat_test.c`, `tests/run_native.sh`; copy app-behavior tests/support only, excluding outputs and canonical shared TLS/touch tests. No SDK/vendor-tree copying.

**Shared/root files (narrow extraction only):**
- Create `components/pet_tls/{CMakeLists.txt,pet_tls.c,include/pet_tls.h,include/tailnet_http.h}` from existing TLS/parser bodies; retain app `main/tailnet_tls.c/.h` as compatibility adapter.
- Move `main/touch_calibration.c` to `components/pet_touch_calibration/touch_calibration.c` and its two public headers (`touch_calibration.h`, `touch_alignment.h`) to `include/`; create component `CMakeLists.txt`.
- Modify root `main/CMakeLists.txt` to remove only `touch_calibration.c` from SRCS, keep thin `tailnet_tls.c`, and add `REQUIRES pet_tls pet_touch_calibration`; adjust `main/tailnet_tls.h` to import the unchanged result type/fields from `pet_tls.h`. Root `main/main.c` calls remain unchanged.
- Adapt root `tests/run_native.sh` and `tests/run_spike.sh` include paths; create `tests/run_shared.sh`, `tests/touch_calibration_test.c`, `tests/touch_host/` hardware/LVGL/NVS stubs. Reuse canonical TLS/HTTP/alignment tests against shared production, not copied implementations.

Adapt copied native/enrollment/TCP/WireGuard runners to resolve only vendor paths at repository root;
their app paths remain app-local. Shared tests resolve extracted components explicitly; no source symlinks back to root `main`.

**Interfaces:**
- Consumes existing app/build inputs once; thereafter app behavior is local and shared implementation comes from named components.
- Produces `sh apps/unified-pet/scripts/build.sh` -> `apps/unified-pet/build/unified_pet.bin`; exits nonzero on wrong SDK or dependency drift.
- Produces `python3 apps/unified-pet/tests/isolation_test.py --baseline "$BASELINE"` and `sh apps/unified-pet/tests/run_native.sh "$OUT"`; exit 0 only when assertions pass.
- Preserve copied `network_copy_api_key(char *, size_t)`, `network_copy_tailscale_auth_key(char *, size_t)`, `network_copy_unified_target(setup_unified_target_t *)`, audio and voice public signatures at this task.
- Keep `esp_err_t tailnet_tls_probe(int borrowed_fd, const char *host, uint16_t port, uint64_t deadline_ms, tailtiny_cancel_fn cancel, void *arg, void (*http_stage)(void *), tailnet_tls_result_t *out)` and existing result fields unchanged.
- Only new TLS seam: `esp_err_t pet_tls_exchange_http(int borrowed_fd, const char *host, uint16_t port, const char *request, size_t request_bytes, uint64_t deadline_ms, tailtiny_cancel_fn cancel, void *arg, void (*http_stage)(void *), tailnet_tls_result_t *out)` in shared `pet_tls.h`; borrowed request lives until return, 1–255 bytes, no logging/credential selection. Existing adapter builds the exact bounded GET/Host request.
- Keep `void touch_calibration_init(lv_indev_t *input, lv_obj_t *screen)`, `void touch_calibration_begin(void)`, `bool touch_map_solve(const touch_point_t raw[3], const touch_point_t target[3], touch_map_t *out)`, `touch_point_t touch_map_apply(const touch_map_t *m, touch_point_t raw)`. Reuse the full current LVGL flow; app callers choose when to start it.

- [ ] **1. Preserve before creating app files.** Create a mode-0700 private baseline directory under workspace `working-files/unified-pet-foundation/<unique-run>/`; record it as `BASELINE`. Snapshot dirty/untracked source/config/dotfiles, including these approved docs, without Git commands; exclude only Git metadata, tool/dependency caches and build outputs. Retain all excluded existing outputs/evidence in place. Save a source hash manifest and hashes of existing root build artifacts; privately copy/hash the working binary above. Do not inspect/print credential values or copy private artifacts into source. Stop on incomplete snapshot or binary mismatch.
  Preserve symlinks as links, not traversal into other projects; retain any private archive only outside source with restrictive permissions.
- [ ] **2. RED: author extraction/boundary/storage tests first.** `test_project_boundary` requires one `app_main` per app, no imports from the other's `main`, one shared TLS/calibration implementation, only declared components/SDK dependencies, independent config/output/assets and exact partition bytes. `test_preservation_boundary` allows only the listed extraction/wiring changes, compares mapped original logical content/diffs and all unrelated hashes; `test_storage_compatibility` invokes production storage with synthetic NVS.
  `test_shared_calibration` drives production init/read/advance with LVGL/NVS stubs in root-store and new-app-store modes: saved map roundtrip, missing/invalid records, degenerate solve, unchanged swapped-axis mapping, mapped pressed bounds x=0..367/y=0..447 and unchanged uncalibrated raw behavior.
  Keep canonical real-host-TLS valid/invalid-chain/wrong-host/expiry, exact-request, partial/oversize/truncated response and cancellation tests; extend `tests/tailnet_tls_test.py` only to add deadline expiry. Run baseline characterization before extraction, then reproduce against shared bodies.

  Assertion sketch for test-local observations, not new public APIs:

  ```python
  assert unrelated_hashes_after == unrelated_hashes_before
  assert retained_artifact_hashes_after == retained_artifact_hashes_before
  assert extraction_diff_is_only_declared_wiring_and_seams
  assert app_partition_bytes == root_partition_bytes
  assert app_main_definition_count_per_app == [1, 1]
  assert all(namespace == "unified_pet" for namespace in new_app_nvs_write_namespaces)
  assert legacy_nvs_after == legacy_nvs_before
  ```

- [ ] **3. Run RED.** Isolation test must fail for absent shared wiring/new app; storage test must reject legacy writes by new-app paths. Characterization tests may already pass baseline; record that honestly, never manufacture failures or skip absent implementations.
- [ ] **4. Extract, then create own app/build inputs.** Lift the existing bodies without algorithm changes; only move request formatting to app `tailnet_tls_probe` and add the bounded request-bytes seam. Shared `pet_tls` declares `REQUIRES mbedtls esp_timer esp_hw_support freertos lwip tailtiny`; shared touch declares `REQUIRES lvgl__lvgl nvs_flash log`. Root and new-app `main/CMakeLists.txt` add `REQUIRES pet_tls pet_touch_calibration`; no generic transport/controller framework.
  New project `unified_pet` explicitly locates repo `components/pet_tls`, `components/pet_touch_calibration`, `components/tailtiny`, `components/tailtiny/components/wireguard_lwip`. Copy root config/partition/lock without upgrades (IDF 5.5.4, LVGL 9.3.0, LVGL port 2.6.2, WebSocket 1.6.1). Resolve app-local managed files from permitted caches; missing inputs block, not authorize fetch/install.
- [ ] **5. Wire isolated generation.** Adapt the copied exporter to read only repo `assets/source/tachikoma.png` and write app `assets/generated/`; use UTF-8 for text I/O. Its CMake dependency and assembly `.incbin` target that app output. The new build script uses existing `compose.yaml` explicitly with project working directory `/project/apps/unified-pet`, no root exporter/build invocation, no automatic image build/pull; verify SDK version before building. Preserve the Dockerfile's pinned base digest and Pillow 11.3.0; unavailable compatible image is a blocker, not install permission.
- [ ] **6. Enforce new-app storage boundary.** App `network.c`/`audio_test.c` read legacy `pet_setup`/`pet_audio` read-only and write `unified_pet`; own key first, legacy fallback only on NOT_FOUND, errors visible, blank secret retains effective saved value. No automatic secret copy, erase or AP/token weakening; caller wiping stays intact. Vendored `microlink/identity_v1`, TailTiny and WireGuard padding/security fixes remain unchanged, with no enrollment.
  Shared touch defaults private `PET_TOUCH_STORE_NAMESPACE` to `"pet_touch"` for root; only the new app's component compile definition selects `"unified_pet"`. Preserve `map` key, `{uint32_t version; touch_map_t map;}` version 1 bytes and original acceptance/clamping/axes. New app falls back read-only to `pet_touch/map` only when its map is absent; never migrates or resets it. Explicit calibration saves to the selected store; no callback framework/API change.
- [ ] **7. GREEN shared/storage assertions.** Run `sh tests/run_shared.sh "$OUT/shared"` plus root/app native and Node suites. New-app boot/blank-save/replacement/failed-commit/invalid records leave all legacy namespaces/identity unchanged; root explicit calibration still writes only its original `pet_touch/map`. Both apps load the old map; a new-app calibration roundtrip changes only `unified_pet/map`. No secret echo or unproved buffer ownership.
- [ ] **8. Verify both builds and preservation.** Build root into fresh external output/config paths seeded from its preserved sdkconfig, then new app independently. Check both compilation databases link the same shared sources once and no cross-app behavior. Keep root dependencies/partitions unchanged, all unrelated source/retained artifact hashes identical; review the declared extraction diff and behavioral regressions. New linked firmware hashes may differ: preserve the old binary, never claim byte identity. Build success is not physical requalification.
- [ ] **9. Authorized checkpoint only.** Preserve sanitized command results/hashes outside the repo; optional scoped commit only if separately authorized. Never stage existing dirty work or artifacts.

### Task 2: Measured microphone backpressure and safe retry (milestone 2)

**Files:** Modify app `main/live_voice.c`, `main/live_voice.h`, `main/audio_test.c`, `main/audio_test.h`, `main/main.c` only as needed for measured fixes/status; add app `tests/voice_pipeline_test.c`, `tests/voice_host.h`, `tests/run_voice_pipeline.sh`; extend app `tests/run_native.sh`.

**Interfaces:**
- Consumes Task 1's local audio interface: `audio_stream_open(audio_session_t **)`, `audio_stream_read(audio_session_t *, void *, unsigned)`, `audio_stream_write(audio_session_t *, void *, unsigned)`, `audio_stream_close(audio_session_t *)`.
- Keep `void live_voice_init(void)`, `void live_voice_start(void)`, `void live_voice_stop(void)`, `void live_voice_reap_finished(void)`, `void live_voice_snapshot(live_status_t *)`.
- Test actual production `capture_audio(void *)`, `send_text(live_connection_t *, const char *)`, `run_session(void)` and lifecycle bodies; host substitutes only hardware/RTOS/clock/transport boundaries, not a second queue/sender policy.
- Produces `sh apps/unified-pet/tests/run_voice_pipeline.sh "$OUT/voice"`; record extracted production-body hashes, strict warnings and ASan/UBSan. No new public queue framework/API.

- [ ] **1. RED: production-path timing tests.** Extend the existing extraction approach with app-local paths and `voice_host.h`; include actual connection definitions and required bodies, not lifecycle-only stubs. Use synthetic 960-byte PCM blocks every 20 ms (24 kHz mono PCM16), existing 96,000-byte capture capacity and 4,800-byte send batches. `test_capture_backpressure` exercises bursty/slow consumption, short sends, hard write failure and sustained stalls; `test_retry_after_failure` starts a fresh attempt after verified cleanup.

  Assertion sketch for test-local observations:

  ```c
  assert(normal_overflows == 0 && normal_unreported_lost_bytes == 0);
  assert(stall_reported_as_error && capture_exit_confirmed);
  assert(retry_started && previous_worker_exit_confirmed);
  assert(boot_capture_opens == 0 && stopped_capture_reads == 0);
  ```

- [ ] **2. Establish diagnostic evidence.** Run the pipeline runner against the copied baseline. Record which controlled schedule causes growth/overflow and which succeeds. Add numeric queue high-water/current bytes, maximum dequeue gap, read/encode/send timing, captured/sent bytes and failure stage inside actual production paths; no PCM/transcript/provider text. Existing priority-4 sender versus priority-5 workers is a hypothesis, not a finding.
  Synchronize cross-task counters/snapshots; avoid per-PCM logging that creates a new sender/capture bottleneck.
- [ ] **3. Pin the failing behavior.** From that reproduction write a regression with a bounded schedule that ought to be serviceable; expect its actual overflow/throughput assertion to fail. A permanently slower-than-capture consumer must instead assert visible controlled failure, not endless buffering. If offline tests do not reproduce the reported failure, stop at instrumentation and request the bounded physical diagnostic gate; do not invent a cause.
- [ ] **4. Implement the smallest demonstrated fix.** Change only the new app's proven bottleneck; keep initial queue/batch sizes, priorities and animation unless measured evidence specifically justifies changing one. Account for every partial write/dequeued byte: no silent success, hidden PCM drop or frame replay. Explicit failure must stop capture and preserve the first error; normal explicit stop may discard remaining unsent PCM with numeric accounting.
- [ ] **5. GREEN.** Assert serviceable schedules have no overflow/unreported PCM loss, failed sends are visible, normal cleanup wipes/releases buffers and task admission, and a new user attempt works. Retain on-demand allocation, no boot/idle capture and exclusive audio/tailnet admission. Run pipeline + app native + both copied Node suites, then isolated build/isolation checks.
- [ ] **6. Conditional physical acceptance, before Task 3.** Present exact artifact/hash, app-only deployment/restore plan and provider/time budget for approval. Preserve/verify existing flash-backup availability; do not read keys or attach USB merely to observe. With authority, use one bounded observer arranged before the test (opening serial may reset the board), and require user Start/Stop actions; propose three sessions capped at 60 seconds each, including retry. Show audible/capture behavior, queue/send measurements and no normal-test overflow. Hardware/provider denial or missing evidence leaves this gate BLOCKED, not passed; do not start repeatability work unless the caller explicitly changes that gate.
  If fresh flash backup is required, obtain specific authority before taking it; prior backup existence is not fresh backup proof.
- [ ] **7. Authorized checkpoint only.** Save sanitized evidence and conditional gaps privately; optional scoped commit only with explicit Git authorization.

### Task 3: Acknowledged setup exit and bounded repeated operation (milestone 3)

**Files:** Modify app `main/network.c`, `main/network.h`, `main/main.c`, `main/setup.html`, `main/live_voice.c`, `main/live_voice.h`, `main/audio_test.c`, `main/audio_test.h`; extend app `tests/audio_lifecycle_test.c`, `tests/run_audio_lifecycle.sh`, `tests/setup_page_test.mjs`; add app `tests/setup_exit_test.c`, `tests/run_setup_exit.sh`; extend app native runner.

**Interfaces:**
- Consumes Task 2's qualified capture/send path and existing `network_snapshot(network_status_t *)`; keep credential accessors unchanged.
- Replace only app-local `network_close_setup(void)` with `esp_err_t network_close_setup(uint32_t *request_id)`; ESP_OK means queued, not closed. Invalid argument/state or queue-full returns an error and no accepted ID.
- Add `setup_close_state_t { SETUP_CLOSE_IDLE, SETUP_CLOSE_PENDING, SETUP_CLOSE_CLOSED, SETUP_CLOSE_FAILED }` and snapshot fields `uint32_t setup_close_id`, `setup_close_state_t setup_close_state`, `esp_err_t setup_close_error`. IDs are monotonic per boot; serialize admissions and reject a second pending close.
- `/finish` returns the accepted ID, not completed-success; sanitized status exposes matching ID/state. CLOSED requires worker-confirmed AP transition; phone link disappearance is uncertainty, never proof. UI navigation Back must remain possible without falsely claiming save or AP closure.
- Keep audio/voice public lifecycle signatures. Add only `bool restart_required` to `live_status_t` for unproved teardown; no automatic session reconnect/replay.

- [ ] **1. RED: diagnose exact setup/recovery failures.** `test_setup_exit_ack` executes production enqueue, `/finish`, worker close and UI callback bodies with injected queue-full, unsaved/disconnected prerequisites, failed AP transition and late completion; copied Node tests cover lost response/newer edits. Trace accepted request -> response-send result -> worker action -> snapshot -> UI, rather than declaring the old 1.8-second delay the cause.
- [ ] **2. RED: real teardown paths.** `test_teardown_fence` exercises production cleanup with blocked/late capture/playback, disconnect while sending, repeated Stop, allocation failure and missing finalization. Extend existing host lifecycle tests beyond their stubbed session body; `test_ten_cycles` asserts ten start/stop/failure/retry sequences have balanced owned allocations/tasks and no capture while stopped. Baseline should fail its unbounded wait/false completion assertions.

  Assertion sketch for test-local observations:

  ```c
  assert(!queue_rejected_request_marked_closed);
  assert(acknowledged_close_id == accepted_close_id);
  assert(unsafe_frees == 0 && retries_before_exit_proof == 0);
  assert(completed_cycles == 10 && owned_task_delta == 0);
  assert(owned_live_allocation_delta == 0 && idle_capture_reads == 0);
  ```

- [ ] **3. Implement acknowledged setup exit.** Worker owns transition and matching completion; check all enqueue/mode-change results, keep errors visible and do not equate acceptance with CLOSED. Do not block LVGL/HTTP awaiting the worker. Phone response transmission must complete before radio shutdown; a failed/uncertain response remains uncertain and is not replayed automatically. Back navigation preserves settings and available setup on failure; Done retains connected-and-committed prerequisites. Replace delay assumptions only where the failing trace supports it.
- [ ] **4. Bound cleanup without unsafe frees.** Replace `portMAX_DELAY` audio-worker exit waiting with a finite deadline; select/document its numeric value from normal read/write/send/teardown measurements plus justified margin before qualification. If worker/callback exit is unproved, publish `restart_required`, keep runtime/buffers/admission fenced, return control and block retry—never force-delete/free live owners or report microphone-off without proof. Later safe reaping requires actual exit/suspended proof, not elapsed time. Normal stopped sessions must have no capture.
  A hardware path that cannot prove capture stopped remains an explicit acceptance gap requiring recovery, not a qualified idle state.
- [ ] **5. Recover without replay.** Preserve bounded saved-Wi-Fi retry/backoff and setup accessibility; after a voice disconnect, terminate/clean or fence the attempt and require explicit user Start. No auto-start microphone/provider session, no replay of uncertain audio/commands and no always-on tailnet claim.
- [ ] **6. GREEN + resource evidence.** Run setup-exit, extended lifecycle/pipeline, app native and Node suites. Inject late completions and disconnect at each owned stage; all finite host deadlines must finish, unsafe retry/free counters remain zero, and ten-cycle owned task/allocation counts balance. Measure warmed-up device internal heap, largest block, PSRAM and task baseline; justify tolerances from repeat measurements before evaluating cycles, not by relaxing failures. Stub counts do not qualify device memory or voice.
- [ ] **7. Conditional device gate.** With separate authority and user actions, complete ten bounded Start/Stop/retry cycles, phone/device Back/Done, setup reopening, connection loss/restoration and idle observation. Declare caps/budget first; no automatic provider loops. Record acknowledgements, task/heap baseline recovery, queue behavior and no stopped capture. Missing physical evidence remains BLOCKED.
- [ ] **8. Authorized checkpoint only.** Optional scoped commit only if authorized; no push/publication. Report offline/build/device results separately, remaining blockers and precise restore artifact.

## Final verification commands and handoff

Run only during approved execution, from repository root with an authorized existing SDK environment and external output directory `OUT`. Node commands require an available approved Node runtime; absence is a prerequisite gap, not install permission.

```sh
node tests/setup_page_test.mjs
node tests/setup_unified_page_test.mjs
sh tests/run_native.sh "$OUT/root-native"
node apps/unified-pet/tests/setup_page_test.mjs
node apps/unified-pet/tests/setup_unified_page_test.mjs
sh apps/unified-pet/tests/run_native.sh "$OUT/app-native"
sh tests/run_shared.sh "$OUT/shared"
# In the approved pinned SDK environment, seed this external config from preserved root sdkconfig first:
idf.py -B "$OUT/root-build" -D "SDKCONFIG=$OUT/root-sdkconfig" build
sh apps/unified-pet/scripts/build.sh
python3 apps/unified-pet/tests/isolation_test.py --baseline "$BASELINE"
```

The new native runner includes storage, pipeline, setup-exit and app-owned native suites; shared suites test extracted production, not copies or silently substituted root behavior.
`tests/run_shared.sh` runs calibration production in both store modes with ASan/UBSan and the original alignment/HTTP tests against component headers. Compile TLS with `-I tests/tls_host -I main -I components/pet_tls/include`, canonical `tests/tailnet_tls_host.c`, root adapter `main/tailnet_tls.c`, shared `components/pet_tls/pet_tls.c`, and `-lmbedtls -lmbedx509 -lmbedcrypto`; repeat using the new app's local adapter/include path, same shared sources/canonical driver, distinct output directories.
For each TLS binary run `python3 tests/tailnet_tls_test.py "$OUT/tailnet_tls_host" "$OUT/cases"` with fresh paths. Missing real-host libraries/toolchain block this gate; no install permission or substituted TLS stubs. Provider WebSocket TLS and vendor TLS remain separate existing paths.
Expected: exit 0, real assertions executed, no sanitizer errors, unchanged unrelated/retained-artifact/dependency/partition hashes, reviewed extraction diff and two app binaries. No whole-tree/new-binary byte-identity promise. These are future requirements, not results.
Self-review: corrected reuse and all other design sections map to Tasks 1–3; five Review Focus cases have owning tests. Measurement-dependent fix/deadline and physical gates remain explicit stop conditions. Authenticated Unified roundtrip stays unimplemented.
Before any implementation, parent links both documents for review and asks for the execution method. Recommend subagent-driven execution for three sequential deliverables with fresh reviews of storage safety and asynchronous teardown; native execution remains an available choice. Design approval alone does not approve this plan or hardware/provider/Git actions.

## Completion gates

| Deliverable | Required evidence | Honest stopping condition |
| --- | --- | --- |
| Task 1 reuse/isolation | Private baseline; shared TLS/touch and storage tests; both builds; reviewed extraction diff | Missing inputs, unrelated hash drift or unproved behavioral preservation |
| Task 2 microphone | Production-path RED/GREEN; measured cause; authorized physical retry | No reproduced cause or no physical/provider authority |
| Task 3 repeatability | Setup/teardown tests; ten device cycles; measured resource tolerance | Unproved shutdown, incomplete cycles or unexplained resource drift |
| Unified roundtrip | Not part of this plan | Keep explicitly deferred; do not invent an integration |

The builder can select private helper names, test stubs and a measured minimal fix within these contracts.
If evidence demands a larger boundary/dependency/product change, return that decision instead of silently expanding scope.
These documents authorize no runtime actions and contain no claim that the planned tests have passed.