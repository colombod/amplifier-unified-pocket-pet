#!/usr/bin/env python3
"""Scripted host for the link rig: a TLS WebSocket peer speaking the pet's
conversation wire protocol with the gateway's framing (one write per message,
4800-byte PCM replies no faster than real time). It is NOT the gateway: no
provider, no Unified, no transcript logic. It records what the device sent and
when, and checks that the microphone stream arrived complete and in order.
Standard library only."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import json
import signal
import ssl
import struct
import sys
import time

BYTES_PER_S = 48000
CHUNK = 4800
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TOKEN = "rigTOKENabcdefghijklmnopqrstuvwxyz012345678"
BEGAN = '{"type":"pet.converse.began","safe_session_label":"Link rig","session_id_short":"a1b2c3d4"}'


def state(name: str) -> str:
    return json.dumps({"type": "pet.converse.state", "state": name}, separators=(",", ":"))


REPLY_PCM = struct.pack("<2h", 8000, -8000) * (CHUNK // 4)  # audible on the device
REPLY = json.dumps(
    {"type": "session.output_audio.delta", "delta": base64.b64encode(REPLY_PCM).decode()},
    separators=(",", ":"),
)


class Call:
    """One accepted connection."""

    def __init__(self, number: int, origin: float) -> None:
        self.number = number
        self.origin = origin  # monotonic time of the FIRST call's began: the scenario clock
        self.began_at: float | None = None
        self.frames: list[tuple[float, int]] = []  # (seconds since this call's began, bytes)
        self.mic_bytes = 0
        self.zero_samples = 0
        self.voice_samples = 0
        self.next_pair: int | None = None
        self.gap_samples = 0
        self.order_errors = 0
        self.first_audio: float | None = None
        self.max_late = 0.0
        self.late_at = 0.0
        self.late_by_second: dict[int, float] = {}
        self.stop_seen = False
        self.close_seen = False
        self.end = "open"
        self.replies_sent = 0
        self.reply_send_max = 0.0

    def audio(self, now: float, pcm: bytes) -> None:
        if self.first_audio is None:
            self.first_audio = now
        # Lateness of this frame against a real-time stream that started with the
        # first frame: 0 while the device keeps up, the device's backlog otherwise.
        late = (now - self.first_audio) - self.mic_bytes / BYTES_PER_S
        second = int(now - (self.began_at or now))
        self.late_by_second[second] = max(self.late_by_second.get(second, 0.0), late)
        if late > self.max_late:
            self.max_late, self.late_at = late, now - (self.began_at or now)
        if len(pcm) % 4:
            self.order_errors += 1
            return
        for low, high in struct.iter_unpack("<HH", pcm):
            if low == high == 0:
                self.zero_samples += 2
                if self.next_pair is not None:
                    self.next_pair += 1
                continue
            self.voice_samples += 2
            if not low or not high:
                self.order_errors += 1
                continue
            pair = low - 1 + (high - 1) * 65535
            if self.next_pair is not None and pair < self.next_pair:
                self.order_errors += 1
                continue  # never move the frontier backwards on replay
            if self.next_pair is not None:
                gap = pair - self.next_pair
                if gap % 240:  # capture drops only whole 960-byte chunks
                    self.order_errors += 1
                self.gap_samples += gap * 2
            self.next_pair = pair + 1
        self.frames.append((now - (self.began_at or now), len(pcm)))
        self.mic_bytes += len(pcm)

    def summary(self) -> dict:
        span = self.frames[-1][0] - self.frames[0][0] if len(self.frames) > 1 else 0.0
        return {
            "call": self.number,
            "began": self.began_at is not None,
            "mic_bytes": self.mic_bytes,
            "mic_seconds": round(self.mic_bytes / BYTES_PER_S, 3),
            "mic_span_seconds": round(span, 3),
            "mic_voice_samples": self.voice_samples,
            "mic_silence_samples": self.zero_samples,
            "mic_order_errors": self.order_errors,
            "mic_gap_samples": self.gap_samples,
            "mic_max_late_seconds": round(self.max_late, 3),
            "mic_max_late_at_seconds": round(self.late_at, 3),
            "mic_late_by_second": {str(k): round(v, 2) for k, v in sorted(self.late_by_second.items())},
            # Arrival trace [milliseconds since began, PCM bytes]: input for gateway timing checks.
            "mic_frames": [[round(t * 1000), n] for t, n in self.frames],
            "replies_sent": self.replies_sent,
            "reply_send_max_seconds": round(self.reply_send_max, 3),
            "pet_stop_seen": self.stop_seen,
            "session_close_seen": self.close_seen,
            "end": self.end,
        }


class Host:
    def __init__(self, scenario: dict, result_path: str) -> None:
        self.scenario = scenario
        self.result_path = result_path
        self.calls: list[Call] = []
        self.origin: float | None = None
        self.max_calls = int(scenario.get("accept_calls", 1))

    def clock(self) -> float:
        return time.monotonic() - (self.origin or time.monotonic())

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        call = Call(len(self.calls) + 1, 0.0)
        self.calls.append(call)
        try:
            await self.handle(call, reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, OSError) as err:
            if call.end == "open":
                call.end = f"lost:{type(err).__name__}"
        finally:
            if call.end == "open":
                call.end = "ended"
            writer.close()
            self.write_result()

    async def handle(self, call: Call, reader, writer) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        lines = request.decode("latin-1").split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
        if not lines[0].startswith("GET /v1/pet/live ") or headers.get("authorization") != f"Bearer {TOKEN}":
            writer.write(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            call.end = "refused:401"
            return
        if call.number > self.max_calls:
            writer.write(b"HTTP/1.1 409 Conflict\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            call.end = "refused:409"
            return
        accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + GUID).encode()).digest()).decode()
        writer.write(
            f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
        )
        await writer.drain()
        lock = asyncio.Lock()

        async def send(text: str) -> None:
            data = text.encode()
            head = bytes([0x81]) + (
                bytes([len(data)]) if len(data) < 126 else b"\x7e" + struct.pack(">H", len(data))
            )
            async with lock:
                writer.write(head + data)  # one write, as the gateway's WebSocket library does
                await writer.drain()

        speaker: asyncio.Task | None = None
        message = b""
        while True:
            head = await reader.readexactly(2)
            opcode, length = head[0] & 0x0F, head[1] & 0x7F
            fin = bool(head[0] & 0x80)
            if length == 126:
                (length,) = struct.unpack(">H", await reader.readexactly(2))
            elif length == 127:
                (length,) = struct.unpack(">Q", await reader.readexactly(8))
            mask = await reader.readexactly(4) if head[1] & 0x80 else b"\0\0\0\0"
            payload = await reader.readexactly(length)
            payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload)) if length < 256 else _unmask(payload, mask)
            if opcode == 8:
                call.end = "ws_close"
                async with lock:
                    writer.write(bytes([0x88, 2]) + struct.pack(">H", 1000))
                    await writer.drain()
                return
            if opcode == 9:
                async with lock:
                    writer.write(bytes([0x8A, len(payload)]) + payload)
                    await writer.drain()
                continue
            if opcode == 10:
                continue
            message += payload
            if not fin:
                continue
            text, message = message, b""
            event = json.loads(text)
            kind = event.get("type")
            if kind == "session.start":
                await send('{"type":"session.started"}')
            elif kind == "pet.converse.begin":
                if self.origin is None:
                    self.origin = time.monotonic()
                call.began_at = time.monotonic()
                await send(BEGAN)
                await send(state("listening"))
                speaker = asyncio.ensure_future(self.speak(call, send))
            elif kind == "session.input_audio.append":
                call.audio(time.monotonic(), base64.b64decode(event["audio"], validate=True))
            elif kind == "pet.stop":
                call.stop_seen = True
            elif kind == "session.close":
                call.close_seen = True
                if speaker:
                    speaker.cancel()
                await send('{"type":"session.closed"}')
                async with lock:
                    writer.write(bytes([0x88, 2]) + struct.pack(">H", 1000))
                    await writer.drain()
                call.end = "closed_on_request"
                return

    async def speak(self, call: Call, send) -> None:
        """Answers at scripted times on the scenario clock (first call's began)."""
        for answer in self.scenario.get("answers", []):
            start, seconds = answer["at_ms"] / 1000, answer["ms"] / 1000
            if self.clock() > start + seconds:
                continue  # belonged to an earlier connection
            await asyncio.sleep(max(0.0, start - 0.4 - self.clock()))
            await send(state("asking"))
            await asyncio.sleep(max(0.0, start - self.clock()))
            await send(state("answering"))
            next_at = time.monotonic()
            for _ in range(int(seconds * BYTES_PER_S / CHUNK)):
                await asyncio.sleep(max(0.0, next_at - time.monotonic()))
                began = time.monotonic()
                await send(REPLY)
                call.reply_send_max = max(call.reply_send_max, time.monotonic() - began)
                call.replies_sent += 1
                next_at = max(next_at, began) + CHUNK / BYTES_PER_S  # never faster than real time
            await send(state("listening"))

    def write_result(self) -> None:
        with open(self.result_path, "w") as out:
            json.dump({"calls": [c.summary() for c in self.calls]}, out, indent=2)
            out.write("\n")


def _unmask(payload: bytes, mask: bytes) -> bytes:
    full = (mask * (len(payload) // 4 + 1))[: len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(full, "big")).to_bytes(len(payload), "big")


async def main(argv: list[str]) -> None:
    cert, key, scenario_json, result_path = argv
    host = Host(json.loads(scenario_json), result_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    server = await asyncio.start_server(host.serve, "192.168.77.1", 8941, ssl=context)
    def finish() -> None:  # the runner ends the host; record connections still open
        host.write_result()
        os._exit(0)

    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, finish)
    print("HOST ready", flush=True)
    async with server:
        await server.serve_forever()


def self_test() -> None:
    def pcm(*positions):
        return b"".join(struct.pack("<HH", 1 + p % 65535, 1 + p // 65535) for p in positions)
    call = Call(1, 0)
    call.audio(1, pcm(65534, 65535, 65536))  # digit carry is not a gap
    assert call.order_errors == call.gap_samples == 0
    call.audio(2, pcm(70337, 70338))  # 4800-pair gap: longer than the old cycle
    assert call.order_errors == 0 and call.gap_samples == 9600
    call.audio(3, pcm(70338))  # duplicate
    assert call.order_errors == 1 and call.next_pair == 70339
    call.audio(4, pcm(65535))  # reordered old chunk
    assert call.order_errors == 2 and call.next_pair == 70339
    call.audio(5, b"\0" * 8 + pcm(70341))  # gated silence advances the frontier
    assert call.order_errors == 2 and call.next_pair == 70342
    call.audio(6, b"\1\0\0\0")  # half-silence / misaligned digits are invalid
    assert call.order_errors == 3
    call.audio(7, b"\1\0")  # partial pair invalid
    assert call.order_errors == 4
    call.audio(8, pcm(70343))  # forward gap not a whole capture chunk
    assert call.order_errors == 5
    print("PASS rig microphone oracle: 8 gap/replay/order/silence/alignment checks")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    else:
        asyncio.run(main(sys.argv[1:]))
