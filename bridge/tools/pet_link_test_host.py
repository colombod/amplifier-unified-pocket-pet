#!/usr/bin/env python3
"""Provider-free link test host for the Pocket pet (diagnostic only).

One process serves ONE leg to ONE admitted pet connection and then releases its
listener. It speaks only the fixed frames the pet needs to open, hold and end a
conversation (session.started, pet.converse.began, session.output_audio.delta,
pet.converse.notice time_limit, session.closed). It has no voice provider, no
Amplifier Unified connection and no outbound network use: outgoing connections
and name lookups are disabled in this process before the bridge is imported.

Admission is the gateway's own (imported unchanged from the pinned bridge
sources): TLS >= 1.2 from private files, the ordered handshake guard and the
stored SHA-256 token hashes. On top of it: a single allowed peer address, one
admission per process (a reopen is refused with the fixed 429 before any
upgrade), and a hard expiry.

Legs differ only in the audio sent to the pet from the moment the conversation
begins: none | silence (zeros, 100 ms cadence) | tone (440 Hz, peak 1000 of
32767, same cadence). Optional echo: for four seconds keep the pet speaker quiet
and temporarily buffer its input in RAM, then play that input back once to the
same pet. No audio is written to disk. No state frames are sent in any leg.

The record holds counts, timings and fixed codes only: no audio, no token, no
hash, no address, no certificate data. Standard output carries fixed words.
The ClientHello counter includes any peer that reached this listener; only the
completed TLS counter is scoped to the configured peer. A zero does not rule
out TCP reaching the host or a failure before OpenSSL processed ClientHello.

Modes (none is default; a mode must be named):
  loopback-test  127.0.0.1 only, explicit synthetic TLS files and token hash.
  lan            the existing gateway configuration through the gateway's own
                 loader, plus --confirm-bind IP:PORT and --pet-ip. RFC1918 only,
                 no tailnet listener.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import array
import asyncio
import base64
import contextlib
import dataclasses
import hashlib
import ipaddress
import json
import math
import os
import re
import signal
import socket
import struct
import time
import types
from pathlib import Path
from typing import Any

HOST_VERSION = "pet-link-test-host/2"
# Accept only the gateway sources from this reviewed checkout (or an exact copy).
# The actual content digest is retained in each record, not a private staging path.
PRODUCT_BRIDGE = Path(__file__).resolve().parents[1]
RATE = 24000
CHUNK_BYTES = 4800  # 100 ms of 24 kHz PCM16 mono
STEP_S = 0.1
TONE_HZ = 440
TONE_PEAK = 1000
ECHO_SECONDS = 4
LEGS = ("none", "silence", "tone", "echo")
MODES = ("loopback-test", "lan")
LOOPBACK = "127.0.0.1"
_RFC1918 = tuple(ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_TCP_INFO_FMT = "8B24I"
_MAX_SAMPLES = 200
_MAX_REOPEN_TIMES = 16
REFUSAL_CODES = frozenset({
    "args_refused", "bridge_mismatch", "bridge_import_failed", "record_refused", "token_file_refused",
    "tls_refused", "config_refused", "bind_not_lan", "bind_not_confirmed", "pet_ip_refused",
    "bind_failed", "bind_unverified",
})
_outbound_attempts = 0


class Refuse(Exception):
    def __init__(self, code: str) -> None:
        if code not in REFUSAL_CODES:
            raise ValueError("unknown refusal")
        super().__init__(code)
        self.code = code


# -- pure helpers (importable by the tests) --------------------------------------------
def tone_chunk() -> bytes:
    """100 ms of a 440 Hz sine, peak 1000: 44 whole cycles, so chunks join without a step."""
    count = CHUNK_BYTES // 2
    return struct.pack(
        f"<{count}h",
        *(int(round(TONE_PEAK * math.sin(2 * math.pi * TONE_HZ * i / RATE))) for i in range(count)),
    )


def leg_chunk(leg: str) -> bytes | None:
    if leg == "none":
        return None
    if leg == "silence":
        return bytes(CHUNK_BYTES)
    if leg == "tone":
        return tone_chunk()
    raise ValueError("unknown leg")


def is_rfc1918(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return str(address) == value and any(address in net for net in _RFC1918)


def bridge_digest(bridge_dir: Path) -> str:
    digest = hashlib.sha256()
    names = sorted(p.name for p in (bridge_dir / "pet_bridge").glob("*.py"))
    for name in names:
        inner = hashlib.sha256((bridge_dir / "pet_bridge" / name).read_bytes()).hexdigest()
        digest.update(name.encode() + b"\0" + inner.encode() + b"\n")
    return digest.hexdigest()


BRIDGE_PIN = bridge_digest(PRODUCT_BRIDGE)


def block_outbound() -> None:
    """This process may accept connections; it may never open one or resolve a name."""

    def refused(*_a: Any, **_k: Any) -> Any:
        global _outbound_attempts
        _outbound_attempts += 1
        raise OSError("outbound network use is disabled in the link test host")

    socket.socket.connect = refused  # type: ignore[method-assign]
    socket.socket.connect_ex = refused  # type: ignore[method-assign]
    socket.create_connection = refused  # type: ignore[assignment]
    socket.getaddrinfo = refused  # type: ignore[assignment]
    socket.gethostbyname = refused  # type: ignore[assignment]
    socket.gethostbyname_ex = refused  # type: ignore[assignment]


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="pet_link_test_host", allow_abbrev=False)
    p.add_argument("--bridge-dir", default=str(PRODUCT_BRIDGE))
    p.add_argument("--mode", required=True, choices=MODES)
    p.add_argument("--leg", required=True, choices=LEGS)
    p.add_argument("--record", required=True)
    p.add_argument("--wait-seconds", required=True, type=int)
    p.add_argument("--expiry-seconds", required=True, type=int)
    p.add_argument("--output-seconds", required=True, type=int)
    p.add_argument("--linger-seconds", required=True, type=int)
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--sample-tcp-info", action="store_true")
    p.add_argument("--port", type=int)
    p.add_argument("--tls-cert")
    p.add_argument("--tls-key")
    p.add_argument("--token-sha256-file")
    p.add_argument("--gateway-config")
    p.add_argument("--gateway-env-file")
    p.add_argument("--confirm-bind")
    p.add_argument("--pet-ip")
    return p.parse_args(argv)


def check_common(a: argparse.Namespace) -> None:
    ok = (
        1 <= a.wait_seconds <= 300
        and 5 <= a.expiry_seconds <= 120
        and 1 <= a.output_seconds <= a.expiry_seconds
        and 1 <= a.linger_seconds <= 15
        and os.path.isabs(a.bridge_dir)
        and os.path.isabs(a.record)
    )
    loop_only = (a.port, a.tls_cert, a.tls_key, a.token_sha256_file)
    if a.leg == "echo":
        ok = ok and a.output_seconds >= ECHO_SECONDS and a.expiry_seconds >= ECHO_SECONDS * 2 + 1
    lan_only = (a.gateway_config, a.gateway_env_file, a.confirm_bind, a.pet_ip)
    if a.mode == "loopback-test":
        ok = ok and all(v is not None for v in loop_only) and all(v is None for v in lan_only)
        ok = ok and a.port is not None and 1024 <= a.port <= 65535
    else:
        ok = ok and all(v is None for v in loop_only)
        ok = ok and (a.gateway_config is None) != (a.gateway_env_file is None)
        ok = ok and a.confirm_bind is not None and a.pet_ip is not None
    if not ok:
        raise Refuse("args_refused")


@dataclasses.dataclass
class Plan:
    cfg: Any
    bind_host: str
    port: int
    peer: str
    ssl_context: Any


def load_bridge(bridge_dir: str) -> tuple[Any, Any]:
    root = Path(bridge_dir)
    try:
        matches = bridge_digest(root) == BRIDGE_PIN
    except OSError:
        matches = False
    if not matches:
        raise Refuse("bridge_mismatch")
    # An alternate directory can corroborate the hash, but never supplies code.
    sys.path.insert(0, str(PRODUCT_BRIDGE))
    try:
        from pet_bridge import gateway_server as srv  # noqa: PLC0415
        from pet_bridge import gateway_state as gs  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - fixed refusal, nothing formatted
        raise Refuse("bridge_import_failed") from None
    return srv, gs


def plan_loopback(a: argparse.Namespace, srv: Any, gs: Any) -> Plan:
    try:
        raw = srv.check_private_file(Path(a.token_sha256_file), max_bytes=128, read=True) or b""
        text = raw.decode("ascii").removesuffix("\n")
    except (OSError, ValueError, UnicodeError):
        text = ""
    if _HASH_RE.fullmatch(text) is None:
        raise Refuse("token_file_refused")
    try:
        ctx = srv.build_server_ssl_context(Path(a.tls_cert), Path(a.tls_key))
    except Exception:  # noqa: BLE001
        raise Refuse("tls_refused") from None
    cfg = types.SimpleNamespace(
        host_header=f"{LOOPBACK}:{a.port}", bind_host=LOOPBACK, port=a.port,
        tailnet_bind_host=None, tailnet_fqdn=None, pet_token_sha256=(text,),
        model=gs.DEFAULT_MODEL, voice=gs.DEFAULT_VOICE,
    )
    return Plan(cfg, LOOPBACK, a.port, LOOPBACK, ctx)


def plan_lan(a: argparse.Namespace, srv: Any) -> Plan:
    try:
        if a.gateway_config is not None:
            cfg = srv.load_config(Path(a.gateway_config))
        else:
            cfg, inline = srv.load_env_config(Path(a.gateway_env_file))
            del inline  # the provider key, if the file holds one, is never used here
    except Exception:  # noqa: BLE001
        raise Refuse("config_refused") from None
    if not is_rfc1918(cfg.bind_host):
        raise Refuse("bind_not_lan")
    if a.confirm_bind != f"{cfg.bind_host}:{cfg.port}":
        raise Refuse("bind_not_confirmed")
    if not is_rfc1918(a.pet_ip) or a.pet_ip == cfg.bind_host:
        raise Refuse("pet_ip_refused")
    cfg = dataclasses.replace(
        cfg, tailnet_bind_host=None, tailnet_fqdn=None, provider_key_file=None, provider_key_env=None
    )
    try:
        ctx = srv.build_server_ssl_context(Path(cfg.tls_cert_file), Path(cfg.tls_key_file))
    except Exception:  # noqa: BLE001
        raise Refuse("tls_refused") from None
    return Plan(cfg, cfg.bind_host, cfg.port, a.pet_ip, ctx)


def open_record(path: str) -> int:
    try:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise Refuse("record_refused") from None


# -- the one leg -----------------------------------------------------------------------
class Leg:
    def __init__(self, a: argparse.Namespace, plan: Plan, srv: Any, gs: Any, stop: asyncio.Event) -> None:
        from aiohttp import WSMsgType  # noqa: PLC0415 - after the outbound guard

        self._types = WSMsgType
        self.a, self.plan, self.srv, self.gs, self.stop = a, plan, srv, gs, stop
        self.registry = srv.AuthRegistry()
        self.t0 = time.monotonic()
        self.listening_unix_ms = int(time.time() * 1000)
        self.admitted = False
        self.admitted_ev = asyncio.Event()
        self.finished_ev = asyncio.Event()
        self.connections = 0
        self.tls_client_hello_count = 0
        self.allowed_peer_tls_completed = 0
        self.refusals: dict[str, int] = {}
        self.reopen_ms: list[int] = []
        self.reopens = 0
        self.phase = "await_start"
        self.marks: dict[str, int | None] = {
            "admitted_ms": None, "start_ms": None, "began_ms": None, "stop_ms": None, "close_ms": None,
        }
        self.began_mono: float | None = None
        self.reason: str | None = None
        self.reason_ms: int | None = None
        self.ws_close_code: int | None = None
        self.peer_close_confirmed = False  # our orderly ending was answered by the peer's close
        self._end_done = False
        self.inp = {"frames": 0, "bytes": 0, "samples": 0, "nonzero_samples": 0,
                    "first_ms": None, "last_ms": None, "max_gap_ms": 0}
        self.inp_per_s: list[int] = []
        self.echo_pcm = bytearray()
        self.capture_echo = a.leg == "echo"
        self.out = {"frames": 0, "bytes": 0, "first_ms": None, "last_ms": None, "max_late_ms": 0}
        self.tcp: list[list[int]] = []

    def ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    def first(self, reason: str) -> None:
        if self.reason is None:
            self.reason, self.reason_ms = reason, self.ms()

    def _refuse(self, status: int, code: str) -> Any:
        self.refusals[code] = self.refusals.get(code, 0) + 1
        return self.srv.reject_response(self.srv.Rejection(status, code))

    async def handle(self, request: Any) -> Any:
        self.connections += 1
        try:
            if self.srv._peer_ip(request) != self.plan.peer:
                return self._refuse(404, "not_found")
            rejection = self.srv.check_handshake(request, self.plan.cfg, self.registry)
            if rejection is not None:
                return self._refuse(rejection.status, rejection.code)
            if self.admitted:
                self.reopens += 1
                if len(self.reopen_ms) < _MAX_REOPEN_TIMES:
                    self.reopen_ms.append(self.ms())
                return self._refuse(429, "rate_limited")
            ws = self.srv._new_ws()
            if not ws.can_prepare(request).ok:
                return self._refuse(400, "bad_request")
            self.admitted = True
            self.marks["admitted_ms"] = self.ms()
            self.admitted_ev.set()
        except Exception:  # noqa: BLE001 - fixed answer, nothing formatted
            return self._refuse(500, "internal_error")
        try:
            await ws.prepare(request)
            await self._session(ws, request.transport)
        except asyncio.CancelledError:
            self.first("operator_stop")
            raise
        except Exception:  # noqa: BLE001
            self.first("device_link_error")
        finally:
            if (not ws.closed or not self.peer_close_confirmed) and request.transport is not None:
                with contextlib.suppress(Exception):
                    request.transport.abort()
            self.finished_ev.set()
        return ws

    async def _session(self, ws: Any, transport: Any) -> None:
        reader = asyncio.ensure_future(self._read(ws))
        stopper = asyncio.ensure_future(self.stop.wait())
        self._pacer: asyncio.Future[Any] | None = None
        sampler = asyncio.ensure_future(self._sample(transport)) if self.a.sample_tcp_info else None
        self._transport = transport
        try:
            done, _ = await asyncio.wait(
                {reader, stopper}, timeout=self.a.expiry_seconds, return_when=asyncio.FIRST_COMPLETED
            )
            if reader not in done:
                expired = stopper not in done
                self.first("expiry" if expired else "operator_stop")
                # Leave receive() first: a close issued while another task is reading
                # would cut the socket without waiting for the peer's own close frame.
                reader.cancel()
                await asyncio.wait({reader})
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._end(ws, notice=expired), 2.0)
        finally:
            tasks = [t for t in (reader, stopper, self._pacer, sampler) if t is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.wait(tasks)
            self.echo_pcm.clear()
            self.capture_echo = False
            self.ws_close_code = ws.close_code
            self.peer_close_confirmed = self._end_done and ws.close_code not in (None, 1006)

    async def _end(self, ws: Any, *, notice: bool) -> None:
        if self.phase == "conversing" and notice:
            await ws.send_str(self.gs.build_converse_notice("time_limit"))
        await ws.send_str(self.gs.build_session_closed())
        await ws.close(code=1000)  # returns once the peer's close frame arrived, or raises
        self._end_done = True

    async def _fail(self, ws: Any, reason: str) -> None:
        self.first(reason)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(code=1002), 2.0)

    async def _read(self, ws: Any) -> None:
        kinds = self._types
        while True:
            try:
                msg = await ws.receive()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self.first("device_link_error")
                return
            if msg.type == kinds.CLOSE:
                self.first("device_ws_close_frame")
                return
            if msg.type in (kinds.CLOSING, kinds.CLOSED):
                self.first("device_eof_without_close")
                return
            if msg.type == kinds.ERROR:
                self.first("device_link_error")
                return
            if msg.type != kinds.TEXT:
                await self._fail(ws, "protocol_error")
                return
            try:
                frame = self.gs.parse_device_frame(
                    msg.data, model=self.plan.cfg.model, voice=self.plan.cfg.voice
                )
            except self.gs.GatewayProtocolError:
                await self._fail(ws, "protocol_error")
                return
            kind = frame.kind
            if kind == "audio" and self.phase == "conversing":
                self._count_input(frame)
            elif kind == "start" and self.phase == "await_start":
                self.marks["start_ms"] = self.ms()
                self.phase = "await_begin"
                await ws.send_str(self.gs.build_session_started())
            elif kind == "converse_begin" and self.phase == "await_begin":
                self.phase = "conversing"
                await ws.send_str(self.gs.build_converse_began("Link test", "00000000"))
                self.began_mono = time.monotonic()
                self.marks["began_ms"] = self.ms()
                self._pacer = asyncio.ensure_future(self._pace(ws))
            elif kind == "stop":
                if self.marks["stop_ms"] is None:
                    self.marks["stop_ms"] = self.ms()  # the pacer stops sending from here
            elif kind == "close":
                self.marks["close_ms"] = self.ms()
                self.first("device_stop" if self.marks["stop_ms"] is not None else "device_close_without_stop")
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._end(ws, notice=False), 2.0)
                return
            else:
                await self._fail(ws, "unexpected_frame" if kind in _KNOWN_KINDS else "protocol_error")
                return

    def _count_input(self, frame: Any) -> None:
        now = self.ms()
        pcm = base64.b64decode(frame.b64)
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) & ~1])
        inp = self.inp
        inp["frames"] += 1
        inp["bytes"] += len(pcm)
        inp["samples"] += len(samples)
        inp["nonzero_samples"] += sum(1 for s in samples if s)
        if (self.capture_echo and self.began_mono is not None and
                time.monotonic() - self.began_mono < ECHO_SECONDS):
            free = ECHO_SECONDS * RATE * 2 - len(self.echo_pcm)
            self.echo_pcm.extend(pcm[:free])
        del pcm, samples
        if inp["first_ms"] is None:
            inp["first_ms"] = now
        else:
            inp["max_gap_ms"] = max(inp["max_gap_ms"], now - inp["last_ms"])
        inp["last_ms"] = now
        assert self.began_mono is not None
        second = int(time.monotonic() - self.began_mono)
        if 0 <= second <= self.a.expiry_seconds:
            while len(self.inp_per_s) <= second:
                self.inp_per_s.append(0)
            self.inp_per_s[second] += frame.pcm_len

    async def _pace(self, ws: Any) -> None:
        assert self.began_mono is not None
        start = self.began_mono
        if self.a.leg == "echo":
            # No simultaneous capture/playback or feedback. Never synthesize a
            # response if no mic samples arrived. Mute/close still takes priority.
            start += ECHO_SECONDS
            await asyncio.sleep(max(0, start - time.monotonic()))
            self.capture_echo = False
            chunks = (self.echo_pcm[i:i + CHUNK_BYTES]
                      for i in range(0, len(self.echo_pcm), CHUNK_BYTES))
        else:
            chunk = leg_chunk(self.a.leg)
            if chunk is None:
                return
            chunks = (chunk for _ in range(self.a.output_seconds * 10))
        for index, piece in enumerate(chunks):
            delay = start + index * STEP_S - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if self.marks["stop_ms"] is not None or self.reason is not None:
                return
            late = int((time.monotonic() - (start + index * STEP_S)) * 1000)
            try:
                await ws.send_str(self.gs.build_audio_delta(bytes(piece)))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self.first("host_write_failed")
                with contextlib.suppress(Exception):
                    self._transport.abort()  # ends the reader; the reason above stays first
                return
            now = self.ms()
            out = self.out
            out["frames"] += 1
            out["bytes"] += len(piece)
            out["max_late_ms"] = max(out["max_late_ms"], late)
            if out["first_ms"] is None:
                out["first_ms"] = now
            out["last_ms"] = now

    async def _sample(self, transport: Any) -> None:
        """Once a second, the kernel's own counters for this one connection. No payload."""
        size = struct.calcsize(_TCP_INFO_FMT)
        sock = transport.get_extra_info("socket") if transport is not None else None
        while sock is not None and len(self.tcp) < _MAX_SAMPLES:
            try:
                raw = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, size)
            except OSError:
                return
            if len(raw) < size:
                return
            f = struct.unpack(_TCP_INFO_FMT, raw[:size])
            u32 = f[8:]
            # ms, unacked segments, total retransmits, ms since last data received,
            # ms since last ack received, retransmit backoff count, retransmit timeout ms
            self.tcp.append([self.ms(), u32[4], u32[23], u32[11], u32[12], f[4], u32[0] // 1000])
            await asyncio.sleep(1.0)

    def record(self, outcome: str) -> dict[str, Any]:
        return {
            "host": HOST_VERSION, "bridge_pin": BRIDGE_PIN, "mode": self.a.mode, "leg": self.a.leg,
            "limits": {"wait_s": self.a.wait_seconds, "expiry_s": self.a.expiry_seconds,
                       "output_s": self.a.output_seconds, "linger_s": self.a.linger_seconds},
            "listening_unix_ms": self.listening_unix_ms, "times_are": "ms since listening",
            "outcome": outcome, "connections_seen": self.connections, "admissions": int(self.admitted),
            "tls_client_hello_count": self.tls_client_hello_count,
            "allowed_peer_tls_completed": self.allowed_peer_tls_completed,
            "refusals": dict(sorted(self.refusals.items())),
            "reopens_refused": self.reopens, "reopen_refused_ms": self.reopen_ms,
            "marks": self.marks, "first_close_reason": self.reason, "first_close_ms": self.reason_ms,
            "ws_close_code": self.ws_close_code, "peer_close_confirmed": self.peer_close_confirmed,
            "input": self.inp, "input_bytes_per_second_since_began": self.inp_per_s,
            "output_written": self.out,
            "tcp_info_samples": self.tcp,
            "tcp_info_fields": ["ms", "unacked_segments", "total_retransmits", "ms_since_data_received",
                                "ms_since_ack_received", "backoff", "rto_ms"],
            "outbound_attempts": _outbound_attempts,
        }


_KNOWN_KINDS = frozenset({"start", "greeting", "audio", "begin", "converse_begin", "capture_stop",
                          "send_confirm", "stop", "close"})


async def _first_of(timeout: float, *events: asyncio.Event) -> None:
    waits = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in waits:
            task.cancel()
        for task in waits:
            with contextlib.suppress(BaseException):
                await task


async def serve(a: argparse.Namespace, plan: Plan, srv: Any, gs: Any, record_fd: int) -> None:
    from aiohttp import web  # noqa: PLC0415 - after the outbound guard

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    leg = Leg(a, plan, srv, gs, stop)

    if plan.ssl_context.sni_callback is not None:
        raise Refuse("tls_refused")

    def on_client_hello(_ssl_object: Any, _name: str | None, _context: Any) -> None:
        # Runs before the HTTP handler, even for clients without SNI. Do not
        # retain the name or alter the certificate or the handshake outcome.
        leg.tls_client_hello_count += 1

    class CountedServer(web.Server):
        def connection_made(self, handler: Any, transport: Any) -> None:
            super().connection_made(handler, transport)
            peer = transport.get_extra_info("peername")
            if (isinstance(peer, tuple) and peer and peer[0] == plan.peer
                    and transport.get_extra_info("ssl_object") is not None):
                leg.allowed_peer_tls_completed += 1

    plan.ssl_context.sni_callback = on_client_hello
    runner = web.ServerRunner(
        CountedServer(leg.handle, access_log=None, logger=srv._NullLogger()), shutdown_timeout=2.0
    )
    await runner.setup()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server = None
    try:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((plan.bind_host, plan.port))
        except OSError:
            raise Refuse("bind_failed") from None
        if sock.getsockname() != (plan.bind_host, plan.port):
            raise Refuse("bind_unverified")
        server = await loop.create_server(
            runner.server, sock=sock, ssl=plan.ssl_context, backlog=8, start_serving=False
        )
        if [s.getsockname() for s in server.sockets] != [(plan.bind_host, plan.port)]:
            raise Refuse("bind_unverified")
        await server.start_serving()
        print("listening", flush=True)
        await _first_of(a.wait_seconds, leg.admitted_ev, stop)
        if leg.admitted:
            print("admitted", flush=True)
            await _first_of(a.expiry_seconds + 8, leg.finished_ev)
            outcome = "leg_ended"
            print("ended " + (leg.reason or "unknown"), flush=True)
            await _first_of(a.linger_seconds, stop)  # reopens get the fixed refusal meanwhile
        else:
            outcome = "operator_stop" if stop.is_set() else "no_admission"
            leg.first(outcome)
            print("ended " + outcome, flush=True)
    finally:
        if server is not None:
            server.close()
        sock.close()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(runner.cleanup(), 5.0)
        plan.ssl_context.sni_callback = None
    os.write(record_fd, (json.dumps(leg.record(outcome), indent=1, sort_keys=True) + "\n").encode())
    print("record_written", flush=True)
    print("released", flush=True)


def main(argv: list[str]) -> int:
    a = parse_args(argv)
    record_fd = -1
    try:
        check_common(a)
        block_outbound()
        srv, gs = load_bridge(a.bridge_dir)
        plan = plan_loopback(a, srv, gs) if a.mode == "loopback-test" else plan_lan(a, srv)
        if a.check_only:
            print("check_ok", flush=True)
            return 0
        record_fd = open_record(a.record)
        asyncio.run(serve(a, plan, srv, gs, record_fd))
        return 0
    except Refuse as refusal:
        if record_fd >= 0:  # created by this run and still empty: leave nothing behind
            with contextlib.suppress(OSError):
                os.unlink(a.record)
        print("refused " + refusal.code, file=sys.stderr, flush=True)
        return 3
    finally:
        if record_fd >= 0:
            os.close(record_fd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
