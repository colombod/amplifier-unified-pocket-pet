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

## Interpretation (not original wording or a settled design)

The desired experience is a small portable Wi-Fi-connected pet-like device with
microphone, speaker and touch display. It connects the human to their existing
Unified workspaces and sessions, supports selecting a session, and brings
attention to work waiting on them, with voice enabling a screen-off experience.

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
   discover a supported authenticated ingress and test selection/input/reply in
   the exact existing session with genuine human provenance and receipts/revisions.
3. [Voice round trip and device concurrency](https://github.com/colombod/amplifier-unified-pocket-pet/issues/3):
   measure onboard audio through the server/session and back while touch,
   animation and networking remain active. Push-to-talk is only a possible
   simplifying spike assumption; wakeword and barge-in are unproven.
4. [Attention question and answer round trip](https://github.com/colombod/amplifier-unified-pocket-pet/issues/4):
   test a genuine waiting question, device cue and voice answer to its exact
   revision, including duplicates, reconnect and stale responses. The integrated
   check depends on spikes 2 and 3; investigation can be independent.

No external Unified contract or complete attention feed has been established.
Internal app action names are not URLs. A generic chatbot or a replacement
session would not demonstrate the requested existing-session connector.

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
