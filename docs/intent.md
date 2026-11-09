# Pocket pet experiment intent

## Source: quotations from initial workspace chat 2026-11-09

User source session: `5d83ab07-c4fb-4d06-9df1-881014048096`.
These are task-relevant user excerpts supplied from that conversation, not
assistant conclusions. Spelling and quoted wording are retained as supplied.
No message identifiers are asserted.

### 1. Objective

```text
I want to create a pocket assistant using esp32 dev kit to interface me with amplifier unified. The objective is to have a little desktop portable wifi connected assistant to work with my open sessions and be my connector. It will ahve microphone, speaker and touch display so that can help with intercepting notifications and for me to have a \"screen off\" experience
```

### 2. Device

```text
this is the device i will be using <https://docs.waveshare.com/ESP32-S3-Touch-LCD-1.83>
```

### 3. Server, voice and attention

```text
so unified runs on the amplifier-srv-01 server, the service is there. i cann have hte device to conenct via wifi (but we need to investigate hte option of using a tail scale connection so it can be reached from outside world) then the device will use htescreen to be like a chatgpt pet and innteract with me only as voice gateway to the amplifeir unified, help me work with teh worksapces, runnign sessions and give attention toteh thigsn waiting on me
```

### 4. Availability context

```text
the amplifier uified server is always on
```

### 5. Connectivity question

```text
the amplifeir srv is on tail scale, can  we have tailscale client on teh esp32 device ?
```

### 6. Session selection

```text
we can always have omn esp 32 a way to select a session to work with
```

### 7. Process question

```text
should we do this owrk using converge bundle philosophy and protocol ?
```

### 8. Scope correction

```text
those are spiketo derisk
```

### 9. Tracking decision

```text
create a private repo in colombod call it amplifier-unified-pocket-pet, we will track there the work for this experiment
```

### 10. Whole-Unified manager scope clarification

Date: 2026-11-09. Source session: `5d83ab07-c4fb-4d06-9df1-881014048096`.
Latest user message supplied from that conversation; no message ID asserted.

```text
how do you envision teh interaction ? for me this asistant is able to let me interact withteh full of amplifeir unified, and is alike a maanger on top of all of teh work spoaces but also able to crete new oens and start new work ,would it be possible with amplifeir unified ?
```

## Interpretation (not original wording or a settled design)

The desired experience is a small portable Wi-Fi-connected pet-like device with
microphone, speaker and touch display. The latest clarification broadens its
purpose to a personal manager across Unified workspaces, including creating new
workspaces and starting new work. Existing-session continuity, session selection
and attention to waiting work remain required parts of that broader service,
with voice enabling a screen-off experience; they are not its scope ceiling.
The user's phrase "full of amplifeir unified" states the goal, not verified
feature parity or permission to control arbitrary projects.

A persistent server-side personal-assistant conversation and a default global
manager with a direct-focus mode for a specific session are **assistant proposals
for review**, not user quotations, accepted interaction choices or settled
architecture. The user has asked how the interaction could work and whether
Unified supports it, not ratified those answers.

The user identifies the Waveshare ESP32-S3-Touch-LCD-1.83 and reports that Unified
runs on an always-on server already on Tailscale. Those statements give context;
they do not prove board capabilities, ingress contracts or connectivity.
The later correction frames the work as spikes to de-risk. The Converge question
is not approval of a frozen architecture, ratified contracts or manager/lanes.
GitHub is the selected tracking home.

## Proposed experiments (not user-ratified acceptance contracts)

