# Amplifier Unified Pocket Pet — Vision (DRAFT)

**For review, not ratified or locked.** This is a proposed interpretation of
[the user's original intent](intent.md#source-quotations-from-initial-workspace-chat-2026-11-09),
not evidence of implemented behavior. The destination below uses present tense.
Connectivity, same-session integration, voice concurrency and attention remain
de-risking spikes, not settled designs. No governing project contract is
established or invented here.

## What Pocket Pet is

Pocket Pet is a portable Wi-Fi voice gateway to the human's existing Amplifier
Unified workspaces and running sessions, on the fixed Waveshare
ESP32-S3-Touch-LCD-1.83 with microphone, speaker and touch display. Its animated,
ChatGPT-like pet makes the assistant present without requiring the main computer
screen. The human selects a session on the device and otherwise interacts by
voice. Things waiting on the human come to their attention through the pet.
The intelligence and session runtime stay with Unified on `amplifier-srv-01`,
the server the user describes as always on and already on Tailscale.

## Principles

### 1. The same session continues

The human can identify and select the existing session they are addressing.
Their input and the spoken reply belong to that session, not a parallel chatbot.
Switching to another session and back retains the original conversations and
their histories; selecting a session does not mean creating a replacement.

### 2. Voice is the interaction

Speech carries the conversation. Touch supports session selection, and the
display carries the pet rather than requiring a miniature text-chat workflow.
“Screen-off” means reduced dependence on the main computer screen, not a
requirement to read that screen to complete an otherwise spoken interaction.

### 3. Waiting work reaches its human

Things waiting on the human in relevant workspaces become noticeable through
the pet. A waiting question keeps its workspace and session context when the
human answers by voice. A cue with no connection back to the actual waiting work,
or an answer applied to a different question, is not a completed experience.

### 4. Uncertain delivery stays uncertain

**Proposed safety interpretation:** a disconnected or unconfirmed exchange is
not presented as successfully delivered. The human can distinguish uncertainty
from a confirmed result. Recovery does not silently duplicate their request or
redirect it to a different session.

### 5. Host authority stays intact

**Proposed integration boundary:** Unified owns authentication, permissions and
session execution. The pet uses supported interfaces and genuine user-input
provenance. It neither silently takes over a session nor forges approvals or
grants itself permissions. Choosing voice does not broaden the human's authority.

## What this deliberately resists

- **A self-contained chatbot.** Intelligence and session execution belong to the
  existing Unified host; a separate conversation is not the requested connector.
- **A pocket chat browser.** The display serves the pet and touch session choice.
  The voice experience is not replaced by required reading and typing.
- **A transport decision disguised as direction.** Away-from-home use over Wi-Fi
  is a goal; the authenticated connectivity path remains open. Native Tailscale
  is a spike, not a chosen architecture. This draft selects neither public
  exposure nor an extra router.
- **Unasked capture, wake and notification policies.** This vision chooses no
  always-on recording, wakeword, barge-in, deep-sleep behavior, interruption rule
  or quiet-hours policy. Those choices require their own evidence and decisions.
- **A build plan or invented performance promise.** SDKs, build artifacts, stack
  and provider/model choices belong in scoped experiments and design records.
  This destination promises no offline inference, battery life or numeric
  latency or reliability target.
- **A second authority.** Existing host controls own access; the pet does not
  create an alternative route around them.

## How you can tell it is working

These are **proposed indicators for review**, not passed tests or ratified
acceptance contracts. Counts distinguish repeated use from a one-off demo;
they are not user-set performance targets.

- The device's user selects between **two existing sessions**, speaks into each,
  then switches back. Input and replies stay in the selected original session,
  with its prior history retained. A **second cycle** preserves earlier results
  without duplicate answers or a reply attached to the wrong session.
- The user completes a voice request and hears its reply without looking at
  the main computer screen. Touch session selection and pet animation remain
  usable during the voice/network round trip; a frozen display fails this sign.
- The user notices a genuine waiting question from a relevant workspace and
  answers by voice into that exact waiting question revision. With a **second
  waiting question**, the earlier answer is not reused or applied to it.
- The user reaches the selected existing session from **another Wi-Fi network**
  away from home. A working path only on the home network does not demonstrate
  this goal, and no particular transport earns a pass merely by connecting.
- The user encounters **one interrupted exchange** and is not told it succeeded
  without confirmation. Recovery preserves the intended session and avoids a
  duplicate effect; unresolved delivery remains visibly or audibly uncertain.

## Changelog

| Date | Proposal or accepted change | Source / evidence |
| --- | --- | --- |
| 2026-11-09 | Initial proposal, pending review; no ratification or lock. No accepted changes recorded. | [Original user quotations and provenance](intent.md#source-quotations-from-initial-workspace-chat-2026-11-09); drafting references below. No runtime acceptance evidence. |

*Drafting references, not governing project contracts:* [Method document
shape][shape], [Unified's vision guidance][unified] and [collaboration
guidance][collaboration]. Their process conventions are not automatically
adopted; see [Unified's adoption limits][adoption]. These citations do not
ratify or lock this draft.

[shape]: https://github.com/microsoft/amplifier-converge-method/blob/86b9a4a457df22c48b80a4c423b3e421dd309292/docs/DOCUMENT-SHAPES.md#L34-L67
[collaboration]: https://github.com/microsoft/amplifier-bundle-converge/blob/5d77bfb159869ec4a32077defe259550fa0a07d3/packages/collaborative/src/converge_instructions/instructions/collaboration.md#L8-L98
[unified]: https://github.com/microsoft/amplifier-unified/blob/d7b16b8d637c86370b11423c850cf8407be9b54c/docs/vision-and-contracts.md#L8-L59
[adoption]: https://github.com/microsoft/amplifier-unified/blob/d7b16b8d637c86370b11423c850cf8407be9b54c/docs/clients/direction.md#L60-L72
