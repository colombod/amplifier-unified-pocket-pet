# PR #6 review and health gate

Reviewed the firmware, phone provisioning, asset/build/recovery paths and source
changes following the Copilot review on fdd783e. This is an engineering review,
not an external approval or a claim that all hardware behavior is proven.

## Findings and resolution

| Finding | Resolution and validation |
| --- | --- |
| Saved network stops retrying after failed joins (Copilot r4528466055) | Failed joins schedule bounded 5–60 second backoff while disconnected. Retry admission checks active join, queued commands and recent provisioning activity under the state lock. Native policy tests cover retries, cap, reset, provisioning pause and timer wrap. Physical router outage/return is untested. |
| An older save can clear newer unsent edits (Copilot r4528466138) | Editing invalidates awaitingSave. The page regression simulates editing during a pending generation, confirms preservation/Done gating, then confirms a new explicit submission can complete normally. |
| Amplifier not explicitly disabled at boot (restart health investigation) | Hold the exact BSP amplifier pin low before other app setup and during release; no codec/microphone opens at boot. Fresh-boot log and user listening check are separate evidence. |
| Sparse playback can exhaust hardware DMA (health investigation) | The first 160 ms prebuffer still logged 58 underruns. The current candidate uses 320 ms and voice-only modem-sleep disable/restore, with underrun counts and short fades. Physical audio check still required. |
| Codec automatically enables PA before output settles (startup-click investigation) | Use the codec driver’s supported no-PA mode, prime zero samples with the amplifier held off, settle/unmute the DAC at zero volume, then enable the exact BSP amplifier pin and restore volume. Hardware start/stop acceptance is pending. |
| Audio workers depend on sender noticing Stop (manual review) | Workers now check Stop directly, including after the blocking microphone read; cleanup still joins them before freeing the codec. |

## Other reviewed boundaries

- Original source artwork is unchanged and mapped in flash, not decoded wholesale into PSRAM.
- Correct AMOLED V2 BSP, display geometry and ES8311 wiring are used; no managed component patches.
- TLS verifies CA/hostname; the saved key stays on device and never appears in phone responses, URLs or logs.
- Phone setup is AP-only with a per-boot mutation token. First key is required; Wi-Fi edits can retain it.
- Continuous returned silence is distinct from audible speech, with a PCM regression test.
- Audio ownership, bounded queues, session startup/close, failure cleanup and Stop were inspected.
- Factory backup is private, digest-verified and recovery was exercised. App-only updates retain NVS.
- The milestone remains standalone speech without persistent memory or Unified integration.

## Merge gate

Build, partition fit, native address/protocol/touch/retry tests, phone regressions,
staged-secret checks and flash verification must pass on the final source. Both
inline review findings must be fixed. Physical post-restart idle/tone/voice health
must be confirmed before merge. No GitHub Actions success is inferred from an
empty checks list or the earlier review job that reported an Actions budget limit.

The user authorized merge after a satisfactory review and health check. There is
no authority to waive a failing physical audio check. Keep unmeasured long-duration
stability, touch-calibration accuracy and router-outage recovery explicit in the
handoff; they are not silently reported as passes.
