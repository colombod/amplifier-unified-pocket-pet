---
name: pet-device-bench
description: Measure the pocket pet's firmware on the physical device from USB without anyone pressing the mic control. Use to start and stop calls from the console, read the pet's per-call counters, compare one setting before and after in the same build, check which Wi-Fi access point the pet joined, or read the report of a call a person just made.
compatibility: Linux host with the pet on USB Serial/JTAG, read/write access to its console node, Python 3 stdlib. Needs the private bench firmware (PET_PRIVATE_MIC_DIAG=1) installed.
---

# Pet device bench

The native suites and the link rig replace the CPU, scheduler, codec, display and
radio, so timing, audio and Wi-Fi faults do not appear there. Reproduce on the pet.

Before you start: installing firmware restarts the pet and blanks its screen, and
calls play sound. Tell the person what they will see and for how long.

## Steps

1. Build the bench variant: the normal build command from
   `apps/unified-pet/scripts/build.sh` with `PET_PRIVATE_MIC_DIAG=1` added
   (output `apps/unified-pet/build-bench/unified_pet.bin`, version
   `*-private-micdiag`). The normal build has no console and keeps no report.
2. Install it app-only and keep the previous image for rollback.
3. Give the pet something to call. The real gateway dials the voice provider:
   that is a paid call, ask first. A provider-free listener avoids that.
4. Run a call and read it:
   `python3 scripts/pet_bench.py run --port <console node> --seconds 28 --out <run dir>/<leg>/device`
   writes `device-report.txt` and `summary.json` there. Per-call switches: `--core`,
   `--prio`, `--audio <priority><core>`, `--jitter <ms>`, `--bw <20|40>`.
5. A call the person made: `pet_bench.py last --port <node> --out <dir>` reads the
   last three reports kept since boot.
6. Wi-Fi: `pet_bench.py scan --port <node>` lists the joined network's access points
   by signal and says whether the pet is on the strongest.
7. Tables over several legs: `playstats.py <run dir>` (playback continuity, late codec
   writes), `corestats.py <run dir>` (task placement, CPU share, uplink). Both
   read `<run dir>/<leg>/device/summary.json`.

Scripts are in `scripts/` beside this file; record formats, console commands and
exit reasons are in [references/report-format.md](references/report-format.md).

## Method

- Compare inside one build, switched from the console, legs interleaved
  (A B A B ...), at least six per side. Separate builds and separate hours differ in
  signal and placement and prove nothing.
- State the question and the result that would change the decision before running.
- Check the report's `wifi` record on every leg: a weak or different access point
  invalidates a comparison.
- A report covers the first 32 seconds of a call. For longer calls use the other
  end's record.
- `run` rejects a truncated report. Do not hand-repair one; rerun the leg.
- Before restoring a paid-capable gateway, verify Mic Off and that the test
  listener is released. For the supervised single-connection test, use the
  completed device user-stop report plus the listener's confirmed peer closure
  after `pet.stop` and `session.close`, with no reopen. An empty close payload
  can be reported as code `0`; neither that value nor `1000` alone proves Stop.
  If Stop is uncertain, preserve the records and do not replay or restore in a
  way that could trigger a paid reconnect.

## Limits

- The report and the uplink ratio count bytes of the audio stream, not sound. A
  leg whose listener plays a tone carries only zeros, because the firmware zeroes
  microphone data while the speaker plays. To see microphone content use a leg
  with a silent speaker and count non-zero samples at the other end. No bench leg
  shows that speech is captured or recognised: only a spoken call through the
  real gateway does.
- Nothing here can see the screen.
- Opening the console resets the pet unless DTR and RTS are released in one request.
  `pet_bench.Console` does this; pyserial and esptool do not.
- `voice audio` applies to the next call only; `voice core`, `voice prio` and
  `voice jitter` stay until changed or reboot.
- One scan in ten returned an incomplete answer in testing; rerun it.
- The portable provider-free listener and loopback test are in `bridge/tools/`.
  The original site-specific app-only installer is not a teammate release tool.

Done when: each claim cites a saved `summary.json`, with the number of legs per
side and the signal they ran at.
