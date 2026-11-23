# First Unified milestone: existing-session voice round trip (DRAFT)

**Retained historical milestone.** Current direction and sequencing are in
[PROJECT-BRIEF](../../PROJECT-BRIEF.md), with promises in the
[product delivery contract](../../contracts/pocket-assistant-delivery.v1.md).
The snapshot below is not current status or permission. Its separate prototype
scope and old review prerequisites must not displace the current incremental
product goal or cause repeated component qualification.

**Release agreement draft, pending the owner's written approval.** Nothing here is
shipped, accepted or qualified. This is not [Milestone 1](../milestone-1.md), the
already-demonstrated standalone voice chatbot; that record stays unchanged.

## Goal, in the owner's words

> "i want to reach the stage where we can use the pet to talk to a selected
> session in amplifier unified"

On topology: "we already agreed. on the bridge for the moment". Both quotations
were retained in a private, workspace-local manager ledger that is not part of
this repository; no topology re-approval is needed.

Interpretation: speech from the physical pet reaches one human-selected
**existing** Unified session, with that session's context, through the host
bridge; the session's reply is spoken back. The promises are in
[existing-session-voice.v1](../../contracts/existing-session-voice.v1.md) (ESV) and
[pet-bridge-wire.v1](../../contracts/pet-bridge-wire.v1.md) (PBW), both DRAFT.

## Scope

**In:** one selected existing session; one human-confirmed transcript per turn,
sent at most once; correlated reply spoken back and labelled not verbatim; honest
busy/rejected/uncertain/unavailable/stopped outcomes; Stop silencing the pet only.

**Not in this release:** workspace manager overview, new workspaces or new work,
attention questions, switching sessions, autonomous or always-on use, away-from-home
use, other hardware or firmware lines. The broader [vision](../VISION.md) is kept.

**Prerequisites, not completion:** M2 measured microphone with safe Stop, and M3
repeatability. Passing them does not deliver this milestone.

## Deliverables and owners

Roles, not session IDs; the host's saved tasks and receipts own current status.

| ID | Deliverable | Owner role |
| --- | --- | --- |
| D1 | Device firmware for the exact AMOLED board: bridge config, Talk/Done talking/preview/Send/Stop controls, honest outcome display | device app owner (`apps/unified-pet/`, managed checkout) |
| D2 | Host bridge: core, auth transport, voice gateway, explicit selected-session binding | host bridge owner (`bridge/`) |
| D3 | Safe user controls and recovery: Stop scope, uncertain handling, no auto-replay | D1 and D2 owners, per contract |
| D4 | Installable release: artifact hash, pinned toolchain, rollback to a retained recovery image, current Wi-Fi/NVS preserved (never old NVS restored), evidence handoff | integration manager (root coordinator) |
| — | Microphone measurement collector (M2 tooling) | collector owner; completed offline |

Authority: drafting these files authorizes nothing physical. Pairing, updating or
sending to the selected session, USB, flashing, provider calls and voice attempts
each need their own bounded human permission. Physical voice is **paused**.

## Acceptance worksheet

"Existing" means a test suite exists in this repository today (`bridge/tests`,
host core and auth only); it does not mean the row's case is covered or passed.
"Owner-side" means tests reported by an owner and not read for this draft.
Offline fakes cannot prove the provider protocol, reply correlation or physical
audio. Every row's status is **not verified**.

