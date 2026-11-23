# Existing-Session Voice Contract — v1 (DRAFT)

**Historical prototype scope, not the current product contract.** The
[whole-product delivery contract](../docs/CONTRACT.md) and
[current brief](../PROJECT-BRIEF.md) supersede this draft's fixed-session,
one-turn and deferred-portability scope. Preserve existing explicit-send and
uncertain-delivery behavior until its deliberate implementation change; this
notice does not silently enable automatic sending or a new paid connection.

**Who builds against this:** the person speaking to the pet, and the person who
chose its target session; the host bridge (`bridge/`) and the device app
(`apps/unified-pet/`), which originally built this prototype; reviewers of the
historical prototype. Exact
device↔host frames live once in [pet-bridge-wire.v1.md](pet-bridge-wire.v1.md);
this contract owns what the human experiences. No implementation is accepted yet.

## What it looks like

The human has already chosen one existing Unified session. They tap Talk, speak,
tap Done talking, check the preview and tap Send. Their words arrive once, as
their own input, in that session; the session's reply is spoken back.

```
pet screen: "Talking to: workspace progress  …0f3c9a21"   (illustrative label)
human: speaks → Done talking → reads preview → Send
pet: Sent (accepted) → reply (spoken rendering, not verbatim) → finished
Unified: one new user input in that same session; canonical reply text stays there
```

## Purpose

Without this promise the pet quietly becomes a parallel chatbot, posts into the
wrong conversation, sends the same words twice, or reports success it cannot
prove. Because of it, the human can trust that the pet is a voice for a session
they chose, that nothing is sent they did not confirm, and that every unclear
outcome stays visibly unclear.

## Core (the teeth)

1. **The target is one existing Unified session the human explicitly selected.**
   The bridge never creates, guesses ("latest", default), switches or redirects
   it, including after an error. Broken: a reply from any other conversation.
2. **That session keeps its own context.** Only the human's confirmed turn is
   added to it. No separate reasoning model answers instead, and device-supplied
   greetings or instructions are not injected into it.
3. **Only text the human saw and confirmed is sent.** Each preview needs its own
   explicit Send. Transcript heuristics, provider delegation events and model
   output never authorize a send; a changed preview needs a new tap.
4. **A confirmed turn is sent at most once.** No automatic retry, replay or new
   send identity for an uncertain post. A busy or conflicting session yields a
   rejection, not a queued or repeated send.
5. **Uncertainty is durable and honest.** When the send outcome is unknown, the
   pet says it is unknown and must be checked in Unified, and the host keeps a
   durable record that it may have happened. It is never shown as sent or failed.
6. **Every outcome is distinct.** Send: accepted, rejected, uncertain. Reply:
   completed, unavailable, stopped. Busy, no speech, expired and other errors are
   shown as themselves; an unavailable or stopped reply is never shown as Done.
7. **The spoken reply is this turn's correlated reply, labelled not verbatim.**
   It comes from this turn's own completed generation, never the latest or
   positional assistant message. Audio before correlation is dropped. The exact
   text remains in Unified; the pet labels speech a spoken rendering.
8. **Stop stops the pet, not Unified.** Stop closes the microphone and reply
   audio at once. A send or run already in Unified continues; the pet never
   claims it cancelled Unified work.
9. **Nothing listens or connects without a human action, and one turn ends the
   connection.** Talk starts it; after one turn the connection closes with no
   automatic reconnect or resume.
10. **Credentials stay on the host.** Unified and provider keys never reach the
    device; the device holds only its own pet token. TLS verification is never
    bypassed. Transcripts, audio, labels and tokens stay out of logs and status.

## What v1 deliberately does NOT freeze

- How the human selects the session (today a host-side binding set by the owner)
  — promoted when an on-device or multi-session choice is agreed.
- Switching between sessions, new work, waiting questions, away-from-home use —
  promoted when a later milestone agreement names them ([vision](../docs/VISION.md)).
- Barge-in, wakeword, continuous conversation — promoted on evidence and decision.
- Latency or reliability numbers — promoted after measured M2/M3 baselines exist.
- Exact on-screen and spoken wording beyond the labels above.

## Conformance kit asserts

No kit exists. Every assertion below currently reports **Can't check**; an
unrun check is not a pass. Offline fakes can cover parts of 3–6 and 8 but cannot
prove 1, 2, 7 or physical audio; a pass needs a bounded, human-permitted round
trip on the real device against the real selected session.

- The reply heard after one confirmed turn traces to that turn's generation in
  the selected session, whose earlier history is intact (1, 2, 7).
- A second Send of the same preview, a stale revision, or a delegation event
  produces no second input in Unified (3, 4).
- A forced unknown send outcome shows "unknown" and leaves a durable host record,
  with no resend after reconnect (5).
- Unavailable and stopped replies never render as Done (6).
- Stop during a pending send silences the pet while the Unified input proceeds (8).
- Canary tokens, transcript and audio never appear in logs or status (10).

## Reserved / open questions (NOT frozen)

- How the human later learns the true outcome of an uncertain send beyond
  "check in Unified".
