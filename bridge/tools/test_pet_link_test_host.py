#!/usr/bin/env python3
"""Loopback tests of pet_link_test_host.py. Everything is synthetic: a TLS
certificate, key and bearer token made for this run in a private temporary
folder, PCM made here, and 127.0.0.1 only. The host runs as its real command
line in a subprocess. No production file, address or credential is read.

usage: python3 -B test_pet_link_test_host.py -v
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import asyncio
import base64
import datetime
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import ssl
import stat
import struct
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import aiohttp
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

HERE = Path(__file__).resolve().parent
HOST = HERE / "pet_link_test_host.py"
BRIDGE = HERE.parent
HEADER = BRIDGE.parent / "apps/unified-pet/main/assistant_config.h"
BEGIN = '{"type":"pet.converse.begin"}'
STOP = '{"type":"pet.stop"}'
CLOSE = '{"type":"session.close"}'
VOICED = struct.pack("<2400h", *((i % 1000) + 1 for i in range(2400)))  # 100 ms, no zero sample
SILENT = bytes(4800)
STDOUT_LINE = re.compile(rb"(listening|admitted|ended [a-z_]+|record_written|released|check_ok)")
RECORD_KEYS = {
    "host", "bridge_pin", "mode", "leg", "limits", "listening_unix_ms", "times_are", "outcome",
    "connections_seen", "admissions", "refusals", "reopens_refused", "reopen_refused_ms", "marks",
    "first_close_reason", "first_close_ms", "ws_close_code", "peer_close_confirmed", "input",
    "input_bytes_per_second_since_began", "output_written", "tcp_info_samples", "tcp_info_fields",
    "outbound_attempts", "tls_client_hello_count", "allowed_peer_tls_completed",
}


def mic_frame(pcm: bytes) -> str:
    return json.dumps({"type": "session.input_audio.append", "audio": base64.b64encode(pcm).decode()},
                      separators=(",", ":"))


def write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, mode)


def firmware_start_frame() -> str:
    """The pet's own session.start text, evaluated from the firmware header's macros."""
    text = HEADER.read_text().replace("\\\n", " ")
    macros = dict(re.findall(r"^#define\s+([A-Z_]+)\s+(.*)$", text, flags=re.M))

    def value(name: str) -> str:
        out = []
        for literal, ident in re.findall(r'"((?:[^"\\]|\\.)*)"|([A-Za-z_][A-Za-z0-9_]*)', macros[name]):
            out.append(value(ident) if ident else literal.replace('\\"', '"').replace("\\\\", "\\"))
        return "".join(out)

    frame = value("PET_LIVE_START")
    assert json.loads(frame)["type"] == "session.start"
    return frame


class Fixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="linktest-")
        self.dir = Path(self.tmp.name)
        os.chmod(self.dir, 0o700)
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic link test")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        self.cert, self.key = self.dir / "cert.pem", self.dir / "key.pem"
        self.cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        write_private(self.cert, self.cert_pem)
        write_private(self.key, key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        self.token = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        self.other_token = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        self.token_hash = hashlib.sha256(self.token.encode()).hexdigest()
        self.hash_file = self.dir / "token.sha256"
        write_private(self.hash_file, (self.token_hash + "\n").encode())
        self.start = firmware_start_frame()
        self.count = 0

    def record_path(self) -> Path:
        self.count += 1
        return self.dir / f"record-{self.count}.json"

    def client_ssl(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.cert))


F: Fixture


def setUpModule() -> None:
    global F, SOURCE_BEFORE
    SOURCE_BEFORE = {p.name: hashlib.sha256(p.read_bytes()).digest()
                     for p in (BRIDGE / "pet_bridge").glob("*.py")}
    F = Fixture()


def tearDownModule() -> None:
    F.tmp.cleanup()
    if SOURCE_BEFORE != {p.name: hashlib.sha256(p.read_bytes()).digest()
                         for p in (BRIDGE / "pet_bridge").glob("*.py")}:
        raise AssertionError("loopback test changed product source")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def loop_args(leg: str, port: int, record: Path, *, wait=10, expiry=6, output=2, linger=1, **over) -> list[str]:
    values = {
        "--bridge-dir": str(BRIDGE), "--mode": "loopback-test", "--leg": leg, "--record": str(record),
        "--wait-seconds": str(wait), "--expiry-seconds": str(expiry), "--output-seconds": str(output),
        "--linger-seconds": str(linger), "--port": str(port), "--tls-cert": str(F.cert),
        "--tls-key": str(F.key), "--token-sha256-file": str(F.hash_file),
    }
    for name, value in over.items():
        key = "--" + name.replace("_", "-")
        if value is None:
            values.pop(key, None)
        else:
            values[key] = str(value)
    return [item for pair in values.items() for item in pair]


def run_host(args: list[str]) -> tuple[int, bytes, bytes]:
    done = subprocess.run([sys.executable, "-B", str(HOST), *args], capture_output=True, timeout=30)
    return done.returncode, done.stdout, done.stderr


class HostProc:
    def __init__(self, leg: str = "none", *, flags: tuple[str, ...] = (), **kw) -> None:
        self.port = free_port()
        self.record_file = F.record_path()
        self.args = loop_args(leg, self.port, self.record_file, **kw) + list(flags)

    async def start(self) -> "HostProc":
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-B", str(HOST), *self.args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        line = await asyncio.wait_for(self.proc.stdout.readline(), 20)
        if line != b"listening\n":
            raise AssertionError((line, await self.proc.stderr.read()))
        return self

    async def finish(self, timeout: float = 20) -> dict:
        out, err = await asyncio.wait_for(self.proc.communicate(), timeout)
        self.stdout, self.stderr, self.code = b"listening\n" + out, err, self.proc.returncode
        self.record_text = self.record_file.read_text()
        return json.loads(self.record_text)

    async def kill(self) -> None:
        if self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()

    def url(self, path: str = "/v1/pet/live") -> str:
        return f"https://127.0.0.1:{self.port}{path}"


def session(local: str | None = None) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(
        ssl=F.client_ssl(), force_close=True, local_addr=(local, 0) if local else None)
    return aiohttp.ClientSession(connector=connector)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + token}


class Pet:
    """A scripted stand-in for the pet's side of the conversation."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws
        self.got: list[tuple[float, dict]] = []
        self.sent_stop_at: float | None = None

    async def open(self, read: bool = True) -> "Pet":
        await self.ws.send_str(F.start)
        assert (await self.ws.receive_json(timeout=5)) == {"type": "session.started"}
        await self.ws.send_str(BEGIN)
        began = await self.ws.receive_json(timeout=5)
        assert began["type"] == "pet.converse.began", began
        self.began = time.monotonic()
        if read:
            self.reader = asyncio.ensure_future(self._pump())
        return self

    async def _pump(self) -> None:
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.got.append((time.monotonic() - self.began, json.loads(msg.data)))

    async def mic(self, voiced: int, silent: int = 0) -> None:
        start = time.monotonic()
        for index in range(voiced + silent):
            try:
                await self.ws.send_str(mic_frame(VOICED if index < voiced else SILENT))
            except (ConnectionError, aiohttp.ClientError):
                return  # the host ended the call; the tests assert on what it counted
            await asyncio.sleep(max(0.0, start + (index + 1) * 0.1 - time.monotonic()))

    async def stop(self) -> None:
        self.sent_stop_at = time.monotonic() - self.began
        await self.ws.send_str(STOP)

    def audio(self) -> list[tuple[float, bytes]]:
        return [(t, base64.b64decode(o["delta"])) for t, o in self.got if o["type"] == "session.output_audio.delta"]

    def other(self) -> list[str]:
        return [o["type"] if o["type"] != "pet.converse.notice" else "notice:" + o["code"]
                for _t, o in self.got if o["type"] != "session.output_audio.delta"]


class Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hosts: list[HostProc] = []

    async def asyncTearDown(self) -> None:
        for host in self.hosts:
            await host.kill()

    async def host(self, leg: str = "none", **kw) -> HostProc:
        host = HostProc(leg, **kw)
        self.hosts.append(host)
        return await host.start()

    def check_private_record(self, host: HostProc, record: dict) -> None:
        self.assertEqual(set(record), RECORD_KEYS)
        self.assertEqual(stat.S_IMODE(os.stat(host.record_file).st_mode), 0o600)
        text = host.record_text + host.stdout.decode() + host.stderr.decode()
        for secret in (F.token, F.other_token, F.token_hash, "Bearer", "127.0.0.1", str(host.port),
                       base64.b64encode(VOICED).decode()[:32], F.cert_pem.decode().splitlines()[1],
                       str(F.dir)):
            self.assertNotIn(secret, text)
        self.assertEqual(host.stderr, b"")
        for line in host.stdout.splitlines():
            self.assertIsNotNone(STDOUT_LINE.fullmatch(line), line)
        self.assertEqual(record["outbound_attempts"], 0)
        self.assertEqual(host.code, 0)


class LaunchRefusalTest(unittest.TestCase):
    """Nothing here opens a socket: every case ends before the listener exists."""

    def refused(self, args: list[str], code: str) -> None:
        rc, out, err = run_host(args)
        self.assertEqual((rc, out, err), (3, b"", f"refused {code}\n".encode()))

    def test_a_mode_must_be_named_and_there_is_no_bind_option_for_the_loopback_mode(self):
        base = loop_args("none", 5555, F.record_path())
        rc, out, _ = run_host(loop_args("none", 5555, F.record_path(), mode=None))
        self.assertEqual((rc, out), (2, b""))
        rc, out, _ = run_host(base + ["--bind", "0.0.0.0"])
        self.assertEqual((rc, out), (2, b""))
        rc, out, _ = run_host(base + ["--confirm-bind", "192.168.1.5:5555"])  # lan argument in loopback mode
        self.assertEqual((rc, out), (3, b""))
        rc, out, _ = run_host([])
        self.assertEqual((rc, out), (2, b""))

    def test_limits_and_paths_are_bounded(self):
        cases = [loop_args("none", 5555, F.record_path(), **over) for over in (
            {"expiry_seconds": 121}, {"expiry_seconds": 4}, {"output_seconds": 7}, {"wait_seconds": 301},
            {"linger_seconds": 16})]
        cases.append(loop_args("none", 80, F.record_path()))             # a privileged port
        cases.append(loop_args("none", 5555, Path("relative.json")))      # a record path that is not absolute
        cases.append(loop_args("none", 5555, F.record_path(), bridge_dir="bridge"))
        for index, args in enumerate(cases):
            with self.subTest(case=index):
                self.refused(args, "args_refused")

    def test_private_file_rules_of_the_gateway_apply(self):
        loose_hash = F.dir / "loose.sha256"
        write_private(loose_hash, (F.token_hash + "\n").encode(), 0o644)
        self.refused(loop_args("none", 5555, F.record_path(), token_sha256_file=loose_hash), "token_file_refused")
        bad_hash = F.dir / "bad.sha256"
        write_private(bad_hash, b"not-a-hash\n")
        self.refused(loop_args("none", 5555, F.record_path(), token_sha256_file=bad_hash), "token_file_refused")
        loose_key = F.dir / "loose-key.pem"
        write_private(loose_key, F.key.read_bytes(), 0o640)
        self.refused(loop_args("none", 5555, F.record_path(), tls_key=loose_key), "tls_refused")
        self.refused(loop_args("none", 5555, F.record_path(), tls_cert=F.dir / "absent.pem"), "tls_refused")

    def test_a_changed_bridge_is_refused(self):
        copy = F.dir / "bridge-copy"
        shutil.copytree(BRIDGE / "pet_bridge", copy / "pet_bridge", ignore=shutil.ignore_patterns("__pycache__"))
        rc, out, err = run_host(loop_args("none", 5555, F.record_path(), bridge_dir=copy) + ["--check-only"])
        self.assertEqual((rc, out, err), (0, b"check_ok\n", b""))  # an identical copy passes
        with open(copy / "pet_bridge" / "gateway_server.py", "ab") as handle:
            handle.write(b"\n# changed\n")
        self.refused(loop_args("none", 5555, F.record_path(), bridge_dir=copy) + ["--check-only"], "bridge_mismatch")

    def test_an_existing_record_is_never_overwritten(self):
        record = F.record_path()
        write_private(record, b"kept\n")
        self.refused(loop_args("none", free_port(), record), "record_refused")
        self.assertEqual(record.read_bytes(), b"kept\n")

    def test_a_busy_port_is_refused_and_leaves_no_record(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            record = F.record_path()
            self.refused(loop_args("none", taken.getsockname()[1], record), "bind_failed")
            self.assertFalse(record.exists())


class LanCheckOnlyTest(unittest.TestCase):
    """The lan mode is validated with a synthetic gateway configuration and --check-only,
    which ends before any socket exists. No lan listener is ever opened by these tests."""

    def config(self, **over) -> dict:
        doc = {
            "version": 1, "state_root": str(F.dir / "state"), "credential_ref": "device-" + "0" * 32,
            "session_id": "synthetic-session-0123abcd", "session_label": "Synthetic",
            "bind_host": "192.168.77.10", "port": 8443, "host_header": "192.168.77.10:8443",
            "tls_cert_file": str(F.cert), "tls_key_file": str(F.key),
            "pet_token_sha256": [F.token_hash], "provider_key_env": "SYNTHETIC_UNUSED_NAME",
        }
        doc.update(over)
        return doc

    def args(self, doc: dict, confirm: str | None, pet: str | None, *, env: bool = False) -> list[str]:
        self.n = getattr(self, "n", 0) + 1
        path = F.dir / f"cfg-{id(self)}-{self.n}"
        if env:
            lines = {
                "POCKET_GATEWAY_CONFIG_VERSION": "1", "POCKET_GATEWAY_STATE_ROOT": doc["state_root"],
                "POCKET_GATEWAY_CREDENTIAL_REF": doc["credential_ref"], "POCKET_GATEWAY_SESSION_ID": doc["session_id"],
                "POCKET_GATEWAY_SESSION_LABEL": doc["session_label"], "POCKET_GATEWAY_BIND_HOST": doc["bind_host"],
                "POCKET_GATEWAY_PORT": str(doc["port"]), "POCKET_GATEWAY_HOST_HEADER": doc["host_header"],
                "POCKET_GATEWAY_TLS_CERT_FILE": doc["tls_cert_file"], "POCKET_GATEWAY_TLS_KEY_FILE": doc["tls_key_file"],
                "POCKET_GATEWAY_PET_TOKEN_SHA256": ",".join(doc["pet_token_sha256"]),
                "POCKET_GATEWAY_PROVIDER_KEY_FILE": str(F.dir / "never-read"),
            }
            write_private(path, "".join(f"{k}={v}\n" for k, v in lines.items()).encode())
        else:
            write_private(path, json.dumps(doc).encode())
        out = ["--bridge-dir", str(BRIDGE), "--mode", "lan", "--leg", "tone", "--record", str(F.record_path()),
               "--wait-seconds", "60", "--expiry-seconds", "30", "--output-seconds", "20", "--linger-seconds", "5",
               "--gateway-env-file" if env else "--gateway-config", str(path), "--check-only"]
        if confirm is not None:
            out += ["--confirm-bind", confirm]
        if pet is not None:
            out += ["--pet-ip", pet]
        return out

    def outcome(self, args: list[str]) -> tuple[int, bytes, bytes]:
        return run_host(args)

    def test_exact_confirmed_private_bind_and_pet_address_pass_the_check(self):
        self.assertEqual(self.outcome(self.args(self.config(), "192.168.77.10:8443", "192.168.77.20")),
                         (0, b"check_ok\n", b""))
        self.assertEqual(self.outcome(self.args(self.config(), "192.168.77.10:8443", "192.168.77.20", env=True)),
                         (0, b"check_ok\n", b""))

    def test_refusals(self):
        cases = [
            (self.config(), "192.168.77.10:8444", "192.168.77.20", "bind_not_confirmed"),
            (self.config(), "192.168.77.11:8443", "192.168.77.20", "bind_not_confirmed"),
            (self.config(), "0.0.0.0:8443", "192.168.77.20", "bind_not_confirmed"),
            (self.config(bind_host="100.64.0.5", host_header="100.64.0.5:8443"), "100.64.0.5:8443", "192.168.77.20", "bind_not_lan"),
            (self.config(bind_host="0.0.0.0"), "0.0.0.0:8443", "192.168.77.20", "config_refused"),
            (self.config(bind_host="127.0.0.1"), "127.0.0.1:8443", "192.168.77.20", "config_refused"),
            (self.config(), "192.168.77.10:8443", "8.8.8.8", "pet_ip_refused"),
            (self.config(), "192.168.77.10:8443", "192.168.77.10", "pet_ip_refused"),
            (self.config(), "192.168.77.10:8443", "100.64.0.9", "pet_ip_refused"),
            (self.config(), None, "192.168.77.20", "args_refused"),
            (self.config(), "192.168.77.10:8443", None, "args_refused"),
            (self.config(pet_token_sha256=[]), "192.168.77.10:8443", "192.168.77.20", "config_refused"),
        ]
        for doc, confirm, pet, code in cases:
            with self.subTest(code=code, confirm=confirm, pet=pet):
                self.assertEqual(self.outcome(self.args(doc, confirm, pet)), (3, b"", f"refused {code}\n".encode()))

    def test_a_group_readable_configuration_is_refused(self):
        args = self.args(self.config(), "192.168.77.10:8443", "192.168.77.20")
        os.chmod(args[args.index("--gateway-config") + 1], 0o640)
        self.assertEqual(self.outcome(args), (3, b"", b"refused config_refused\n"))


class OutboundTest(unittest.TestCase):
    def test_the_host_source_names_no_provider_unified_or_environment_capability(self):
        source = HOST.read_text()
        for word in ("connect_provider", "ws_connect", "ClientSession", "PROVIDER_URL", "environ", "getenv",
                     "logging", "provider_key_file)", "reveal(", "urllib", "requests", "subprocess"):
            self.assertNotIn(word, source, word)

    def test_outgoing_connections_and_lookups_are_disabled_in_the_host_process(self):
        probe = (
            "import asyncio, importlib.util, socket, sys\n"
            "sys.dont_write_bytecode = True\n"
            "spec = importlib.util.spec_from_file_location('h', sys.argv[1])\n"
            "h = importlib.util.module_from_spec(spec); sys.modules['h'] = h; spec.loader.exec_module(h)\n"
            "h.block_outbound(); port = int(sys.argv[2]); refused = 0\n"
            "for attempt in (lambda: socket.create_connection(('127.0.0.1', port), 1),\n"
            "                lambda: socket.socket().connect(('127.0.0.1', port)),\n"
            "                lambda: socket.socket().connect_ex(('127.0.0.1', port)),\n"
            "                lambda: socket.getaddrinfo('localhost', 80),\n"
            "                lambda: socket.gethostbyname('localhost'),\n"
            "                lambda: asyncio.run(asyncio.open_connection('127.0.0.1', port))):\n"
            "    try:\n        attempt()\n    except OSError:\n        refused += 1\n"
            "print(refused, h._outbound_attempts >= 6)\n"
        )
        with socket.socket() as listener:  # a loopback target only: nothing leaves this machine
            listener.bind(("127.0.0.1", 0))
            listener.listen(8)
            done = subprocess.run([sys.executable, "-B", "-c", probe, str(HOST), str(listener.getsockname()[1])],
                                  capture_output=True, timeout=30)
            self.assertEqual((done.returncode, done.stdout), (0, b"6 True\n"), done.stderr)
            listener.settimeout(0.5)
            with self.assertRaises(TimeoutError):
                listener.accept()


class HandshakeCountTest(Case):
    async def test_tcp_connect_without_tls_counts_no_handshake(self):
        host = await self.host(wait=2)
        _, writer = await asyncio.open_connection("127.0.0.1", host.port)
        writer.close()
        await writer.wait_closed()
        record = await host.finish()
        self.assertEqual(record["tls_client_hello_count"], 0)
        self.assertEqual(record["allowed_peer_tls_completed"], 0)
        self.assertEqual((record["connections_seen"], record["admissions"]), (0, 0))
        self.check_private_record(host, record)

    async def test_rejected_certificate_reaches_hello_but_not_http(self):
        host = await self.host(wait=2)
        with self.assertRaises(ssl.SSLCertVerificationError):
            await asyncio.open_connection(
                "127.0.0.1", host.port, ssl=ssl.create_default_context(),
            )
        record = await host.finish()
        self.assertEqual(record["tls_client_hello_count"], 1)
        self.assertEqual(record["allowed_peer_tls_completed"], 0)
        self.assertEqual((record["connections_seen"], record["admissions"]), (0, 0))
        self.check_private_record(host, record)

    async def test_completed_tls_without_http_or_admission(self):
        host = await self.host(wait=2)
        for address in ("127.0.0.1", "127.0.0.2"):
            _, writer = await asyncio.open_connection(
                "127.0.0.1", host.port, ssl=F.client_ssl(), local_addr=(address, 0),
            )
            writer.close()
            await writer.wait_closed()
        record = await host.finish()
        self.assertEqual(record["tls_client_hello_count"], 2)
        self.assertEqual(record["allowed_peer_tls_completed"], 1)
        self.assertEqual((record["connections_seen"], record["admissions"]), (0, 0))
        self.check_private_record(host, record)


class AdmissionTest(Case):
    async def test_refusals_come_before_any_upgrade_and_do_not_use_the_one_admission(self):
        host = await self.host("none", wait=20)
        expected = []
        async with session() as s:
            async def get(code, **kw):
                method = kw.pop("method", "GET")
                async with s.request(method, kw.pop("url", host.url()), allow_redirects=False, **kw) as r:
                    self.assertEqual((r.status, await r.read()), (code[0], b'{"error":"%s"}' % code[1].encode()))
                    self.assertNotIn("Upgrade", r.headers)
                expected.append(code[1])
            await get((401, "unauthorized"), headers=bearer(F.other_token))
            await get((401, "unauthorized"))
            await get((404, "not_found"), url=host.url("/v1/pet/other"), headers=bearer(F.token))
            await get((421, "misdirected"), headers={**bearer(F.token), "Host": "127.0.0.1:1"})
            await get((400, "bad_request"), headers={**bearer(F.token), "Origin": "https://example.invalid"})
            await get((405, "method_not_allowed"), method="POST", headers=bearer(F.token))
            await get((400, "bad_request"), headers=bearer(F.token))  # right token, not a WebSocket upgrade
            async with session("127.0.0.2") as other:  # right token from an address that is not the pet's
                async with other.get(host.url(), headers=bearer(F.token)) as r:
                    self.assertEqual((r.status, await r.read()), (404, b'{"error":"not_found"}'))
                expected.append("not_found")
            ws = await s.ws_connect(host.url(), headers=bearer(F.token))  # the admission is still available
            pet = await Pet(ws).open()
            await pet.stop()
            await ws.send_str(CLOSE)
            await asyncio.wait_for(pet.reader, 5)
        record = await host.finish()
        self.assertEqual(record["refusals"], {c: expected.count(c) for c in sorted(set(expected))})
        self.assertEqual((record["admissions"], record["connections_seen"]), (1, len(expected) + 1))
        self.assertEqual(record["first_close_reason"], "device_stop")
        self.check_private_record(host, record)

    async def test_repeated_wrong_tokens_lock_the_address_out_like_the_gateway(self):
        host = await self.host("none", wait=20)
        async with session() as s:
            for _ in range(5):
                async with s.get(host.url(), headers=bearer(F.other_token)) as r:
                    self.assertEqual(r.status, 401)
            async with s.get(host.url(), headers=bearer(F.token)) as r:
                self.assertEqual((r.status, await r.read()), (429, b'{"error":"rate_limited"}'))
        host.proc.send_signal(signal.SIGTERM)
        record = await host.finish(5)
        self.assertEqual(record["refusals"], {"rate_limited": 1, "unauthorized": 5})
        self.assertEqual((record["admissions"], record["outcome"], record["first_close_reason"]),
                         (0, "operator_stop", "operator_stop"))
        self.check_private_record(host, record)

    async def test_a_reopen_is_refused_before_any_audio_during_and_after_the_one_call(self):
        host = await self.host("tone", output=3, linger=3)
        async with session() as s:
            ws = await s.ws_connect(host.url(), headers=bearer(F.token))
            pet = await Pet(ws).open()
            await pet.mic(5)
            with self.assertRaises(aiohttp.WSServerHandshakeError) as second:
                await s.ws_connect(host.url(), headers=bearer(F.token))
            self.assertEqual(second.exception.status, 429)
            before = len(pet.audio())
            await pet.mic(5)
            self.assertGreater(len(pet.audio()), before)  # the admitted call is not disturbed
            await pet.stop()
            await ws.send_str(CLOSE)
            await asyncio.wait_for(pet.reader, 5)
            async with s.get(host.url(), headers={**bearer(F.token), "Upgrade": "websocket", "Connection": "Upgrade"}) as r:
                self.assertEqual((r.status, await r.read()), (429, b'{"error":"rate_limited"}'))
        record = await host.finish()
        self.assertEqual((record["admissions"], record["reopens_refused"], len(record["reopen_refused_ms"])), (1, 2, 2))
        self.assertEqual(record["refusals"], {"rate_limited": 2})
        self.check_private_record(host, record)

    async def test_no_admission_ends_the_run_and_releases_the_port(self):
        host = await self.host("silence", wait=2)
        record = await host.finish(8)
        self.assertEqual((record["outcome"], record["admissions"], record["first_close_reason"]),
                         ("no_admission", 0, "no_admission"))
        self.assertEqual(host.stdout, b"listening\nended no_admission\nrecord_written\nreleased\n")
        with socket.socket() as again:
            again.bind(("127.0.0.1", host.port))
        self.check_private_record(host, record)


class LegTest(Case):
    async def test_echo_captures_in_quiet_then_returns_exact_pcm_once(self):
        host = await self.host("echo", expiry=10, output=4)
        async with session() as s:
            ws = await s.ws_connect(host.url(), headers=bearer(F.token))
            pet = await Pet(ws).open(read=False)
            await ws.send_str(mic_frame(SILENT))
            await ws.send_str(mic_frame(VOICED))
            # Only submitted device PCM, in order, after the listening window.
            for idx, expected in enumerate((SILENT, VOICED)):
                msg = await ws.receive_json(timeout=6)
                self.assertEqual(msg["type"], "session.output_audio.delta")
                self.assertEqual(base64.b64decode(msg["delta"]), expected)
                if idx == 0:
                    self.assertGreater(time.monotonic() - pet.began, 3.4)
                    await ws.send_str(mic_frame(VOICED))  # during playback: never echoed
            await pet.stop()
            await ws.send_str(CLOSE)
            self.assertEqual(await ws.receive_json(timeout=2), {"type": "session.closed"})
            await ws.close()
        record = await host.finish()
        self.assertEqual(record["input"]["frames"], 3)
        self.assertEqual(record["output_written"]["frames"], 2)
        self.assertEqual(record["outbound_attempts"], 0)
        self.check_private_record(host, record)

    async def leg(self, leg: str, flags: tuple[str, ...] = ()) -> tuple[HostProc, Pet, dict]:
        host = await self.host(leg, expiry=8, output=2, flags=flags)
        async with session() as s:
            ws = await s.ws_connect(host.url(), headers=bearer(F.token))
            pet = await Pet(ws).open()
            await pet.mic(20, 10)
            await pet.stop()
            await ws.send_str(CLOSE)
            await asyncio.wait_for(pet.reader, 5)
            self.assertEqual(ws.close_code, 1000)
        record = await host.finish()
        self.assertEqual(pet.other(), ["session.closed"])  # no state or notice frame in any leg
        self.assertEqual(record["input"]["frames"], 30)
        self.assertEqual((record["input"]["bytes"], record["input"]["samples"], record["input"]["nonzero_samples"]),
                         (144000, 72000, 48000))
        self.assertEqual(sum(record["input_bytes_per_second_since_began"]), 144000)
        self.assertLess(record["input"]["max_gap_ms"], 400)
        marks = record["marks"]
        self.assertTrue(marks["admitted_ms"] <= marks["start_ms"] <= marks["began_ms"] < marks["stop_ms"] <= marks["close_ms"])
        self.assertEqual((record["first_close_reason"], record["ws_close_code"], record["admissions"], record["leg"]),
                         ("device_stop", 1000, 1, leg))
        self.assertIs(record["peer_close_confirmed"], True)
        self.assertEqual(host.stdout, b"listening\nadmitted\nended device_stop\nrecord_written\nreleased\n")
        self.check_private_record(host, record)
        return host, pet, record

    def check_cadence(self, pet: Pet, record: dict) -> list[bytes]:
        audio = pet.audio()
        self.assertEqual(len(audio), 20)  # 2 s at one 100 ms chunk per 100 ms, then nothing
        times = [t for t, _ in audio]
        self.assertLess(times[0], 0.3)
        self.assertTrue(1.6 < times[-1] < 2.4, times[-1])
        self.assertLess(max(b - a for a, b in zip(times, times[1:])), 0.3)
        self.assertEqual({len(pcm) for _, pcm in audio}, {4800})
        self.assertEqual((record["output_written"]["frames"], record["output_written"]["bytes"]), (20, 96000))
        self.assertLess(record["output_written"]["max_late_ms"], 150)
        return [pcm for _, pcm in audio]

    async def test_leg_none_sends_no_audio(self):
        _host, pet, record = await self.leg("none")
        self.assertEqual(pet.audio(), [])
        self.assertEqual(record["output_written"], {"frames": 0, "bytes": 0, "first_ms": None, "last_ms": None, "max_late_ms": 0})

    async def test_leg_silence_sends_zeros_at_the_same_cadence(self):
        _host, pet, record = await self.leg("silence")
        self.assertEqual(set(self.check_cadence(pet, record)), {bytes(4800)})

    async def test_leg_tone_sends_a_low_level_tone_at_the_same_cadence(self):
        _host, pet, record = await self.leg("tone", flags=("--sample-tcp-info",))
        chunks = self.check_cadence(pet, record)
        self.assertEqual(len(set(chunks)), 1)
        samples = struct.unpack("<2400h", chunks[0])
        self.assertEqual(max(abs(s) for s in samples), 1000)  # about -30 dBFS; the firmware calls >= 250 audible
        self.assertEqual(samples[0], 0)
        crossings = sum(1 for a, b in zip(samples, samples[1:] + samples[:1]) if a < 0 <= b)
        self.assertEqual(crossings, 44)  # 440 Hz: 44 whole cycles per 100 ms, so chunks join without a step
        rows = record["tcp_info_samples"]
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(all(len(row) == len(record["tcp_info_fields"]) == 7 for row in rows))
        self.assertTrue(all(isinstance(v, int) for row in rows for v in row))

    async def test_the_host_process_holds_only_its_loopback_listener_and_the_one_call(self):
        host = await self.host("silence", expiry=8, output=2)
        async with session() as s:
            ws = await s.ws_connect(host.url(), headers=bearer(F.token))
            pet = await Pet(ws).open()
            await pet.mic(5)
            mark = f"pid={host.proc.pid},".encode()
            tcp = [l.split() for l in subprocess.run(["ss", "-Htanp"], capture_output=True).stdout.splitlines() if mark in l]
            udp = [l for l in subprocess.run(["ss", "-Huanp"], capture_output=True).stdout.splitlines() if mark in l]
            await pet.stop()
            await ws.send_str(CLOSE)
            await asyncio.wait_for(pet.reader, 5)
        await host.finish()
        own = f"127.0.0.1:{host.port}".encode()
        self.assertEqual(sorted(row[0] for row in tcp), [b"ESTAB", b"LISTEN"])
        self.assertTrue(all(row[3] == own for row in tcp), tcp)
        self.assertEqual(udp, [])


class EndingTest(Case):
    async def test_stop_from_the_pet_ends_the_output_at_once(self):
        host = await self.host("tone", expiry=8, output=5)
        async with session() as s:
            ws = await s.ws_connect(host.url(), headers=bearer(F.token))
            pet = await Pet(ws).open()
            await pet.mic(10)
            await pet.stop()
            await asyncio.sleep(0.8)
            late = [t for t, _ in pet.audio() if t > pet.sent_stop_at + 0.15]
            await ws.send_str(CLOSE)
            await asyncio.wait_for(pet.reader, 5)
        record = await host.finish()
        self.assertEqual(late, [])
        self.assertLessEqual(len(pet.audio()), 12)
        self.assertLessEqual(record["output_written"]["frames"], 12)
        self.assertEqual(record["first_close_reason"], "device_stop")
        self.check_private_record(host, record)

    async def test_abrupt_loss_is_recorded_as_a_loss_and_the_listener_is_released(self):
        for leg in ("none", "tone"):
            with self.subTest(leg=leg):
                host = await self.host(leg, expiry=8, output=5)
                async with session() as s:
                    ws = await s.ws_connect(host.url(), headers=bearer(F.token))
                    pet = await Pet(ws).open()
                    await pet.mic(5)
                    ws._response.connection.transport.abort()  # no WebSocket close frame, no stop
                    record = await host.finish()
                self.assertIn(record["first_close_reason"],
                              ("device_eof_without_close", "device_link_error", "host_write_failed"))
                self.assertIsNone(record["marks"]["stop_ms"])
                self.assertEqual(record["input"]["frames"], 5)
                self.assertLess(record["first_close_ms"] - record["marks"]["began_ms"], 1500)
                print(f"  abrupt loss, leg {leg}: first_close_reason={record['first_close_reason']}", file=sys.stderr)
                self.check_private_record(host, record)

    async def test_expiry_ends_a_call_the_pet_keeps_open(self):
        host = await self.host("silence", expiry=5, output=5)
        async with session() as s:
            ws = await s.ws_connect(host.url(), headers=bearer(F.token))
            pet = await Pet(ws).open()
            await pet.mic(60)  # still talking when the host's limit passes
            await asyncio.wait_for(pet.reader, 5)
            self.assertEqual(ws.close_code, 1000)
        record = await host.finish()
        self.assertEqual(pet.other(), ["notice:time_limit", "session.closed"])
        self.assertEqual(record["first_close_reason"], "expiry")
        self.assertIs(record["peer_close_confirmed"], True)
        self.assertTrue(4900 <= record["first_close_ms"] - record["marks"]["admitted_ms"] <= 5400)
        self.assertLessEqual(len(pet.audio()), 50)
        self.check_private_record(host, record)

    async def test_expiry_cuts_a_peer_that_stops_answering(self):
        host = await self.host("silence", expiry=5, output=5)
        async with session() as s:
            ws = await s.ws_connect(host.url(), headers=bearer(F.token))
            await Pet(ws).open(read=False)  # connected, then never reads or answers again
            started = time.monotonic()
            record = await host.finish(5 + 2 + 1 + 4)
            self.assertLess(time.monotonic() - started, 5 + 2 + 1 + 3)
            await ws.close()  # host is gone; release the deliberately unresponsive test peer
        self.assertEqual(record["first_close_reason"], "expiry")
        self.assertIs(record["peer_close_confirmed"], False)  # nothing answered the host's close
        self.check_private_record(host, record)

    async def test_operator_stop_ends_the_call_and_releases(self):
        host = await self.host("tone", expiry=20, output=20, linger=5)
        async with session() as s:
            ws = await s.ws_connect(host.url(), headers=bearer(F.token))
            pet = await Pet(ws).open()
            await pet.mic(5)
            host.proc.send_signal(signal.SIGTERM)
            await asyncio.wait_for(pet.reader, 5)
            self.assertEqual(ws.close_code, 1000)
        record = await host.finish(4)
        self.assertEqual(pet.other(), ["session.closed"])
        self.assertEqual((record["first_close_reason"], record["outcome"]), ("operator_stop", "leg_ended"))
        self.assertIs(record["peer_close_confirmed"], True)
        self.check_private_record(host, record)

    async def test_frames_outside_the_fixed_conversation_end_the_call(self):
        wrong_model = F.start.replace('"model":"gpt-live-1"', '"model":"other-model"', 1)
        self.assertNotEqual(wrong_model, F.start)
        turn = '{"type":"pet.turn.begin"}'
        for first, second, reason in ((BEGIN, None, "unexpected_frame"), (wrong_model, None, "protocol_error"),
                                      (F.start, turn, "unexpected_frame"), ('{"type":"nonsense"}', None, "protocol_error")):
            with self.subTest(reason=reason, first=first[:28]):
                host = await self.host("tone", expiry=6, output=2)
                async with session() as s:
                    ws = await s.ws_connect(host.url(), headers=bearer(F.token))
                    await ws.send_str(first)
                    if second is not None:
                        self.assertEqual(await ws.receive_json(timeout=5), {"type": "session.started"})
                        await ws.send_str(second)
                    msg = await ws.receive(timeout=5)
                    self.assertEqual((msg.type, msg.data), (aiohttp.WSMsgType.CLOSE, 1002))
                record = await host.finish()
                self.assertEqual((record["first_close_reason"], record["output_written"]["frames"]), (reason, 0))
                self.check_private_record(host, record)


class LeftBehindTest(unittest.TestCase):
    def test_pin_is_the_product_gateway_in_this_checkout(self):
        spec = importlib.util.spec_from_file_location("host_under_test", HOST)
        module = importlib.util.module_from_spec(spec)
        sys.modules["host_under_test"] = module
        spec.loader.exec_module(module)
        self.assertEqual(module.bridge_digest(BRIDGE), module.BRIDGE_PIN)
        self.assertTrue(module.is_rfc1918("192.168.1.5") and module.is_rfc1918("10.0.0.1") and module.is_rfc1918("172.31.0.1"))
        for bad in ("127.0.0.1", "0.0.0.0", "100.64.0.1", "8.8.8.8", "192.168.1.05", "::1", "localhost", None):
            self.assertFalse(module.is_rfc1918(bad), bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
