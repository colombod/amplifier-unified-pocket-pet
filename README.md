# Amplifier Unified Pocket Pet

Private experiment tracking for a portable, Wi-Fi-connected ESP32 assistant:
a pet-like touch display and voice gateway to existing Amplifier Unified
workspaces, running sessions and things waiting on the human.

**Status: de-risking spikes, not settled architecture.** No firmware or Unified
connector is implemented or proven here. Native Tailscale, voice concurrency and
the attention round trip remain unverified. Proposed checks are experiments,
not user-ratified acceptance contracts.

## Hardware and intent

- Device: [Waveshare ESP32-S3-Touch-LCD-1.83](https://docs.waveshare.com/ESP32-S3-Touch-LCD-1.83).
- [Original user intent, interpretation and experiment boundaries](docs/intent.md).
- The user reports an always-on Unified server already on Tailscale; this is
  context, not evidence of a supported external device integration.

## Open spikes

GitHub issues own the work and its evidence. A spike may conclude with a negative,
blocked or inconclusive finding; closing one records learning, not product acceptance.

| Spike | Question |
| --- | --- |
| [#1 Native ESP32 Tailscale connectivity](https://github.com/colombod/amplifier-unified-pocket-pet/issues/1) | Can the exact board connect, reconnect and stay within measured resource limits? |
| [#2 Existing Unified session integration](https://github.com/colombod/amplifier-unified-pocket-pet/issues/2) | Is there a supported authenticated path into the exact existing session? |
| [#3 Voice round trip and device concurrency](https://github.com/colombod/amplifier-unified-pocket-pet/issues/3) | Can voice coexist with responsive touch, animation and networking? |
| [#4 Attention question and answer round trip](https://github.com/colombod/amplifier-unified-pocket-pet/issues/4) | Can a voice answer reach the exact waiting question revision without stale or duplicate effects? |

The integrated attention check depends on session integration and voice checks;
independent investigation can proceed separately. This seed contains records
only: no dependency installation, flashing, server/network changes or deployment.
