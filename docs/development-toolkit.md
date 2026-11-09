# Workspace development toolkit

Delivered 2026-11-09 for [setup issue #5](https://github.com/colombod/amplifier-unified-pocket-pet/issues/5).
**Independent verdict: PASS — toolkit compile readiness only.** This is not
firmware/product acceptance, a connectivity result or a frozen SDK architecture.

## Installed and isolated

One native Linux toolkit is at workspace `.toolchains/esp32s3`; three scratch
build trees and detailed evidence are at `working-files/esp-idf-preflight`.
All paths below are workspace-relative (the workspace is this repo's parent).
Together they occupied approximately 6.05 GiB at audit, retained intentionally.
The dedicated Python dependency venv reuses an existing base interpreter
read-only; the toolkit is isolated, not completely self-contained.

No Amplifier-environment installation, global package/profile/udev/group change,
device/serial access, server/Tailscale change or enrollment was performed.
No service, container or background process was left running.

| Tool | Delivered version |
| --- | --- |
| ESP-IDF | v5.5.6 |
| ESP32-S3 Xtensa GCC | 14.2.0 / esp-14.2.0_20260121 |
| CMake / Ninja | 3.30.2 / 1.12.1 |
| Python / esptool | 3.13.14 / 4.13.0 |
| Xtensa GDB / OpenOCD | 17.1_20260402 / v0.12.0-esp32-20260831; version probes only |

Pinned sources:

- [ESP-IDF](https://github.com/espressif/esp-idf/tree/880a203ae603d08a94d06aa01e0d6761958e5e9e):
  `880a203ae603d08a94d06aa01e0d6761958e5e9e` (v5.5.6).
- [Waveshare](https://github.com/waveshareteam/ESP32-S3-Touch-LCD-1.83/tree/a790c727912d8f2f5f7cf56fd8cca8c892a36c76):
  `a790c727912d8f2f5f7cf56fd8cca8c892a36c76`; example
  `examples/esp-idf/02_lvgl_demo_v9`.
- [MicroLink](https://github.com/CamM2325/microlink/tree/216da3300f0493b0860247d43f7af5ce29df63a5):
  `216da3300f0493b0860247d43f7af5ce29df63a5`; `examples/basic_connect`.

Compatibility basis: the pinned [vendor manifest](https://github.com/waveshareteam/ESP32-S3-Touch-LCD-1.83/blob/a790c727912d8f2f5f7cf56fd8cca8c892a36c76/examples/esp-idf/02_lvgl_demo_v9/main/idf_component.yml#L3)
requires `idf: ">=5.5.0"`. The [vendor CI description](https://github.com/waveshareteam/ESP32-S3-Touch-LCD-1.83/blob/a790c727912d8f2f5f7cf56fd8cca8c892a36c76/README.md#L82)
names v5.5.5 and v6.0.2, not v5.5.6. Our selected v5.5.6 patch passed these
compile trials; this is a toolkit compatibility choice, not a vendor runtime
guarantee. The retained Waveshare `dependencies.lock` resolves BSP 2.0.0,
LVGL 9.6.0~1 and esp_lvgl_port 2.9.0. MicroLink has no managed manifests or
`dependencies.lock`; its local dependencies are pinned by MicroLink and IDF.

## Use in a NEW Bash shell

Start in the workspace root, not this repository or an activated Amplifier
runtime. Source the activation script; close that shell to discard its environment.
The independent check used a fresh sanitized Bash shell without profiles.

```bash
source .toolchains/esp32s3/activate.sh
idf.py --version

# Representative verified incremental build: no hardware access.
E="$PWD/working-files/esp-idf-preflight"
P="$E/examples/waveshare"
IDF_PY_BUILD_JOBS=8 idf.py -C "$P" -B "$P/build" -D "SDKCONFIG=$P/sdkconfig.preflight" build
```

Original activation/build-wrapper invocations and exact commands for all three
examples are in local `working-files/esp-idf-preflight/toolkit-receipt.txt`.
The wrapper can rerun build configuration; it is not required to use this handoff.
Do not rerun `collect-evidence.py` as a verification command: it creates exclusive
receipts. No USB command or port assumption is part of this setup.

## Verified results and authoritative evidence

Independent receipt: `working-files/esp-idf-preflight/independent-verification.json`,
observed 2026-11-09 17:25 UTC, verifier session `0000000000000000-9cf8a6addd9e45b7`.
Fresh-shell activation, compiler/CMake/Ninja/esptool checks, `pip check`, SDK tool
check and constrained Python dependency check all returned 0; all 62 package
versions were unchanged. Three source checkouts were clean, all 27 IDF gitlinks
matched, and eight retained SDK archives matched pinned metadata.

Original full compiles and subsequent independent incremental `build`/`size`
checks all succeeded. The independent runs used the existing trees: no new source
compilation or CMake regeneration was observed. Counts below belong to the
retained original full compile logs, not fresh recompilation.

| Example | Flash configuration | Original object actions / warnings | App BIN bytes |
| --- | --- | --- | --- |
| IDF hello_world | Generic default 2 MB, DIO | 583 / 0 | 163440 |
| Waveshare LVGL | 16 MB, QIO; octal PSRAM 80 MHz | 2129 / 15 | 1346480 |
| MicroLink basic_connect | 16 MB, DIO; octal PSRAM 80 MHz | 1098 / 11 | 1056192 |

MicroLink's five credential/device/peer configuration fields were verified empty.
The native ESP32 Tailscale candidate **compiles**; no enrollment or runtime test
was performed. Warnings remain: Waveshare LVGL section attributes/unused atomic
helpers; MicroLink unused code/type-limits comparison. Waveshare also retained a
configure warning followed by successful automatic reconfiguration.

All 21 artifacts in `SHA256SUMS` matched before/after independent builds. File-only
image checks passed checksums, validation hashes and embedded ELF SHA256 binding.
Application hashes (paths relative to `working-files/esp-idf-preflight`):

```text
3189ab4a41e94e9fbc72c2868660b01153cc4c2b1efc3ae861943a55b628729a  examples/hello_world/build/hello_world.bin
326820cd6d6a335fc53adc0005dad4c38c5ed838d89828f889d8c190c1c34ef2  examples/waveshare/build/lvgl_demo_v9.bin
425f9674d31a3ba79f97105ec1e265e821ab643839d54677aa0447efcdca241a  examples/microlink/examples/basic_connect/build/microlink_v2_basic_connect.bin
```

Evidence identity (same evidence root; detailed logs/binaries/caches stay out of Git):

```text
668c51b62a9182481e3be6510e7ae45caefadfa5f56dff94cd2274f00fa4ccf3  independent-verification.json
19462af3d9f9fdb69afbfbeb8035ad5642012e7f09704ede46b630fdf5b340f4  receipt.json
6ba11f297c2ee37e104873a62ede86d1601254a8323a89b6719df9823ff5b01d  SHA256SUMS
```

Use `receipt.json` and **`hello_world-result-20261109T170603546116Z.json`**,
`waveshare-result.json`, `microlink-result.json` for original successful commands,
exit codes and log paths. Original full logs are `logs/hello_world-build.log`,
`logs/waveshare-build.log`, `logs/microlink-build.log`; successful audit is
**`logs/evidence-audit-passed.log`**.

Preserved failures are not final status: `hello_world-result.json` is the earlier
unsupported `idf.py -j` configure failure (exit 2);
`logs/evidence-audit-final.log` is the earlier missing-MicroLink-lock audit failure.
Neither was renamed, rewritten or deleted. The independent receipt also retains
its corrected read-only QIO/header assertion history; no toolkit/source patch
was needed. Do not infer authority from a filename containing “final”.

## Limits and next handoff

This is not warning-free, physical-device, connected, fully offline clean-build
or runtime-headroom evidence. Static size/partition fit is not runtime resource
measurement. USB permissions, flash/boot, actual PSRAM capacity/stability,
display/touch/audio/battery, target debugging, Wi-Fi/enrollment/tailnet behavior,
voice/session concurrency and attention routing remain unchecked.

The user will tell us when USB is attached; then enumerate the real device and
choose a safe test without assuming a port or promising a flash now. Existing
spikes [#1–#4](../README.md#open-spikes) remain open and own those later checks.
Original intent and the vision's **DRAFT** status are unchanged.
