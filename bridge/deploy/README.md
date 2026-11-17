# Run the pocket gateway as a user service

This is a template, not an installer. Nothing in this directory enables a service.
Use a Linux host with systemd user services, the bridge's existing Python/aiohttp
environment, working private gateway settings (a private `.env` file, recommended,
or the alternative private JSON config), TLS files, a paired restricted
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

## Private settings in a local `.env` file (recommended)

Machine-specific and secret values stay on the machine, never in the repository.
The tracked [`bridge/.env.example`](../.env.example) lists every setting with
placeholders only. Make a private copy and fill it in. The copy must never
replace a file you have already filled in, so create it with this command, not
with `cp`:

```sh
PET_ENV_EXAMPLE="/ABSOLUTE/REPOSITORY/bridge/.env.example"
PET_ENV_FILE="/ABSOLUTE/private/.env"
if [ -e "$PET_ENV_FILE" ] || [ -L "$PET_ENV_FILE" ]; then
  echo "Stopped: the destination already exists. Nothing was copied or changed." >&2; false
elif ( umask 077 && set -C && cat "$PET_ENV_EXAMPLE" > "$PET_ENV_FILE" ); then
  echo "Created a new private settings file."
else
  echo "Stopped: the copy did not complete. No existing file was overwritten." >&2; false
fi
```

It creates the destination only if nothing is there. An existing file, or a
symbolic link of any kind, is left untouched and you see
`Stopped: the destination already exists. Nothing was copied or changed.`

The command assumes the destination directory is private and controlled only by
you, so that nothing else creates files in it. It is a check followed by a
redirect, not one atomic step. The redirect runs with the shell's no-overwrite
option (`set -C`); in the `sh` and `bash` used for this change's tests it
refused to write to a regular file, a symbolic link or a directory that was
already at the path. That is not a guarantee for everything that could be
created there between the check and the redirect: a FIFO, for example, could
make the command block or fail.

When the redirect does not complete you see
`Stopped: the copy did not complete. No existing file was overwritten.` After
that second message (also shown when the example or the destination directory
is missing), inspect the destination before doing anything else; it can be an
empty new file. The new file is owner-only (mode 0600) because of `umask 077`.
Do not `chmod`, move or delete an existing destination to make the command
pass: if a private `.env` is already there, keep it and edit it, or choose a
different absent path.

Keep the copy outside the repository, in an owner-controlled private directory
such as the one that holds the pairing state. (`.gitignore` also ignores `.env`
and `.env.*` inside the checkout and allows only `.env.example`, as a safety
net, not as the recommended place.) Replace every `<placeholder>`, then start
the gateway with `serve --env-file /ABSOLUTE/private/.env`.

- **Same checks as JSON.** Each variable maps to one field of the JSON config
  and the values go through the same validation. A wrong value, an unknown,
  repeated, empty or malformed entry, or a missing required one stops the gateway
  before it listens with `error: config_invalid` and no value shown. As with
  JSON, a conversation id whose last eight characters are not lowercase hex is
  `core_binding_required`, and an absent or malformed provider key is
  `provider_key_missing`.
- **Private file required.** The path must be absolute and the file a regular
  file owned by you with no group/other permissions and not a symlink, as for the
  JSON config and key files. Anything else is also `config_invalid`.
- **Do not edit it while the gateway starts.** Stop the service, edit, then
  start. The gateway refuses a settings file (`.env` or JSON) or provider key
  file that yields fewer bytes than its size at open, or whose size,
  permissions, owner or modification/change time differ after the read. That
  is a before-and-after comparison, not a lock: an edit that leaves all of those
  equal (for example a same-size rewrite within the file system's timestamp
  resolution) is not detected, and refusal of every concurrent edit is not claimed.
- **Literal values.** One `NAME=value` per line; the value is the text to the end
  of the line. No quotes, no spaces around `=`, no `export`, no `$VAR` expansion,
  no inline comments, Unix line endings. Comment lines start with `#` in the
  first column. This is not a shell script; do not `source` it. A value cannot
  start or end with a space or quote character or contain a space followed by
  `#`; a conversation label that needs one of those requires the JSON config.
- **Provider key, exactly one of two.** `POCKET_GATEWAY_PROVIDER_KEY_FILE` is the
  absolute path of an owner-only key file. `POCKET_GATEWAY_PROVIDER_KEY` is the
  key itself, kept only in the private copy. Setting both or neither is
  `config_invalid`. In this mode the gateway does not read the provider key from
  the process environment.
