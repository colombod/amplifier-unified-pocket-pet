---
name: pet-unified-compat
description: Check that the pet gateway still matches an Amplifier Unified release, and repair it when Unified changed. Use after a Unified update, before targeting another Unified version, when the gateway's requests are answered 403 or 404, or when the pet says a conversation is busy although it is idle.
---

# Gateway and Unified compatibility

Unified answers a paired device only for an explicit allow-list of requests, and
that list changes between releases. Older gateways misreported a denied listing
as "busy"; see the current conversation-mode behavior in `AGENTS.md`.
Unified 0.114.5 and 0.116.0 had matching inspected device-facing files and
passed the read-only check. At the pilot switch, 0.117.0's in-memory
companion policy also allowed the four request shapes below. Unified 0.119.0
and 0.119.1 pass the same in-memory check and were used in the normal-firmware
LAN calls recorded in `PROJECT-BRIEF.md`. The policy check itself does not
exercise a live credential, handler, context or voice.

## The gateway's requests

| Purpose | Request | Built in |
| --- | --- | --- |
| Attach | `POST /api/clients/attach`, `kind: native`, `protocolVersion: 1` | `bridge/pet_bridge/auth.py` |
| Session list, idle check | `GET /api/sessions?view=observation&scope=roots&activity=all&pinned=all&limit=200` | `transport.py` `route_sessions_observation`, `companion.py` `list_sessions` |
| Read replies | `GET /api/conversation/detail?sessionId=...&part=messages` | `transport.py` `route_detail` |
| Send | `POST /api/sessions/<id>/commands`, `conversation.send`, `via: chat`, `preserveDraft: true`, `attachmentIds: []` | `wire.py` |

Every listing page must name the attached host identity and instance.

## Steps

1. Find the Unified release in use and its source (the installed `amplifier_web`
   package, or that release in the Unified repository).
2. In that installation's Python, run `-I -B bridge/tools/check_unified.py`
   from the product checkout. It exercises the candidate release's actual
   `permits_companion` policy without a socket, credential or service restart.
   For a new target, read `amplifier_web/device_pairing.py`. Check each
   row of the table against it, including query and body shape.
3. Compare the device-facing files with the last release checked:
   `auth.py`, `device_pairing.py`, `live_clients.py`, `client_views.py`,
   `client_observations.py`, and the conversation-detail handler in `server.py`.
   No differences there means no gateway change is needed.
4. Read-only check against the running Unified with the gateway's own credential.
   The credential store has one owner, so with no call in progress stop the
   gateway, run
   `<gateway python> scripts/unified_probe.py --config <absolute gateway config>`
   (beside this file; it checks this repository's `bridge/`), and start the
   gateway again. It sends nothing. Expected: `session list: ok`,
   `bound conversation: status=idle idle=True reason=idle`, `messages: ok`,
   exit 0. Use the Python environment the gateway runs in.
5. If a request changed: change it in the gateway, move the offline fixtures in
   `bridge/tests` to the new document shape (translate the cases, do not delete
   them), run `python3 -m unittest discover -s bridge/tests -t bridge`, and record
   the Unified release next to this table and in `PINS.md`.

## Limits

- Steps 2 to 4 do not prove a send. An authorized request accepted by the
  intended conversation proves a send; user-level acceptance also needs the
  correlated result, intelligible playback and reuse on normal firmware.
- A conversation is listed only while its workspace folder is registered in
  Unified. "Not listed" is a setup fault, not an API change.
- The gateway follows a Unified restart by itself (`host_restart_follow`).

Done when: the release is named, each row is confirmed against that release's
`permits_companion`, and the read-only check's results are recorded.
