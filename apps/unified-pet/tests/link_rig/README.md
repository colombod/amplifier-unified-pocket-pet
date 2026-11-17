# Link rig

Runs the pet's **production** conversation owner and transport on the pinned
network stack, over an impaired link, against a scripted host. It exists to
answer one question offline: can this build carry a running conversation when
the link is slow, lossy or briefly gone, and what does the pet do when it cannot?

## What is real and what is not

Compiled exactly as checked in, nothing replaced or skipped:

- `main/bridge_voice.c` (owner loop, both audio tasks, cleanup, reconnect),
  `main/bridge_transport.c`, `main/bridge_protocol.c`;
- the pinned `esp_websocket_client` 1.6.1 from the verified component cache;
- ESP-IDF 5.5.4 `tcp_transport`, `esp-tls`, mbedTLS (a real TLS 1.2 handshake
  with certificate and hostname verification against a throwaway authority) and
  **lwIP's TCP/IP stack**, on the SDK's FreeRTOS POSIX port.

The TCP, TLS and WebSocket settings that decide link behaviour are copied from
the firmware `sdkconfig` under test; the build fails if the rig's stack differs,
and `rig-config.json` records what was used.

Synthetic, and therefore **not** proven here:

- the radio: an Ethernet tap with scripted delay, loss, rate and blackouts, not
  Wi-Fi airtime, driver buffers, power save or Bluetooth coexistence;
- the processor: a desktop CPU, not the ESP32-S3; encryption, base64 and display
  work cost almost nothing here, so owner-loop starvation is not measured;
- the codec: a real-time counter pattern in, a real-time sink out;
- the host: a script that speaks the conversation wire protocol with the
  gateway's framing and pacing. It is not the gateway, the provider or Unified;
- the parent voice worker, stored token and `esp_timer` are small seams in
  `main/rig_seams.c`.

One host-ABI shim (`main/rig_va_shim.h`) is applied to the WebSocket client only:
its error path formats a `va_list` twice, which is harmless on the device's
by-value Xtensa ABI and crashes on x86-64. The pinned source is not edited.

A pass is offline evidence about the checked-in source and configuration. It is
not an installed-firmware, Wi-Fi or voice result.

## Running

Linux with Docker, the local ESP-IDF 5.5.4 tools image and the verified component
cache used for the firmware build. Build the firmware first so its generated
`sdkconfig` exists.

```sh
PET_DEV_IMAGE="YOUR-VERIFIED-LOCAL-IMAGE" PET_COMPONENT_CACHE="/absolute/cache" \
  sh apps/unified-pet/tests/link_rig/run_link_rig.sh \
  /absolute/fresh/output apps/unified-pet/sdkconfig all
```

The build step is unprivileged with networking disabled. The run step is root
inside a container that has **no network** (`--network none`) and only
`NET_ADMIN`, `CHOWN` and `DAC_OVERRIDE` plus `/dev/net/tun`: the tap interface
exists only in that private namespace and disappears with the container. Each
scenario runs in real time (10 to 20 seconds).

Trailing `KEY=VALUE` arguments override a mirrored setting to try a candidate
stack configuration without changing the firmware, for example
`LWIP_TCP_SND_BUF_DEFAULT=5760 LWIP_TCP_WND_DEFAULT=5760 LWIP_TCP_RECVMBOX_SIZE=6`
to reproduce the earlier configuration.

Per scenario the output directory holds the device log, the host record and
`result.json`: what the pet showed and when, every microphone message's arrival
time at the host, whether the stream arrived complete and in order, and per
second where the owner's send time went (transport lock, socket, or neither).

## Scenarios and required behaviour

`scenarios.json`. The same judgement is applied to every build.

| Scenario | Link | Required |
| --- | --- | --- |
| `clean` | 4 ms round trip | one call from mic on to the user's mic off |
| `talk-rtt60` | 60 ms round trip, one 5 s answer | same |
| `talk-rtt160` | 160 ms round trip, one 5 s answer | same |
| `listen-rtt160` | 160 ms round trip, microphone only | same |
| `loss-0.5pct` | 10 ms round trip, 0.5% loss each way | same |
| `blackout-600ms` | 0.6 s total blackout while listening | may be replaced by a new call; ends on the user's mic off; nothing twice or out of order |
| `blackout-2500ms` | 2.5 s total blackout during an answer | same |

## What it found (17 November 2026)

With the configuration of `f4f5e76` (lwIP default 5760-byte TCP send buffer and
receive window) `talk-rtt60`, `talk-rtt160`, `listen-rtt160` and `loss-0.5pct`
fail. In `talk-rtt60` the pet plays part of the answer, its microphone falls
more than two seconds behind, the 2 s capture buffer overflows and the call
ends: the local condition the installed build 11 reports as "Microphone stream
interrupted." The probes separate two causes:

- **Throughput.** Reply audio and backlogged microphone messages are about
  6.4 kB on the wire, larger than the send buffer. Each one waits for
  acknowledgements, so the link carries at most one buffer per round trip:
  microphone only, with no lock contention at all, already fails at 100 ms.
- **The transport lock.** Reads and writes share one I/O lock. A reply message
  larger than the receive window is read across a round trip with the lock
  held, and the microphone send waits for it. With both directions flowing the
  call fails from about 40 to 60 ms round trip.

With 23040-byte buffers (`sdkconfig.defaults`) the same source passes all seven.
At a 60 ms round trip the longest send took under 10 ms with no lock waiting. At
160 ms it took about 0.1 s, and with 0.5% loss anything from a millisecond to
0.45 s across nine runs, against the 1 s send limit: slower links still queue,
they no longer overflow. Six further loss seeds also passed (five of six failed before). Why
loss recovers faster was not instrumented. A larger microphone
capture buffer was not used: it leaves both causes in place and only postpones
the overflow while throughput stays below real time.

Measured limits that remain, on either configuration:

- A total blackout long enough to lose a TCP retransmission (about half a second
  or more) stalls lwIP for roughly four seconds, beyond the 1 s send limit and
  the 2 s capture buffer. The call ends and, while the mic is on, is replaced by
  a new one. Microphone audio captured but not yet sent is discarded with the
  old call; nothing is sent twice.
- A blackout that does recover on the first retransmission finishes with about
  0.9 s of the 1 s send limit used. That margin is thin and was not widened.
- Changing lwIP's initial retransmission time made no measurable difference and
  was not adopted.

Whether the round trip to the gateway actually rose during the physical call of
16 November is **not known**. The rig shows a cause that produces that failure
in the real code path, not that it was the cause.
