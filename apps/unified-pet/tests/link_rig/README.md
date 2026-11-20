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
| `blackout-600ms` | 0.6 s total blackout while listening | same |
| `blackout-2500ms` | 2.5 s total blackout during an answer | one call through mic off; up to 4 s of whole-chunk capture gaps; nothing twice or out of order |

Since `0.3.12-voice-steady` (retained in `0.3.13-voice-clear`), only the long-blackout expectation changes: a 5 s
send allowance and newest-chunk dropping replace the former 1.8 s send limit
and FIFO-overflow exit. It must now keep one call, not reconnect. Its delivered
audio minimum subtracts the explicit 4 s gap budget; all six other scenarios
still require their original audio minimum and no observed gaps.

The Linux-port task seam checks the audio tasks' priority 7/core 0 and the
owner's priority 6/core 1 requests, then creates real SDK tasks on its single
simulated CPU. It does not reproduce ESP32 affinity, codec DMA or the measured
on-device improvement.

The codec seam now encodes an absolute index in each nonzero sample pair rather
than a counter repeating every 0.34 s. The host distinguishes forward,
whole-960-byte capture gaps from replay, backward order and misalignment, which
remain failures even in the long blackout. Eight oracle self-checks run before
the build. Identical digital silence cannot reveal a gap or replay by content;
gap counts cover nonsilent counter observations, not all physical audio.

One offline run of this source passed all seven scenarios with one call each,
no failed sends and final `Mic off`. The 2.5 s blackout held one send for
3.099 s, delivered 18.56 s of microphone audio over the 20 s scenario, and
exposed 26,400 missing counter samples (1.10 s); no replay/order/alignment
errors occurred. The six other cases had no observed counter gaps. This is a
single impaired-link run, not a radio or normal-firmware device qualification.

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

Measured limits on either configuration, as of 17 November (the first two are
revisited below):

- A total blackout long enough to lose a TCP retransmission (about half a second
  or more) stalls lwIP for roughly four seconds, beyond the 1 s send limit and
  the 2 s capture buffer. The call ends and, while the mic is on, is replaced by
  a new one. Microphone audio captured but not yet sent is discarded with the
  old call; nothing is sent twice.
- A blackout that does recover on the first retransmission finishes with about
  0.9 s of the 1 s send limit used. That margin is thin and was not widened.
- Changing lwIP's initial retransmission time made no measurable difference and
  was not adopted.

## Reconnecting while speaking (19 November 2026)

A physical test of `0.3.9-voice-link` showed "Reconnecting..." then "Listening"
repeatedly while the user spoke; the gateway recorded each call as lost without
a stop from the pet. The rig reproduces that sequence with 2% packet loss from
the pet (`delay:both:0:0:5;loss:up:0:0:20`, mic on for 100 s): five calls, each
ended by a failed microphone send, none told to stop. Loss toward the pet only
kept one call for the whole 100 s.

The cause in the code path: the pinned WebSocket client closes the connection
itself when a send times out, a microphone frame was given 1 s, and one lost
packet holds the socket for 1.0 to 1.8 s until lwIP sends it again. A 0.6 s
blackout survived or not depending on where in a frame it began (two of five
start offsets survived).

In `0.3.10-send-wait`, a conversation's microphone frame could wait for as long as the 2 s capture
buffer still has room, less 0.2 s: at most 1.8 s. Measured on the rig:

- `blackout-600ms` at five start offsets 100 ms apart: one call each, longest
  send 0.86 to 1.77 s, nothing failed. The maintained scenario is now required
  to survive (five runs: 1.15 to 1.18 s).
- 2% loss from the pet, or both ways, still replaces the call: four calls in
  100 s instead of five, in one run each, which is not a measured improvement.
  Two packets lost close together stall the socket for more than three seconds,
  beyond what the capture buffer holds.
- `blackout-2500ms` is still replaced, now after 1.8 s instead of 1 s.
- Mic off during such a wait is silent at once (both audio gates and the
  amplifier), but the connection is released only when the waiting frame
  leaves or is given up: up to 1.8 s after it began, where it was 1 s. This
  was checked in the native conversation cases `send-wait-off` and
  `send-wait-off-lost`, not on the rig.

Whether packet loss caused the physical reconnecting is **not known**; the
gateway's record (lost, no stop) fits it and does not prove it.

Whether the round trip to the gateway actually rose during the physical call of
16 November is **not known**. The rig shows a cause that produces that failure
in the real code path, not that it was the cause.
