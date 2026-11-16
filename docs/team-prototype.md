# Pocket assistant team prototype

This guide describes an **early, maintainer-assisted prototype**, not a turnkey
release. One pet has completed two multi-exchange calls through an Amplifier
Unified front-door chat on a home LAN. A teammate has not yet been walked through
a fresh board and server setup. Firmware installation, TLS setup, the independent
service and reconnect must be verified for the particular package being used.

The goal is a voice interface to the user's authorized work across Unified, not
another chatbot or a permanently limited single-project assistant. The current
prototype uses a dedicated Unified chat as its front door. Spoken requests reach
that chat through the gateway, and the pet speaks the correlated response. Its
actual capabilities are bounded by that Unified account's permissions.
Microphone audio is sent to OpenAI during a call; recognized requests can be sent
to the Unified chat. The returned answer text is sent to OpenAI for spoken
rendering, including any private workspace information the front door includes.
The provider's spoken rendering is not guaranteed verbatim: check the Unified
conversation for consequential details.

## What to get

- A **Waveshare ESP32-S3-Touch-AMOLED-1.8 V2**, with CO5300 display and CST820
  touch. The tested board has 16 MB flash, 8 MB PSRAM and ES8311 audio. Do not
  substitute the LCD-1.83 board. See the [Waveshare product page](https://www.waveshare.com/esp32-s3-touch-amoled-1.8.htm)
  and [board documentation](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-1.8);
  verify the V2 hardware revision with the seller before ordering.
- A USB-C data cable, a flashing computer and a phone with a browser.
- A 2.4 GHz personal Wi-Fi network and a Linux host running the user's own
  Amplifier Unified server. The demonstrated voice path uses the same LAN; away
  access over Tailscale is still a product goal, not a qualified setup.
- An OpenAI API key with access to `gpt-live-1` and billing enabled for the
  gateway. Unified itself needs its normal model configuration. The gateway's
  default voice is `marin`; do not assume another model is protocol-compatible.
- A certificate chain trusted by the firmware's ESP-IDF certificate bundle, a
  matching private key and an FQDN for the gateway. Certificate issuance, renewal
  and a repeatable second-device trust procedure are not yet established.

Each voice connection is paid provider usage, including time spent listening or
waiting; Unified model usage is separate. There is no gateway daily spending cap.
The user chose automatic reconnect while the mic is on, and a reconnect can create
another paid connection. **The demonstrated build 11 does not yet provide that
reconnect behavior.**

## 1. Obtain a matched build and preserve the board

Ask the maintainer for a matched, checkpointed gateway and `unified_pet` firmware
package with its exact versions, hashes and flash manifest. There is not yet a
published release package or validated fresh-board flashing procedure. The root
app's older `esp32_pet` image is **not** the Unified app.

The independent app's build script is `apps/unified-pet/scripts/build.sh`. It
requires a locally cached ESP-IDF 5.5.4 image with Pillow 11.3.0 and, for its
explicit `--offline-cache` mode, the matching component cache. It deliberately
does not download an SDK or components during the build.

The public component cache can be prepared separately on Linux with Python 3,
`/proc/self/fd`, `renameat2`, and direct HTTPS access to
`components-file.espressif.com` (the downloader does not use proxy settings).
Pick an **absent** absolute destination under an existing immediate parent
directory that you own and that is not group- or world-writable; no path
component may be a symlink. From the repository root:

```sh
export PET_COMPONENT_CACHE="/absolute/path/to/new-component-cache"
python3 -B apps/unified-pet/scripts/prepare_components.py --prepare "$PET_COMPONENT_CACHE"
```

This opt-in command downloads exact locked versions, rejects unexpected content
and publishes only after all 13 complete component trees match the checked-in
pins. Fresh public downloads and the existing independent `offline_cache.py`
checker verified 4,523 files on the maintainer's Linux host. A clean
network-disabled firmware build also passed with that freshly prepared cache
and an **existing, separately prepared AMD64 tools image**. It did not validate
a fresh Dockerfile image build. If preparation fails after staging is created,
it retains that directory for inspection; an earlier failure may leave no stage.
It does not replace an existing cache. Keep the resulting cache in an
owner-controlled location and do not modify it during the build. A teammate's
complete fresh-machine setup is not yet verified.

The repository's `Dockerfile` pins the ESP-IDF 5.5.4 multi-platform image index
and Pillow wheel hashes. The index includes the originally pinned ARM64 image
and a different, official AMD64 image from the same release. A fresh Dockerfile
image build, including actual architecture and Python compatibility, has not
been verified on either platform. The root `compose.yaml` selects ARM64 and
builds the older root app, not this Unified app. A clean teammate's image
bootstrap remains to be validated; do not describe a substituted tools image as
the result of that build.
Once a matched local image is available, including Pillow at
`/opt/pet-assets/bin/python`, substitute its name below; the command uses the
exported `PET_COMPONENT_CACHE` above:

```sh
PET_DEV_IMAGE="YOUR-VERIFIED-LOCAL-IMAGE" \
  sh apps/unified-pet/scripts/build.sh --offline-cache
```

The build container has networking disabled; outputs and `flasher_args.json`
belong to `apps/unified-pet/build/`. Building is not installation or physical proof.

A maintainer must identify the actual board and port, preserve and verify the
board's existing flash before replacing firmware, use the exact matching manifest,
and verify both the write and a cold boot. Opening serial can reset the device.
Existing-board app-only updates and a first full flash are different operations;
never restore another board's NVS or credentials. Flash backups can contain
secrets and must not be shared. The current fresh-board workflow is **not yet
physically verified**; do not use another person's device-specific installer.

## 2. Prepare Unified and the gateway

Use your own Unified account and a dedicated private state directory, outside Git.
The host needs companion pairing, a pinned chat and the supported app actions used
by the front door. Unified 0.98.6 was inspected for the prototype; no minimum
supported version has been established. A second account's permissions and setup
have not yet been tested.

1. In Unified, create a **new dedicated workspace** and a chat called **Pocket
   assistant** in it, with `app_control` available, and pin the chat. In its first
   message, ask it to save the block below unchanged as `AGENTS.md` at that
   workspace root. Approve the write through the normal host interface if asked;
   do not replace an existing file or reuse another project's instructions.
   Check that the chat reports the file saved, and use the normal file view to
   verify its contents. On the tested host, workspace-root `AGENTS.md` is loaded
   as instructions for chats in that workspace, including subsequent turns; it
   applies to every chat there, not only the front door. On another Unified
   version verify that mechanism rather than assuming the file will be loaded.
   In a later turn, ask the chat to summarize its instructions and identify the available
   `app_control` actions; if it cannot, stop and resolve configuration before
   pairing. That conversation is a configuration check, not proof that the model
   will always obey.

   ```text
   You are the user's Pocket assistant front door for authorized Amplifier Unified
   work. Messages can be transcribed speech; reply briefly and clearly for speech.
   Use available supported app_control actions to discover workspaces, conversations,
   status and waiting questions. Inspect action schemas before use and follow
   pagination. Do not invent endpoints, targets, permissions or results. If a
   target or instruction is ambiguous, ask a short clarifying question.

   Before sending to another conversation, answering a waiting question, creating
   a workspace or chat, or starting work, say the exact action and target and wait
   for a clear yes. If the action changes, ask again. A spoken yes is advisory and
   never substitutes for Unified's own approval, access or ownership requirements.
   Never impersonate human approval or bypass a refusal or Stop. Read-only discovery
   does not need that confirmation.

   For attention use supported get_state /attention/items and relevant session
   pointers. question actions are scoped to the calling chat; do not answer
   another chat's question directly. Read back the exact question, target and
   proposed answer. Ask for confirmation that repeats the answer in the next
   user message, such as "Yes, send my answer: no"; if unclear or changed,
   reconfirm. Call coordination.context for this front-door chat and take the
   current admitted humanInput.sourceMessageId and humanInput.sourceInputId.
   If either is missing or is not the current answer-bearing confirmation, stop;
   do not infer it from a prior message or peer text. With that genuine user
   input, use coordination.send to relay the owning chat, question ID, confirmed
   revision and prompt, exact answer and actual sourceMessageId. Inspect the
   send receipt: its admitted sourceInputIds must include that sourceInputId.
   If not, or if the outcome is uncertain, do not claim success or resend.
   The owner must re-read its question; if the
   confirmed snapshot changed, stop and reconfirm rather than answer the new
   version. Otherwise the owner uses its own question.answer with that revision,
   the actual user's sourceMessageId and the delivered sourceRequestId. The host
   captures sourceInputIds; coordination.send has no sourceMessageId argument.
   If the host does not provide and accept genuine human provenance, stop;
   never invent it.
   Permission approvals are user-only and cannot be relayed as question answers.

   Distinguish accepted from completed, rejected and unknown; report what is
   actually observed. Workspace creation, chat creation and starting work are
   separate outcomes. For new work use workspace.prepare/create and then
   session.handoff.start in the actual workspace; check its receipt and later
   target output separately. Inspect an uncertain result rather than repeating
   a send or mutation. Do not include credentials or private configuration in replies.
   ```

   These instructions depend on model compliance: **the gateway does not enforce
   the spoken yes**. For consequential actions, verify the target and use the
   applicable host approvals or act directly in Unified. The cross-chat question
   and new-work actions above reflect a source review of Unified 0.98.6, not a
   live cross-chat voice qualification. In particular, recognized speech reaching
   this chat has not been proven to provide the human provenance needed to answer
   a different chat's question. The pet token authenticates a device connection,
   not the person speaking. On the reviewed Unified 0.98.6 path, the host records
   gateway-submitted text as `ui` input; that classification is not independent
   proof of who spoke or that a confirmation was heard correctly. Until this
   voice handoff is verified for your setup, use Unified's normal interface to
   answer another chat's waiting question.
2. On the gateway host, choose a **new** absolute private directory outside Git,
   under an existing owner-controlled private parent. Do not use symlinks in
   the path or change permissions on someone else's existing directory. Keep the same
   `XDG_CONFIG_HOME` for pairing, attach, list and bind. In a local shell, change
   to this package's `bridge/` directory and use the Python environment from
   [service setup](../bridge/deploy/README.md). Use a new POSIX-compatible shell
   on Linux for these steps; `umask` and exported variables stay in this shell
   until you exit it. Replace the example paths and server origin with your own
   values. Stop at any failed command:

   ```sh
   umask 077
   export PET_PYTHON="/absolute/private/path/venv/bin/python"
   export XDG_CONFIG_HOME="/absolute/private/directory"
   mkdir -m 700 "$XDG_CONFIG_HOME"
   ```

   If `mkdir` failed, stop: do not continue with or change an existing directory.
   Once it succeeded, run:

   ```sh
   "$PET_PYTHON" -B -m pet_bridge pair-start --origin https://YOUR-UNIFIED-HOST:PORT --name pocket-assistant
   ```

   The Unified origin must pass hostname and system CA verification in the
   gateway's HTTPS child. The child has a deliberately stripped environment:
   a parent-shell or systemd `SSL_CERT_FILE` override is **not passed to it**.
   The child's Python/OpenSSL default trust store must trust the Unified server's
   certificate chain and the origin hostname must match. Ask the maintainer for a
   verified host-trust setup; never disable verification.
   Open the printed address in your signed-in Unified browser, compare the code
   on both screens, and approve only the matching request. Then run
   `"$PET_PYTHON" -B -m pet_bridge pair-poll` once. **A zero exit status alone is not
   approval:** if it prints `Not approved yet` or `Too early`, wait at least the
   displayed number of seconds and run the poll again. Proceed only when it
   prints `Approved. Credential reference:`; retain that reference privately for
   the config. A denied, revoked, expired or invalid request is not approval.
   Stop on an error or lost response, preserve the local state and inspect the
   outcome before deciding on recovery; do not assume approval or blindly start
   a new request.
   If a pairing start has an unknown result, stop and inspect it before recovery.
   `pair-abandon` forgets the local proof only; it does **not** cancel any server
   request, which may persist until expiry. Do not abandon or restart blindly.

   Next run `"$PET_PYTHON" -B -m pet_bridge attach`; proceed only on `Attached.`.
   Run `"$PET_PYTHON" -B -m pet_bridge list` and proceed only if it says `complete`.
   This is the restricted active-or-pinned companion view, not an all-work list.
   The bridge view does not show the workspace, so a matching title alone is
   insufficient. In the **Pocket assistant** chat you created, ask:

   > Use `app_control` operation `get_state` with args
   > `{"path":"/selectedSessionId"}` and show me only the exact `value` returned
   > for this conversation. Do not select another chat or send any message.

   On the inspected host this caller-scoped read returned the calling chat's full
   ID; it does not enumerate other chats. It is an app tool call, not a terminal
   command. Check that the reply is the tool's actual value, not a guessed ID.
   The chat response and tool history contain this sensitive metadata. Copy it
   only to your own private configuration and terminal; shell history, process
   listings, scrollback or screen recording can expose it. Do not publish it.
   Verify the intended workspace and title in Unified, then compare the last
   eight characters of this exact full ID with the corresponding title in
   `list`. If it is absent, pin the target and recheck. If the value is missing,
   identity uncertain, or the list incomplete, stop rather than guessing.
   The metadata read was verified on the inspected host for its calling chat;
   the teammate's model-mediated copy step remains untested.

   Replace `FULL-CHAT-ID` with the exact value and run:

   ```sh
   "$PET_PYTHON" -B -m pet_bridge bind FULL-CHAT-ID
   ```

   Proceed only on `Bound to session` with the expected short ID and title.
   Binding re-fetches the listing, requires the exact full ID in a complete list,
   and sends no chat message; it does not prove idleness. The resulting state
   root is `$XDG_CONFIG_HOME/amplifier-unified-pet-bridge`. Use
   `"$PET_PYTHON" -B -m pet_bridge --help` for the supported CLI. Exact-ID binding
   and incomplete-list refusal are tested with synthetic companion responses, not
   yet on a second teammate's host.
   If the shell closes after a confirmed step, start a new private shell in
   `bridge/`, restore `PET_PYTHON` and `XDG_CONFIG_HOME` to the **same** paths
   and continue from the last confirmed checkpoint. Do not rerun `mkdir`,
   `pair-start` or token generation simply to resume. If a checkpoint is unknown,
   preserve the state and investigate it before any new action.
3. Configure the gateway with the same state root, credential reference and full
   bound chat ID. Keep the restricted Unified credential and OpenAI provider key
   on the host. Create a **separate** random 32-byte pet token, encoded as
   unpadded base64url (43 characters), and configure only its SHA-256 hex digest
   over its ASCII text in `pet_token_sha256`. The raw token is needed privately
   for phone setup; it is not the OpenAI key or the Unified credential. Never put
   tokens in Git, chat, command arguments or a service unit. For a new device,
   the following Linux command creates an owner-only token file **once** in the
   same private directory from step 2, and prints only its hash for the config:

   ```sh
   "$PET_PYTHON" -B - <<'PY'
   import hashlib, os, secrets, stat, sys
   root = os.environ.get("XDG_CONFIG_HOME", "")
   try:
       parts = root.split("/")
       if not os.path.isabs(root) or any(part in (".", "..") for part in parts):
           raise ValueError("private absolute directory required")
       flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
       dfd = os.open("/", flags)
       try:
           for part in filter(None, parts):
               nextfd = os.open(part, flags, dir_fd=dfd)
               os.close(dfd)
               dfd = nextfd
           info = os.fstat(dfd)
           if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
               raise ValueError("owner-only directory required")
           token = secrets.token_urlsafe(32).encode("ascii")
           fd = os.open("pet-token.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                        os.O_NOFOLLOW, 0o600, dir_fd=dfd)
           with os.fdopen(fd, "wb") as out:
               out.write(token + b"\n")
               out.flush()
               os.fsync(out.fileno())
           os.fsync(dfd)
       finally:
           os.close(dfd)
   except (OSError, ValueError):
       print("Token creation did not complete. Preserve the file and inspect privately.",
             file=sys.stderr)
       sys.exit(1)
   print("Token stored privately; SHA-256 of ASCII token, without newline:",
         hashlib.sha256(token).hexdigest())
   PY
   ```

   Proceed only on success and copy the printed digest into the private config;
   do not hash the trailing newline. For phone setup, on a trusted local host
   open the file in a private, non-recording viewer that makes no backups or
   cloud copies. With the phone in hand, type the 43 token characters (not the
   newline) into its masked setup field, taking care with each character. The
   field has no reveal control, so visual comparison after entry is unavailable.
   Do not paste the token into a shell, chat, command argument, shared clipboard or recorded
   terminal. Protect both screens from observers and close the viewer afterwards.
   If no such private transfer is available, stop and arrange one; this guide
   does not establish a secure remote transfer. If the command fails or the
   file already exists, it may be partial or its durability uncertain. Preserve
   it and inspect privately before deciding what to do; never overwrite it or
   generate a replacement blindly.
4. The gateway config requires its private state and credential paths, chat ID
   and label, LAN IPv4 and port, literal HTTP Host value, TLS certificate and key,
   pet token hash, and exactly one provider-key source. Use an owner-only 0600
   provider key file for a service. Config, certificates, keys and state must be
   private, owner-controlled files/directories; final-component symlinks are
   rejected. This is the shape of a private JSON config; replace every value in
   angle brackets with your own, using absolute paths and an RFC1918 LAN address.
   The literal `host_header` must match the device's dial address and port; it is
   not the TLS certificate FQDN.

   ```json
   {
     "version": 1,
     "state_root": "<absolute-XDG_CONFIG_HOME>/amplifier-unified-pet-bridge",
     "credential_ref": "<pair-poll-credential-reference>",
     "session_id": "<full-pinned-front-door-chat-id>",
     "session_label": "Pocket assistant",
     "bind_host": "<server-lan-ipv4>",
     "port": 9443,
     "host_header": "<server-lan-ipv4>:9443",
     "tls_cert_file": "<absolute-private-certificate-chain-path>",
     "tls_key_file": "<absolute-private-certificate-key-path>",
     "pet_token_sha256": ["<lowercase-sha256-of-pet-token-ascii>"],
     "model": "gpt-live-1",
     "voice": "marin",
     "provider_key_file": "<absolute-private-openai-key-path>"
   }
   ```

   `auth_root` may be omitted to use the state root's `auth` directory. Use only
   one of `provider_key_file` and `provider_key_env`, never both. See
   `GatewayConfig` in `bridge/pet_bridge/gateway_state.py` and
   [service setup](../bridge/deploy/README.md). A complete clean-server config
   and installation walkthrough still needs maintainer validation. An optional
   source-level dual LAN/Tailnet listener is described in the service setup;
   it requires a matched device implementation and is not yet physically verified.

For the pet-to-gateway connection, the device dials the configured LAN IPv4 but
verifies the TLS certificate against a separately configured FQDN. The FQDN must
match the certificate, the actual firmware bundle must trust its chain, and the
pet needs working DNS/network time synchronization for a valid clock. The FQDN
must be lowercase, dotted and at most 63 characters; the gateway accepts ports
1024–65535. A private or self-signed CA is not automatically trusted. Do not
disable TLS verification to work around a missing certificate. A host-side TLS
test or unauthenticated `401` does not prove the firmware trusts this chain or
that a voice conversation works.

The gateway-to-OpenAI connection is a **third** TLS connection: it uses the
gateway process's Python/OpenSSL default trust configuration, distinct from both
the device bundle and the isolated Unified HTTPS child. Systemd may use a
different environment from a shell; OpenSSL CA overrides can affect this parent
process even though aiohttp proxy environment handling is disabled. Verify the
intended service trust configuration without printing credentials or weakening
TLS. These trust procedures are not yet qualified on a teammate's host.

## 3. Configure from a phone

This bridge-first sequence is supported by the current source but has not yet
been demonstrated on a factory-fresh teammate device:

1. On the pet open **Settings → Phone setup**, scan the QR and join the displayed
   WPA2 setup Wi-Fi. If the page does not open, browse to the address shown on the
   pet while staying on that setup network.
2. Under **Pocket Unified bridge**, enable the bridge and enter the gateway LAN
   IPv4, its TLS port, the certificate FQDN and the raw pet token. Save the bridge
   settings first and wait for the save acknowledgement. A successful save does not
   prove the gateway is reachable. If the acknowledgement is lost, reconnect to
   the setup page and inspect the nonsecret address settings. A saved-token flag
   means only that **some** token is present, not which one. Preserve the retained
   original token; do not generate a new one. A deliberate save of the same
   known token and same settings can establish a fresh acknowledgement once the
   page is reachable; if the prior outcome or settings remain unclear, stop and
   seek maintainer help instead of repeatedly retrying.
3. Choose the 2.4 GHz Wi-Fi network and enter its password. In bridge mode the
   OpenAI key is not needed on the pet. Wait for the connection and save result,
   then select Done. If the phone disconnects before acknowledgement, reconnect
   to the setup network and inspect the result before retrying.
4. Confirm Wi-Fi and bridge status on the pet. The optional historical Tailscale
   test fields do not, by themselves, enable off-home voice. Do not run the
   diagnostic Tailscale tunnel in this prototype: source review identified a
   shutdown race, and its task-done signal does not prove safe teardown. A repair
   and verification are in progress.

The setup page is HTTP inside the WPA2 setup network and uses a per-boot mutation
control; it is not HTTPS provisioning. The pet token is stored in device NVS.
Protect the phone session and flash dumps. The prototype is not a production
security qualification. Diagnostic serial logs can include network identities;
do not publish or share them without reviewing and redacting them.

## Offline checks for maintainers

These checks do not start a paid call, access the device or prove a fresh setup.
From the repository root, with the gateway Python environment described in the
[service instructions](../bridge/deploy/README.md):

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s bridge/tests -t bridge -q
node apps/unified-pet/tests/setup_page_test.mjs
node apps/unified-pet/tests/setup_bridge_page_test.mjs
node apps/unified-pet/tests/setup_unified_page_test.mjs
PYTHONDONTWRITEBYTECODE=1 python3 -B apps/unified-pet/tests/ui_contract_test.py
```

The full bridge suite and these app setup/UI checks passed on the maintainer's
host for the prepared prototype. For native firmware and shared TLS checks, run
`apps/unified-pet/tests/run_native.sh` and `tests/run_shared.sh` inside the
pinned ESP-IDF 5.5.4 environment with `PET_TLS_HOST_LIB` pointing to the
appropriate real-host mbedTLS libraries; see `apps/unified-pet/AGENTS.md`.
Those SDK checks and a target build must be tied to the exact release source and
inputs. The historical isolation test requires its original private baseline;
it is not a clean-clone acceptance test. A failed or unrun check is not a pass.

## 4. Run and use the prototype

Start only one gateway against a given state and listener. The Linux service is a
**template**, not an already installed service: follow
[bridge/deploy/README.md](../bridge/deploy/README.md) after the maintainer has
verified the paths, dependencies and certificate. Unified must remain available
separately. Systemd ordering is not proof that the network is ready. The service
was installed on the original host; its TLS listener returned the expected
unauthenticated `401`, and the user heard a real Unified reply through it. The
gateway journal matched the recognized input to the front-door chat. Same-host
restart handling and a second person's service setup remain unverified.

Tap the round microphone control to connect and speak. Wait for **Listening**,
ask a short question, and wait for the spoken reply before speaking again. Tap
again to mute; muting ends audio, not work already accepted by Unified. There is no
transcript preview or Send tap. Speech during an outstanding request or the
answer is not queued, and barge-in is not yet qualified. Ask the front door to
confirm consequential actions and inspect Unified if it reports uncertainty.

An ordinary call ends after about 90 seconds of silence or at the ten-minute
limit. The demonstrated build 11 needs another tap after a lost connection,
once cleanup has settled. If it remains closing or cannot safely release the
transport, seek maintainer help instead of repeatedly retrying. A
newer automatic-reconnect implementation is being developed, not established as
installed or demonstrated. The gateway can reconcile some lost acknowledgements
by exact input identity and hash; it never re-sends to find out. If the outcome
is unknown, inspect the conversation and preserve the journal rather than
repeating an action. A response arriving after the wait limit may not be spoken.

## Remaining acceptance work

- A matched package and a verified factory-board install, phone setup and first
  real conversation on a teammate's own server.
- Repeatable certificate provisioning, fresh-environment dependency installation
  and a verified complete setup on another host.
- Installed service surviving app restart; reconnect and mute without replay on
  the final device firmware.
- A real waiting question answered in the correct chat, and new workspace,
  chat and requested work actually started by voice.
- Real voice and context across Tailscale away from the home LAN, plus longer
  reliability, security and multi-device checks.

A passing build or a saved setup page is not evidence that these journeys work.
See [PROJECT-BRIEF.md](../PROJECT-BRIEF.md) for the whole-product delivery plan.
