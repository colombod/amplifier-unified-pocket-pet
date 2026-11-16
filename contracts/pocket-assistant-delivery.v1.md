# Pocket Assistant delivery contract — v1

**Agreement status (14 November 2026):** records the user's explicit requirements
in this conversation; the acceptance scenarios and implementation sequence are
the delivery manager's proposed means of meeting them, not a claim that the user
approved this exact artifact or that the product works. Work already authorized
continues; drafting is neither a new approval gate nor new release/spend authority.

**Boundary and owners:** the user relies on the combined physical pet + host
adapter + Amplifier Unified experience. The delivery manager owns that combined
result, including integration, ordinary repairs and verification. Device and
host contributors own their implementation boundaries, not independent
substitutes for the outcome. [Vision](../docs/VISION.md) owns the destination;
[PROJECT-BRIEF](../PROJECT-BRIEF.md) owns the current design and delivery order.

## Essential promises

| ID | Promise | What shows it is kept or broken |
| --- | --- | --- |
| P1 | **All authorized Unified work, not one bound session.** The user can discover work, choose an existing conversation across workspaces, and continue its real context. | Speak to two existing conversations in different workspaces and switch back. Original histories remain; each input and reply belongs to the intended destination. A parallel chatbot or permanently hard-coded session fails. |
| P2 | **Voice is the normal interface.** The pet captures intentional speech and speaks the actual correlated Unified result. It distinguishes accepted/ongoing work from a finished result. | The user completes an exchange without the main computer screen. Evidence joins the input identity to the owning root result and audible reply. Latest-message guesses, unrelated narration, silent truncation or calling acceptance completion fail. Speech rendering need not be verbatim; it must not invent work or results. |
| P3 | **Attention leads back to its owner.** Status and waiting questions retain their workspace, conversation and current revision. A voice answer reaches that exact question through supported authority. | Read a genuine waiting question, answer it, then handle a second question. No answer is reused, marked handled merely by reading, or applied to a stale/different question. Approvals retain their own host requirements; voice does not grant authority. |
| P4 | **New workspace, new chat, actual work.** The user can request all three by voice. | A real workspace is created at the intended location, a distinct chat belongs to it, and the requested work starts there. An empty folder, saved task, prepared chat or accepted request alone fails. Ambiguous destinations and consequential choices are clarified. |
| P5 | **Portable through Tailscale.** The same product reaches the always-on Unified host away from the private LAN, using supported Wi-Fi, authenticated TLS and the retained device identity. | Real speech and a correlated spoken result over another Wi-Fi network with the home-LAN path unavailable. A tunnel/HTTP probe alone fails. No public exposure, extra VPN router or identity regeneration is silently substituted. |
| P6 | **Clean, responsive pet experience.** Keep the Tachikoma, one primary voice control, clear context/state and useful Settings. No extra Stop button, normal-screen transport counters, test controls or required diagnostic rituals. | Inspect the installed display and operate touch/animation during voice/network activity. The user's chosen running conversation sends recognized requests without a preview or Send tap; consequential downstream actions retain applicable confirmation and host authority. A mandatory text-chat workflow is not the final voice experience. |
| P7 | **Recover without ordinary power cycling.** A normal close, network loss, timeout or cancelled call releases the media/transport safely and allows another interaction without restarting the board. | Repeated normal exchanges plus interruption before and after input acceptance; another interaction works without reset, stale callbacks, retained admission or resource growth. The user chose automatic reconnect while the mic is on; disclose that a new paid call may result. Never replay uncertain input or hide a leak or unsafe free by changing an error label. |
| P8 | **Preserve authority, data and honest outcomes.** Unified owns permissions/execution; full host/provider credentials stay host-side. Keep current Wi-Fi, token, calibration and device identity. Input delivery is at most once; uncertainty stays visible until reconciled. Ending audio does not cancel accepted Unified work. | Check rejected/stale commands, lost acknowledgements and stop races. No duplicate effect, cross-context reply, fabricated approval, credential leak, old NVS restoration or false Done. Backup and exact app/source/build identity accompany installation. |
| P9 | **Ship usable increments, not evidence piles.** Each increment extends the previous working product and is verified at its user-visible boundary. | The release report names the usable outcome, exact received versions, observed journey and unresolved limits. Source edits, built, installed and physically demonstrated remain separate. Component tests, installation and documents alone do not complete an increment. |

## Design commitments and open implementation choices

Reuse the documented ESP32-S3 AMOLED board, existing audio/display/artwork/phone
setup and native Tailscale work. Reuse Unified's current supported conversation,
voice, work-discovery, question and workspace/chat mechanisms. No second chatbot,
manager runtime, task database, orchestration framework or contract enforcement
platform is part of this delivery.

One intentional request has one known destination/action; “all work” does not
mean broadcasting it. Exact media adaptation and resource-lifecycle details are
implementation choices, justified by an observed gap. A persistent native
Unified overview conversation is an implementation option, not a new runtime or
an accepted user choice. No full host-control credential is placed on the pet to
avoid an authentication gap.

Wakewords, always-on capture, barge-in, notification/quiet-hours policy and numeric
battery/latency promises are not settled here. Measure real power, memory and
latency while delivering; disclose limitations instead of inventing targets.
These open choices do not suspend independent authorized implementation.

## Verification, change and release

The [brief](../PROJECT-BRIEF.md) sequences usable increments. Saved host tasks own
status; task-owned evidence records exact versions, conditions, observations,
failures and remaining uncertainty. Existing accepted component evidence is
reused when relevant inputs are unchanged. The manager checks contributions and
the integrated journey; no mandatory new review worker or machinery is added.
People supply physical observations only where our mounted tools cannot; they
are not the routine debugging workforce.

Current user corrections supersede older draft scope, including the fixed-session
ceiling and an open transport choice. Older wire/prototype documents remain
implementation references for unchanged behavior, not authority to undo this
product scope or bypass send/approval rules. The user settles product changes;
the manager chooses reversible implementation details within the accepted goal.
Record consequential promise changes explicitly and notify affected contributors.

**No delivery is claimed by this contract.** Installation, host updates, physical
operation and paid calls remain subject to the real existing permissions and
limits; no consumed one-shot allowance is reset here.

Method: Microsoft Amplifier Converge at
[`e39fe76`](https://github.com/microsoft/amplifier-converge/tree/e39fe7676ba88737319bca21af78dd0f819eb8a3),
particularly shared direction, execution/recovery and verification/delivery.
