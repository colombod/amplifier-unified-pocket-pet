# Pet–Bridge Wire Contract — v1 (DRAFT)

**Historical one-turn design.** The running conversation on build 11 uses
`pet.converse.*`, without the preview and Send confirmation described below.
This document is retained for the earlier one-turn path; it is not the current
device interaction or authority to change the user's chosen send behavior.
Current behavior is implemented and tested in `bridge/pet_bridge/`,
`bridge/tests/`, and `apps/unified-pet/`.

**Who builds against this:** the device app owner (`apps/unified-pet/`) and the
host gateway owner (`bridge/`), whose code meets at this seam; reviewers of either
side of the earlier prototype. This draft is an unratified historical migration
of its promises, not a current conversation-mode specification.

## What it looks like

One WebSocket carries exactly one turn:

```
pet → GET /v1/pet/live  (TLS, Authorization: Bearer <43-char pet token>)
host → {"type":"session.started"}
pet → {"type":"pet.turn.begin"}
host → {"type":"pet.turn.began","turn_id":"<uuid4>","safe_session_label":"…","session_id_short":"0f3c9a21"}
pet → {"type":"pet.turn.capture_stop","turn_id":"<uuid4>"}
host → {"type":"pet.turn.preview","turn_id":"<uuid4>","preview_revision":1,"text":"…","text_sha256":"<64 hex>"}
pet → {"type":"pet.turn.send_confirm","turn_id":"<uuid4>","text_sha256":"<64 hex>","preview_revision":1}
host → pet.turn.send_result{accepted} → pet.reply.started → session.output_audio.delta… → pet.reply.done{completed} → session.closed
```

## Purpose

Two independently built programs must agree exactly, or a confirmed sentence can
be sent twice, sent unseen, or answered with audio from somewhere else. These
rules make every frame checkable on both sides of the seam.

## Core (the teeth)

1. **Transport is TLS WebSocket on exactly `/v1/pet/live`.** The device dials a
   configured RFC 1918 IPv4 literal and verifies the certificate (SNI and name
   check) against a separately configured FQDN; no verification or clock bypass.
   The gateway accepts the configured literal Host exactly, rejects other hosts
   and paths directly, and never answers with a redirect. Any 3xx fails closed on
   the device; capture and send stay disabled until an actual HTTP 101.
2. **The pet authenticates with `Authorization: Bearer` and a 43-character
   base64url, 256-bit per-pet token**, checked before upgrade and before any
   provider connection. No Unified or provider key is ever in a device header.
3. **One turn per WebSocket, one identity per turn.** `turn_id` is a canonical
   lowercase UUID4 (36 characters), identical in every frame of the turn. After
   reply done, rejected, uncertain or cancel the connection closes, never resumes.
4. **Frame names are exact.** Client: `pet.turn.begin`, `pet.turn.capture_stop`,
   `pet.turn.send_confirm`, `pet.stop`, `session.close`. Server: `pet.turn.began`,
   `pet.turn.preview`, `pet.turn.preview_invalidated`, `pet.turn.send_result`
   (`accepted|rejected|uncertain`), `pet.reply.started`, `pet.reply.done`
   (`completed|unavailable|stopped`), `pet.error` with fixed codes `busy`,
   `unknown_command`, `model_mismatch`, `no_speech`, `unstable_transcript`,
   `expired`, `no_delegation`, `reply_unavailable`, `deadline`, `overflow`,
   `close_unconfirmed`, `invalid_preview`.
5. **Exactly one nonempty sealed preview per turn.** `preview_revision` is an
   integer 1..2147483647 (booleans refused): a local compare value, distinct from
   Unified's global revision, which the device never chooses. `text` is at most
   4000 code points and 16000 UTF-8 bytes, refused rather than truncated.
   `text_sha256` is lowercase hex SHA-256 of the exact UTF-8 bytes, unnormalized.
   `send_confirm` must match turn, hash and revision, checked once.
6. **A late transcript invalidates; nothing reseals.** After a preview, late
   speech yields `pet.turn.preview_invalidated` with reason `late_transcript`,
   then `pet.error` `invalid_preview`, then close: no resume, replacement or new
   turn. An empty settled transcript yields `pet.error` `no_speech`, no preview,
   close. A reason is 1–64 printable ASCII characters.
7. **Labels are safe and suffixes are real.** `safe_session_label` is nonempty,
   at most 40 code points, control-free, with a fixed safe fallback.
   `session_id_short` is the last 8 characters of the selected full session ID,
   validated as-is as lowercase hex or the selection is refused.
8. **Legacy frames are minimal.** Only `{type:session.started}`,
   `{type:session.closed}` and `{type:session.output_audio.delta, delta}` (base64
   PCM16 mono 24 kHz); no provider fields pass through. Audio may omit `turn_id`;
   the device gates by its current turn and reply epoch.
9. **Reply framing brackets the audio.** `pet.reply.started` (rendering
   `spoken_rendering_not_verbatim`) precedes reply audio; `pet.reply.done` follows
   the final delta. No delegation event ever enables Send.
10. **Stop and close are idempotent with one provider close.** Normal
    `session.closed` is sent only after a confirmed provider close; otherwise
    `pet.error` `close_unconfirmed`, then the socket closes. Neither cancels Unified.
11. **The device never shows `unavailable` or `stopped` as Done.**

## What v1 deliberately does NOT freeze

- Port, deadlines (start 25 s, cap 60 s, close 15 s) and size caps — kept in the
  briefs; promoted when M3 repeatability measures whether a turn fits.
- Version negotiation — promoted when a second protocol version is proposed.
- Multi-session or session-selection frames — promoted with a later milestone.

## Conformance kit asserts

No kit exists; each line reports **Can't check** until a recorded run passes.
Owner-run suites in `bridge/tests/` and the app checkout are not accepted runs.

- Both sides parse and emit each frame in clause 4 byte-for-byte, refusing
  bad UUIDs, booleans, oversize text and mismatched hashes (3–5, 7).
- Late transcript and empty transcript produce exactly the clause 6 sequences.
- Any 3xx, wrong Host or wrong path never yields a connected, sending device (1).
- Audio outside the current reply epoch is dropped (8, 9).
- Unconfirmed provider close never yields `session.closed` (10).

## Reserved / open questions (NOT frozen)

- Exact Host/SNI behavior of the pinned ESP-IDF 5.5.4 client is not proven; only
  a 5.5.6 source reading exists.
- Whether `session.commentary.appended` `client_event_id` matching is
  authoritative provider evidence for opening the reply audio gate.
- Whether a supported public proof of WebSocket task final access exists for the
  pinned client 1.6.1; until then the device quarantines and requires restart.
