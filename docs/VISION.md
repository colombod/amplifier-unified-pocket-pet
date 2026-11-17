# Pocket Assistant — product vision

**Direction:** the user's current product request, restated on 14 November 2026.
This is the destination, not a claim of delivery. Exact commitments and their
agreement status live in the [delivery contract](../contracts/pocket-assistant-delivery.v1.md).

The pocket pet is the user's portable voice connection to **all their authorized
Amplifier Unified work**. Away from the computer, the user can ask what is
happening, hear what needs their attention, answer a waiting question, continue
an existing conversation, or start a new chat in a new workspace. Unified does
the work and the pet speaks back. It is not a separate chatbot and is not
permanently bound to one conversation.

The experience is simple: an animated Tachikoma, one understandable voice
control, a clear indication of connection and current context, and useful
Settings. Voice carries the conversation; touch helps choose context. The user
is not required to operate a diagnostic console, read long transcript previews,
run speaker/microphone tests, or power-cycle the device to make another call.
A failure is explained honestly and recovery does not duplicate an uncertain
request. Ending audio does not secretly cancel work already accepted by Unified.

The device reaches the always-on Unified server through **Tailscale**, including
from supported Wi-Fi away from the private home network. The same interaction
works there; there is no separate LAN-only product and no requirement to expose
Unified publicly. Portability includes usable setup, repeat use, reconnect and
power behavior, not merely a successful tunnel probe.

The documented Waveshare ESP32-S3-Touch-AMOLED-1.8 V2, its existing artwork,
working audio/display, phone setup and saved connectivity are our starting
point. Unified remains the owner of conversations, context, execution,
permissions and waiting questions. A hardware adapter carries media and
necessary authenticated client traffic; it must not become a second manager,
chat history store or orchestration system.

We reach the destination through **small working increments**. First make the
pet a clean, reusable voice connection to a real Unified conversation. Then let
the user choose and switch work across workspaces, manage attention and start
new work, and complete reliable away-from-home operation. Tailscale constraints
shape the first increment; the one-conversation demonstration does not define
the final architecture. Every increment extends the working experience instead
of replacing it with another experiment.

## How delivery is recognized

- Speak into two existing conversations in different workspaces, switch back,
  and hear replies belonging to the right original context.
- Ask what needs attention and answer the intended source-owned waiting question.
- Ask for a new workspace and a new chat there; see that the requested work
  actually starts and hear its result or honest ongoing status.
- Use those flows on another supported Wi-Fi network over Tailscale, without the
  private LAN path, while animation and touch remain responsive.
- Complete repeated interactions and recover from an ordinary interruption
  without rebooting, losing settings, replaying input or claiming false success.

## Boundaries and ownership

The user owns product direction, consequential choices and explicit limits.
The delivery manager owns design, sequencing, implementation, ordinary repairs,
integration and evidence within that authority. Routine work is not returned to
the user for supervision. No wakeword, always-on recording, offline intelligence,
numeric battery-life promise or new spending/deployment permission is implied.
Those remain separate choices where genuinely needed.

This follows the concise narrative-vision convention of
[Microsoft Amplifier Converge](https://github.com/microsoft/amplifier-converge/tree/e39fe7676ba88737319bca21af78dd0f819eb8a3).
The [original quotations](intent.md) remain preserved. This revision replaces the
older draft's open transport choice: the user now explicitly requires Tailscale.
