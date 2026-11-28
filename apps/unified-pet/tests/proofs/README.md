# At-home firmware verification

This check covers the V2 LAN voice client in `apps/unified-pet`. The ESP32 owns touch, buttons, display, codec and Wi-Fi. The Linux gateway owns provider access and Unified integration. The tests use an isolated host and synthetic peripherals. They require no device, provider credentials or battery.

| Requirement | Evidence | Scope |
| --- | --- | --- |
| Joining Wi-Fi preserves foreground setup | Lean proves the retry guards, the 5–60 s backoff invariant and the deadline edge across 32-bit clock wrap. CBMC checks the production retry header with symbolic timestamps and activity flags. Native storage/join tests exercise access-point choice and credential fallback. | This covers eligibility to retry. Association also depends on the AP and radio. |
| Mic on, mic off and recovery behave predictably | Native tests drive the real conversation owner, capture and playback tasks through cancellation, loss, reconnect, exhaustion and cleanup. Lean proves whole-chunk admission stays within capacity under the single-writer assumption. The C check reads the production buffer, task priority and core constants. | At 48 PCM bytes/ms the 244,800-byte capture FIFO holds 5.1 s from empty. Occupied space reduces headroom. Sustained congestion can drop new chunks. Audible playback gates microphone content. |
| Catching up preserves quiet speech | Lean proves that a chunk with any nonzero sample cannot meet the zero-peak discard rule. CBMC checks the C peak calculation for every single PCM16 sample. The `quiet-backlog` test sends two seconds of amplitude-one samples through the production owner and counts delivery at the codec boundary. | This is about playback backlog shedding. It establishes neither intelligibility nor lossless operation under every transport or buffer fault. |
| Touch and physical buttons preserve user intent | Lean checks arbitrary input sequences without a fresh release and repeat confirmation. CBMC checks the BOOT press/release sequence for arbitrary initial 32-bit time, PWR admission after a clean quiet interval, and individual atomic power-gate claims. Native tests cover bounce, stale PMIC interrupts, touch calibration and routing, busy operations and restart/shutdown confirmation. | The gate proof assumes the caller pairs its leases. Switch bounce on this unit, touch alignment and human input latency still require measurement. |
| Configuration survives errors | Lean specifies when legacy fallback is permitted. CBMC checks the production string accessor with arbitrary own/legacy read results, including restoration of buffer capacity and wiping failed output. The native NVS and phone-page tests exercise save failures, late responses and unsaved edits. | The NVS tests substitute the storage backend. They do not exercise flash wear or a real brownout. |
| Stable screens avoid duplicate work | Lean proves suppressing an equal setter preserves the sequence of requested displayed values. The host check compiles the actual label-update and frame-selection code. Over 180 steady idle ticks it checks every frame and counts 60 image-source updates. It also checks joining, wave and label transitions. | These are calls into LVGL. Panel refresh rate, power draw and physical latency require instruments. |
| A future display can distinguish USB power and battery discharge | Lean and the actual C reader agree for all 65,536 pairs of PMIC status bytes. The host driver checks the starting register, transaction size, timeout, unavailable device and failed read after success. | The V2 board and AXP2101 documentation define the hardware assumptions. A worker can call `device_power_read_source()` on demand. The current UI never requests it. |

The power API reports USB for a valid VBUS supply with standby current, including USB with no cell, or with a detected cell charging. Battery requires a detected, discharging cell and VBUS outside its valid input range. These flags describe the supply carrying the load. A cable delivering inadequate voltage can still be physically attached. Unavailable, conflicting or reserved states return unknown. The read uses one transaction on the existing bus and writes no PMIC configuration. [Waveshare board guide](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-1.8) · [X-Powers register definitions, §6.13.2.1–2](https://bbs.aw-ol.com/assets/uploads/files/1660616603344-15714b00-4eed-4559-8ade-19a51f064af3-axp2101_datasheet_v1.0_en.pdf) · [Pinned PMIC reference driver](https://github.com/waveshareteam/ESP32-S3-Touch-AMOLED-1.8/blob/01fd3200370a4417b257e8a4c185a04ef931342a/examples/arduino/libraries/XPowersLib/src/XPowersAXP2101.tpp)

## Verified build, 28 November 2026

Local source based on `801fc67c34626976aba922b4f6afd845bc70e75e` built the normal ESP32-S3 firmware with ESP-IDF 5.5.4. The binary is 7,512,560 bytes, with SHA-256 `ba43ee1bfa0bdb87c2065610e39fca014a5586ee9e17784dd03292e58a0557e8`. Nothing was installed on the device.

* Lean 4.19.0 checked 13 theorems. CBMC 6.11.0 passed all 336 reported properties under the supplied harness. The power reader agreed with Lean on every status-byte pair and returned unknown on bus failure.
* The full application native runner passed, including 177 conversation scenarios, the 658-second simulated cutoff, and 30 standalone audio schedules. The final power and UI edits also passed their affected suites. Shared TLS and touch, artwork, all five phone setup suites, and all 1,605 Linux gateway unit tests passed.
* All seven link-rig scenarios passed. During the 2.5-second blackout the single call recovered, the host measured zero missing microphone samples and a maximum arrival delay of 3.284 seconds, and mic-off ended it. This measures a scripted transport interruption, not acoustic quality.
* The quiet-backlog regression failed on the starting playback code and passed with the zero-peak test. The standalone stall harness now holds the sender until a real enqueue overflows. Both preserve their original teardown and accounting assertions.

The attached unit was treated as V2 per the owner. No USB operation, battery test, live voice call or physical display measurement was part of this run. The historical isolation suite could not be qualified from the public checkout because its private reference artifacts were unavailable.

## Reproduce the proofs

From `apps/unified-pet`, with Lean 4.19.0, Python 3 and a C11 compiler on the host:

```sh
sh tests/proofs/check.sh
```

The script checks the Lean theorems, extracts the C reader and display helpers, and compares the entire power-source table byte for byte. A nonzero exit indicates a failed proof, assertion, compilation or comparison.

For the symbolic C checks, run CBMC 6.11.0 with a 32-bit little-endian data model from the same directory:

```sh
cbmc tests/proofs/policies.c -I main -I ../../tests/touch_host -I tests/tls_host \
  --32 --little-endian --function main --unwind 6 --unwinding-assertions
```

`run_native.sh`, `run_shared.sh` and the impaired-link rig use the repository's pinned ESP-IDF 5.5.4 environment. The link rig runs the production voice/transport code and network stack with a synthetic codec and host. The firmware's scheduler, radio, codec DMA and display are outside the formal model. The separate historical preservation check requires the original private baseline described in `docs/team-prototype.md`.
