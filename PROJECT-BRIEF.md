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

**Recovery decision, 21 November 2026: no release candidate has passed.**
Build 11 (`1004293`, app SHA-256 `fadafa58091bb1c6366e5fba79d9bce2c20e94176c8b417c605a78ab83151a25`)
and gateway `8857705` produced the user-heard two-answer call on 16 November.
Use that pair as a comparator, not an automatic rollback. The last installed
normal build was `0.3.17-strong-ap` from `8d2f313`, gateway `7a31bba`, Unified
0.116.0. Its 19:33 call produced no transcript and no send. The AP and the
cause are unknown. The earlier `0.3.12`/`7161575` reply had a long silent wait
and choppy audio; it is not a replacement release. PR #7 at `16d89c8` is held.

**First localized repair (`0.3.18-voice-buffer`, not physically accepted):**
the post-Build-11 firmware will wait five seconds on a socket send but
buffered only two seconds of microphone input. When it filled, it silently
dropped new 20 ms chunks. In a retained device report the queue reached
96,000 bytes and one send was blocked for 3.336 s. In the firmware owner test,
three distinct 200 ms input markers went from 4,800 / 4,800 / 4,800 samples on
the wire to 4,800 / **0** / 4,800 with a 2.6 s stall. Reserving 244,800 bytes
in PSRAM instead of 96,000 preserves every marker in those tested transient
stalls. It does not delay normal sends or enlarge playback buffering. Prolonged
overload can still lose speech. This is a specific loss mechanism, **not**
proof of the last call's cause, microphone operation, or spoken acceptance.
It changes no storage layout, credentials, or phone setup.

The firmware alone did not fix delivery: replaying the candidate's 2.5 s
blackout timings through the previous gateway gave `not_sent` while delayed
audio was still arriving. With the paired gateway change, a single long,
demonstrably draining stall can receive up to two additional seconds before
refusal. The exact same arrival trace with scripted transcript and HTTP
then submitted the complete phrase once (13.633 s into the replay); a
stopped recovery still refused. This is a joined offline regression, not a
human-heard reply or a qualified Unified release.

**Installed and checked, 21 November:** the provisioned board received the
`0.3.18-private-micdiag` pilot from `13ff037`, with the settings region unchanged;
the gateway switched to `d0c18d2` on a host serving Unified 0.117.0. At the
22:04 UTC log check there had been no device connection, provider dial or
completed call since activation. The normal image is not installed. That is
installation evidence, not a spoken acceptance result.

