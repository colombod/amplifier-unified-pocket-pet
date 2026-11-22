# Pocket assistant implementation brief

## Outcome and agreement

Build and ship the [portable whole-Unified voice assistant](docs/VISION.md).
The [delivery contract](contracts/pocket-assistant-delivery.v1.md) owns product
promises P1–P9 and their agreement status. This is the one current design and
delivery plan; saved host tasks own execution status. The earlier fixed-session
milestone is an integration step, not the product boundary. Current user
corrections require a clean UI, recovery without ordinary power cycling, and
starting a new chat in a new workspace as well as continuing existing work.

## Design: extend what already exists

    apps/unified-pet/ — microphone, speaker, Tachikoma, touch, phone setup
        ↕ authenticated audio and state over Wi-Fi (LAN today)
    bridge/pet_bridge/ — adapt pet media to voice provider; send and correlate once
        ↕ supported scoped Unified client interface
    Amplifier Unified — owns workspaces, conversations, attention, permissions and work

`bridge/deploy/` and the team guide belong to the product, as do the portable
installation and self-check tools; site secrets, deployment state, captures and
flash backups do not. `bridge/tools/pet_link_test_host.py` is a provider-free
listener, not the production gateway. Root `main/` is the earlier standalone
experiment, **not** the firmware to flash for Unified. Keep device-side
Tailscale as a distinct, unfinished transport for this same product.

- **Pet:** documented Waveshare ESP32-S3-Touch-AMOLED-1.8 V2, CO5300/CST820,
  ES8311, 16 MiB flash / 8 MiB PSRAM, pinned IDF 5.5.4 and official BSP 2.0.3.
  Reuse artwork, codec, animation, calibration, saved settings and phone setup.
  One primary voice control; no extra Stop or normal diagnostic/test screens.
- **Unified:** owns conversation history/context, execution, work lifecycle,
  discovery, waiting questions and permissions. Global questions use supported
  all-work discovery; focused speech reaches the actual chosen conversation.
  Each action freezes its destination before asynchronous work; switching focus
  never redirects in-flight input or replies. New workspace and new-chat start
  are separate supported operations, both verified, not a saved task substitute.
- **Adapter:** only media/authenticated client adaptation and result correlation.
  Retain useful bridge code; remove duplicated native routing policy when that
  is the evidenced obstacle. No second manager/chatbot, transcript store or new
  orchestration platform. A native Unified overview conversation can support
  global dialogue; it need not create a separate reasoning runtime.
- **Authority:** host/provider keys remain host-side. Companion-v1 does not grant
  arbitrary settings/admin/approval actions. Use supported host authority for
  management and exact question revisions; do not impersonate human provenance
  or put a full-control token on the pet to bridge a missing capability.
- **Send/recovery:** the user decided on 14 November 2026, after trying the
  explicit-preview prototype on the device, that voice is a running spoken
  conversation: mic on means the pet listens and answers, mic off means it does
  not listen. There is no "Done talking", transcript preview or Send tap, so a
  recognised request goes to Unified without a confirmation tap; it is still
  sent at most once. The user chose automatic reconnection while the mic is on,
  knowing a reconnect opens another paid call; do not replay uncertain input.
  Mic off does not cancel Unified work. Operations such as sending to another
  chat or creating work still require the applicable confirmation and authority.

### What the source review changes

The earlier review of Unified release 0.91 (`29e3f4f`) found conversation-addressed live-first Send:
the native owner chooses steering/request-boundary/idle delivery. A client-side
idle-only rule is not a product requirement. Existing native voice already
submits to the same session and waits by input identity for the completed root
result. Do not replace this with latest-message polling or separate reasoning.
That review's 0.90 serving-host observation is historical; the subsequent
prototype survey inspected Unified 0.98.6. Recheck live versions when needed.

The public voice connect route is WebRTC SDP, **not an ESP raw-PCM WebSocket**.
Keep necessary hardware media adaptation rather than claiming a native route
exists. Shared external-agent rooms and the post-release ACP room-history change
are not needed for this pet and must not trigger a room/runtime migration.

### Historical engineering findings (through the early LAN candidate)

1. Build 11 was installed and used for two multi-exchange calls; its mic
   catch-up fix held over the longest call, about seven minutes. On 16 November
   a later call gave one heard answer and then "Microphone stream interrupted":
   its capture buffer overflowed. The physical trigger is unknown. Offline, the
   production voice path on the pinned network stack reproduces that overflow
   whenever the round trip to the host passes about 60 ms during an answer,
   because lwIP's default TCP buffers were smaller than one audio message
   ([link rig](apps/unified-pet/tests/link_rig/README.md)). Firmware
   `0.3.9-voice-link` enlarges them (rig-tested, not physically verified). A
   physical test of it was reported to reconnect repeatedly while the user
   spoke; the rig reproduces that with packet loss from the pet.
   `0.3.10-send-wait` (built and rig-tested, not installed) lets a microphone
   frame wait up to 1.8 s, which keeps the call through a 0.6 s blackout.
   Sustained loss or a blackout of seconds still ends the call and replaces it
   while the mic is on. Never free under a live callback or replay a prior
   uncertain send.
