# Run the pocket gateway as a user service

This is a template, not an installer. Nothing in this directory enables a service.
Use a Linux host with systemd user services, the bridge's existing Python/aiohttp
environment, a working private gateway JSON config, TLS files, a paired restricted
credential and an already-selected Unified conversation. Pairing and phone/device
setup are separate; this unit does not perform them.

## Tested host runtime

The service and bridge tests were run with CPython 3.13.14 and aiohttp 3.14.5.
One isolated environment used `aiohappyeyeballs==2.7.1`, `aiosignal==1.4.0`,
`attrs==26.1.0`, `frozenlist==1.8.0`, `multidict==7.2.1`, `propcache==0.5.4`,
`yarl==1.25.1` and `idna==3.20`. This is one tested combination, not a supported
version matrix or a guarantee that wheels are available on every platform.
For example, with an installed `uv` and that Python interpreter:

```sh
uv venv --python /absolute/path/to/python3.13 /absolute/private/path/venv
uv pip install --python /absolute/private/path/venv/bin/python \
  'aiohttp==3.14.5' 'aiohappyeyeballs==2.7.1' 'aiosignal==1.4.0' \
  'attrs==26.1.0' 'frozenlist==1.8.0' 'multidict==7.2.1' \
  'propcache==0.5.4' 'yarl==1.25.1' 'idna==3.20'
```

The environment still depends on its base Python installation. Use a stable
interpreter path, not a temporary application update directory; test the
environment again after changes. Installing dependencies may access a package
registry. No package hashes or fresh-machine install qualification are claimed.

## Install and start

1. Create an owner-only log directory, for example:
   `install -d -m 700 "$HOME/.local/state/pocket-gateway"`.
   Keep the config, credential and key files private. Set `provider_key_file` in
   the config to the absolute path of the mode-0600 provider key file; do not put
   a key or token in this unit or use a shell environment variable for it.
2. Copy `pocket-gateway.service` to
   `~/.config/systemd/user/pocket-gateway.service` (create that directory if needed).
   Replace **every** placeholder in the copy:
   - `@PYTHON@`: absolute Python interpreter with the bridge's dependencies.
   - `@BRIDGE_DIR@`: absolute repository `bridge` directory, where `pet_bridge` lives.
   - `@CONFIG@`: absolute private gateway JSON path.
   - `@EVENT_LOG@`: absolute JSONL path in the owner-only directory from step 1.
   These are systemd paths, not shell commands; no `~` or `$HOME` expansion occurs
   inside the unit. Quote the interpreter if its path contains spaces. Escape `%`
   as `%%` in literal paths for systemd.
3. Stop any manually launched gateway yourself first: only one may own the journal
   and listen address. Then run:

   ```sh
   systemctl --user daemon-reload
   systemctl --user enable --now pocket-gateway.service
   systemctl --user status pocket-gateway.service
   ```

`enable` starts it on future user-manager starts. To keep this service running
after logout or start it before login, an administrator may need to enable
lingering for your user (`loginctl enable-linger USERNAME`). Closing the Unified
app does not stop the gateway, but Unified must be running/reachable to do work.
User `network-online.target` is only ordering, not a guarantee that Wi-Fi,
Tailscale or Unified is ready.

## Optional LAN and Tailnet listeners

The default config still serves only its existing `bind_host`/`host_header`.
For a matched gateway and device package that supports native Tailnet voice, one
gateway can also bind a second explicitly chosen local IPv4 address. Add these
two optional fields to the **same** private JSON document, without changing the LAN
settings:

```text
"tailnet_bind_host": "100.64.12.34",
"tailnet_fqdn": "gateway.example.test"
```

Replace the example with this host's actual Tailscale IPv4, in `100.64.0.0/10`;
the two range endpoints, malformed and duplicate addresses are rejected. This is
an exact local bind on the existing `port`, not a wildcard or a second process.
Replace the example FQDN with the same canonical lowercase dotted certificate
FQDN configured on the device (at most 63 characters). Both fields must be
present together. The additional listener accepts exactly
`Host: <tailnet_fqdn>:<port>`;
the LAN listener still accepts exactly the pre-existing `host_header`. The
native Tailnet transport uses the FQDN and port in Host while dialing the chosen
CGNAT IP; the LAN transport uses its configured legacy Host. Each request must
also pass the existing bearer and TLS checks, and both routes share one rate
registry, journal and single active call. The device still verifies the server
certificate against its separately configured FQDN; use a matching trusted
certificate on both paths. Do not disable TLS verification.