| ID | Clause | Success | Failure that must be caught | Check |
| --- | --- | --- | --- | --- |
| A1 | ESV-1, ESV-2, ESV-7 | Real round trip: pet speech becomes one input in the selected session; prior context is preserved; the reply is taken from the exact completed generation of the confirmed turn and plays | reply from another session, new session, latest-message fallback, lost context | proposed: human-permitted physical run |
| A2 | ESV-3, ESV-4, PBW-5 | One Send → one Unified input | duplicate, stale revision or delegation-triggered send | existing: `bridge/tests` core; proposed: gateway tests, physical |
| A3 | ESV-5 | Forced unknown send shows "unknown", durable host record, no resend | shown as sent/failed; replay after reconnect | existing: `bridge/tests` core; proposed: device display test |
| A4 | ESV-6, PBW-11 | Unavailable/stopped never render Done | Done shown for unavailable/stopped (open review finding) | proposed: app view test after repair |
| A5 | ESV-8, PBW-10 | Stop silences pet; pending Unified send continues. Stop then Close is idempotent: one provider close, no false close ACK, no Unified cancel | claims Unified cancelled; false `session.closed`; second provider close or cancel on repeat | proposed: gateway tests (gateway unbuilt), physical |
| A6 | PBW-1, PBW-2 | 101 required; bad token, Host, path or any 3xx never connect. Untrusted CA, hostname mismatch or clock failure fails closed. Verify FQDN is separate from dial IP. Auth, path, Host, 3xx and 101 are checked before capture | redirect followed; credential reflected; capture before auth; dial IP used as verify name | owner-side app transport tests; pinned 5.5.4 proof: gap; proposed: gateway tests |
| A7 | PBW-3..9 | Exact frames, late/empty transcript sequences, audio epoch gating | truncation, reseal, provider passthrough | owner-side app protocol tests; proposed: gateway tests |
| A8 | ESV-10 | Canaries, transcripts, audio, labels and tokens absent from logs and status. Host holds Unified and provider credentials; device holds only the pet token | token, transcript, audio or label leaked; provider or Unified credential on device | existing: `bridge/tests`; proposed: gateway and device |
| A9 | D4 | Install, verify hash, roll back, Wi-Fi intact | unrecoverable board; old NVS restored | proposed: bounded physical install |
| A10 | ESV-9 | Human starts connection and capture; one-turn close; no automatic reconnect or resume | capture or send without a human action; automatic reconnect, resume or resend | proposed: gateway tests, device display test, physical |

**Combined acceptance evidence (any release claim).** A release record must state the exact device firmware binary and host bridge artifact with source and binary hashes; the toolchain; and the pinned configuration, without secrets. The selected session's identity is retained privately and not written here. It must also record the earlier context, the confirmed input, the accepted generation, the canonical reply and the physical speech observations, with their conditions and date; an independent checker; and failures, limits and the recovery receipt. Component PASS rows are not a combined release PASS. Live permissions remain separate.

## Evidence snapshot (2026-11-11) and limits

- Collector (M2 tooling): root-run offline suite accepted. Its final handoff
  remains private and workspace-local, not in this repository. Offline only;
  not physical readiness.
- Earlier bounded native tailnet TLS HTTP 200 on the device: historical, tunnel
  stopped; not a bridge or voice pass.
- Host bridge: an earlier root-run failure is retained as history.
  Owner-run reports, not independently verified here: 551 tests OK; spec-quality
  and security PASS in independent reviews. Coverage is NOT READY: a `worker.run`
  failed POST connector/request single-call regression, though the production path
  is already correct. A planned test-only round 2 did not execute: a saved helper
  resume was refused before execution ("Saved child image provider binding
  has changed; review the image configuration before resuming."). Coverage pending or
  passing and auth acceptance are not claimed. The voice gateway is still unbuilt.
  Existing auth repair records remain private, outside this repository.
- Device app: component review needs changes (e.g. Done shown when reply is
  unavailable or stopped), and configuration findings remain open. Its admission
  task was refused before execution (`work_precondition_unavailable`);
  no sdkconfig restore, migration or runner edit was made. That agent dependency
  block is not a user Stop. Generated app output was deleted before an approved
  preservation; logs remain but no original byte-hash inventory exists. That gap is
  not waived, original bytes are unrecoverable, and new runs are not restoration.
  No claim is made that all source writers are blocked globally.
- Selected session: human choice only; readiness unknown; no pairing, update or
  send authorized. The client-event correlation field is commissioned and unproven
  (research, not root-verified).
- Physical voice paused: one provider attempt used, two remaining.

Technical briefs and detailed evidence remain in private workspace storage and
are not included in this repository.

## Pending written approval

- Accept or edit: milestone name, scope lists, deliverables D1–D4, worksheet A1–A10.
  The initial scope goal is already human-agreed and is not asked again; the new
  draft wording (A10, combined evidence paragraph, row changes) requires agreement
  and is not ratified by this edit.
- Accept or edit: both contract drafts.
- Approver and date: _not yet given_.
