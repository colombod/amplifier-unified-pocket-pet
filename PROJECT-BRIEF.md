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

    pet microphone / speaker / animation / context / Settings
        → existing Wi-Fi + native Tailscale + authenticated TLS
        → narrow host hardware/media adapter
        → Unified native conversations, voice execution and supported host actions
        → correlated result → spoken reply on the pet

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

### Real gaps, not reasons to redesign everything

1. Build 11 is installed and was used for two multi-exchange calls; its mic
   catch-up fix held over the longest call, about seven minutes. This does not
   qualify every transport, reconnection or resource-lifecycle failure. Never
   free under a live callback or replay a prior uncertain send.
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
   An opt-in observation-v1 catalog adapter now exists, including dormant chats
   and coherent continuation, but the inspected host's `companion-v1` middleware
   denies `/api/sessions` despite advertising it. Its tests verify rejection,
   not usable all-work access. Do not require pinning every chat, widen the
   credential, or build UI on an inaccessible catalog.
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

Tailscale integration may advance alongside I1/I2 once file/resource ownership
allows; off-site proof is required for product delivery, not deferred out of scope.
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
- Last demonstrated device boundary: build 11 completed two multi-exchange
  calls through the Unified front door. A later call through the installed,
  independent gateway yielded one user-heard answer with the exact input matched
  to the chat; it ended normally on quiet. Survival across Unified restart and
  automatic reconnect remain unverified. A later post-upgrade test reached the
  front-door chat, but the call had closed before its answer was ready; the
  reason for the device-side stop is unknown. Do not resend that request. The
  original host's LAN service now runs the pre-dial safeguard, but voice after
  that cutover has not yet been verified. Any
  firmware update requires a concrete candidate, a reviewed safe app-only
  installation path and applicable device authority. Do not replay prior
  one-shot operations.

## Provenance

[Original user words](docs/intent.md); current conversation's explicit whole-work,
Tailscale, clean-experience, recoverability and new-workspace/new-chat corrections.
Method: [Microsoft Amplifier Converge](https://github.com/microsoft/amplifier-converge/tree/e39fe7676ba88737319bca21af78dd0f819eb8a3).
Existing standalone GPT-Live/Responses and earlier milestone documents are retained
as implementation/evidence history, not substitutes for this Unified product.
