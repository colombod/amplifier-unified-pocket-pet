# Pocket Pet architecture

The pet is a voice client for Amplifier Unified, not a second assistant. The
[vision](VISION.md) describes the destination. The [delivery contract](CONTRACT.md)
owns behavior and acceptance. This page explains how the parts fit together and
where to extend them.

## The released LAN path

```text
Phone ── local setup page ──► Pocket Pet (ESP32-S3)
                                      │
                       authenticated TLS WebSocket
                                      │
                                      ▼
                        Pocket gateway (Linux)
                          │                │
            OpenAI GPT-Live voice     scoped companion client
                                           │
                                           ▼
                                  Amplifier Unified
```

1. The phone setup page stores Wi-Fi and gateway settings on the pet. It is
   not a Unified login. See [phone setup](team-prototype.md#3-configure-from-a-phone).
2. When the user turns the mic on, the pet sends audio to the gateway on the
   private LAN. The pet verifies the gateway's TLS name and presents its own
   device token. The gateway runs GPT-Live on the host; the provider key and
   paired Unified credential are never copied onto the pet.
3. The gateway submits a recognized Unified request to the selected existing
   chat and matches its reply to that request. **Accepted** is not completion.
   While one reply is pending, the released gateway does not queue follow-ups.
   Turning the mic off closes the voice session, not work already accepted by Unified.

| Boundary | Owns | Where to make changes |
| --- | --- | --- |
| Pet | Touch and animation, mic/speaker, setup, saved device settings, and the gateway connection | [`apps/unified-pet`](../apps/unified-pet/), especially [`main.c`](../apps/unified-pet/main/main.c), [`setup.html`](../apps/unified-pet/main/setup.html), and [`bridge_voice.c`](../apps/unified-pet/main/bridge_voice.c) |
| Pocket gateway | Device admission, media adaptation, voice-turn state, reply correlation, and safe failure reporting | [`bridge/pet_bridge`](../bridge/pet_bridge/), including [`gateway_core.py`](../bridge/pet_bridge/gateway_core.py), [`gateway_provider.py`](../bridge/pet_bridge/gateway_provider.py), and [`companion.py`](../bridge/pet_bridge/companion.py) |
| Amplifier Unified | Workspace and chat identity, permissions, execution, history, and waiting questions | A supported stock Unified release. Check its supported companion capabilities; file upstream feedback when a capability is missing. |

## Hardware, build, and deployment

- Use the **Waveshare ESP32-S3-Touch-AMOLED-1.8 V2**: CO5300 368×448 display,
  CST820 touch, ES8311 audio, 16 MiB flash, and 8 MiB PSRAM. The LCD-1.83 and
  earlier AMOLED boards are not substitutes. The [board and toolchain pins](../PINS.md#hardware)
  identify the BSP and ESP-IDF versions. The [board selection guide](team-prototype.md#what-to-get)
  links to the vendor hardware; the [bring-up record](device-bringup.md#hardware-correction-and-recovery)
  explains why the earlier board profile did not work.
- Follow the [matched firmware build](team-prototype.md#1-obtain-a-matched-build-and-preserve-the-board).
  The supported project is `apps/unified-pet`; the root `scripts/build.sh`
  delegates to it on `main`. Use the guide for the chosen release. The root
  `main/` is historical source retained for tests, not another firmware target.
- The [Linux deployment guide](../bridge/deploy/README.md) covers the gateway
  service, pairing, and TLS listener. Keep the firmware and gateway source from
  the same release. Neither a listener probe nor a successful build proves
  a fresh-board install. The [setup guide](team-prototype.md#start-here) keeps
  first install separate from updates that must preserve settings.

## Extending the product

- For controls or setup, start with the [device guidance](../apps/unified-pet/AGENTS.md)
  and run the affected [offline checks](team-prototype.md#offline-checks-for-maintainers).
  For a transport change, test both peers together under slow and interrupted
  links with the [link rig](../apps/unified-pet/tests/link_rig/README.md).
- For conversation behavior, keep one authoritative destination and correlate
  replies to the original request. Check rejection, missing acknowledgements,
  mic-off, and subsequent recovery before claiming a change works. Never replay
  an uncertain send. A new workspace, a new chat, and work actually starting
  are distinct outcomes.
- A single chat on the LAN is the released increment, not the design limit.
  Extending to off-home Tailscale requires the **pet itself** to work away from
  the LAN; a gateway that can already reach Unified over Tailscale is not proof.
  Queued follow-ups stay off until a supported Unified release provides safe
  idle-only admission ([Unified #2420](https://github.com/microsoft/amplifier-unified/issues/2420)).
  Pocket follow-up is tracked in [#11](https://github.com/colombod/amplifier-unified-pocket-pet/issues/11).

The [implementation brief](../PROJECT-BRIEF.md#delivery-sequence) tracks sequencing
and evidence, not a second set of promises. These boundaries follow Converge's
[system design and decomposition contract](https://github.com/microsoft/amplifier-converge/blob/9c785283c6ef16782ad7847e3ef3ddc8ac7e6462/contracts/system-design-and-decomposition-contract.md).