- **Do not also load it another way.** With `--env-file` the gateway opens the
  file itself and applies the private-file check. Loading the same file by
  another route does not remove that check, and it gains nothing: in this mode
  the gateway never reads its provider key from the environment.
  - Systemd `EnvironmentFile=`, and dotenv tools that export what they read,
    copy every value, including an inline provider key, into the process
    environment. There a child process that is not given a cleaned environment
    inherits it, and other processes of the same user can normally read it
    through `/proc/<pid>/environ`.
  - A bare shell `source` is different: it sets shell variables, which reach a
    process environment only if the file or the shell exports them (for
    example under `set -a`). Do not `source` the file anyway. It is not a shell
    script: the shell applies its own quoting, expansion and word splitting to
    values the gateway reads literally, so a label containing a space is not
    even read as one value.
- **Token hashes.** `POCKET_GATEWAY_PET_TOKEN_SHA256` takes one hash, or several
  separated by commas with no spaces.

The gateway never writes the provider key, credential reference, token hashes,
full conversation id or file paths to stdout, stderr or the event log, and a
startup failure prints a fixed code only. Exactly as with JSON, a successful
start prints one line with the listen address(es), port and the last eight
characters of the conversation id. This path is tested offline with synthetic
values. It has not been demonstrated on a running service or on a second
machine.

### Alternative: a private JSON config

`serve --config /ABSOLUTE/private/gateway.json` remains supported and unchanged;
its shape is shown in the [team guide](../../docs/team-prototype.md). Give exactly
one of `--config` and `--env-file`. With JSON, set `provider_key_file` to the
absolute path of the mode-0600 provider key file; do not put a key or token in
the unit or use a shell environment variable for it.

### Move an existing JSON-configured service to `.env`

Nothing here is automatic, and the JSON file keeps working if you do nothing.

1. Create the private owner-only file with the no-overwrite copy command above.
   If it stops because the destination already exists, do not replace that file;
   review it or choose a different absent path.
2. Copy each value from the private JSON config to its variable; the names match
   (`state_root` → `POCKET_GATEWAY_STATE_ROOT`, and so on; `version` →
   `POCKET_GATEWAY_CONFIG_VERSION`). Write values without the JSON quotes, join
   the `pet_token_sha256` list with commas, and leave optional settings that the
   JSON omits commented out. If the JSON used `provider_key_env`, either point
   `POCKET_GATEWAY_PROVIDER_KEY_FILE` at an owner-only key file or put the key in
   `POCKET_GATEWAY_PROVIDER_KEY`; the name of the old variable is not carried over.
3. Wait until no call is in progress, then stop the service
   (`systemctl --user stop pocket-gateway.service`). Only one gateway may own the
   journal and listen address.
4. In `~/.config/systemd/user/pocket-gateway.service`, replace the `ExecStart=`
   line with the `--env-file` form from the template, using the absolute path of
   the private file. Keep your existing `--event-log` path.