CGNAT range validation does **not** prove the address is a Tailscale interface
or restrict who can reach it. Choose the intended local Tailscale address
yourself and configure appropriate host firewall and Tailnet ACLs. If the
optional address is unavailable at bind time (`EADDRNOTAVAIL`), the gateway
serves the existing LAN listener and retries that exact Tailnet address every
10 seconds. Startup reports `Tailnet address unavailable; retrying` and reports
`Tailnet listener ready` once the bind succeeds. This is listener readiness, not
proof that the Tailnet or device can reach it. Other bind/start failures, including
address in use or permission errors, fail closed with `listen_failed`. LAN bind
failure also fails the whole startup. Network ordering does not guarantee
Tailscale readiness. The gateway does not detect interface loss after a successful
bind or automatically select a changed address; recovery after an interface goes
away and returns is unverified. Failure to bind can occur after the existing same-host
attachment refresh, so do not assume no prior startup state change. Cleanup
failure or forced process interruption is not proof all sockets were released;
verify the old process has exited before starting another owner. A stalled
listener startup or cleanup can delay cancellation while ownership is retained.
Listener cleanup does not prove every accepted transport has closed: an
incomplete TLS handshake can remain outside aiohttp's tracked connections.
The production gateway's release path is the sole owner of listener cleanup;
it does not call the returned closer concurrently. To restore LAN-only operation
after a deliberate stop, remove both optional fields and start
the same service with its original configuration. The feature is offline-tested;
it does not establish deployed, off-home or device voice success.

The equivalent foreground CLI, **shown for reference, not a second process**:

```sh
cd "/ABSOLUTE/REPOSITORY/bridge"
"/ABSOLUTE/PYTHON" -B -m pet_bridge serve --config "/ABSOLUTE/private/gateway.json" --event-log "/ABSOLUTE/private/events.jsonl"
```

Omit `--event-log` for the existing no-file-log behaviour. A supplied path must be
absolute; its parent must already exist, be owned by you and have no group/other
permissions. A new file is 0600; an existing file must be an owner-owned regular
0600 file (no symlinks or hardlinks). Invalid paths fail before listening with
`event_log_invalid` (a relative CLI path is a usage error).

## Operate and inspect

```sh
systemctl --user stop pocket-gateway.service
systemctl --user start pocket-gateway.service
systemctl --user restart pocket-gateway.service
systemctl --user status pocket-gateway.service
journalctl --user -u pocket-gateway.service -n 50 --no-pager
tail -n 50 /ABSOLUTE/private/events.jsonl
```

The JSONL file appends UTC-timestamped gateway start/stop, connection decisions,
provider dial attempt/open/failure, session outcome metadata and changed host
instance prefixes (8 characters). It contains no transcript, reply, audio, keys,
full session IDs or stable host identities. Byte counts are metadata, not audio.
It has no automatic rotation or size cap. Plan disk monitoring/retention; stop
the service before rotating the file because it holds an open descriptor.
Writes are not fsynced; sudden power loss can lose the tail. If writing fails
during operation, logging is disabled with `error: event_log_failed` on stderr
(visible in the systemd journal); the call is not interrupted or replayed.

The gateway follows a restart of the **same** Unified host at startup and before
each new device session, never during a request. A changed stable host identity
refuses that connection with HTTP 503 / `core_binding_required`; later sessions
can try again. An unreachable host leaves the stored pin unchanged and normal
Unified calls still fail closed. No uncertain send is replayed. It does not
keep Unified alive, restart it, cancel Unified work when the microphone stops,
or repair credential revocation, TLS expiry or journal faults.

**Paid calls:** the gateway accepts a paid voice connection each time the device
turns its microphone on. There is **no built-in daily or lifetime call cap**.
Device reconnect can therefore incur another paid connection. Existing per-call
time/quiet limits still apply. `Restart=on-failure` restarts the gateway after a
process failure (10-second delay), not an active voice call or an uncertain send.
It does not reconnect the device; device firmware owns that behaviour.

## Remove

```sh
systemctl --user disable --now pocket-gateway.service
rm "$HOME/.config/systemd/user/pocket-gateway.service"
systemctl --user daemon-reload
systemctl --user reset-failed
```

This leaves the repository, private config, credentials, journal and event log
untouched. Retain them or remove them separately only when you intend to.
If an administrator enabled lingering only for this service, ask them whether it
should be disabled; other user services may depend on it.