**Teammate setup:** from a fresh local clone of `1d9a514`, an empty home and a
new Python environment, the [gateway installer](bridge/deploy/README.md#install-and-start)
installed the user unit and systemd validated it. This used the local package
cache and disposable settings. It did not enable the unit or pair a teammate's
Unified installation. Independent host/board setup remains open.

One integration owner ships one tested firmware/gateway/Unified combination
from the product repository. Never bulk-promote experimental branches or private
history. Recover existing tools into the product when a teammate needs them.
Keep a tag and artifact hashes for a demonstrated combination, and distinguish
a historical observation from a supported release. A worker may supply an
isolated change; the integration owner reviews and verifies it in the product.

**Immediate proof:** with a quiet pet speaker, distinguish missing input,
digital silence, nonzero signal, a recognized known phrase, a send to the right
conversation, the correlated result and intelligible playback. Nonzero signal
may be noise. Continuous-tone tests are for transport/recovery only. Keep the
normal firmware stable while isolating the first missing link. A bench twin
is a diagnostic comparison, never a substitute for the normal build. Use a
known external acoustic source if available. If it is not available, report
that limitation rather than making the user the test rig. Do not change Wi-Fi
or buffer settings on the basis of a byte count. The reported black screen is
a separate physical failure, not cleared by ping or USB output.

On one named normal candidate demonstrate three consecutive contextual requests,
including two in one conversation and one after power-up; hear intelligible
answers, observe the actual screen and repeat after ordinary interruption
without duplicates or power cycling. Only then offer a LAN team candidate.
Validate a clean clone, phone setup, fresh-board install versus app-only rollback,
gateway installation and pairing, and a one-command Unified compatibility check.
The gateway service must recover from app/host restart, not merely listen.

| Release decision | Smallest usable result | Stop or proceed |
| --- | --- | --- |
| Locate the LAN failure | Trace the November 16 working code against the candidate; reproduce a difference in the joined path or isolate an exact remaining uncertainty. | No tester scheduling and no request to the user for unchanged-firmware echo, tone or mic checks. Ask for a spoken **product** test only on a named candidate, with the change, evidence, expected result and failure readout ready. Preserve the provisioned board. |
| Restore the conversation | Three intended requests produce three attributable, intelligible replies on normal firmware, with honest progress; one follows power-up. | Preserve the exact working image, gateway source, Unified version and limits. If the first broken boundary changes, investigate it before adding features. |
| Hand out a LAN prototype | A teammate with a fresh board/host can complete the setup above and repeat the conversation using the repo alone. | One independent clean setup. Missing installer, trust setup or incompatible Unified blocks release, not a note to fix after publication. Recheck the exact assembled head before PR approval. |
| Ship the requested assistant | I2, I3 and I4 below verified on the same accumulating product. | No “whole Unified” label based on a text-only demo, and no “portable” label based on host-side VPN reachability. |

The user will perform the meaningful device acceptance test when needed.
First exhaust the preserved evidence and discriminating source/host tests.
Once the first failing boundary is known, estimate **that repair**, then
present the integrated acceptance candidate. A clean-host/board setup with
a teammate is a later release requirement, not a prerequisite for this work.
Do not promise a ship date before the fault, teammate hardware, and scoped
Unified operations are established. The integration owner reports the next
decision, exact candidate and expected user-visible outcome, not worker counts.

The next usable outcome is a team-shareable prototype whose gateway remains
available across Unified restarts and whose pet reconnects while the mic is on.
Finish and verify the existing gateway and firmware work, protect private values,
and prepare a truthful setup guide and reviewable public PR. Demonstrate the
service and reconnect on the installed device when the required device operation
is authorized. A teammate's first setup and off-home use are separate proofs.

Each row below is a usable outcome; its checks are supporting work, not another release.
The delivery manager owns integration and ordinary repairs. Parallelize only
independent implementation with explicit file/resource ownership.

| Increment | User can do | Smallest necessary work | Demonstration / completion |
| --- | --- | --- | --- |
| I1 — clean, reusable Unified voice | Speak to a deliberately selected real conversation, hear its contextual reply, then use voice again without rebooting. | Remove extra/test controls; repair transport/audio ownership and close/reuse; exercise the actual SDK client + adapter + host path, not only extracted lock/pacing functions. Keep native correlation. Design the tailnet join now; do not build a replacement LAN product. | Installed clean screen; correct original-session input/root reply/speaker result; repeated exchange and ordinary disconnect recovery without reset, stale callbacks, duplicate send or admission/resource leak. A single lucky call is insufficient. |
| I2 — choose and continue any work | Find work across workspaces, switch between real conversations and return with context intact. | Supported paged discovery and explicit focus on host/device; per-interaction frozen target; native send delivery rather than guessed busy policy. | Two conversations in different workspaces, switch away/back and speak again; no new replacement chat, cross-target input or wrong spoken reply. |
| I3 — manage attention and create work | Ask what needs attention, answer the intended waiting question, create a new workspace and start a new chat/work there. | Join supported attention/question revision and workspace prepare/create/chat-start actions to the existing voice interaction, preserving permissions. | Genuine waiting question answered at its source; second question not confused; real new workspace + distinct chat + actual requested execution, not just a folder/task/accepted request. |
| I4 — portable daily use | Do the same useful work away from home and recover from network changes without maintenance rituals. | Complete sustained native Tailscale/audio/UI coexistence, setup/reconnect and power behavior. Retain identity; measure actual resources, latency and battery rather than invent targets. | Supported off-home Wi-Fi with home-LAN route unavailable, real voice/result and context switch/new work; repeated calls and interruption recovery; usable animation/touch; measured limits documented. |

After the team LAN increment, present the device-side Tailscale plan and estimate
for decision before implementation. Off-site proof remains required for the product.
Do not expand I1 into a new framework or repeat accepted microphone qualification.
If evidence contradicts the existing path, change only the implicated boundary
and explain the effect on the next usable outcome before dependent changes.

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
  Reuse the established app-only backup and verification sequence, but do not
  rerun or repin a consumed installer: its retry and execution-custody failure
  paths need repair, and direct device access is not established. No
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
