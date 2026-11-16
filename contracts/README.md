# Contracts

Start with the [product delivery contract](pocket-assistant-delivery.v1.md).
It records the current whole-Unified requirements, clean/recoverable experience,
Tailscale portability and new-workspace/new-chat delivery. Its status distinguishes
the user's explicit requirements from the manager's proposed checks; it does not
claim exact-artifact approval or product completion.

| Contract | Owns | Builders |
| --- | --- | --- |
| [pocket-assistant-delivery.v1.md](pocket-assistant-delivery.v1.md) | Whole-product promises P1–P9, ownership, usable delivery and evidence boundary | delivery manager, device and host contributors |
| [existing-session-voice.v1.md](existing-session-voice.v1.md) | Earlier explicit-preview prototype; DRAFT implementation/history reference, not the final product scope | current prototype maintainers |
| [pet-bridge-wire.v1.md](pet-bridge-wire.v1.md) | Earlier explicit-preview device↔host wire; DRAFT historical reference, not the active conversation-mode behavior | device app and host gateway |

The [vision](../docs/VISION.md) owns the destination; the
[implementation brief](../PROJECT-BRIEF.md) owns the current incremental plan.
The [older milestone](../docs/milestones/existing-session-voice.md) is retained
history. It cannot make selected-session-only, LAN-only or reboot-required
behavior a product acceptance condition.

## Status and changes

- Each contract states its agreement status. `(DRAFT)` means not yet accepted;
  no locked stamp exists here.
- Acceptance needs the approver's actual written wording, recorded with its date
  and source. A draft or an agent summary is not acceptance.
- Until accepted, drafts may be corrected in place. No changelog is kept until a
  real amendment of accepted text happens.
- Current user corrections supersede stale prototype scope. Real permissions,
  consumed paid/device limits and uncertain operations are preserved. This index
  neither widens authority nor adds a paperwork gate to routine work.

## What is not set up

No conformance ledger, conformance kit, pre-push guard, locked-contract scan or
Converge manager registration exists for this repository. Nothing here should be
read as a ratchet or freeze. Unrun conformance assertions report "Can't check".

## Method provenance

- Public method: [microsoft/amplifier-converge at
  `e39fe76`](https://github.com/microsoft/amplifier-converge/tree/e39fe7676ba88737319bca21af78dd0f819eb8a3)
  — one current agreement per promise, accepted wording with a named approver,
  owner boundaries, independent verification, and preserved uncertain effects.
  It leaves exact filenames, trackers and tools open; this layout is a local choice.
- Current wording follows that repository's `docs/writing-guide.md`: concise
  narrative vision, focused checkable promises, explicit ownership/agreement
  status and real user journeys. The old `DOCUMENT-SHAPES.md` path is absent in
  this revision; no missing template or separate framework was installed.
