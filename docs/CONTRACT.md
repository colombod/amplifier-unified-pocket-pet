# Pocket Pet delivery contract — v1

This contract records the user's stated product requirements and proposed
checks for the combined pet, gateway, and Unified experience. It does not claim
approval of every line or delivery of the full product. The maintainers own
integration and verification; device and gateway contributors own their
respective boundaries. The user settles consequential changes to the outcome.
See the [vision](VISION.md) and [architecture](ARCHITECTURE.md).

## Essential promises

| ID | Promise | What shows it is kept or broken |
| --- | --- | --- |
| P1 | **All authorized Unified work, not one bound session.** Discover work, choose existing conversations across workspaces, and continue their real context. | Speak to two existing conversations in different workspaces and switch back. Each input and reply belongs to its original chat. A separate chatbot or permanent binding fails. |
| P2 | **Voice is the normal interface.** Capture intentional speech and speak the actual correlated Unified result. Distinguish accepted or ongoing work from completion. | Complete a real exchange without the main computer screen. Match input identity, root result, and audible reply. Speech need not be verbatim; unrelated narration, latest-message guesses, invented results, and silent truncation fail. |
| P3 | **Attention leads back to its owner.** A waiting question retains its workspace, conversation, and revision. Voice answers use supported authority. | Hear a real waiting question, answer it, then handle another one. Nothing is marked handled simply by reading it. No stale answer, reused answer, or fabricated approval. |
| P4 | **New workspace, new chat, actual work.** Request all three by voice. | Verify that a workspace exists at the intended location, a distinct chat belongs to it, and the requested work starts there. A folder, staged chat, saved task, or acceptance alone fails. Clarify ambiguous or consequential choices. |
| P5 | **Portable through Tailscale.** Reach the always-on host away from the private LAN with supported Wi-Fi, authenticated TLS, and retained device identity. | Speak and hear a correlated reply over another Wi-Fi network with no path through the home LAN. A tunnel probe, public exposure, or an extra VPN router is not a substitute. |
| P6 | **Clean, responsive pet experience.** Keep Tachikoma, one primary voice control, clear state and context, and useful Settings. No extra Stop button, transport counters, or diagnostic controls in everyday use. | Check touch and animation during voice and network activity. Ordinary speech to the chosen running conversation needs no transcript preview or Send tap. Consequential downstream actions still require applicable confirmation and host authority. |
| P7 | **Recover without ordinary power cycling.** A close, network loss, timeout, or cancellation releases media safely so the next interaction works. | Run repeated exchanges and interrupt before and after acceptance. No stale callbacks, retained admission, or growing resource use. Automatic reconnect is limited to mic-on and may start another paid call; disclose this. Never replay an uncertain input. |
| P8 | **Preserve authority, data, and honest outcomes.** Unified owns permissions and execution. Keep host/provider credentials host-side; retain Wi-Fi, device token, calibration, and device identity. Send one intentional request to one known destination at most once. Ending audio does not cancel accepted work. | Check rejected or stale commands, lost acknowledgements, and stop races. No duplicate effect, cross-context reply, credential disclosure, old NVS restoration, or false Done. Preserve uncertainty until reconciled. |
| P9 | **Ship usable increments.** Each release extends the previous working experience; verify what the recipient actually receives. | Name the exact variants, observed user journey, and unresolved limits. Source changed, built, installed, and physically demonstrated are distinct. A build or guide alone is not a verified install. |

## Setup and release boundaries

- **A teammate can set up their own pet.** Identify the exact board and matching
  release, deploy and pair the gateway on a supported stock Unified host,
  configure from a phone, check audio without provider spend, and hear a real
  reply. Private credentials and host settings are inputs, not unpublished
  product code. A source build is available today; the desired prebuilt path
  builds once per release, not once per recipient. Before claiming a supported
  download, verify the exact released bundle on a fresh matching board from
  flash through setup to a spoken reply.
- **An update preserves existing settings in place.** First install is a
  separate operation on an unprovisioned board. Use only an authorized,
  verified app-partition update on a provisioned pet, with a private rollback
  for that board. Reject an unexpected board/layout, incompatible storage,
  or a write outside the app partition. Do not require re-pairing, Wi-Fi entry,
  or restoration of saved NVS. Verify preservation without printing secrets.
- **One-at-a-time preview.** The [LAN source preview](team-prototype.md#start-here)
  serves one selected chat. While its reply is pending, follow-ups are refused,
  not queued or silently dropped. This is an increment, not a limit on the
  product. Versioned evidence and unresolved checks belong in the
  [implementation brief](../PROJECT-BRIEF.md#delivery-sequence) and release notes.

## Check and change

Check the actual delivered journey, including recovery and user-visible
failures. Record exact versions, observed behavior, and remaining uncertainty;
reuse relevant evidence when inputs are unchanged. Do not turn acceptance of
one increment into approval of the whole product. Record the agreed wording and
notify affected owners when a consequential promise changes. Ordinary
implementation choices within the agreement do not need a separate process.

**Left open:** wakewords, always-on capture, barge-in, notification and
quiet-hours policy, and numeric latency or battery targets. Media adaptation
and how to expose a native Unified overview remain implementation choices
within the [architecture boundaries](ARCHITECTURE.md).

Method: Microsoft Amplifier Converge's [shared direction](https://github.com/microsoft/amplifier-converge/blob/9c785283c6ef16782ad7847e3ef3ddc8ac7e6462/contracts/shared-direction-contract.md)
and [verification and delivery](https://github.com/microsoft/amplifier-converge/blob/9c785283c6ef16782ad7847e3ef3ddc8ac7e6462/contracts/verification-and-delivery-contract.md).