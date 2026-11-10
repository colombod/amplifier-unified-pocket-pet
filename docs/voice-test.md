# First GPT-Live device test

Firmware: **0.3.3-amoled-v2-live**. This is an isolated device audio/network test,
not Amplifier Unified integration. The voice model is **gpt-live-1**, voice Marin;
managed Responses delegation uses **gpt-6-luna** separately for reasoning, with
no tools or Unified access. It does not silently fall back to legacy Realtime or
another voice model. The initial 0.3.0 trial reached session.started and received output audio, but overflowed its smaller playback buffer. The 0.3.2 trial returned digital silence and exposed microphone gating and pacing bugs. Independent audio workers and audible-speech gating in 0.3.3 remain to be checked.

## Run on the device

1. Confirm Wi-Fi setup is saved. Set **Audio / volume** to a comfortable level,
   such as the approximately 70% that worked in local tests; return home.
2. Tap the round **microphone** (**Start voice**). Only this action starts clock synchronization, authentication
   and voice capture. Wait for Listening, then say “Hello Tachikoma” and a short
   question. Animation and Stop should remain responsive.
3. Listen for a coherent reply and ask a second question in the same session.
4. Tap the square **Stop**. The microphone is released; the device waits up to 15 seconds
   for `session.closed`. An unconfirmed finalization is shown as such.

The user requested that conversation stay open after Talk until Stop or a
connection/error condition. There is no application three-minute limit. Talk starts
a fresh conversation; there is no persisted/resumed transcript or automatic retry.
Input becomes silence while audible speaker output plays and for a 300 ms tail, to prevent
self-echo without claiming AEC. Wait for Listening before speaking again. This
tradeoff prevents barge-in in this test and is not a final interaction policy.

If an error appears, retain the exact displayed error/code and firmware version,
without sharing the API key or network password. HTTP rejection may mean key,
quota or model-access restrictions; no successful authentication is inferred from
having saved a key. The device never prints server event bodies or transcripts.

## Protocol and resource behavior

- TLS checks the server against ESP-IDF's CA bundle and hostname; SNTP sets the
  clock for certificate validity. No certificate-verification bypass is used.
- Primary endpoint: `wss://api.openai.com/v1/live/sessions`, bearer authentication
  from device-local NVS, no query parameters. Temporary credential buffers are wiped.
- `session.start` is first; PCM is sent only after `session.started`.
- Input `session.input_audio.append` and output `session.output_audio.delta`
  contain Base64 PCM16 little-endian mono at 24 kHz, without WAV headers.
- WebSocket JSON assembly is capped at 96 KiB and handles TCP splits and RFC6455
  continuation frames. A 1 MiB playback buffer and a two-second capture queue are in PSRAM; separate capture/playback workers keep hardware pacing independent of network writes. Overflow, malformed
  audio or lost connections stop the test with an error rather than unbounded buffering.
- `store:false`; application audio buffers are wiped/freed. No audio file or
  transcript is persisted. This does not make a claim about account-level OpenAI
  data retention; device storage and API policies are separate.
- One codec owner prevents concurrent local audio tests and voice use. Volume
  uses the same saved setting. Setup and audio-test entry are disabled during voice.
- Logs include state, finalization confirmation and aggregate free-memory values,
  never credentials or transcript/audio bodies. Actual memory values are not
  measured until a live session starts.

The current personal BYOK prototype keeps a project key on the device in standard
NVS. OpenAI's guidance recommends trusted server-side key handling. A production
or multi-user design needs a scoped broker/enrollment design; the saved key is
never served to the phone or retrieved to the Mac for API testing.

## Official references checked 10 November 2026

[WebSocket guide](https://developers.openai.com/api/docs/guides/voice-websockets),
[primary Live WebSocket reference](https://developers.openai.com/api/reference/resources/live/primary-websocket),
[Responses delegation](https://developers.openai.com/api/docs/guides/live-delegation),
[session lifecycle](https://developers.openai.com/api/docs/guides/live-conversations).
The checked documentation specifies a distinct Live wire protocol. Native parser
and build checks are necessary but do not prove endpoint availability for this key
or an audible response. No live API call is made by the automated checks.
