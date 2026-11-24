# Pocket Pet firmware user manual

This guide is for the **Waveshare ESP32-S3-Touch-AMOLED-1.8 V2** running Pocket
Pet. It covers using the pet and configuring it in your phone’s browser; there
is no phone app to install. For the correct board, installing or updating the
firmware without losing settings, and preparing the Linux gateway, start with
the [setup and build guide](team-prototype.md#start-here).

In the LAN preview, the pet talks to **one selected Unified chat**, one request
at a time. The pet and its gateway must be on the same LAN. A phone connected
to Tailscale does not give the pet away-from-home voice access.

## On the pet

- **Microphone:** tap once to open voice. Wait for **Listening** before speaking.
  **Accepted** means Unified took the request, not that it finished. Wait for
  the spoken reply before asking again; follow-ups are not queued. Tap again to
  turn the mic off. This ends audio, not work Unified already accepted. The
  voice service processes microphone audio and reply text during a call and can
  incur charges.
- **Settings (gear):** see the Wi-Fi and bridge state. **Phone setup** opens the
  setup network and shows its QR code and password. **Volume** adjusts the
  speaker. Use **Touch alignment** if taps land in the wrong place.
- **Connected** under Wi-Fi and **Host bridge configured** mean the pet joined
  Wi-Fi and has a saved bridge configuration, respectively. Neither proves an
  active voice connection. Use the [gateway readiness checks](../bridge/deploy/README.md#check-the-current-start)
  before trying your first call.

### Restart and shut down

These controls describe the [device power candidate](https://github.com/colombod/amplifier-unified-pocket-pet/issues/15),
not the released `0.3.19` LAN preview.

- **Restart:** On the running pet, open **Settings → Restart**, or press and
  release **BOOT**. At **Restart the pet?**, tap **Restart** or **Cancel**.
  This is not a factory reset. Keep BOOT released while confirming and during
  normal startup. If the pet says **Restart not performed**, release BOOT and
  try again.
- **Shut down:** Open **Settings → Shut down**, or briefly press **PWR** on
  the running pet. At **Shut down the pet?**, tap **Shut down** or **Cancel**.
  The firmware requests power-off from the board’s power controller, not
  just a blank display. If the pet shows **Shutdown not confirmed** or
  **Shut down unavailable**, it is still on; no second command is sent
  automatically.

Finish phone setup and turn off the mic before restarting or shutting down.
If you see **Pet is busy**, wait for Wi-Fi, settings saves or audio cleanup
to finish, then try again. Use short presses only; the controls do not
replace the board’s hardware recovery functions.

The update leaves stored Wi-Fi, gateway pairing and calibration alone. A
normal restart starts with the mic off. Neither action cancels work already
accepted by Unified or resubmits a request. Runtime buttons, staying off,
powering back on with **PWR**, and retaining settings after a power cycle
still need physical verification on the exact V2 board, on battery and
with USB connected.

## Set up from your phone

1. With the mic off, open **Settings → Phone setup** on the pet. Scan its QR
   code with the phone camera and accept the prompt to join the pet’s Wi-Fi.
   Or join the displayed network in the phone’s Wi-Fi settings using the
   password **on the pet screen**. This is not your home Wi-Fi password.
2. Stay on the pet’s setup Wi-Fi even if the phone says it has no internet.
   If the setup page doesn't open, enter `http://192.168.4.1/` in the phone
   browser. The page says **Meet Tachikoma**. Use only the pet’s own setup
   network; the page uses local HTTP inside that password-protected network,
   not HTTPS. Don’t share the QR code or a screenshot containing credentials.
3. Scroll down to **Pocket Unified bridge**. Check **Use the host bridge (not
   the standalone chatbot)**. Enter the four values below and tap **Save bridge
   settings only**. Wait for **Bridge settings saved. No connection started.**
   Leave **Clear the saved pet token (disable the bridge first)** unchecked.
   If you are editing a working configuration, a blank token field keeps the
   saved token.
4. Back at the top, choose your **2.4 GHz** home Wi-Fi. If it is hidden, choose
   **Other / hidden network…** and enter its exact name. Enter that network’s
   password, leave **OpenAI API key** blank for bridge mode, then tap **Connect
   Tachikoma**. Wait for the connection and save result; tap **Done · return to
   my Wi-Fi** on the phone. Return the phone to its usual Wi-Fi and close the
   pet’s setup screen with **Back / Done** if still open.
5. Verify the Wi-Fi and bridge indicators on the pet. If the gateway is ready,
   try [one chat-specific spoken question](team-prototype.md#first-spoken-check).
   Check the selected Unified chat for one request and its actual answer.

### Where to get the bridge values

| Phone field | Where to get the value |
| --- | --- |
| **Gateway LAN IPv4** | The Linux gateway’s private LAN address: `POCKET_GATEWAY_BIND_HOST` in its private `.env`, or `bind_host` in its JSON config. Use the address the gateway owner configured, not `192.168.4.1`, a Tailscale address, or the Unified server’s URL. |
| **Gateway TLS port** | `POCKET_GATEWAY_PORT` or JSON `port` on that same gateway. The example is `9443`; enter the configured value. |
| **Certificate FQDN** | The exact lowercase hostname covered by the **gateway’s** TLS certificate. Ask the gateway owner for the certificate name. Enter just the name: no `https://`, port, or path. The pet connects to the LAN IP but verifies this hostname. |
| **Pet token** | The raw 43-character device token the gateway owner created in the [gateway setup instructions, step 3](team-prototype.md#2-prepare-unified-and-the-gateway). Those instructions save it as `pet-token.txt` in the private setup directory. Enter the token without its trailing newline, not the SHA-256 digest in the gateway config. Transfer it privately, not in a chat, shared clipboard, screenshot, or command argument. |

The **Pet token** is sometimes called the device pairing token. It is not the
**Unified browser pairing code**: the gateway operator gets that code from
`pet_bridge pair-start`, compares it in their signed-in Unified browser, and
approves it there. The resulting Unified companion credential and the gateway’s
OpenAI API key **stay on the host**. Neither goes in the phone’s Pet token field.

### Optional Tailscale fields

**Leave these fields alone for the LAN preview.** Saving an auth key or Unified
target here does not enroll the pet, start a tunnel, or enable off-LAN voice.
It is not needed for gateway-to-Unified traffic over the gateway host’s own
Tailscale connection.

- **Tailscale auth key · optional:** only for a separate, authorized device
  connectivity test. An authorized tailnet admin goes to **[Keys](https://console.tailscale.com/admin/settings/keys)
  → Generate auth key**. Prefer a short-lived one-off key for the intended
  device, under your tailnet’s policy. Use the generated auth key, not a
  Tailscale API access token, Pet token, or Unified pairing code. Enter it in
  this masked field and tap **Save Tailscale key only**. A blank field keeps the
  saved key; a saved badge does not identify the value or mean the pet joined
  Tailscale. See [Tailscale’s auth key guide](https://tailscale.com/docs/features/access-control/auth-keys#generate-an-auth-key).
- **Unified peer FQDN** and **HTTPS port:** separate fields for that same
  historical test. Ask the operator for the Unified peer’s exact lowercase
  full hostname and HTTPS port. A blank hostname disables the test target.
  **Save Unified target only** only stores these values. Do not run the legacy
  device-side tunnel in the LAN preview.

## If setup is interrupted

- If the browser disconnects while saving, rejoin the pet’s setup Wi-Fi and
  reopen `http://192.168.4.1/`. Check the save result before changing anything.
  A **saved token** indicator means some token exists, not that it matches the
  gateway. Use the original known value; don’t generate a new one just to retry.
- If the pet is connected but never shows **Listening**, check the configured
  LAN IP, gateway port, TLS certificate name and token before opening another
  paid call. A gateway TLS failure is not a reason to disable verification.
- If the pet says **Accepted** but you don't hear a reply, first check the
  selected Unified chat. Do not repeat a request whose outcome is unknown.
- For a pet already set up, use the [settings-preserving update guidance](team-prototype.md#update-a-pet-thats-already-set-up),
  not the first-install or factory-recovery procedure. Keep device backups private.