2. Existing Tailscale evidence proves a bounded device TLS/HTTP exchange with
   retained identity, not voice. The current spike excludes audio, and the
   bridge validates RFC1918-only dial addresses. Its native socket path is not
   automatically routed through TailTiny's user-space TCP/TLS path. Integrate
   that path with audio/UI and test the real combined resource/throughput budget.
   Removing an address check alone cannot deliver off-site operation.
3. Multi-work context, attention/answer authority and workspace/chat creation
   must join the voice path. Implement those on current supported interfaces;
   identify a real capability gap before changing host architecture.
   `/api/companion` is an active-or-pinned preview, not the all-work catalog.
   An opt-in observation-v1 adapter exists. The early inspected host denied
   scoped session listing. By Unified 0.114.5/0.116.0 the observed situation
   had reversed: `GET /api/sessions?view=observation` worked for the paired
   client and `/api/companion` was denied. Listing is not send, focus or answer
   authority. Do not require pinning every chat or widen the device credential.
   Global discovery, attention and management therefore belong in the existing
   configured Unified manager and its supported host tools; the media adapter
   delivers the request and correlated result, never executes parsed action
   prose. Direct catalog/focus controls wait for supported scoped access.
   Selecting an existing management conversation for physical use remains
   explicit; this does not retarget the pending I1 diagnostic or create a
   second manager runtime.
4. GPT-Live's primary WebSocket has no output-audio-done event. Commentary,
   captions, packet quiet and an empty playback queue do not independently prove
   the user heard a complete answer. Multi-exchange conversation has been used
   successfully on build 11; its speech-end heuristic, interruption behavior,
   recovery and physical repeatability remain limits to verify.

## Delivery sequence

