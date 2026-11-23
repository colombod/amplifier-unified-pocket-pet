# Handoff: turn the working voice pet into a Unified assistant

This is a historical record of the standalone app. For the supported firmware,
use the [Pocket Pet setup guide](team-prototype.md#start-here).

## User direction

The user has a working standalone voice pet and wants a useful portable interface
to Amplifier Unified, with durable conversation and access to their real work.
They explicitly paused integration to check post-restart crackling and review
PR #6, then requested a handoff after satisfactory health/review and merge.

**Earlier direction: use the shared local network first.** The user stated device
and server are on the same network; Tailscale was the later reachability step.
The latest question asks whether to test Tailscale now; see the
[current next-spike scope](#latest-next-spike-scope--2026-11-10), not a settled
replacement architecture. Do not enroll a tailnet client,
change a router, enable public exposure or replace the working voice baseline
merely to start integration.

## Baseline and repository

Repository: https://github.com/colombod/amplifier-unified-pocket-pet

- `milestone-1-voice` / fdd783e preserves the first user-confirmed spoken baseline,
  firmware 0.3.5. See [Architecture](ARCHITECTURE.md) for the current design.
- PR #6 contains the device implementation plus subsequent health/review fixes.
  Consult its actual merge state and the current main commit before continuing.
- The accepted health baseline is **0.3.8-amoled-v2-health**: the user confirmed
  two clean starts/stops with clean speech. `milestone-1-reviewed` preserves the
  reviewed baseline. Latest physical verdict,
  artifact hash, installation status and limits are in [device-evidence.json](device-evidence.json).
  Do not infer acceptance from the version number alone.
- [Bring-up history](device-bringup.md) and [PR review](pr-6-review.md) retain
  observed failures and the distinction between build, flash, logs and audibility.

Hardware is the actual **Waveshare ESP32-S3-Touch-AMOLED-1.8 V2**, 368×448
CO5300/CST820, ES8311, 16 MiB flash and 8 MiB octal PSRAM. The original LCD-1.83
link is wrong for this connected unit. Use the pinned official AMOLED BSP 2.0.3;
never reuse the LCD/ST7789 mapping. SDK/compiler/dependencies run in Docker.

## What exists

Animated original Tachikoma pixels are flash-mapped. Phone QR/AP setup saves
2.4 GHz Wi-Fi and a required initial OpenAI key independently. Blank replacement
key retains the existing key. Current edits survive old saved-state polls and
new edits invalidate prior pending save confirmations. Saved networks retry
with bounded backoff without interrupting recent provisioning activity.

Settings holds Wi-Fi/key, persistent volume/local tests and four-point measured
touch alignment. Home and Settings distinguish Wi-Fi/RSSI from voice status.
Talk explicitly starts capture/API; Stop ends capture and closes the session.
The voice model is **gpt-live-1**, Marin, PCM16 mono 24 kHz. The current isolated
backend is managed **gpt-6-luna Responses**. There is no Unified connector and no
persistent memory across fresh voice sessions. Its spoken answers do not run
work in Unified.

`main/assistant_config.h` is the voice/backend configuration seam. `live_voice.c`
handles verified TLS, startup/close, bounded streams and separate capture/playback.
It distinguishes digital silence from audible output. Speaker playback gates
input until AEC is qualified; barge-in/wakeword and long-duration robustness are
not certified. Boot and cleanup explicitly disable the amplifier. The health
update adds a 320 ms playback prebuffer and underrun diagnostics, and disables
Wi-Fi modem sleep only during voice, restoring the previous setting afterward.

## Supported Unified integration facts

Public source documentation was read at commit
`2788dda66f748b90fcc9968ab80467df3aded8ba` on 10 November 2026. This is a source
reference, **not** proof of the installed service version or capabilities:

- [Live sessions v1](https://github.com/microsoft/amplifier-unified/blob/2788dda66f748b90fcc9968ab80467df3aded8ba/docs/clients/live-sessions.md)
  documents authenticated client attachment, session discovery/snapshots, commands
  with stable IDs, SSE and action-schema discovery. Attachment does not transfer
  Foundation execution ownership.
- [Native/TUI handoff](https://github.com/microsoft/amplifier-unified/blob/2788dda66f748b90fcc9968ab80467df3aded8ba/docs/clients/tui-handoff.md)
  describes the existing control bearer credential as broad service control, not
  restricted per-device access. Keep that credential server-side; do not put it
  on the ESP32 or expose it through the phone page.
- [Observations v1](https://github.com/microsoft/amplifier-unified/blob/2788dda66f748b90fcc9968ab80467df3aded8ba/docs/clients/observations.md)
  is optional capability-discovered functionality. Do not assume support from the
  product name or replace the shipped HTTP/SSE interface with draft AHP/ACP.

The user supplied their existing HTTPS/Tailscale service address in the device
chat. A non-authenticated Mac probe reached it with valid TLS: root returned 307
and `/api/actions` returned **401**. No credential was obtained, no authenticated
attachment or workspace/session operation was performed, and no server was changed.
The actual LAN endpoint, installed capabilities and owner-authorized credentials
still need qualification; a Mac tailnet connection does not prove ESP32 reachability.
The private endpoint is deliberately not copied into this committed handoff.

## Intended first useful proof

Use a supported authenticated connection to the running host, discover its actual
action schemas and bind voice to an explicitly chosen existing persistent Unified
conversation. A spoken input and its returned answer must belong to that same
host-owned conversation, with prior context retained across a device voice-session
restart. Retain stable command IDs/receipts; uncertainty must not automatically
resubmit an input. Reconnection or session selection does not start work, acquire
ownership, warm a runtime or forge human provenance.

Keep both explicit requirements from the [2026-11-10 clarification](intent.md):
a place to configure the pet connection and see the session list. A temporary
bounded single-session proof must not drop either. Session labels and stable IDs
must come from the real host, not stale or guessed entries. With 16 MiB flash and
8 MiB PSRAM, keep a small projection, not replicated conversation history; preserve
the working voice baseline. The latest [source clarification](intent.md#existing-phone-setup-and-tailscale-sequencing-question--2026-11-10)
requires Unified address/port on the pet's **existing phone-served setup page**
with the other configuration, prefilled from saved nonsecret values when opened,
not a separate settings app. This is next-spike scope, not already-implemented
Unified fields. Never expose tokens/passwords. Preserving pending edits during
asynchronous prefill is an assistant engineering proposal, not an explicit user
decision. A paginated pet picker and its placement remain proposals; the
session-list/selection requirement is unchanged.

The broader user goal includes managing workspaces, creating new work, continuing
existing sessions and answering genuine waiting questions. The global-manager /
direct-focus interaction in VISION.md is still a proposal, not a newly ratified
architecture. Do not quietly substitute a new isolated chatbot for existing-session
continuity. Waiting-question answers require exact identity, revision and actual
human input; a general spoken instruction is not an invented approval receipt.

The old Raspberry Pi/OpenClaw/Slack/Vercel proposal is historical. No such service,
cloud queue or distributed orchestration is required or implemented here.

## Reproduction and recovery

The standalone build commands live in Git history. The full factory backup for
the original board was digest-verified and restored successfully at the time;
that recovery was not a firmware update. Keep backups private: they can contain
credentials. For current installation guidance, see the [setup guide](team-prototype.md#start-here).

Opening USB serial can reset this board even with nominal no-reset flags. Start
one bounded observer before a physical test and keep it open. Do not reopen it
mid-conversation or pairing. Logs retain aggregate state/levels only; no speech
content or key should be logged or committed.

## Remaining evidence limits

The reported startup/speaking crackle regression passed the user’s final two-start
check on 0.3.8. Preserve that accepted baseline when extending the system.
Router outage/return, long-running voice stability, measured latency/CPU envelope,
physical touch accuracy after calibration and AEC/barge-in are not automatically
passed by unit tests or milestone acceptance. Existing spikes #1–#4 remain open;
this handoff does not claim their integrated outcomes. Preserve factory recovery,
source evidence and these limits when extending the system.

## First connection qualification — 2026-11-10

This is a server-side observation, not a device connection or authenticated
session proof. No firmware, microphone, provider, service configuration, restart,
pairing request or authenticated operation was performed.

**Origin and local service evidence.** Read-only `amplifier-unified config get`
for only `public_origins`, `bind` and `port` identified the actual configured HTTPS
origin. `systemctl --user show amplifier-unified.service` reported
`ActiveState=active`, `SubState=running`; PID-filtered `ss -lntp` matched that
service's main process to the configured loopback and tailnet listeners on port
8941. `tailscale serve status --json` contained no proxy mapping to that listener;
its other advertised ports must not be mistaken for Unified. The private origin,
addresses, certificate identity and raw metadata are deliberately omitted.

**TLS evidence at 19:42:47 UTC.** `amplifier-unified setup-tls status` reported
`Local CA: configured`, `TLS: ca`, `TLS listener: ready`. A direct OpenSSL handshake
with hostname verification and `-verify_return_error`, using the host's existing
trust configuration without changes, returned exit 0, `Protocol version: TLSv1.3`
and `Verification: OK`; its verified peername matched the configured origin.
This qualifies TLS from this Linux host, not trust or reachability from the ESP32
or an unqualified phone/browser.

**HTTP evidence gap.** The `web_fetch` tool failed for the configured HTTPS
origin's `/`, `/api/connections/v1`, `/api/companion` and `/api/activity` before
returning any HTTP status or body: `Cannot connect to host ... ssl:default [None]`
(private hostname elided). A loopback HTTP root probe was rejected by the tool
as `Invalid or blocked URL`. These are tool-path failures, not observed server
HTTP denials or proof that the host is absent. The tool did not expose the
underlying HTTPS failure reason; the successful OpenSSL check does not establish
which trust/network difference caused it. No certificate verification was disabled.

**Source versus runtime.** The calling app had already reported version **0.73.2**,
revision `6ab4fce30fce74c996aed0d1e69474f2ea051d4c`; these HTTP attempts did not
independently identify the serving revision. The matching
[native approval contract](https://github.com/microsoft/amplifier-unified/blob/6ab4fce30fce74c996aed0d1e69474f2ea051d4c/docs/clients/native-pairing.md)
documents proof-key pairing, genuine signed-in owner approval and revocable
`companion-v1` credentials. This is host-wide chat authority, not a
per-conversation ACL or arbitrary action/approval authority. Source qualification
also identifies public `/api/connections/v1` as distinct `device-v1` discovery;
a successful response there alone would not prove the companion chat catalog.
Actual public response fields, unauthenticated companion/activity denials,
authenticated catalog/pagination, exact-session continuity and device reachability
remain **unchecked**, not passed.

**Earlier proposed next action, superseded by the LAN-first diagnosis below:**
resolve the verified-HTTPS tool-path gap, then retain public response/status and
auth-denial evidence. That failure remains historical evidence, not the reason
the numeric LAN endpoint currently refuses connections.

The subsequent authorization boundary is unchanged: only after actual verified
reachability initiate one explicit proof-key pairing request, present its real
comparison code and exact-origin approval link, and wait for the signed-in user
to approve it in the host's browser UI. No owner cookie or full-control credential
is a substitute. Redeem only the approved request, attach a native protocol-v1
client, and bind subsequent allowed reads to its returned client ID before showing
a bounded real-host session list. The chosen session and picker location remain
unselected; connection fields belong on the existing phone setup page as clarified
above. Pairing must not start voice, send a message, or warm a runtime.

## LAN-first diagnosis and supported repair — 2026-11-10

The exact user correction is retained separately from interpretation in
[intent.md](intent.md#lan-first-clarification--2026-11-10). This bounded diagnostic
performed one TCP connect to the actual server LAN address on port 8941:
`ConnectionRefusedError: [Errno 111] Connection refused`. The simultaneous exact-port
socket recheck still found only the configured loopback and tailnet listeners,
owned by the same service main process; no LAN listener exists. This establishes
a local listener/configuration gap, not a successful device route or a reason
to change router/firewall settings.

**Certificate identity is a second independent gap.** The publicly served
certificate has a single DNS SAN for the existing origin and no IP SAN.
OpenSSL connected to the already-known TLS listener with the LAN address as the
verification identity (`-verify_ip`, with `-verify_return_error`) failed with
`verify error:num=64:IP address mismatch`. This was not a LAN transport test.
The served issuer is Let's Encrypt and the leaf expires on 2026-11-13.
Selected `config get tls.cert` identifies a supplied certificate, not the fixed
app-owned leaf. Although `setup-tls status` says a local CA is configured, public
`setup-tls export` metadata identifies **ISRG Root X1**, not an app-owned issuing
CA. Its presence is not proof that this host owns a usable CA signing pair.
Do not blindly run `setup-tls force`: it can switch the service to an app-owned
leaf/CA and change trust for existing browser clients. No private key was read.

**Supported configuration shape, not executed.** Installed CLI help and exact
revision `6ab4fce30fce74c996aed0d1e69474f2ea051d4c`
[deployment guidance](https://github.com/microsoft/amplifier-unified/blob/6ab4fce30fce74c996aed0d1e69474f2ea051d4c/docs/DEPLOYMENT.md)
agree on list-valued `bind`, exact list-valued HTTPS `public_origins`, the atomic
`tls` setting and the lifecycle commands below. Preserve the current entries;
add only the actual LAN address, not a wildcard bind. Placeholders intentionally
keep private addresses and paths out of this record:

```sh
amplifier-unified config set public_origins '["https://<existing-tailnet-host>:8941", "https://<server-lan-ip>:8941"]'
amplifier-unified config set tls '{method: ca, cert: /absolute/path/reviewed-chain.crt, key: /absolute/path/reviewed.key}'
amplifier-unified config set bind '["<existing-tailnet-ip>", "127.0.0.1", "<server-lan-ip>"]'
amplifier-unified doctor
# Only at a confirmed safe host-wide maintenance boundary:
amplifier-unified service restart
amplifier-unified service status --wait 60
```

The missing TLS artifact is a reviewed matching certificate/chain and private key
covering the LAN **IP SAN** and retained client identities, plus an explicit trust
distribution plan if migrating from the supplied public certificate. The
documented local-CA workflow is supported, but the observed exported public root
does not establish that the existing CA state is suitable for it. Do not change
trust, generate certificates or replace that state as part of this diagnostic.
Exact origins retain Host/Origin protections; no wildcard-host bypass is needed.

**Live restart constraint.** The matching `cli.py` constructs TLS and listening
sockets at startup. `deployment_service.py` sends an immediate systemd restart;
the CLI restart is not an idle-wait scheduler. Current systemd metadata reports
`KillMode=mixed`, SIGTERM, and a 90-second stop timeout. This permits graceful
host-first cleanup but does not guarantee that active turns/calls survive.
Root must retain recovery settings privately, settle affected host work and
perform the supported restart outside active execution; no restart was requested.
Changing files alone would not prove adoption by the running process.

**Next technical owner:** original root session (identifier redacted).
Resolve the certificate/trust choice and safe restart boundary, then qualify the
new LAN socket, served IP identity, supported HTTP response and actual device
reachability before owner-approved pairing. Earlier tool-path failures remain
unchecked HTTP history, not a request for another user-supplied URL. No server
mutation, certificate generation, firewall change, restart, pairing, chat send,
voice/provider start, USB operation or background resource occurred here.

## Latest next-spike scope — 2026-11-10

The latest [exact user words and labelled interpretation](intent.md#existing-phone-setup-and-tailscale-sequencing-question--2026-11-10)
ask to consider Tailscale now. The earlier LAN-first decision, diagnosis and
proposed repair above are retained as history, not a mandatory next mutation.
**Assistant recommendation, pending implementation choice:** probe Tailscale
before LAN reconfiguration to de-risk portability; retain the existing phone-page
extension as next-spike scope. No LAN server mutation has started.

The LAN listener, certificate SAN and November 13 expiry findings remain in the
diagnosis above; the host TLS pass is not a physical ESP32 pass. For a native
Tailscale probe, community MicroLink is a candidate only: historical compilation
is neither runtime proof nor qualification for the corrected AMOLED board.
No native embedded architecture, gateway choice, firmware flash or new fixed
day/attempt limit is approved by this question. The next technical owner remains
the root session named above; no implementation or runtime probe is claimed here.

## Latest key-setup decision — 2026-11-10

The later [exact user correction and accepted interpretation](intent.md#reuse-existing-key-setup-approach--2026-11-10)
choose the existing OpenAI API-key phone-entry/NVS-storage approach for a Tailscale
auth key, saved before a future connectivity test reads it. This is the chosen
reuse pattern for this increment, not a new security migration, deployment or
enrollment approval, nor a waiver of every security check. Ordinary NVS remains
unencrypted; recording that limit does not request a storage migration.

**Assistant working decision:** saving alone does not enroll or connect Tailscale.
Unified address/port, saved nonsecret-value prefill and session-list/selection
remain required; this key-only increment does not cancel them or ratify the vision.

**Root-reported implementation status:** key-storage source is implemented;
independent review is pending and hardware is unchecked. This is not a PASS or
evidence of device Tailscale connectivity. The earlier LAN diagnosis and
certificate warnings above remain unchanged.