1. [Native ESP32 Tailscale connectivity](https://github.com/colombod/amplifier-unified-pocket-pet/issues/1):
   evaluate community MicroLink separately from official support, exact-board
   reachability from another network, direct/DERP behavior, reconnect and resources.
2. [Existing Unified session integration](https://github.com/colombod/amplifier-unified-pocket-pet/issues/2):
   qualify the documented authenticated ingress on the installed host and test
   selection/input/reply in the exact existing session with genuine human
   provenance and receipts/revisions. The broader manager goal also needs
   qualification of workspace creation and starting separate new work; existing
   session continuity is one subcase, not a separate chatbot.
3. [Voice round trip and device concurrency](https://github.com/colombod/amplifier-unified-pocket-pet/issues/3):
   measure onboard audio through the server/session and back while touch,
   animation and networking remain active. Push-to-talk is only a possible
   simplifying spike assumption; wakeword and barge-in are unproven.
4. [Attention question and answer round trip](https://github.com/colombod/amplifier-unified-pocket-pet/issues/4):
   test a genuine waiting question, device cue and voice answer to its exact
   revision, including duplicates, reconnect and stale responses. The integrated
   check depends on spikes 2 and 3; investigation can be independent.

### Integration evidence correction — 2026-11-09

The earlier repository observation was: "No external Unified contract or complete
attention feed has been established." That recorded a documentation-search gap,
not proof that no interface existed. The external-contract gap is now narrowed
by reading Unified source documentation at commit
`0a89b6f460d78f84c2e8c4f15f67bc9d164e1fa4` (the main HEAD observed for this read):

- [Live session contract v1][live-sessions] documents authenticated client
  attachment, paged session discovery, exact-session snapshots, explicit
  session commands with stable IDs, SSE snapshots and action-schema discovery.
  It includes `conversation.send` with `args.text`; these are documented HTTP
  routes, not endpoints inferred from internal app action names.
- Attachment is not execution takeover; disconnect does not cancel accepted
  work. Reconnection does not automatically replay uncertain inputs. Command
  acceptance and completed work are distinct.
- The [TUI integration handoff][tui-handoff] describes the native control bearer
  credential as service control, not restricted per-device access. The optional
  [observation contract][observations] documents session/attention discovery
  with capability negotiation and typed targets, not a complete feed for every
  Unified feature or proof that the installed host supports it. Scoped
  least-privilege native-device enrollment remains unimplemented in that slice.
- The parent session's live schema inspection, reported from the source session
  above on 2026-11-09, exposes `workspace.list`, read-only `workspace.prepare`,
  and `workspace.create` from a reviewed plan with stable identity.
  `session.handoff.start` describes an authorized independent chat plus opening
  brief in an existing folder without changing desktop selection; direct agent
  calls to `session.create` are refused. These were schema reads, not executed
  workspace or session operations.
- That inspection also reports all-workspace conversation discovery and
  `coordination.send` to existing roots across workspaces under existing
  authority. `coordination.create` workers inherit the current workspace; they
  are not new chats in another folder. `task.create` saves a goal but does not
  start work. Question answers remain bound to revision and human provenance.

**Interpretation and remaining gaps:** these documented and schema-level paths
make the broader manager interaction plausible, not proven end to end. The
handoff's external-client `session.create` example does not override the live
agent-call restriction. Installed-host versions/capabilities, authorization
scope, exact question routing, device transport, audio round trip, concurrent
voice/touch/animation and voice-call arbitration still need qualification.
The live session contract explicitly excludes multi-device voice-call
arbitration. AHP/ACP is a proposed successor, not a shipped replacement inferred
from the design packet. Reading main-branch documentation is not deployment or
device evidence; no runtime probe or integration operation was performed here.

[live-sessions]: https://github.com/microsoft/amplifier-unified/blob/0a89b6f460d78f84c2e8c4f15f67bc9d164e1fa4/docs/clients/live-sessions.md
[tui-handoff]: https://github.com/microsoft/amplifier-unified/blob/0a89b6f460d78f84c2e8c4f15f67bc9d164e1fa4/docs/clients/tui-handoff.md
[observations]: https://github.com/microsoft/amplifier-unified/blob/0a89b6f460d78f84c2e8c4f15f67bc9d164e1fa4/docs/clients/observations.md

## Boundaries and learning

This seed implements no firmware or connector and proves no product capability.
It creates private experiment records and four open issues only. It does not
install dependencies, flash hardware, enroll devices, change servers/Tailscale,
deploy services or start Converge operations.

Future setup must use actual authorization and restricted, reversible access;
it must not silently select public exposure, a router or a replacement architecture.
Keep credentials, Wi-Fi passwords, enrollment keys, private IPs and raw audio out
of committed records. Use sanitized evidence and exact versions/measurements,
including failed attempts and gaps.

Each issue asks a question and proposes evidence to collect. A documented
negative, blocked or inconclusive result can conclude a spike and inform the next
decision; it is distinct from successful product acceptance. Nothing here
requires a positive outcome or turns a proposed experiment into a ratified contract.