**As of 22 November 2026: display restored, voice failed, release not delivered.**
PR #7 is held. The [original request](docs/intent.md#1-objective) and
[delivery contract](contracts/pocket-assistant-delivery.v1.md#essential-promises)
require useful screen-off voice across **all authorized Unified work**. A
conversation in one selected chat is the first recovery milestone, not the
product. Live state and private evidence are indexed in the workspace `SCRATCH.md`.

### What is done, and what the evidence does not prove

| Work | Verified result and limit |
| --- | --- |
| Display and safe install | App-only update to `0.3.19-amoled-micdiag` (`a17bf56`, image `8536bcf3…`) was read back exactly; the settings region was unchanged before reboot. The user saw Tachikoma and opened Settings on that boot, with no power cycle. This passes display and touch on that boot, not all future boots or voice. |
| Running gateway | `cea016b` is deployed. One 9.3 s check recovered all eight words of host-generated speech in a preview. It did not use the pet microphone or speaker or send to Unified. |
| Voice transport repairs | Firmware `13ff037` preserved marked samples through tested transient socket stalls; paired gateway `d0c18d2` preserved one complete scripted request in the matched offline replay. These repairs do not explain or clear the current capture deficit. |
| Useful historical baseline | Build 11 (`1004293`, image `fadafa58…`) with gateway `8857705` produced two answers the user heard on 16 November; that call later lost the device. Keep the pair for comparison, not automatic rollback. |
| Teammate tools and routing | `1d9a514` generated a valid systemd unit in a clean local environment; `8fac599` validated the portable app updater with simulated flash. `86c3df7` verified restricted HTTPS pairing and workspace-filtered A → B → A binding in a disposable Unified 0.117.0 host. None proves a fresh-board install, independent teammate setup, or in-call voice switching. |
| Pending feedback repair | Local source distinguishes no recognized speech, sending, accepted/waiting, and speaking. Fourteen focused gateway checks and the firmware protocol/status check passed offline. The full changed path has not been qualified, built into a new image, or deployed. It does not repair capture. |

### Immediate recovery: make the next real call useful

On **22 November 15:38–15:40 UTC** the user spoke twice. The pet stayed on
Listening, gave no useful feedback, and the call ended after about 92.5 s.
The gateway admitted 56.84 s worth of PCM, produced zero recognized segments,
and sent zero requests to Unified. In the first 32 s the pet reported 20.32 s
worth of capture and 20.12 s worth of completed sends, with no read errors,
good recorded signal (−60 to −51 dBm), and a sampled capture queue peak of
13,440 bytes. The shortfall was already visible before the send queue. These
are byte counts, not proof of intelligible speech. Long codec reads and
writes and a snapshot of LVGL at effective priority 22 on audio's core are
leads, not proof of scheduling as the root cause. The failed call and its
saved device report have already been collected; do not repeat them.

1. **Isolate the capture deficit.** Compare Build 11 and the current firmware
   and retained timing records with unchanged codec configuration. Use the
   saved evidence to narrow the test to actual sample count at the codec
   boundary versus scheduling or blocking during display and playback.
   In the pinned LVGL code, the timer task can wait on the mutex held during
   touch reads. Check whether priority inheritance keeps the display above
   the audio tasks during slow reads; the snapshot alone cannot show how
   long that happened. Compare actual returned sample count, time between
   reads, and time inside reads, rather than retuning the socket buffer.
   If a new physical observation is needed, propose one authorized, provider-free
   comparison with a quiet pet speaker, a known external spoken phrase and
   a silence control. Keep diagnostics to counts and timings. Compare samples
   read and sent per elapsed second under otherwise matching conditions. A
   tone played by the pet, nonzero PCM, or synthetic input past its microphone
   cannot establish spoken capture. If there is no usable acoustic source,
   state that specific gap; do not substitute another paid call.
2. **Build one matched candidate.** Change only the demonstrated faulty
   boundary, retain the display repair, and finish the feedback change.
   Exercise the device parser/owner and gateway offline: no-recognition
   feedback without a send, pending send versus acknowledged acceptance,
   correlated reply, refusal, and loss without replay. Verify that the capture
   shortfall no longer reproduces under the same conditions before spending
   on another product call. Do not guess at Wi-Fi, buffer size, or task
   priority changes.
3. **Demonstrate one real conversation.** Identify firmware image, gateway
   commit and running Unified version. If deployment is needed, obtain the
   specific device/service authority; preserve app rollback, settings and
   identity. Before asking the user to test, verify a known external phrase
   through the actual pet microphone and live recognition path on that build,
   with specific authority for the provider connection. Do not repeat the
   host-generated preview. For the authorized product call, ask about a
   known decision in the selected real chat, then ask why it was made. The
   user must hear both intelligible answers. Verify that the screen reports
   acceptance only after Unified accepts, and that the journal and chat match
   each request once to its own completed reply. Mute without cancelling
   accepted work. Stop at the first failed boundary; preserve evidence, no
   automatic test retries.
4. **Qualify LAN reuse on the normal build.** Three consecutive attributable
   responses overall, including a contextual pair and one after power-up.
   Confirm visible UI, working touch, an ordinary interruption and another
   interaction without a power cycle, duplicated work, or an unresolved
   cleanup. Confirm the live gateway follows an authorized Unified restart.
   Do not claim this gate on the basis of the diagnostic build alone.

### Remaining release gates, in order

| Gate | Delivery proof |
| --- | --- |
| 1. LAN conversation and recovery | Complete the immediate recovery above with one named, reusable normal combination. Honest status must distinguish listening, not sent, accepted work and the actual answer. |
| 2. Whole-Unified on LAN | Discover work, use the original chats in two workspaces, switch A → B → A by voice and keep context. Read real waiting questions and answer only the intended current one. Create a real workspace and a distinct chat there, then verify that the requested work starts. Use supported authority and never widen the device credential to bridge a missing capability. |
| 3. Independent teammate setup | A second operator uses one clean checkout or approved package, a fresh board, their own host and phone: install, compatibility/trust check, pair, provider-free self-check, real interaction and safe update/rollback/removal. Repeatable maintainer tests are prerequisites, not acceptance. A teammate LAN pilot can ship only as LAN-only and only after its demonstrated scope and publication are approved. |
| 4. Off-home use and final release | After LAN acceptance and the separate transport decision below, demonstrate real voice on another Wi-Fi with the home-LAN path unavailable, including reconnect, correct context and real work. Publish the reviewed combination only with authority. Record exact revisions, image hashes and evidence; exclude private history, credentials and captures. |

One integration owner, one product checkout. Run only the check that can
change the next decision. No repeated paid probes, new architecture, or
off-home implementation while the LAN failure is unresolved. The audit's
historical cost telemetry was partial; it does not verify a current bill or
support a ship date.

### Off-home transport: preliminary scope for the post-LAN decision

The host already has an optional authenticated tailnet listener. The missing
link is **inside the pet**: the voice call takes exclusive audio ownership and
opens the LAN transport, while the diagnostic tunnel requires a different
owner. A listener on the host, or accepting a tailnet IP in the settings form,
does not join those paths.

Reuse the recovered prototypes rather than rewriting or merging whole branches.
`3638a72` adds streaming TLS over a borrowed socket; `8471e3c` adds the transport
constructor and explicit route selection to the Build 11 code. Across the 19
distinct device source trees at the reviewed local branch tips, that constructor
had a definition but no production caller. `307e2e0` and `08e4a05` contain a
tunnel worker-exit fix and its regression tests; `ba5575f` contains the packet
self-loop guard. These are candidate changes to port and review, not evidence
that the current firmware can run a voice call through Tailscale.

| Work after LAN acceptance | Exit condition | Planning allowance |
| --- | --- | --- |
| Port the teardown and borrowed-socket pieces onto the accepted firmware. Keep LAN the default and preserve the provisioned identity and settings. | Stop or failed setup releases only resources whose workers have exited. A tailnet selection never silently falls back to LAN. | 1–2 engineer-days |
| Connect the voice call to the tunnel. Reuse the same gateway protocol, certificate verification, send journal and reply correlation. | A device on a different Wi-Fi sustains both audio directions and a responsive screen. Measure direct and relayed paths where available: 24 kHz PCM16 needs 48 kB/s **each way**, about 64 kB/s after base64 before protocol overhead. Check actual memory headroom, lag and missing audio, not just bytes received. | 2–5 engineer-days |
| Qualify the user journey and recovery on the normal build. | With the home-LAN route unavailable, hear the right answer and a follow-up, switch workspaces, interrupt the link and resume without a power cycle or duplicate work. Record which paths were actually exercised. | 2–3 engineer-days |

**Preliminary total: 5–10 engineer-days for the transport increment, not a ship
date.** This assumes a working LAN voice baseline, available off-home Wi-Fi, a
qualified trust path and authorized hardware access. It excludes implementing
the cross-workspace and new-work features themselves. If the joined tunnel,
codec and UI cannot fit or sustain the measured rate, stop at that finding and
re-estimate before changing the architecture. No tunnel, enrollment, install,
or paid test is authorized by this estimate.

### Follow-up: investigate a more native gateway connection

The user asked to track an architecture investigation, including ACP. After the
working prototype increment, compare the supported Unified ACP launcher and
scoped native HTTP/SSE with the existing companion gateway's text/result seam.
Inspect the then-current host, not just the older 0.98.6 survey. Measure useful
streaming/latency and recovery against the same front-door user experience.
Check exact-session/workspace scope, cross-workspace discovery, approvals,
host-restart handling and uncertainty after disconnect. ACP's inspected version
uses broader owner-control credentials and does not itself provide pet audio or
all-work discovery; a request ID is not yet proven to deduplicate a prompt.
Keep credentials on the host, never replay an uncertain send, and do not silently
switch authority or cancel accepted work when audio ends. First use synthetic
tests, then qualify any candidate through an authorized real voice journey.
Adopt a change only if the verified benefit justifies its authority and recovery
cost; otherwise keep the existing path and record the finding.

## Verification and release boundary

- Maintain focused regressions for changed risks. Reuse valid A6 microphone/Stop,
  setup, audio and device identity evidence while relevant inputs are unchanged.
- Before physical use, check the joined actual SDK-client/adapter/runtime path
  with real local TLS and synthetic remote/provider seams where feasible. This
  catches integration errors but is explicitly not paid-provider/hardware proof.
- For each delivered increment retain exact source/build/app/host identities,
  observed journey, command/result correlation, conditions, failures and limits.
  Source-only, built, installed and physically demonstrated are different states.
- Preserve current settings, credentials, backups, source and uncertain attempts.
  Reuse the verified app-only backup and readback procedure only for an
  explicitly authorized operation on a verified device. Do not rerun or repin
  a consumed one-shot installation, or substitute the simulated updater
  result for qualification on the board. No
  bootloader/table write, automatic rollback, old NVS restore or token regeneration.
- Earlier demonstrated device boundary (15–16 November): build 11 completed two multi-exchange
  calls through the Unified front door. A later call through the installed,
  independent gateway yielded one user-heard answer with the exact input matched
  to the chat; it ended normally on quiet. Survival across Unified restart and
  automatic reconnect remain unverified. A later post-upgrade test reached the
  front-door chat, but the call had closed before its answer was ready; the
  reason for the device-side stop is unknown. Do not resend that request. The
  original host's LAN service subsequently ran the pre-dial safeguard; see the
  dated recovery decision above for the later physical outcome. Any
  firmware update requires a concrete candidate, a reviewed safe app-only
  installation path and applicable device authority. Do not replay prior
  one-shot operations.

## Provenance

[Original user words](docs/intent.md); current conversation's explicit whole-work,
Tailscale, clean-experience, recoverability and new-workspace/new-chat corrections.
Method: [Microsoft Amplifier Converge](https://github.com/microsoft/amplifier-converge/tree/e39fe7676ba88737319bca21af78dd0f819eb8a3).
Existing standalone GPT-Live/Responses and earlier milestone documents are retained
as implementation/evidence history, not substitutes for this Unified product.
