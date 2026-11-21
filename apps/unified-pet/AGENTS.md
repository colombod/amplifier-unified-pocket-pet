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

## Measured on the device (November 2026): do not relearn these

Each line was shown on the physical pet by a before/after comparison in one build.
Change one only with a new device measurement.

Scheduling and audio
- The voice owner (`pet_live`) runs pinned to core 1 at priority 6. Unpinned at the
  display task's priority (LVGL, 4, about 75% of a core) it fell behind real time
  and calls ended themselves. This chip pins a task to its current core at its first
  FPU use, and the codec's volume/gain calls use the FPU: "unpinned" is a random core.
- The microphone and speaker tasks run at priority 7 on core 0. The codec's DMA
  holds 60 ms; at priority 5 writes came up to 117 ms late, which is crackling in
  playback and lost microphone audio.
- In a conversation a full microphone buffer drops its newest audio and the call
  continues; a send may wait 5 s. A full buffer must never end the call or redial.
- The playback prebuffer is 320 ms. 640 and 960 ms made no difference in 12 calls:
  the gaps heard then were late codec writes, not an empty buffer.

Wi-Fi
- A network can have several access points. After an unclean restart (power cycle,
  flash) the one the pet was on rejects the first attempt within milliseconds and
  the stack silently joins a weaker one: about every second restart on the test
  network, at -75 to -91 dBm, where calls starve. Sorting by signal alone does not
  fix it (9 of 18 restarts). `network.c` scans, pins the strongest and retries it:
  53 of 53 restarts joined the strongest, each time on the third attempt, about 13 s
  after boot (the first is turned away, the second fails without reaching the access
  point). Five pinned attempts are allowed; the two spare ones have never been used
  or tested. The pet does not roam after joining.
- After a restart, a dead link, a killed listener or a dropped access point in the
  middle of a call, the pet's next call streamed at full rate and ended on mic off
  every time (20 restarts and 3 of each fault on the bench build 0.3.17,
  provider-free listener). That is reconnection and stream rate ONLY: the stream
  was digital silence and nobody spoke. The spoken call on the normal 0.3.17 the
  same evening failed (half the audio arrived, nothing recognised, cause unknown).
  Not tested: the real gateway killed during a call, and a redial that succeeds
  while the mic stays on.
- In a conversation the microphone data is zeroed while the speaker is playing
  (`bridge_voice.c`). A bench leg that plays a tone therefore carries no microphone
  content; "uplink ratio" and "microphone %" count bytes on time, not sound.
- Check signal strength before blaming firmware, gateway or Unified.

Console and bench build
- Opening the USB Serial/JTAG port resets the pet unless DTR and RTS are released
  in ONE request (`TIOCMBIC` with both bits). pyserial releases them in turn and
  resets it; so does esptool, by design.
- The normal build has no console: only a person can start a call on it. Build with
  `PET_PRIVATE_MIC_DIAG=1` for the bench: `voice on|off`, `voice last 0..2` (reports
  of the last three calls, kept until reboot), `voice scan`, `voice drop` (leave the
  access point), and per-call switches.
  Reports are counts and timings only. The bench build's sdkconfig lives in
  `build-bench/`; the root `.gitignore` is hash-pinned by the isolation test.
- The normal binary must contain no bench strings; `tests/run_bridge_native.sh` and
  `scripts/build.sh` check this.

Testing
- The link rig and the native suites replace the CPU, scheduler, codec, display and
  radio. They cannot show any of the faults above. Reproduce on the pet first.
- Test the first call after a restart explicitly: it has been the failing one.
- `tests/isolation_test.py` fails on a missing `.superpowers/sdd/.gitignore` and
  stops there; its pinned hashes of both AGENTS.md files were already stale before
  this work. Its three other tests pass and must keep passing.
- One SDK build at a time, pinned local image, no network.