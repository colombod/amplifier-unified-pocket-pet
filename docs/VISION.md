# Pocket Assistant — product vision

This is the product destination, not a claim of delivery. Observable promises
and checks live in the [delivery contract](CONTRACT.md); the
[architecture](ARCHITECTURE.md) explains how the parts fit together.

The pocket pet is the user's portable voice connection to **all their authorized
Amplifier Unified work**. Away from the computer, the user can ask what is
happening, hear what needs their attention, answer a waiting question, continue
an existing conversation, or start a new chat in a new workspace. Unified does
the work and the pet speaks back. It is not a separate chatbot and is not
permanently bound to one conversation.

The experience is simple: an animated Amplifier Assistant, one understandable voice
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

A teammate should be able to identify the board, download matching firmware
without compiling it, set up their own gateway, and go from phone setup to a
spoken reply. An update should leave the pet they already set up intact.

## Boundaries and ownership

The user owns product direction, consequential choices and explicit limits.
The delivery manager owns design, sequencing, implementation, ordinary repairs,
integration and evidence within that authority. Routine work is not returned to
the user for supervision. The owner can opt into a configurable local wake phrase. Manual microphone control and touch-to-talk remain available. The conversation and pending-question behavior is defined in the [delivery contract](CONTRACT.md). Battery-life targets, offline intelligence and new spending remain separate decisions.

This follows the concise narrative-vision convention of
[Microsoft Amplifier Converge](https://github.com/microsoft/amplifier-converge/blob/9c785283c6ef16782ad7847e3ef3ddc8ac7e6462/docs/writing-guide.md).
The [original quotations](intent.md) remain preserved. This revision replaces the
older draft's open transport choice: the user now explicitly requires Tailscale.