5. Run these in order, in one shell. The first two note the time **before** the
   start, which [Check the current start](#check-the-current-start) needs:

   ```sh
   PET_START_LOCAL="$(date '+%Y-%m-%d %H:%M:%S')"
   date -u '+%Y-%m-%dT%H:%M:%S+00:00'
   systemctl --user daemon-reload
   systemctl --user start pocket-gateway.service
   ```

   Then follow that section. The
   journal still holds `gateway: listening on` lines from earlier runs with the
   JSON config, so that text alone proves nothing: accept only the evidence tied
   to this start, with the same address and short conversation id as before. A
   `gateway_stop` record with `code` `config_invalid` means the file was refused
   and nothing listened.
6. Keep the private JSON file until the service has run from `.env`. To go back,
   stop the service, restore the `--config` line, reload and start.

## Install and start

1. Create an owner-only log directory, for example:
   `install -d -m 700 "$HOME/.local/state/pocket-gateway"`.
   Keep the settings, credential and key files private; do not put a key or
   token in the unit.
2. Copy `pocket-gateway.service` to
   `~/.config/systemd/user/pocket-gateway.service` (create that directory if needed).
   Replace **every** placeholder in the copy:
   - `@PYTHON@`: absolute Python interpreter with the bridge's dependencies.
   - `@BRIDGE_DIR@`: absolute repository `bridge` directory, where `pet_bridge` lives.
   - `@ENV_FILE@`: absolute path of the private `.env` file.
   - `@EVENT_LOG@`: absolute JSONL path in the owner-only directory from step 1.
   To use a JSON config instead, delete the `--env-file` line, uncomment the
   `--config` line below it and replace `@CONFIG@` with the absolute private JSON
   path. Exactly one `ExecStart=` line may be active.
   These are systemd paths, not shell commands; no `~` or `$HOME` expansion occurs
   inside the unit. Quote the interpreter if its path contains spaces. Escape `%`
   as `%%` in literal paths for systemd.
3. Stop any manually launched gateway yourself first: only one may own the journal
   and listen address. Then run these in order, in one shell. The first two note
   the time **before** the start, which the check afterwards needs:

   ```sh
   PET_START_LOCAL="$(date '+%Y-%m-%d %H:%M:%S')"
   date -u '+%Y-%m-%dT%H:%M:%S+00:00'
   systemctl --user daemon-reload
   systemctl --user enable --now pocket-gateway.service
   systemctl --user status pocket-gateway.service
   ```

   Then confirm this start as described in
   [Check the current start](#check-the-current-start).

`enable` starts it on future user-manager starts. To keep this service running
after logout or start it before login, an administrator may need to enable
lingering for your user (`loginctl enable-linger USERNAME`). Closing the Unified
app does not stop the gateway, but Unified must be running/reachable to do work.
User `network-online.target` is only ordering, not a guarantee that Wi-Fi,
Tailscale or Unified is ready.

### Check the current start

`Type=simple` reports the unit as active as soon as the process exists, before
the settings are read, and the journal keeps the output of earlier runs. Judge
only evidence that belongs to the start you just made.

The check compares against a time noted immediately **before** the start. The
start commands in [Install and start](#install-and-start) step 3 and in step 5
of the move to `.env` begin with the two `date` commands that note it: the
first keeps the local time in `PET_START_LOCAL` for this shell, the second
prints the same moment in UTC for comparison with the event log. Before any
other start or restart, run those two commands first. Do not run them once the
service has started: a time taken afterwards is later than the start and would
make a healthy start fail the comparisons below. If you started without noting
the time, wait until no call is in progress, stop the service, and start again
from those commands.

After the start, wait about 15 seconds (longer than `RestartSec`), then, in the
same shell:

```sh
systemctl --user show pocket-gateway.service \
  -p ActiveState -p SubState -p ActiveEnterTimestamp -p InvocationID
journalctl --user -u pocket-gateway.service --no-pager \
  _SYSTEMD_INVOCATION_ID="$(systemctl --user show pocket-gateway.service -p InvocationID --value)"
grep '"event":"gateway_st' /ABSOLUTE/private/events.jsonl | tail -n 2
```

The last command shows the two most recent start/stop records of the event log.
This start is listening only if all of the following hold:

- `ActiveState=active` and `SubState=running`, and `ActiveEnterTimestamp` (shown
  in local time) is not earlier than the time you noted.
- The journal output for this invocation contains
  `gateway: listening on <address>:<port> for session ...<last eight characters>`
  with the values you expect, and no `error:` or `gateway: stopped` line after
  it. The gateway flushes this line when it writes it, so it does not wait for
  the process to exit.
- The newest of those event-log records is a `gateway_start` record whose UTC
  `timestamp` is not earlier than the UTC time printed by the second `date`
  command, with no `gateway_stop` after it.

A `gateway_stop` record after that `gateway_start` means this start ended and
nothing is listening from it. Its `code` says how:

- A code such as `config_invalid` is a failed start. The process exits with a
  failure status, so `Restart=on-failure` starts the gateway again after 10
  seconds, and again after each further failure, until you stop the service and
  correct the cause.
- A `null` code is a clean stop, for example after `systemctl --user stop` or a
  termination signal. The process exits successfully and `Restart=on-failure`
  does not start it again.

If the journal command prints no entries although the unit is running, your
systemd may not tag a user service's output with its invocation. Use
`journalctl --user -u pocket-gateway.service --no-pager --since "$PET_START_LOCAL"`
instead; that restricts by time, not to one invocation, so it also shows any
automatic restarts after your start.

The listening line reports that the socket was bound, not that a device can
reach it. These commands follow the systemd documentation and the gateway
source; they have not been exercised against a live unit in this change.

## Optional LAN and Tailnet listeners

The default config still serves only its existing `bind_host`/`host_header`.
For a matched gateway and device package that supports native Tailnet voice, one
gateway can also bind a second explicitly chosen local IPv4 address. Add these
two optional settings to the **same** private file, without changing the LAN
settings. In `.env`:

```text
POCKET_GATEWAY_TAILNET_BIND_HOST=100.64.12.34
POCKET_GATEWAY_TAILNET_FQDN=gateway.example.test
```

Or, in the JSON document:

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
"/ABSOLUTE/PYTHON" -B -m pet_bridge serve --env-file "/ABSOLUTE/private/.env" --event-log "/ABSOLUTE/private/events.jsonl"
```

With a JSON config, use `--config "/ABSOLUTE/private/gateway.json"` in place of
`--env-file ...`.

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

This leaves the repository, private settings file, credentials, journal and event log
untouched. Retain them or remove them separately only when you intend to.
If an administrator enabled lingering only for this service, ask them whether it
should be disabled; other user services may depend on it.