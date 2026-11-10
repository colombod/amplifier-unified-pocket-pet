# Amplifier Unified Pocket Pet — Vision (DRAFT)

**For review, not ratified or locked.** This is a proposed interpretation of
[the user's original intent](intent.md#source-quotations-from-initial-workspace-chat-2026-11-09),
not evidence of implemented behavior. The destination below uses present tense.
Connectivity, same-session integration, voice concurrency and attention remain
de-risking spikes, not settled designs. No governing project contract is
established or invented here.

## What Pocket Pet is

Pocket Pet is a portable Wi-Fi voice interface to a personal manager across
Amplifier Unified: existing workspaces and sessions, creating new workspaces,
starting new work, and bringing things waiting on the human to their attention.
The connected device was subsequently identified as Waveshare
ESP32-S3-Touch-AMOLED-1.8 V2 (368×448), with microphone, speaker and touch display;
the initial LCD-1.83 link was a mismatched reference. Its animated, ChatGPT-like pet makes the assistant present without
requiring the main computer screen. Voice carries the interaction; touch supports
explicit context selection.
The intelligence and session runtime stay with Unified on `amplifier-srv-01`,
the server the user describes as always on and already on Tailscale.

The latest user clarification sets the whole-Unified manager goal; it does not
prove full feature parity or authorize arbitrary projects. Existing-session
continuity is a required part of that goal, not its ceiling. See the
[source words and integration findings](intent.md#10-whole-unified-manager-scope-clarification).

**Interaction proposal for review, not an accepted architecture:** a persistent
server-side personal assistant is the default global manager. The human can ask
what needs attention across workspaces, create a workspace and start work there,
or focus directly on an actual existing session and later return to the overview.
Whether to persist a dedicated assistant conversation and use this
global-manager/direct-focus workflow remains DRAFT; it is not the user's choice
merely because it is described here.

## Principles

### 1. Whole-Unified management includes the same session continuing

The human can manage work across workspaces, create a new workspace and start
new work, as well as identify and select an existing session. When directly
addressing that session, their input and the spoken reply belong to it, not a
parallel chatbot. Switching to another context and back retains the original
conversations and histories; selecting a session does not create a replacement.
Creating a workspace, saving a goal and actually starting work are distinct
outcomes, not interchangeable success claims.

### 2. Voice is the interaction

Speech carries the conversation. Touch supports workspace/session selection,
and the display carries the pet rather than requiring a miniature text-chat
workflow.
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

- **A self-contained chatbot or existing-session-only ceiling.** Intelligence
  and execution belong to Unified. Intentionally starting new Unified work is
  part of the goal; substituting a separate chatbot for the selected existing
  session is not continuity.
- **A pocket chat browser.** The display serves the pet and touch context choice.
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
acceptance contracts or implemented acceptance. Counts discriminate context
routing, separate outcomes and repeated use from a one-off demo; they are not
user-set performance targets.

- The user asks for a **new workspace** and then **separate new work** there.
  A real workspace exists at the intended host location and the intended work
  actually starts in its own Unified conversation. An empty folder, saved goal,
  prepared plan or accepted request alone does not demonstrate both outcomes.
- Across **two workspaces**, the user receives status and waiting questions
  traceable to their owning sessions and answers the intended question under
  existing authority. A manager summary does not replace source-owned status,
  revisions or question state; it cannot manufacture an answer or approval.
- From the broader manager interaction the user focuses directly on an
  **actual existing session**, then selects between **two existing sessions**,
  speaks into each and switches back. Input and replies stay in the selected
  original session, with its prior history retained. A **second cycle** preserves
  earlier results without duplicate answers or a reply attached to the wrong session.
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
| 2026-11-09 | Recorded the user's explicit scope clarification: personal manager across Unified, including new workspaces and new work; existing-session continuity is not the ceiling. Revised this draft accordingly. Interaction flow, persistent-assistant architecture and success scenarios remain proposals, not accepted direction or a lock. | [Latest exact source words](intent.md#10-whole-unified-manager-scope-clarification) and [documented/schema-level findings with qualification gaps](intent.md#integration-evidence-correction--2026-11-09). No product operations or device acceptance tested. |

*Drafting references, not governing project contracts:* [Method document
shape][shape], [Unified's vision guidance][unified] and [collaboration
guidance][collaboration]. Their process conventions are not automatically
adopted; see [Unified's adoption limits][adoption]. These citations do not
ratify or lock this draft.

[shape]: https://github.com/microsoft/amplifier-converge-method/blob/86b9a4a457df22c48b80a4c423b3e421dd309292/docs/DOCUMENT-SHAPES.md#L34-L67
[collaboration]: https://github.com/microsoft/amplifier-bundle-converge/blob/5d77bfb159869ec4a32077defe259550fa0a07d3/packages/collaborative/src/converge_instructions/instructions/collaboration.md#L8-L98
[unified]: https://github.com/microsoft/amplifier-unified/blob/d7b16b8d637c86370b11423c850cf8407be9b54c/docs/vision-and-contracts.md#L8-L59
[adoption]: https://github.com/microsoft/amplifier-unified/blob/d7b16b8d637c86370b11423c850cf8407be9b54c/docs/clients/direction.md#L60-L72
