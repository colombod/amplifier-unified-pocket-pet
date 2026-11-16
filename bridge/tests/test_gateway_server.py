"""Offline tests for ``gateway_server`` (U3): config/auth/TLS/adapters/startup.

Everything runs in-process: no listener, no bind (not even loopback), no dial, no DNS, no
provider, no Unified, no real certificate or key.  ``aiohttp.test_utils.make_mocked_request``
builds requests without a socket; the aiohttp client classes are replaced by spies; the
event-loop / socket entry points raise (``OfflineAsyncCase`` and ``ServerOfflineCase``).  The
production functions that would dial or listen are implemented but never invoked with real
effects here.  These tests are offline evidence only: not a live provider, TLS, device or
Unified result, and no claim about a real socket abort.
"""

# pyright: reportArgumentType=false, reportCallIssue=false, reportOptionalMemberAccess=false
# pyright: reportOptionalSubscript=false, reportAttributeAccessIssue=false
# pyright: reportIndexIssue=false, reportGeneralTypeIssues=false, reportPrivateUsage=false
from __future__ import annotations

import ast
import asyncio
import base64
import copy
import hashlib
import hmac
import io
import json
import os
import pickle
import shutil
import ssl
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

import aiohttp
from aiohttp import web
from aiohttp._websocket.reader import WebSocketDataQueue
from aiohttp.client_ws import DEFAULT_WS_CLIENT_TIMEOUT
from aiohttp.test_utils import make_mocked_request
from multidict import CIMultiDict

import pet_bridge
from pet_bridge import auth, core
from pet_bridge import gateway_core as gc
from pet_bridge import gateway_provider as gp
from pet_bridge import gateway_server as gsv
from pet_bridge import gateway_state as gs
from pet_bridge.journal import Journal, JournalError

from .authfakes import (
    CLIENT_ID,
    CRED_REF,
    HOST_IDENTITY,
    INSTANCE,
    INSTANCE_2,
    FakeClient,
    attach_ok,
    json_response,
)
from .fakes import FakeClock, IdFactory, OfflineTestCase
from .gatewayfakes import (
    ASSISTANT_START,
    KEY_CANARY,
    TURN,
    FakeDeviceLink,
    FakeLoopClock,
    OfflineAsyncCase,
)
from .test_auth import attachment, cred
from .test_gateway_core import LABEL, Harness, delegation, delta

SERVER_PY = Path(pet_bridge.__file__).parent / "gateway_server.py"
SESSION_ID = "sess-fixture-0a1b2c3d"
SHORT8 = "0a1b2c3d"
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
GOOD_TOKEN = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
_LAST = ALPHABET.index(GOOD_TOKEN[-1])
assert _LAST % 4 == 0 and len(GOOD_TOKEN) == 43
NONCANONICAL_TOKEN = GOOD_TOKEN[:-1] + ALPHABET[_LAST + 1]
OTHER_TOKEN = base64.urlsafe_b64encode(bytes(range(1, 33))).rstrip(b"=").decode("ascii")
HOST_HEADER = "10.1.2.3:8941"
PEER = ("10.9.8.7", 51234)
CANARY = "HEADERCANARY-Zq7"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


GOOD_HASH = token_hash(GOOD_TOKEN)
OTHER_HASH = token_hash(OTHER_TOKEN)


class ServerOfflineCase(OfflineTestCase):
    """Sync tests: connect/DNS raise (base) plus bind/listen/server/connection."""

    def setUp(self) -> None:
        super().setUp()

        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("network access attempted in an offline test")

        for target in (
            "socket.socket.bind",
            "socket.socket.listen",
            "asyncio.base_events.BaseEventLoop.create_server",
            "asyncio.base_events.BaseEventLoop.create_connection",
        ):
            patcher = mock.patch(target, side_effect=refuse)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="pet-gw-u3-")))
        self.addCleanup(shutil.rmtree, self.base, True)

    def write(self, name: str, data: bytes | str, mode: int = 0o600) -> Path:
        path = self.base / name
        raw = data.encode("utf-8") if isinstance(data, str) else data
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            os.fchmod(fd, mode)
            os.write(fd, raw)
        finally:
            os.close(fd)
        return path


def config_doc(base: Path, **changes: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "version": 1,
        "state_root": str(base / "state"),
        "credential_ref": CRED_REF,
        "session_id": SESSION_ID,
        "session_label": LABEL,
        "bind_host": "10.1.2.3",
        "port": 8941,
        "host_header": HOST_HEADER,
        "tls_cert_file": str(base / "cert.pem"),
        "tls_key_file": str(base / "key.pem"),
        "pet_token_sha256": [GOOD_HASH],
        "provider_key_file": str(base / "provider.key"),
    }
    doc.update(changes)
    return {k: v for k, v in doc.items() if v is not ...}


def make_cfg(base: Path, **changes: Any) -> gs.GatewayConfig:
    return gs.parse_config(config_doc(base, **changes))


# =================================================================================
# Secret
# =================================================================================
class SecretTest(ServerOfflineCase):
    def test_t_srv_key_secret_never_reveals_in_repr_str_format_or_pickle(self):
        secret = gsv.Secret(KEY_CANARY)
        self.assertEqual(secret.reveal(), KEY_CANARY)
        for text in (repr(secret), str(secret), f"{secret}", format(secret, ">40")):
            self.assertNotIn(KEY_CANARY, text)
        self.assertEqual(repr(secret), "Secret(<hidden>)")
        self.assertEqual(str(secret), "Secret(<hidden>)")
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            with self.subTest(protocol=protocol), self.assertRaises(TypeError):
                pickle.dumps(secret, protocol=protocol)
        with self.assertRaises(TypeError):
            copy.copy(secret)
        with self.assertRaises(TypeError):
            copy.deepcopy(secret)

    def test_secret_has_exactly_one_slot_and_no_instance_dict(self):
        self.assertEqual(gsv.Secret.__slots__, ("_v",))
        secret = gsv.Secret(KEY_CANARY)
        self.assertFalse(hasattr(secret, "__dict__"))
        with self.assertRaises(AttributeError):
            secret.other = 1  # type: ignore[attr-defined]

    def test_secret_accepts_only_text(self):
        for bad in (b"bytes", 7, None, ["x"]):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                gsv.Secret(bad)


# =================================================================================
# check_private_file
# =================================================================================
class PrivateFileTest(ServerOfflineCase):
    def test_t_srv_file_private_regular_file_is_read_exactly(self):
        path = self.write("a.bin", b"hello")
        self.assertEqual(
            gsv.check_private_file(path, max_bytes=16, read=True), b"hello"
        )
        self.assertIsNone(gsv.check_private_file(path, max_bytes=None, read=False))
        self.assertEqual(
            gsv.check_private_file(path, max_bytes=None, read=True), b"hello"
        )

    def test_t_srv_file_opens_nofollow_cloexec_and_never_blocks(self):
        path = self.write("a.bin", b"x")
        real_open = os.open
        flags: list[int] = []

        def spy(file: Any, flag: int, *args: Any, **kwargs: Any) -> int:
            flags.append(flag)
            return real_open(file, flag, *args, **kwargs)

        with mock.patch("os.open", side_effect=spy):
            gsv.check_private_file(path, max_bytes=8, read=True)
        (flag,) = flags
        for needed in (os.O_RDONLY, os.O_NOFOLLOW, os.O_CLOEXEC, os.O_NONBLOCK):
            self.assertTrue(flag & needed or needed == os.O_RDONLY)

    def test_t_srv_file_refuses_every_unsafe_form(self):
        loose = self.write("loose.bin", b"x", 0o640)
        group = self.write("group.bin", b"x", 0o660)
        world = self.write("world.bin", b"x", 0o604)
        good = self.write("good.bin", b"x")
        link = self.base / "link.bin"
        os.symlink(good, link)
        directory = self.base / "dir"
        directory.mkdir(mode=0o700)
        fifo = self.base / "fifo"
        os.mkfifo(fifo, 0o600)
        big = self.write("big.bin", b"x" * 17)
        cases = {
            "loose": loose,
            "group": group,
            "world": world,
            "symlink": link,
            "directory": directory,
            "fifo": fifo,
            "missing": self.base / "missing.bin",
            "oversize": big,
            "relative": Path("relative.bin"),
            "nul": Path(str(self.base / "a\x00b")),
        }
        for name, path in cases.items():
            with self.subTest(case=name), self.assertRaises(OSError):
                gsv.check_private_file(path, max_bytes=16, read=True)

    def test_t_srv_file_refuses_a_file_owned_by_another_user(self):
        path = self.write("a.bin", b"x")
        with (
            mock.patch("os.geteuid", return_value=os.geteuid() + 1),
            self.assertRaises(OSError),
        ):
            gsv.check_private_file(path, max_bytes=8, read=True)


# =================================================================================
# load_config
# =================================================================================
class LoadConfigTest(ServerOfflineCase):
    def put(self, doc: Any, mode: int = 0o600, name: str = "gw.json") -> Path:
        text = doc if isinstance(doc, (str, bytes)) else json.dumps(doc)
        return self.write(name, text, mode)

    def test_t_srv_cfg_valid_private_absolute_config_loads(self):
        cfg = gsv.load_config(self.put(config_doc(self.base)))
        self.assertEqual((cfg.session_id_short, cfg.port), (SHORT8, 8941))
        self.assertEqual(cfg.session_label, LABEL)

    def test_t_srv_cfg_every_failure_is_one_fixed_startup_code(self):
        cases: dict[str, tuple[Any, str]] = {
            "bad json": ("{not json", "config_invalid"),
            "duplicate keys": (
                '{"version":1,"version":1}',
                "config_invalid",
            ),
            "not an object": ("[1]", "config_invalid"),
            "extra key": (
                config_doc(self.base, extra_key="CANARY-" + CANARY),
                "config_invalid",
            ),
            "both key sources": (
                config_doc(self.base, provider_key_env="PROVIDER_KEY"),
                "config_invalid",
            ),
            "any-interface bind": (
                config_doc(self.base, bind_host="0.0.0.0"),
                "config_invalid",
            ),
            "uppercase short8": (
                config_doc(self.base, session_id="sess-fixture-0A1B2C3D"),
                "core_binding_required",
            ),
            "deep": ("[" * 20 + "]" * 20, "config_invalid"),
            "oversize": (
                json.dumps(config_doc(self.base, session_label="x" * 17000)),
                "config_invalid",
            ),
            "bad utf8": (b"\xff\xfe{", "config_invalid"),
        }
        for name, (doc, code) in cases.items():
            with self.subTest(case=name):
                path = self.put(doc, name=f"c-{abs(hash(name))}.json")
                with self.assertRaises(gsv.StartupError) as ctx:
                    gsv.load_config(path)
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(str(ctx.exception), code)
                self.assertNotIn("CANARY", str(ctx.exception) + repr(ctx.exception))
                self.assertIsNone(ctx.exception.__cause__)

    def test_t_srv_cfg_file_defects_are_config_invalid(self):
        good = self.put(config_doc(self.base), name="good.json")
        link = self.base / "link.json"
        os.symlink(good, link)
        loose = self.put(config_doc(self.base), mode=0o640, name="loose.json")
        for name, path in {
            "symlink": link,
            "loose mode": loose,
            "relative": Path("gw.json"),
            "missing": self.base / "none.json",
            "directory": self.base,
        }.items():
            with self.subTest(case=name), self.assertRaises(gsv.StartupError) as ctx:
                gsv.load_config(path)
            self.assertEqual(ctx.exception.code, "config_invalid")

    def test_startup_error_codes_are_the_fixed_set(self):
        self.assertEqual(
            gsv.STARTUP_CODES,
            frozenset(
                {
                    "config_invalid",
                    "core_binding_required",
                    "credential_missing",
                    "cert_missing",
                    "provider_key_missing",
                    "listen_failed",
                    "event_log_invalid",
                }
            ),
        )
        with self.assertRaises(ValueError):
            gsv.StartupError("anything_else")


# =================================================================================
# load_provider_key
# =================================================================================
class LoadProviderKeyTest(ServerOfflineCase):
    def cfg_file(self, name: str) -> gs.GatewayConfig:
        return make_cfg(self.base, provider_key_file=str(self.base / name))

    def cfg_env(self, name: str = "GATEWAY_PROVIDER_KEY") -> gs.GatewayConfig:
        return make_cfg(self.base, provider_key_file=..., provider_key_env=name)

    def test_t_srv_key_file_one_trailing_lf_is_stripped(self):
        self.write("k1", KEY_CANARY + "\n")
        self.write("k2", KEY_CANARY)
        for name in ("k1", "k2"):
            with self.subTest(name=name):
                secret = gsv.load_provider_key(self.cfg_file(name), {})
                self.assertEqual(secret.reveal(), KEY_CANARY)
                self.assertNotIn(KEY_CANARY, repr(secret) + str(secret))

    def test_t_srv_key_file_refusals_are_provider_key_missing(self):
        self.write("crlf", KEY_CANARY + "\r\n")
        self.write("twolf", KEY_CANARY + "\n\n")
        self.write("short", "k" * 19)
        self.write("space", "sk-" + "K" * 10 + " " + "K" * 10)
        self.write("ctl", "sk-" + "K" * 10 + "\x07" + "K" * 10)
        self.write("long", "k" * 513)
        self.write("nonascii", "sk-" + "é" * 30)
        self.write("badutf8", b"\xff" * 40)
        self.write("empty", b"")
        self.write("big", b"k" * 2000)
        self.write("loose", KEY_CANARY, 0o640)
        good = self.write("good", KEY_CANARY)
        os.symlink(good, self.base / "link")
        for name in (
            "crlf",
            "twolf",
            "short",
            "space",
            "ctl",
            "long",
            "nonascii",
            "badutf8",
            "empty",
            "big",
            "loose",
            "link",
            "missing",
        ):
            with self.subTest(name=name), self.assertRaises(gsv.StartupError) as ctx:
                gsv.load_provider_key(self.cfg_file(name), {})
            self.assertEqual(ctx.exception.code, "provider_key_missing")
            self.assertNotIn(
                "K", str(ctx.exception).replace("provider_key_missing", "")
            )

    def test_t_srv_key_env_name_is_read_only_from_the_given_mapping(self):
        cfg = self.cfg_env()
        env = {"GATEWAY_PROVIDER_KEY": KEY_CANARY}
        self.assertEqual(gsv.load_provider_key(cfg, env).reveal(), KEY_CANARY)
        with mock.patch.dict(os.environ, {"GATEWAY_PROVIDER_KEY": KEY_CANARY}):
            for bad in ({}, {"OTHER": KEY_CANARY}, {"GATEWAY_PROVIDER_KEY": "short"}):
                with self.subTest(env=bad), self.assertRaises(gsv.StartupError) as ctx:
                    gsv.load_provider_key(cfg, bad)
                self.assertEqual(ctx.exception.code, "provider_key_missing")

    def test_t_srv_key_never_enters_argv_or_a_file_path_of_the_error(self):
        self.write("k", KEY_CANARY)
        secret = gsv.load_provider_key(self.cfg_file("k"), {})
        self.assertEqual(gsv.Secret.__slots__, ("_v",))
        self.assertNotIn(KEY_CANARY, repr(self.cfg_file("k")))
        self.assertTrue(secret.reveal())


# =================================================================================
# build_server_ssl_context
# =================================================================================
class TlsContextTest(ServerOfflineCase):
    def files(self) -> tuple[Path, Path]:
        return self.write("cert.pem", b"not a certificate"), self.write(
            "key.pem", b"not a key"
        )

    def test_t_srv_tls_server_context_is_tls12_no_compression_and_never_prompts(self):
        cert, key = self.files()
        calls: list[tuple[Any, ...]] = []

        def fake_load(
            ctx: Any, certfile: Any, keyfile: Any = None, password: Any = None
        ):
            calls.append((certfile, keyfile, password))

        with mock.patch.object(ssl.SSLContext, "load_cert_chain", fake_load):
            ctx = gsv.build_server_ssl_context(cert, key)
        self.assertEqual(ctx.protocol, ssl.PROTOCOL_TLS_SERVER)
        self.assertEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertTrue(ctx.options & ssl.OP_NO_COMPRESSION)
        ((certfile, keyfile, password),) = calls
        self.assertEqual((Path(certfile), Path(keyfile)), (cert, key))
        self.assertTrue(callable(password))
        with self.assertRaises(ValueError):
            password()

    def test_t_srv_tls_unsafe_or_unloadable_files_are_cert_missing_before_any_load(
        self,
    ):
        cert, key = self.files()
        loose = self.write("loose.pem", b"x", 0o644)
        link = self.base / "link.pem"
        os.symlink(cert, link)
        cases = {
            "symlink cert": (link, key),
            "loose key": (cert, loose),
            "missing key": (cert, self.base / "none.pem"),
            "missing cert": (self.base / "none.pem", key),
            "directory": (self.base, key),
        }
        for name, (c, k) in cases.items():
            load = mock.Mock()
            with (
                self.subTest(case=name),
                mock.patch.object(ssl.SSLContext, "load_cert_chain", load),
                self.assertRaises(gsv.StartupError) as ctx,
            ):
                gsv.build_server_ssl_context(c, k)
            self.assertEqual(ctx.exception.code, "cert_missing")
            load.assert_not_called()
        with (
            mock.patch("os.geteuid", return_value=os.geteuid() + 1),
            mock.patch.object(ssl.SSLContext, "load_cert_chain") as load,
            self.assertRaises(gsv.StartupError) as ctx,
        ):
            gsv.build_server_ssl_context(cert, key)
        self.assertEqual(ctx.exception.code, "cert_missing")
        load.assert_not_called()

    def test_t_srv_tls_a_load_failure_is_cert_missing_with_no_cause_text(self):
        cert, key = self.files()
        for failure in (
            ssl.SSLError("CANARY-" + CANARY),
            ValueError(CANARY),
            OSError(CANARY),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(
                    ssl.SSLContext, "load_cert_chain", side_effect=failure
                ),
                self.assertRaises(gsv.StartupError) as ctx,
            ):
                gsv.build_server_ssl_context(cert, key)
            self.assertEqual(ctx.exception.code, "cert_missing")
            self.assertIsNone(ctx.exception.__cause__)
            self.assertNotIn(CANARY, str(ctx.exception) + repr(ctx.exception))


# =================================================================================
# AuthRegistry / verify_bearer
# =================================================================================
class AuthRegistryTest(ServerOfflineCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = 100.0

    def registry(self, **kw: Any) -> gsv.AuthRegistry:
        return gsv.AuthRegistry(clock=lambda: self.now, **kw)

    def test_t_srv_rate_five_failures_block_until_the_window_expires(self):
        reg = self.registry()
        for _ in range(5):
            self.assertTrue(reg.admit("10.0.0.1"))
            reg.record_failure("10.0.0.1")
        self.assertFalse(reg.admit("10.0.0.1"))
        self.assertTrue(reg.admit("10.0.0.2"))  # per IP
        self.now += 59.9
        self.assertFalse(reg.admit("10.0.0.1"))
        self.now += 0.1  # fixed window from the FIRST failure
        self.assertTrue(reg.admit("10.0.0.1"))
        self.assertEqual(len(reg), 0)  # the expired entry was purged on access
        self.assertNotIn("10.0.0.1", repr(reg))

    def test_t_srv_rate_window_is_fixed_not_sliding(self):
        reg = self.registry()
        reg.record_failure("10.0.0.1")
        self.now += 50
        for _ in range(4):
            reg.record_failure("10.0.0.1")
        self.assertFalse(reg.admit("10.0.0.1"))
        self.now += 10  # 60 s after the FIRST failure
        self.assertTrue(reg.admit("10.0.0.1"))

    def test_t_srv_rate_full_registry_rejects_unknown_ips_and_never_evicts(self):
        reg = self.registry(cap=256)
        for n in range(256):
            reg.record_failure(f"10.1.{n // 256}.{n % 256}")
        self.assertEqual(len(reg), 256)
        self.assertFalse(reg.admit("10.2.0.1"))  # unknown + full: fail closed
        reg.record_failure("10.2.0.1")
        self.assertEqual(len(reg), 256)
        self.assertTrue(reg.admit("10.1.0.7"))  # a known fresh entry is still there
        for _ in range(4):
            reg.record_failure("10.1.0.7")
        self.assertFalse(reg.admit("10.1.0.7"))
        self.now += 60.0
        self.assertTrue(reg.admit("10.2.0.1"))  # expired entries purged: room again
        self.assertLess(len(reg), 256)

    def test_t_srv_rate_expired_entries_are_never_resurrected(self):
        reg = self.registry()
        for _ in range(5):
            reg.record_failure("10.0.0.1")
        self.now += 61
        self.assertTrue(reg.admit("10.0.0.1"))
        reg.record_failure("10.0.0.1")  # a NEW window with count 1, not 6
        for _ in range(3):
            reg.record_failure("10.0.0.1")
        self.assertTrue(reg.admit("10.0.0.1"))
        reg.record_failure("10.0.0.1")
        self.assertFalse(reg.admit("10.0.0.1"))

    def test_registry_defaults_are_the_documented_bounds(self):
        reg = gsv.AuthRegistry()
        self.assertEqual(
            (gs.AUTH_REGISTRY_CAP, gs.AUTH_FAIL_LIMIT, gs.AUTH_WINDOW_S),
            (256, 5, 60.0),
        )
        self.assertEqual(len(reg), 0)


class VerifyBearerTest(ServerOfflineCase):
    def counted(self, values: list[str], hashes: tuple[str, ...]) -> tuple[bool, int]:
        real = hmac.compare_digest
        calls: list[int] = []

        def spy(a: Any, b: Any) -> bool:
            calls.append(1)
            return real(a, b)

        with mock.patch("hmac.compare_digest", side_effect=spy):
            return gsv.verify_bearer(values, hashes), len(calls)

    def test_t_srv_auth_a_canonical_known_token_passes(self):
        hashes = (OTHER_HASH, GOOD_HASH)
        self.assertEqual(self.counted(["Bearer " + GOOD_TOKEN], hashes), (True, 2))

    def test_t_srv_auth_every_bad_form_fails_and_still_compares_every_hash(self):
        hashes = (OTHER_HASH, GOOD_HASH, token_hash("x" * 43))
        bad: dict[str, list[str]] = {
            "lowercase scheme": ["bearer " + GOOD_TOKEN],
            "double space": ["Bearer  " + GOOD_TOKEN],
            "42 chars": ["Bearer " + GOOD_TOKEN[:-1]],
            "44 chars": ["Bearer " + GOOD_TOKEN + "A"],
            "non canonical": ["Bearer " + NONCANONICAL_TOKEN],
            "unknown token": [
                "Bearer " + base64.urlsafe_b64encode(b"z" * 32).rstrip(b"=").decode()
            ],
            "no value": [],
            "two values": ["Bearer " + GOOD_TOKEN, "Bearer " + GOOD_TOKEN],
            "plus slash": ["Bearer " + GOOD_TOKEN[:-3] + "+/A"],
            "trailing space": ["Bearer " + GOOD_TOKEN + " "],
            "unicode": ["Bearer " + "é" * 43],
            "bare token": [GOOD_TOKEN],
        }
        for name, values in bad.items():
            with self.subTest(case=name):
                ok, count = self.counted(values, hashes)
                self.assertFalse(ok)
                self.assertEqual(count, len(hashes))

    def test_mf1_a_matching_first_hash_still_compares_every_configured_hash(self):
        """Kills the in-memory early-break variant: the match is FIRST of three."""
        hashes = (GOOD_HASH, OTHER_HASH, token_hash("x" * 43))
        self.assertEqual(self.counted(["Bearer " + GOOD_TOKEN], hashes), (True, 3))
        self.assertEqual(
            self.counted(["Bearer " + GOOD_TOKEN], (GOOD_HASH, GOOD_HASH)), (True, 2)
        )

    def test_mf2_the_format_rule_is_what_refuses_a_noncanonical_configured_token(self):
        """The hash of every malformed form IS configured, so only the format rule can say no."""
        forms = {
            "noncanonical 43": NONCANONICAL_TOKEN,
            "42 chars": GOOD_TOKEN[:-1],
            "44 chars": GOOD_TOKEN + "A",
        }
        hashes = (*(token_hash(token) for token in forms.values()), OTHER_HASH)
        for name, token in forms.items():
            with self.subTest(case=name):
                self.assertIn(token_hash(token), hashes)
                ok, count = self.counted(["Bearer " + token], hashes)
                self.assertFalse(ok)
                self.assertEqual(count, len(hashes))
        self.assertEqual(
            self.counted(["Bearer " + GOOD_TOKEN], (*hashes, GOOD_HASH)), (True, 5)
        )

    def test_a_malformed_token_cannot_match_even_a_configured_dummy_hash(self):
        dummy = hashlib.sha256(b"pet-gateway-dummy-token").hexdigest()
        for hashes in ((dummy,), (OTHER_HASH, dummy)):
            self.assertFalse(gsv.verify_bearer(["Bearer short"], hashes))


# =================================================================================
# check_handshake / reject_response
# =================================================================================
def transport_for(peer: Any = PEER) -> Any:
    transport = mock.Mock()
    transport.get_extra_info.side_effect = lambda name, default=None: (
        peer if name == "peername" else default
    )
    return transport


def request_for(
    *,
    method: str = "GET",
    path: str = "/v1/pet/live",
    headers: list[tuple[str, str]] | None = None,
    peer: Any = PEER,
    bearer: str | None = "Bearer " + GOOD_TOKEN,
    host: str | None = HOST_HEADER,
    transport: Any = ...,
):
    items: list[tuple[str, str]] = []
    if host is not None:
        items.append(("Host", host))
    if bearer is not None:
        items.append(("Authorization", bearer))
    items.extend(headers or [])
    return make_mocked_request(
        method,
        path,
        CIMultiDict(items),
        transport=transport_for(peer) if transport is ... else transport,
    )


class HandshakeTest(ServerOfflineCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = make_cfg(self.base)
        self.now = 10.0
        self.registry = gsv.AuthRegistry(clock=lambda: self.now)

    def check(self, **kw: Any) -> gsv.Rejection | None:
        return gsv.check_handshake(request_for(**kw), self.cfg, self.registry)

    def code(self, **kw: Any) -> tuple[int, str] | None:
        rej = self.check(**kw)
        return None if rej is None else (rej.status, rej.code)

    def test_t_srv_hs_good_request_passes_with_no_loopback_exemption(self):
        self.assertIsNone(self.check())
        self.assertIsNone(self.check(peer=("127.0.0.1", 5)))
        for _ in range(5):  # loopback gets no exemption from the failure limit either
            self.assertEqual(
                self.code(peer=("127.0.0.1", 5), bearer="Bearer " + OTHER_TOKEN),
                (401, "unauthorized"),
            )
        self.assertEqual(self.code(peer=("127.0.0.1", 5)), (429, "rate_limited"))

    def test_t_srv_hs_method_and_exact_route(self):
        self.assertEqual(self.code(method="POST"), (405, "method_not_allowed"))
        self.assertEqual(self.code(method="HEAD"), (405, "method_not_allowed"))
        for path in (
            "/v1/pet/live/",
            "/V1/pet/live",
            "/v1/pet/live?x=1",
            "/v1/pet//live",
            "/v1/pet/live#x",
            "/",
            "/v1/pet",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.code(path=path), (404, "not_found"))

    def test_t_srv_hs_exact_host_only(self):
        for host in (
            "10.1.2.3",
            "evil.example:8941",
            "10.1.2.3:8942",
            "10.1.2.3:08941",
            "10.1.2.3:8941 ",
            "HOST.example",
            "",
        ):
            with self.subTest(host=host):
                self.assertEqual(self.code(host=host), (421, "misdirected"))
        self.assertEqual(self.code(host=None), (421, "misdirected"))
        self.assertEqual(
            self.code(headers=[("Host", HOST_HEADER)]), (421, "misdirected")
        )  # duplicate Host
        self.assertIsNone(self.check(host=HOST_HEADER))

    def test_t_srv_hs_structure_origin_protocol_and_duplicates_are_bad_requests(self):
        cases = {
            "origin": [("Origin", "https://evil.example")],
            "subprotocol": [("Sec-WebSocket-Protocol", "x")],
            "second authorization": [("Authorization", "Bearer " + GOOD_TOKEN)],
            "double upgrade": [("Upgrade", "websocket"), ("Upgrade", "websocket")],
            "double connection": [("Connection", "Upgrade"), ("Connection", "Upgrade")],
            "double key": [("Sec-WebSocket-Key", "a"), ("Sec-WebSocket-Key", "b")],
            "double version": [
                ("Sec-WebSocket-Version", "13"),
                ("Sec-WebSocket-Version", "13"),
            ],
        }
        for name, headers in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.code(headers=headers), (400, "bad_request"))
        self.assertEqual(
            self.registry.__len__(), 0
        )  # a structure error is not an auth failure

    def test_t_srv_hs_peer_is_required(self):
        for peer in (None, "unix-socket", (), 7):
            with self.subTest(peer=peer):
                self.assertEqual(self.code(peer=peer), (400, "bad_request"))

    def test_t_srv_hs_order_method_path_host_structure_rate_then_bearer(self):
        order: list[str] = []
        real_verify = gsv.verify_bearer
        real_admit = self.registry.admit

        def verify(values: list[str], hashes: tuple[str, ...]) -> bool:
            order.append("verify")
            return real_verify(values, hashes)

        def admit(ip: str) -> bool:
            order.append("admit")
            return real_admit(ip)

        # a request wrong in every way fails at the FIRST check
        with (
            mock.patch.object(gsv, "verify_bearer", side_effect=verify),
            mock.patch.object(self.registry, "admit", side_effect=admit),
        ):
            bad = request_for(
                method="POST",
                path="/x",
                host="evil",
                bearer="Bearer nope",
                headers=[("Origin", "x")],
            )
            self.assertEqual(
                gsv.check_handshake(bad, self.cfg, self.registry).code,
                "method_not_allowed",
            )
            bad = request_for(
                path="/x", host="evil", bearer="Bearer nope", headers=[("Origin", "x")]
            )
            self.assertEqual(
                gsv.check_handshake(bad, self.cfg, self.registry).code, "not_found"
            )
            bad = request_for(
                host="evil", bearer="Bearer nope", headers=[("Origin", "x")]
            )
            self.assertEqual(
                gsv.check_handshake(bad, self.cfg, self.registry).code, "misdirected"
            )
            bad = request_for(bearer="Bearer nope", headers=[("Origin", "x")])
            self.assertEqual(
                gsv.check_handshake(bad, self.cfg, self.registry).code, "bad_request"
            )
            self.assertEqual(order, [])  # neither admit nor verify ran yet
            bad = request_for(bearer="Bearer nope")
            self.assertEqual(
                gsv.check_handshake(bad, self.cfg, self.registry).code, "unauthorized"
            )
            self.assertEqual(order, ["admit", "verify"])

    def test_t_srv_rate_sixth_attempt_is_429_before_the_bearer_is_verified(self):
        for _ in range(5):
            self.assertEqual(
                self.code(bearer="Bearer " + OTHER_TOKEN), (401, "unauthorized")
            )
        verify = mock.Mock(wraps=gsv.verify_bearer)
        with mock.patch.object(gsv, "verify_bearer", verify):
            self.assertEqual(self.code(), (429, "rate_limited"))  # even the GOOD token
        verify.assert_not_called()
        self.now += 60.0
        self.assertIsNone(self.check())

    def test_mf3_the_limit_is_per_ip_not_per_ip_and_port_and_forwarding_is_not_trusted(
        self,
    ):
        for port in range(40001, 40006):  # five failures, five DIFFERENT source ports
            self.assertEqual(
                self.code(peer=("10.9.8.7", port), bearer="Bearer " + OTHER_TOKEN),
                (401, "unauthorized"),
            )
        forwarded = [("X-Forwarded-For", "10.200.0.1"), ("Forwarded", "for=10.200.0.2")]
        verify = mock.Mock(wraps=gsv.verify_bearer)
        with mock.patch.object(gsv, "verify_bearer", verify):
            self.assertEqual(
                self.code(peer=("10.9.8.7", 40006)), (429, "rate_limited")
            )  # a NEW port, same IP: refused before the bearer is looked at
            self.assertEqual(
                self.code(peer=("10.9.8.7", 40007), headers=forwarded),
                (429, "rate_limited"),
            )  # a forwarding header names other IPs and buys no fresh budget
        verify.assert_not_called()
        self.assertIsNone(  # another peer is unaffected, even when it names the blocked one
            self.check(
                peer=("10.9.8.8", 40001),
                headers=[
                    ("X-Forwarded-For", "10.9.8.7"),
                    ("Forwarded", "for=10.9.8.7"),
                ],
            )
        )
        self.assertEqual(len(self.registry), 1)  # exactly one IP key was ever recorded

    def test_t_srv_hs_good_authentication_does_not_reset_failures(self):
        for _ in range(4):
            self.code(bearer="Bearer " + OTHER_TOKEN)
        self.assertIsNone(self.check())
        self.code(bearer="Bearer " + OTHER_TOKEN)
        self.assertEqual(self.code(), (429, "rate_limited"))

    def test_t_srv_hs_rejection_responses_are_fixed_and_reflect_nothing(self):
        table = {
            (405, "method_not_allowed"),
            (404, "not_found"),
            (421, "misdirected"),
            (400, "bad_request"),
            (429, "rate_limited"),
            (401, "unauthorized"),
            (409, "busy"),
            (500, "internal_error"),
        }
        for status, code in table:
            with self.subTest(code=code):
                rej = gsv.Rejection(status, code)
                resp = gsv.reject_response(rej)
                self.assertEqual(resp.status, status)
                self.assertEqual(resp.body, b'{"error":"' + code.encode() + b'"}')
                self.assertEqual(resp.headers["Content-Type"], "application/json")
                self.assertEqual(resp.headers["Connection"], "close")
                for forbidden in ("Location", "WWW-Authenticate", "Set-Cookie"):
                    self.assertNotIn(forbidden, resp.headers)
                self.assertLessEqual(
                    set(resp.headers), {"Content-Type", "Connection", "Content-Length"}
                )
        with self.assertRaises(ValueError):
            gsv.reject_response(gsv.Rejection(200, "ok"))
        with self.assertRaises(ValueError):
            gsv.reject_response(gsv.Rejection(401, "not_found"))

    def test_t_srv_hs_rejections_never_echo_request_values(self):
        bad_requests = (
            request_for(host=CANARY),
            request_for(path="/" + CANARY),
            request_for(bearer="Bearer " + CANARY),
            request_for(headers=[("Origin", CANARY)]),
            request_for(method="POST", headers=[("X-Canary", CANARY)]),
        )
        for request in bad_requests:
            rej = gsv.check_handshake(request, self.cfg, self.registry)
            self.assertIsNotNone(rej)
            body = gsv.reject_response(rej).body.decode()
            self.assertNotIn(CANARY, body + repr(rej) + str(rej))

    def test_t_srv_hs_a_real_websocket_handshake_is_ready_only_with_upgrade_headers(
        self,
    ):
        ok_headers = [
            ("Upgrade", "websocket"),
            ("Connection", "Upgrade"),
            ("Sec-WebSocket-Key", base64.b64encode(b"0123456789abcdef").decode()),
            ("Sec-WebSocket-Version", "13"),
        ]
        ws = gsv._new_ws()
        self.assertTrue(ws.can_prepare(request_for(headers=ok_headers)).ok)
        self.assertFalse(gsv._new_ws().can_prepare(request_for()).ok)
        self.assertEqual(ws._max_msg_size, 98304)
        self.assertFalse(ws.compress)


# =================================================================================
# Device / provider link adapters (fake sockets; no aiohttp network object)
# =================================================================================
def ws_msg(kind: aiohttp.WSMsgType, data: Any = None) -> aiohttp.WSMessage:
    return aiohttp.WSMessage(kind, data, None)


class FakeServerWS:
    """The slice of ``web.WebSocketResponse`` the device link uses."""

    def __init__(self) -> None:
        self.closed = False
        self.prepared = False
        self.sent: list[str] = []
        self.inbound: asyncio.Queue[Any] = asyncio.Queue()
        self.send_gate: asyncio.Event | None = None
        self.close_gate: asyncio.Event | None = None
        self.send_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.close_codes: list[int] = []
        self.ready = True

    def can_prepare(self, _request: Any) -> Any:
        return mock.Mock(ok=self.ready)

    async def prepare(self, _request: Any) -> None:
        self.prepared = True

    async def send_str(self, text: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        if self.send_gate is not None:
            await self.send_gate.wait()
        self.sent.append(text)

    async def receive(self) -> Any:
        return await self.inbound.get()

    async def close(self, *, code: int = 1000) -> bool:
        if self.close_error is not None:
            raise self.close_error
        if self.close_gate is not None:
            await self.close_gate.wait()  # the aiohttp close_wait that ignores cancel
        self.close_codes.append(code)
        self.closed = True
        return True


class FakeTransport:
    def __init__(self) -> None:
        self.aborts = 0

    def abort(self) -> None:
        self.aborts += 1


class DeviceLinkTest(OfflineAsyncCase):
    def setUp(self) -> None:
        super().setUp()
        self.ws = FakeServerWS()
        self.transport = FakeTransport()
        self.link = gsv.AiohttpDeviceLink(self.ws, self.transport)

    async def test_receive_maps_every_message_type(self):
        T = aiohttp.WSMsgType
        for kind, want in (
            (T.TEXT, ("text", "hi")),
            (T.BINARY, ("binary", None)),
            (T.CLOSE, ("closed", None)),
            (T.CLOSING, ("closed", None)),
            (T.CLOSED, ("closed", None)),
            (T.ERROR, ("closed", None)),
            (T.CONTINUATION, ("binary", None)),
        ):
            with self.subTest(kind=kind):
                self.ws.inbound.put_nowait(
                    ws_msg(kind, "hi" if kind is T.TEXT else b"")
                )
                msg = await self.link.receive()
                self.assertEqual((msg.kind, msg.data), want)
        self.assertNotIn("hi", repr(msg))

    async def test_send_text_writes_the_text_frame(self):
        await self.link.send_text("frame")
        self.assertEqual(self.ws.sent, ["frame"])
        self.assertEqual(self.transport.aborts, 0)

    async def test_t_srv_adapter_cancelled_send_aborts_the_transport(self):
        self.ws.send_gate = asyncio.Event()
        task = asyncio.create_task(self.link.send_text("x"))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.transport.aborts, 1)

    async def test_t_srv_adapter_cancel_at_close_wait_aborts_never_waits_for_the_peer(
        self,
    ):
        self.ws.close_gate = asyncio.Event()  # a stubborn peer: never answers the close
        task = asyncio.create_task(self.link.close(1000))
        await asyncio.sleep(0)
        self.assertEqual(self.transport.aborts, 0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):  # re-raised, never swallowed
            await task
        self.assertEqual(self.transport.aborts, 1)
        self.assertFalse(self.ws.closed)

    async def test_normal_close_uses_the_code_and_does_not_abort(self):
        await self.link.close(1011)
        self.assertEqual(self.ws.close_codes, [1011])
        self.assertEqual(self.transport.aborts, 0)

    async def test_errors_become_fixed_link_errors_after_an_abort(self):
        for name, setup, call in (
            (
                "send",
                lambda: setattr(self.ws, "send_error", ConnectionResetError(CANARY)),
                lambda: self.link.send_text("x"),
            ),
            (
                "close",
                lambda: setattr(self.ws, "close_error", RuntimeError(CANARY)),
                lambda: self.link.close(1000),
            ),
        ):
            with self.subTest(op=name):
                before = self.transport.aborts
                setup()
                with self.assertRaises(gsv.LinkError) as ctx:
                    await call()
                self.assertEqual(ctx.exception.code, "device_link")
                self.assertEqual(self.transport.aborts, before + 1)
                self.assertIsNone(ctx.exception.__cause__)
                self.assertIsNone(ctx.exception.__context__)
                self.assertNotIn(CANARY, str(ctx.exception) + repr(ctx.exception))
                self.ws.send_error = self.ws.close_error = None

    async def test_receive_error_is_a_fixed_link_error_and_cancel_is_not_an_abort(self):
        task = asyncio.create_task(self.link.receive())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(
            self.transport.aborts, 0
        )  # the reader is cancelled, then close()
        broken = FakeServerWS()
        broken.receive = mock.AsyncMock(side_effect=RuntimeError(CANARY))
        link = gsv.AiohttpDeviceLink(broken, self.transport)
        with self.assertRaises(gsv.LinkError) as ctx:
            await link.receive()
        self.assertNotIn(CANARY, str(ctx.exception))

    async def test_mf4_a_receive_error_aborts_the_transport_once_with_a_fixed_payloadless_error(
        self,
    ):
        broken = FakeServerWS()
        broken.receive = mock.AsyncMock(side_effect=ConnectionResetError(CANARY))
        link = gsv.AiohttpDeviceLink(broken, self.transport)
        with self.assertRaises(gsv.LinkError) as ctx:
            await link.receive()
        self.assertEqual(self.transport.aborts, 1)
        self.assertEqual(ctx.exception.args, ("device_link",))
        self.assertEqual(ctx.exception.code, "device_link")
        self.assertIsNone(ctx.exception.__cause__)
        self.assertIsNone(ctx.exception.__context__)
        self.assertNotIn(CANARY, str(ctx.exception) + repr(ctx.exception))

    async def test_abort_if_open_only_aborts_an_unclosed_socket(self):
        self.link.abort_if_open()
        self.assertEqual(self.transport.aborts, 1)
        self.ws.closed = True
        self.link.abort_if_open()
        self.assertEqual(self.transport.aborts, 1)
        gsv.AiohttpDeviceLink(self.ws, None).abort_if_open()  # a lost transport is fine

    async def test_repr_hides_everything(self):
        self.assertEqual(repr(self.link), "AiohttpDeviceLink(<hidden>)")


class FakeClientWS:
    def __init__(self) -> None:
        self.closed = False
        self.close_code: int | None = None
        self.sent: list[str] = []
        self.inbound: asyncio.Queue[Any] = asyncio.Queue()
        self.send_gate: asyncio.Event | None = None
        self.close_gate: asyncio.Event | None = None
        self.send_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.close_calls = 0

    async def send_str(self, text: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        if self.send_gate is not None:
            await self.send_gate.wait()
        self.sent.append(text)

    async def receive(self) -> Any:
        return await self.inbound.get()

    async def close(self) -> bool:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        if self.close_gate is not None:
            await self.close_gate.wait()
        self.closed = True
        return True


class FakeConnector:
    instances: ClassVar[list[FakeConnector]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closes: list[bool] = []
        self.close_error: BaseException | None = None
        FakeConnector.instances.append(self)

    async def close(self, *, abort_ssl: bool = False) -> None:
        self.closes.append(abort_ssl)
        if self.close_error is not None:
            raise self.close_error


class FakeClientSession:
    instances: ClassVar[list[FakeClientSession]] = []
    ws_behavior: Callable[[FakeClientSession], Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closes = 0
        self.connect_calls: list[tuple[Any, dict[str, Any]]] = []
        self.ws = FakeClientWS()
        FakeClientSession.instances.append(self)

    async def ws_connect(self, url: Any, **kwargs: Any) -> Any:
        self.connect_calls.append((url, kwargs))
        behavior = FakeClientSession.ws_behavior
        if behavior is not None:
            return await behavior(self)
        return self.ws

    async def close(self) -> None:
        self.closes += 1


class ProviderLinkTest(OfflineAsyncCase):
    def setUp(self) -> None:
        super().setUp()
        self.ws = FakeClientWS()
        self.session = FakeClientSession()
        self.connector = FakeConnector()
        self.link = gsv.AiohttpProviderLink(self.ws, self.session, self.connector)

    async def test_receive_maps_text_and_closed_with_the_clean_flag(self):
        T = aiohttp.WSMsgType
        self.ws.inbound.put_nowait(ws_msg(T.TEXT, "hello"))
        msg = await self.link.receive()
        self.assertEqual((msg.kind, msg.data, msg.clean), ("text", "hello", False))
        for kind, code, clean in (
            (T.CLOSE, 1000, True),
            # BUGFIX (final repair R1): aiohttp reports CLOSED with code 1000 for a bare
            # connection loss / EOF, so CLOSED is never a close confirmation, even at 1000.
            (T.CLOSED, 1000, False),
            (T.CLOSING, 1006, False),
            (T.CLOSE, None, False),
            (T.ERROR, 1000, False),  # an error is never a confirmation
            (T.BINARY, 1000, False),
        ):
            with self.subTest(kind=kind, code=code):
                self.ws.close_code = code
                self.ws.inbound.put_nowait(ws_msg(kind, b""))
                msg = await self.link.receive()
                self.assertEqual(
                    (msg.kind, msg.data, msg.clean), ("closed", None, clean)
                )

    async def _real_ws(self) -> aiohttp.ClientWebSocketResponse:
        """A REAL aiohttp client WebSocket over a real data queue: no socket, no parser."""
        loop = asyncio.get_running_loop()
        protocol = mock.Mock(_reading_paused=False)
        self.real_queue = WebSocketDataQueue(protocol, 2**16, loop=loop)
        writer = mock.Mock()
        writer.close = mock.AsyncMock()
        response = mock.Mock(connection=None)
        return aiohttp.ClientWebSocketResponse(
            self.real_queue,
            writer,
            None,
            response,
            DEFAULT_WS_CLIENT_TIMEOUT,
            True,
            True,
            loop,
        )

    async def test_r1_a_real_bare_eof_is_an_unclean_close_never_a_confirmation(self):
        """aiohttp's own receive() turns EOF into CLOSED with close_code 1000 (no frame)."""
        ws = await self._real_ws()
        link = gsv.AiohttpProviderLink(ws, self.session, self.connector)
        self.real_queue.feed_eof()
        msg = await link.receive()
        # the premise this defect rests on: a bare EOF really is CLOSED + 1000
        self.assertEqual(ws.close_code, 1000)
        self.assertEqual((msg.kind, msg.data, msg.clean), ("closed", None, False))

    async def test_r1_a_real_close_frame_is_clean_only_with_code_1000(self):
        for code, clean in ((1000, True), (1001, False), (1006, False), (1011, False)):
            with self.subTest(code=code):
                ws = await self._real_ws()
                link = gsv.AiohttpProviderLink(ws, self.session, self.connector)
                self.real_queue.feed_data(
                    aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, code, ""), 0
                )
                msg = await link.receive()
                self.assertEqual(
                    (msg.kind, msg.data, msg.clean), ("closed", None, clean)
                )

    async def test_send_text_writes_and_does_not_touch_the_connector(self):
        await self.link.send_text("frame")
        self.assertEqual(self.ws.sent, ["frame"])
        self.assertEqual(self.connector.closes, [])

    async def test_t_srv_adapter_cancelled_send_aborts_the_connector_and_reraises(self):
        self.ws.send_gate = asyncio.Event()
        task = asyncio.create_task(self.link.send_text("x"))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.connector.closes, [True])  # abort_ssl=True
        self.assertEqual(self.session.closes, 1)

    async def test_sf1_a_second_cancel_during_the_abort_still_closes_the_session(self):
        gate = asyncio.Event()

        class GatedConnector(FakeConnector):
            async def close(self, *, abort_ssl: bool = False) -> None:
                await gate.wait()
                await super().close(abort_ssl=abort_ssl)

        self.connector = GatedConnector()
        self.link = gsv.AiohttpProviderLink(self.ws, self.session, self.connector)
        self.ws.send_gate = asyncio.Event()
        task = asyncio.create_task(self.link.send_text("x"))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)  # the abort is now parked on the connector
        task.cancel()  # a second cancel lands on the abort wait
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.session.closes, 0)
        gate.set()  # the shielded abort finishes on its own
        await self.link._abort_task
        self.assertEqual(self.connector.closes, [True])
        self.assertEqual(self.session.closes, 1)

    async def test_t_srv_adapter_cancel_at_provider_close_wait_aborts(self):
        self.ws.close_gate = asyncio.Event()
        task = asyncio.create_task(self.link.close())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.connector.closes, [True])
        self.assertEqual(self.session.closes, 1)

    async def test_normal_close_closes_ws_then_session_once_without_abort(self):
        await self.link.close()
        await self.link.close()  # idempotent
        self.assertEqual((self.ws.close_calls, self.session.closes), (1, 1))
        self.assertEqual(self.connector.closes, [])

    async def test_errors_are_fixed_provider_errors_after_an_abort(self):
        for name, setup, call in (
            (
                "send",
                lambda: setattr(self.ws, "send_error", ConnectionResetError(CANARY)),
                lambda: self.link.send_text("x"),
            ),
            (
                "close",
                lambda: setattr(self.ws, "close_error", RuntimeError(CANARY)),
                lambda: self.link.close(),
            ),
        ):
            with self.subTest(op=name):
                self.setUp()
                setup()
                with self.assertRaises(gsv.ProviderError) as ctx:
                    await call()
                self.assertEqual(ctx.exception.code, "provider_link")
                self.assertEqual(self.connector.closes, [True])
                self.assertIsNone(ctx.exception.__cause__)
                self.assertIsNone(ctx.exception.__context__)
                self.assertNotIn(CANARY, str(ctx.exception) + repr(ctx.exception))

    async def test_receive_error_is_fixed_and_cancel_is_not_an_abort(self):
        task = asyncio.create_task(self.link.receive())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.connector.closes, [])
        self.ws.receive = mock.AsyncMock(side_effect=RuntimeError(CANARY))
        with self.assertRaises(gsv.ProviderError) as ctx:
            await self.link.receive()
        self.assertEqual(ctx.exception.code, "provider_link")
        self.assertNotIn(CANARY, str(ctx.exception))
        self.assertEqual(self.connector.closes, [True])

    async def test_a_failing_connector_close_never_masks_the_abort_result(self):
        self.connector.close_error = RuntimeError(CANARY)
        self.ws.send_error = RuntimeError("x")
        with self.assertRaises(gsv.ProviderError):
            await self.link.send_text("x")

    async def test_repr_hides_everything(self):
        self.assertEqual(repr(self.link), "AiohttpProviderLink(<hidden>)")


# =================================================================================
# connect_provider
# =================================================================================
class ConnectProviderTest(OfflineAsyncCase):
    def setUp(self) -> None:
        super().setUp()
        FakeConnector.instances = []
        FakeClientSession.instances = []
        FakeClientSession.ws_behavior = None
        self.addCleanup(setattr, FakeClientSession, "ws_behavior", None)
        for target, replacement in (
            ("TCPConnector", FakeConnector),
            ("ClientSession", FakeClientSession),
        ):
            patcher = mock.patch.object(aiohttp, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        # a REAL client context with no certificate loaded: nothing is read from disk
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        patcher = mock.patch.object(
            ssl, "create_default_context", return_value=self.ctx
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.key = gsv.Secret(KEY_CANARY)

    async def test_t_srv_provider_dial_is_exactly_the_reviewed_shape(self):
        link = await gsv.connect_provider(self.key, timeout=25.0)
        (connector,), (session,) = FakeConnector.instances, FakeClientSession.instances
        self.assertIsInstance(link, gsv.AiohttpProviderLink)
        self.assertIs(connector.kwargs["ssl"], self.ctx)
        self.assertEqual(set(connector.kwargs), {"ssl"})
        self.assertEqual(self.ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(self.ctx.check_hostname)
        self.assertEqual(self.ctx.minimum_version, ssl.TLSVersion.TLSv1_2)
        kw = session.kwargs
        self.assertIs(kw["connector"], connector)
        self.assertIs(kw["trust_env"], False)
        self.assertEqual(
            kw["timeout"],
            aiohttp.ClientTimeout(total=None, connect=25.0, sock_connect=25.0),
        )
        self.assertEqual(
            set(kw), {"connector", "trust_env", "trace_configs", "timeout"}
        )
        ((url, call),) = session.connect_calls
        self.assertEqual(url, "wss://api.openai.com/v1/live/sessions")
        self.assertEqual(url, gp.PROVIDER_URL)
        self.assertEqual(call["headers"], {"Authorization": "Bearer " + KEY_CANARY})
        self.assertEqual(call["max_msg_size"], 1048576)
        self.assertEqual(call["compress"], 0)
        self.assertIs(call["autoping"], True)
        self.assertEqual(set(call), {"headers", "max_msg_size", "compress", "autoping"})
        self.assertEqual(connector.closes, [])  # a successful dial leaves it open
        self.assertNotIn(KEY_CANARY, repr(link))

    async def test_t_srv_provider_the_redirect_hook_refuses_before_any_follow(self):
        await gsv.connect_provider(self.key, timeout=5.0)
        (trace,) = FakeClientSession.instances[0].kwargs["trace_configs"]
        callbacks = list(trace.on_request_redirect)
        self.assertEqual(len(callbacks), 1)
        for params in (None, object()):  # even a redirect with no Location at all
            with self.assertRaises(gsv.ProviderError) as ctx:
                await callbacks[0](None, None, params)
            self.assertEqual(ctx.exception.code, "provider_redirect")
        for signal in (
            trace.on_request_start,
            trace.on_request_headers_sent,
            trace.on_request_end,
            trace.on_request_exception,
            trace.on_connection_create_start,
        ):
            self.assertEqual(list(signal), [])  # no hook ever sees a header

    async def test_t_srv_provider_a_redirect_error_propagates_unchanged_and_aborts(
        self,
    ):
        async def behavior(_s: Any) -> Any:
            raise gsv.ProviderError("provider_redirect")

        FakeClientSession.ws_behavior = behavior
        with self.assertRaises(gsv.ProviderError) as ctx:
            await gsv.connect_provider(self.key, timeout=5.0)
        self.assertEqual(ctx.exception.code, "provider_redirect")
        self.assertEqual(FakeConnector.instances[0].closes, [True])

    async def test_t_srv_provider_any_other_failure_is_fixed_and_carries_no_text(self):
        async def behavior(_s: Any) -> Any:
            raise RuntimeError("Authorization: Bearer " + KEY_CANARY)

        FakeClientSession.ws_behavior = behavior
        with self.assertRaises(gsv.ProviderError) as ctx:
            await gsv.connect_provider(self.key, timeout=5.0)
        error = ctx.exception
        self.assertEqual(error.code, "provider_connect")
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn(KEY_CANARY, str(error) + repr(error) + repr(error.args))
        self.assertEqual(FakeConnector.instances[0].closes, [True])
        self.assertEqual(FakeClientSession.instances[0].closes, 1)

    async def test_t_srv_provider_the_handshake_is_bounded_by_an_outer_timeout(self):
        async def behavior(_s: Any) -> Any:
            await asyncio.Event().wait()

        FakeClientSession.ws_behavior = behavior
        with self.assertRaises(gsv.ProviderError) as ctx:
            await gsv.connect_provider(self.key, timeout=0.05)
        self.assertEqual(ctx.exception.code, "provider_connect")
        self.assertEqual(FakeConnector.instances[0].closes, [True])

    async def test_t_srv_provider_cancel_closes_the_created_session_and_connector(self):
        gate = asyncio.Event()

        async def behavior(_s: Any) -> Any:
            await gate.wait()

        FakeClientSession.ws_behavior = behavior
        task = asyncio.create_task(gsv.connect_provider(self.key, timeout=25.0))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(FakeConnector.instances[0].closes, [True])
        self.assertEqual(FakeClientSession.instances[0].closes, 1)

    async def test_t_srv_provider_a_late_connect_result_after_cancel_is_closed(self):
        gate = asyncio.Event()

        async def behavior(session: FakeClientSession) -> Any:
            try:
                await gate.wait()
            except asyncio.CancelledError:
                return session.ws  # swallows the cancel and still hands back a socket

        FakeClientSession.ws_behavior = behavior
        task = asyncio.create_task(gsv.connect_provider(self.key, timeout=25.0))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(FakeConnector.instances[0].closes, [True])
        self.assertEqual(FakeClientSession.instances[0].closes, 1)

    async def test_a_session_constructor_failure_leaves_no_orphan_connector(self):
        def boom(**_kw: Any) -> Any:
            raise RuntimeError(CANARY)

        with (
            mock.patch.object(aiohttp, "ClientSession", boom),
            self.assertRaises(gsv.ProviderError) as ctx,
        ):
            await gsv.connect_provider(self.key, timeout=5.0)
        self.assertEqual(ctx.exception.code, "provider_connect")
        self.assertEqual(FakeConnector.instances[0].closes, [True])

    async def test_client_ssl_is_verified_tls12_even_if_the_factory_is_lax(self):
        lax = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        lax.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        with mock.patch.object(ssl, "create_default_context", return_value=lax):
            ctx = gsv._client_ssl()
        self.assertEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)


# =================================================================================
# _start_site (real aiohttp objects, no listener) / signals / null logger
# =================================================================================
class StartSiteTest(OfflineAsyncCase):
    def setUp(self) -> None:
        super().setUp()
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="pet-gw-u3-")))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.cfg = make_cfg(self.base)
        self.ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.sites: list[Any] = []
        self.runners: list[web.ServerRunner] = []
        real_runner = web.ServerRunner

        def runner_spy(*args: Any, **kwargs: Any) -> web.ServerRunner:
            runner = real_runner(*args, **kwargs)
            self.runners.append(runner)
            return runner

        self.runner_kwargs: list[dict[str, Any]] = []
        self.server_kwargs: list[dict[str, Any]] = []
        real_server = web.Server

        def server_spy(*args: Any, **kwargs: Any) -> web.Server:
            self.server_kwargs.append(kwargs)
            return real_server(*args, **kwargs)

        for target, spy in (("ServerRunner", runner_spy), ("Server", server_spy)):
            patcher = mock.patch.object(web, target, spy)
            patcher.start()
            self.addCleanup(patcher.stop)

        sites = self.sites

        class Site:
            fail: BaseException | None = None

            def __init__(
                self, runner: Any, host: Any, port: Any, **kwargs: Any
            ) -> None:
                self.args = (runner, host, port)
                self.kwargs = kwargs
                sites.append(self)

            async def start(self) -> None:
                if Site.fail is not None:
                    raise Site.fail

        self.Site = Site
        patcher = mock.patch.object(web, "TCPSite", Site)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_t_srv_site_is_built_from_the_reviewed_pieces(self):
        async def handler(_request: Any) -> Any:
            raise AssertionError("never called")

        closer = await gsv._start_site(handler, self.cfg, self.ssl_ctx)
        (kwargs,) = self.server_kwargs
        self.assertIsNone(kwargs["access_log"])
        self.assertIsInstance(kwargs["logger"], gsv._NullLogger)
        self.assertEqual(set(kwargs), {"access_log", "logger"})
        (site,) = self.sites
        self.assertIs(site.args[0], self.runners[0])
        self.assertEqual(site.args[1:], ("10.1.2.3", 8941))
        self.assertEqual(
            site.kwargs,
            {"ssl_context": self.ssl_ctx, "reuse_address": True, "reuse_port": False}
        )
        self.assertNotEqual(self.runners[0].server, None)
        self.assertEqual(self.runners[0]._shutdown_timeout, gsv._RUNNER_SHUTDOWN_S)
        await closer()
        self.assertIsNone(self.runners[0].server)  # runner cleaned up

    async def test_t_srv_site_a_start_failure_cleans_the_partial_runner(self):
        self.Site.fail = OSError(CANARY)

        async def handler(_request: Any) -> Any:
            raise AssertionError("never called")

        with self.assertRaises(OSError):
            await gsv._start_site(handler, self.cfg, self.ssl_ctx)
        self.assertIsNone(self.runners[0].server)

    async def test_null_logger_absorbs_everything(self):
        logger = gsv._NullLogger()
        for name in ("debug", "info", "warning", "error", "exception", "critical"):
            self.assertIsNone(getattr(logger, name)("canary %s", CANARY, exc_info=True))
        self.assertIsNone(logger.log(10, CANARY))
        self.assertFalse(logger.isEnabledFor(10))

    async def test_t_srv_privacy_aiohttp_server_logging_is_off(self):
        async def handler(_request: Any) -> Any:
            raise AssertionError("never called")

        server = web.Server(
            handler, access_log=None, logger=gsv._NullLogger()
        )  # the same kwargs as _start_site
        protocol = server()
        self.assertIsInstance(protocol.logger, gsv._NullLogger)
        self.assertIsNone(protocol.access_log)
        self.assertIsNone(protocol.access_logger)
        with self.assertNoLogs("aiohttp", level="DEBUG"):
            protocol.log_exception("Error handling request %s", CANARY)
            protocol.log_debug("debug %s", CANARY)
            protocol.logger.warning("warn %s", CANARY)

    async def test_wait_for_signals_registers_both_and_removes_them(self):
        loop = asyncio.get_running_loop()
        added: dict[int, Callable[[], None]] = {}
        removed: list[int] = []
        with (
            mock.patch.object(
                loop,
                "add_signal_handler",
                side_effect=lambda s, cb: added.update({s: cb}),
            ),
            mock.patch.object(
                loop, "remove_signal_handler", side_effect=removed.append
            ),
        ):
            task = asyncio.create_task(gsv._wait_for_signals())
            await asyncio.sleep(0)
            self.assertEqual(
                set(added),
                {int(__import__("signal").SIGINT), int(__import__("signal").SIGTERM)},
            )
            self.assertFalse(task.done())
            next(iter(added.values()))()
            await task
        self.assertEqual(set(removed), set(added))


# =================================================================================
# _Gateway: handler ordering, the one client slot, custody until the runner is idle
# =================================================================================
async def until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.001)


class StuckSession:
    """A session whose ``run`` never returns on its own and ignores a shutdown request."""

    def __init__(self) -> None:
        self.shutdowns = 0
        self.cancelled = 0

    async def request_shutdown(self) -> None:
        self.shutdowns += 1

    async def run(self) -> Any:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    @property
    def outcome(self) -> gc.SessionOutcome:
        return gc.SessionOutcome("CLOSED", False, None, None, None, False, 0, 0, 0)


class GatewayCase(OfflineAsyncCase):
    async def asyncSetUp(self) -> None:
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="pet-gw-u3-")))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.cfg = make_cfg(self.base)
        self.h = Harness()
        self.h.runner.start()
        self.addAsyncCleanup(self.h.aclose)
        self.connects: list[float] = []
        self.sockets: list[FakeServerWS] = []
        self.devices: list[Any] = [self.h.device]
        self.transports: list[Any] = []
        self.registry = gsv.AuthRegistry(clock=lambda: self.h.clock.now())
        self.deps = gsv.ServeDeps(
            provider_connect=self.connect, clock=lambda: self.h.clock
        )
        self.gw = gsv._Gateway(
            self.cfg,
            gsv.Secret(KEY_CANARY),
            self.registry,
            self.h.runner,
            self.h.ops,
            self.deps,
            ws_factory=self.new_ws,
            device_factory=self.new_device,
        )

    async def connect(self, key: gsv.Secret, *, timeout: float) -> Any:
        self.assertEqual(key.reveal(), KEY_CANARY)
        self.connects.append(timeout)
        return self.h.provider

    def new_ws(self) -> FakeServerWS:
        ws = FakeServerWS()
        self.sockets.append(ws)
        return ws

    def new_device(self, ws: Any, transport: Any) -> Any:
        self.transports.append(transport)
        return self.devices.pop(0) if self.devices else FakeDeviceLink(self.h.clock)

    def request(self, **kw: Any) -> Any:
        return request_for(**kw)

    def sha(self) -> str:
        return hashlib.sha256(b"hello world").hexdigest()

    def confirm_frame(self) -> dict[str, Any]:
        return {
            "type": "pet.turn.send_confirm",
            "turn_id": TURN,
            "text_sha256": self.sha(),
            "preview_revision": 1,
        }

    async def to_blocked_confirm(self) -> asyncio.Task[Any]:
        """Start a request and drive its session to a CONFIRM blocked on the worker."""
        self.release = self.h.ops.hold("confirm")
        task = asyncio.create_task(self.gw.handle(self.request()))
        await self.h.feed(ASSISTANT_START)
        await self.h.feed({"type": "pet.turn.begin"})
        await self.h.push(delegation())
        await self.h.push(delta("e1", "hello world", 0, 500))
        await self.h.feed({"type": "pet.turn.capture_stop", "turn_id": TURN})
        await self.h.clock.advance(1.5)
        await self.h.feed(self.confirm_frame())
        self.assertTrue(self.h.ops.entered["confirm"].is_set())
        return task


class GatewayOrderTest(GatewayCase):
    async def test_t_srv_order_01_rejections_never_prepare_connect_or_take_the_slot(
        self,
    ):
        bad = {
            "method": dict(method="POST"),
            "path": dict(path="/x"),
            "host": dict(host="evil.example"),
            "origin": dict(headers=[("Origin", "https://x")]),
            "bearer": dict(bearer="Bearer " + OTHER_TOKEN),
            "no bearer": dict(bearer=None),
            "no peer": dict(peer=None),
        }
        for name, kw in bad.items():
            with self.subTest(case=name):
                resp = await self.gw.handle(self.request(**kw))
                self.assertIn(resp.status, (400, 401, 404, 405, 421))
                self.assertNotIn("Location", resp.headers)
        self.assertEqual(self.sockets, [])  # not even a WebSocketResponse was built
        self.assertEqual(self.connects, [])
        self.assertFalse(self.gw.busy)
        self.assertEqual(self.h.ops.calls, [])
        self.assertEqual(self.h.runner.depth, 0)
        self.assertEqual(self.gw.sessions_started, 0)

    async def test_t_srv_order_01_a_rate_limited_request_never_reaches_the_provider(
        self,
    ):
        for _ in range(5):
            await self.gw.handle(self.request(bearer="Bearer " + OTHER_TOKEN))
        resp = await self.gw.handle(self.request())
        self.assertEqual(resp.status, 429)
        self.assertEqual((self.sockets, self.connects), ([], []))

    async def test_a_websocket_that_cannot_be_prepared_is_a_400_and_takes_no_slot(self):
        self.gw._ws_factory = lambda: setattr(w := FakeServerWS(), "ready", False) or w
        resp = await self.gw.handle(self.request())
        self.assertEqual((resp.status, resp.body), (400, b'{"error":"bad_request"}'))
        self.assertFalse(self.gw.busy)
        self.assertEqual(self.connects, [])

    async def test_the_provider_is_dialled_only_after_an_authenticated_session_start(
        self,
    ):
        request = self.request()
        task = asyncio.create_task(self.gw.handle(request))
        await self.h.quiesce()
        self.assertTrue(self.gw.busy)
        self.assertEqual(self.connects, [])  # authenticated and upgraded, still no dial
        self.assertTrue(self.sockets[0].prepared)
        await self.h.feed(ASSISTANT_START)
        self.assertEqual(self.connects, [gs.START_DEADLINE_S])
        await self.h.feed({"type": "session.close"})
        resp = await asyncio.wait_for(task, 5)
        self.assertIs(resp, self.sockets[0])
        self.assertIs(self.transports[0], request.transport)
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.gw.sessions_started, 1)
        self.assertEqual(self.gw.last_outcome.source, "session")
        self.assertIsNone(self.gw.last_outcome.send_outcome)

    async def test_t_srv_lock_01_a_second_authenticated_client_is_busy_409(self):
        task = asyncio.create_task(self.gw.handle(self.request()))
        await self.h.quiesce()
        resp = await self.gw.handle(self.request())
        self.assertEqual((resp.status, resp.body), (409, b'{"error":"busy"}'))
        self.assertNotIn("Location", resp.headers)
        self.assertEqual(sum(1 for s in self.sockets if s.prepared), 1)
        self.assertEqual(self.connects, [])
        await self.h.feed(ASSISTANT_START)
        resp = await self.gw.handle(
            self.request()
        )  # loopback gets no exemption here either
        self.assertEqual(resp.status, 409)
        self.assertEqual(self.connects, [gs.START_DEADLINE_S])  # still ONE dial
        await self.h.feed({"type": "session.close"})
        await asyncio.wait_for(task, 5)

    async def test_t_srv_lock_02_the_slot_is_free_after_the_session_settles(self):
        task = asyncio.create_task(self.gw.handle(self.request()))
        await self.h.quiesce()
        await self.h.feed({"type": "session.close"})
        await asyncio.wait_for(task, 5)
        await until(lambda: not self.gw.busy)
        self.devices.append(FakeDeviceLink(self.h.clock))
        self.devices[0].disconnect()
        again = await asyncio.wait_for(self.gw.handle(self.request()), 5)
        self.assertIs(again, self.sockets[-1])
        self.assertEqual(self.gw.sessions_started, 2)

    async def test_a_failure_before_the_session_exists_is_fixed_and_frees_the_slot(
        self,
    ):
        def boom() -> Any:
            raise RuntimeError(CANARY)

        self.gw._ws_factory = boom
        resp = await self.gw.handle(self.request())
        self.assertEqual((resp.status, resp.body), (500, b'{"error":"internal_error"}'))
        self.assertFalse(self.gw.busy)

    async def test_a_failure_after_the_upgrade_closes_1011_and_frees_the_slot(self):
        def boom(_device: Any, _connect: Any) -> Any:
            raise RuntimeError(CANARY)

        self.gw._session_factory = boom
        resp = await self.gw.handle(self.request())
        self.assertIs(resp, self.sockets[0])
        self.assertEqual(self.sockets[0].close_codes, [1011])
        self.assertFalse(self.gw.busy)
        self.assertEqual(self.gw.sessions_started, 0)

    async def test_a_cancel_during_the_upgrade_takes_no_custody(self):
        ws = FakeServerWS()
        gate = asyncio.Event()

        async def slow_prepare(_request: Any) -> None:
            await gate.wait()

        ws.prepare = slow_prepare
        self.gw._ws_factory = lambda: ws
        task = asyncio.create_task(self.gw.handle(self.request()))
        await asyncio.sleep(0)
        self.assertTrue(self.gw.busy)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.gw.busy)
        self.assertEqual(self.gw.sessions_started, 0)

    async def test_make_handler_is_the_gateway_handle(self):
        self.assertEqual(gsv._make_handler(self.gw), self.gw.handle)


class GatewayCustodyTest(GatewayCase):
    async def test_t_srv_lock_waitidle_a_cancelled_request_keeps_the_slot_until_reconciled(
        self,
    ):
        task = await self.to_blocked_confirm()
        task.cancel()  # the request is cancelled while CONFIRM runs on the worker
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.h.quiesce()
        self.assertEqual(self.h.runner.inflight_op, "confirm")
        self.assertTrue(self.gw.busy)  # never freed by the cancel
        self.assertIsNone(self.gw.last_outcome)
        resp = await self.gw.handle(self.request())  # a second authenticated client
        self.assertEqual((resp.status, resp.body), (409, b'{"error":"busy"}'))
        self.assertEqual(self.connects, [gs.START_DEADLINE_S])
        self.release.set()  # the worker finally returns
        await until(lambda: not self.gw.busy)
        outcome = self.gw.last_outcome
        self.assertEqual(outcome.send_outcome, "accepted")  # the exact core outcome
        self.assertEqual(
            outcome.source, "result"
        )  # the direct take_result path, exactly
        self.assertEqual(
            self.h.ops.count("confirm"), 1
        )  # exactly one send, never retried
        self.assertEqual(self.h.runner.held_results(), ())  # taken once, nothing left
        self.assertEqual(self.h.runner.inflight_op, None)
        self.devices.append(FakeDeviceLink(self.h.clock))
        self.devices[0].disconnect()
        again = await asyncio.wait_for(
            self.gw.handle(self.request()), 5
        )  # eligible again
        self.assertTrue(again.prepared)
        self.assertEqual(self.h.ops.count("confirm"), 1)

    async def test_t_srv_lock_waitidle_a_second_cancel_cannot_lose_the_ticket(self):
        task = await self.to_blocked_confirm()
        task.cancel()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.h.quiesce()
        custody = self.gw._custody_task
        await until(lambda: self.gw._run_task.done())
        await self.h.quiesce()  # the custody is now parked in the runner-idle wait
        custody.cancel()  # a second, loop-level cancel while waiting for the worker
        await asyncio.gather(custody, return_exceptions=True)
        self.assertTrue(self.gw.busy)  # fail closed: still not settled
        self.assertIsNone(self.gw.last_outcome)
        self.release.set()
        await self.gw.shutdown()  # the teardown settles it, exactly once
        self.assertFalse(self.gw.busy)
        self.assertEqual(self.gw.last_outcome.send_outcome, "accepted")
        self.assertEqual(self.h.ops.count("confirm"), 1)
        self.assertEqual(self.h.runner.held_results(), ())

    async def test_t_srv_lock_waitidle_an_evicted_job_result_is_uncertain_not_success(
        self,
    ):
        task = await self.to_blocked_confirm()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.h.quiesce()
        original = self.h.runner.take_result

        def evicted(job: int) -> Any:
            original(job)  # the real result is consumed, then reported unavailable
            raise gc.RunnerError("runner_result_unavailable")

        with mock.patch.object(self.h.runner, "take_result", evicted):
            self.release.set()
            await until(lambda: not self.gw.busy)
        self.assertEqual(self.gw.last_outcome.send_outcome, "uncertain")
        self.assertEqual(self.gw.last_outcome.source, "unavailable")
        self.assertEqual(self.h.ops.count("confirm"), 1)  # no resend, no new id

    async def test_an_exception_from_the_confirm_job_is_uncertain_metadata_only(self):
        self.h.ops.results["confirm"] = core.BridgeError("journal_error")
        task = await self.to_blocked_confirm()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.h.quiesce()
        self.release.set()
        await until(lambda: not self.gw.busy)
        self.assertEqual(self.gw.last_outcome.send_outcome, "uncertain")
        self.assertEqual(self.gw.last_outcome.source, "exception")
        self.assertNotIn("journal", repr(self.gw.last_outcome))

    async def test_t_srv_privacy_reconciliation_and_gateway_repr_carry_no_secret(self):
        self.assertNotIn(KEY_CANARY, repr(self.gw))
        self.assertNotIn(self.cfg.session_id, repr(self.gw))
        self.assertNotIn(GOOD_HASH, repr(self.gw) + repr(self.registry))


class GatewayShutdownTest(GatewayCase):
    async def test_shutdown_stops_the_active_session_and_settles(self):
        task = asyncio.create_task(self.gw.handle(self.request()))
        await self.h.feed(ASSISTANT_START)
        self.assertTrue(self.gw.busy)
        await asyncio.wait_for(self.gw.shutdown(), 5)
        await asyncio.wait_for(task, 5)
        self.assertFalse(self.gw.busy)
        self.assertFalse(self.gw.forced_cancel)
        self.assertEqual(self.h.provider.types().count("session.close"), 1)
        resp = await self.gw.handle(self.request())  # closing: nobody new gets in
        self.assertEqual(resp.status, 409)

    async def test_shutdown_without_a_session_is_a_noop_and_closes_the_door(self):
        await self.gw.shutdown()
        self.assertTrue(self.gw.closing)
        resp = await self.gw.handle(self.request())
        self.assertEqual((resp.status, resp.body), (409, b'{"error":"busy"}'))
        self.assertEqual(self.sockets[-1].prepared, False)

    async def test_t_srv_shutdown_bound_is_the_remaining_cap_never_an_extension(self):
        stuck = StuckSession()
        self.gw._session_factory = lambda device, connect: stuck
        task = asyncio.create_task(self.gw.handle(self.request()))
        await self.h.quiesce()
        t0 = self.h.clock.now()
        self.assertEqual(self.gw._shutdown_bound(), gs.CLOSE_DEADLINE_S + 1.0)
        await self.h.clock.advance(60.0)  # 10 s of the outer cap left
        self.assertAlmostEqual(self.gw._shutdown_bound(), 10.0)
        await self.h.clock.advance(10.0)
        self.assertEqual(self.gw._shutdown_bound(), 0.0)
        self.assertGreaterEqual(self.h.clock.now() - t0, 70.0)
        shutdown = asyncio.create_task(self.gw.shutdown())
        await self.h.quiesce()
        self.assertEqual(stuck.shutdowns, 1)
        await self.h.clock.advance(0.0)
        await asyncio.wait_for(shutdown, 5)
        await asyncio.wait_for(task, 5)
        self.assertTrue(self.gw.forced_cancel)  # truthful: the run had to be cancelled
        self.assertGreaterEqual(stuck.cancelled, 1)
        self.assertFalse(self.gw.busy)

    async def test_a_stuck_run_is_cancelled_after_the_bound_not_before(self):
        stuck = StuckSession()
        self.gw._session_factory = lambda device, connect: stuck
        task = asyncio.create_task(self.gw.handle(self.request()))
        await self.h.quiesce()
        shutdown = asyncio.create_task(self.gw.shutdown())
        await self.h.quiesce()
        await self.h.clock.advance(15.9)
        self.assertEqual(stuck.cancelled, 0)
        self.assertFalse(shutdown.done())
        await self.h.clock.advance(0.2)
        await asyncio.wait_for(shutdown, 5)
        await asyncio.wait_for(task, 5)
        self.assertEqual(stuck.cancelled, 1)
        self.assertTrue(self.gw.forced_cancel)


# =================================================================================
# Final repair v1: shutdown during the upgrade, run-task scheduling, custody edge cases
# =================================================================================
class BlockedPrepareWS(FakeServerWS):
    """A server WebSocket whose ``prepare`` (the upgrade) waits for the test."""

    def __init__(self) -> None:
        super().__init__()
        self.prepare_gate = asyncio.Event()
        self.prepare_started = asyncio.Event()

    async def prepare(self, _request: Any) -> None:
        self.prepare_started.set()
        await self.prepare_gate.wait()
        self.prepared = True


class DeviceSpy(FakeDeviceLink):
    """A scripted device link that also counts ``abort_if_open`` calls."""

    def __init__(self, clock: Any) -> None:
        super().__init__(clock)
        self.abort_calls = 0

    def abort_if_open(self) -> None:
        self.abort_calls += 1


class RaisingSession(StuckSession):
    async def run(self) -> Any:
        raise RuntimeError(CANARY)


class QuickSession(StuckSession):
    async def run(self) -> Any:
        return self.outcome


class SwallowingSession(StuckSession):
    """Swallows the FIRST cancel and blocks again; only the SECOND cancel ends it."""

    async def run(self) -> Any:
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                if self.cancelled >= 2:
                    raise


class GatewayFinalRepairTest(GatewayCase):
    def own(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """Cancel a handler task at cleanup so a failing assertion cannot leave it running."""

        async def settle() -> None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(settle)
        return task

    def assertNothingStarted(self) -> None:
        self.assertEqual(self.gw.sessions_started, 0)
        self.assertFalse(self.gw.busy)
        self.assertIsNone(self.gw._custody_task)
        self.assertEqual(self.connects, [])  # no provider dial
        self.assertEqual(self.transports, [])  # no device link was even built
        self.assertEqual(self.h.ops.calls, [])  # no core job
        self.assertEqual(self.h.runner.depth, 0)
        self.assertEqual(self.h.device.frames, [])

    async def test_r2_shutdown_while_the_upgrade_is_pending_starts_no_session(self):
        ws = BlockedPrepareWS()
        self.gw._ws_factory = lambda: ws
        request = self.request()
        task = self.own(asyncio.create_task(self.gw.handle(request)))
        await asyncio.wait_for(ws.prepare_started.wait(), 5)
        self.assertTrue(self.gw.busy)
        await asyncio.wait_for(self.gw.shutdown(), 5)  # nothing to settle yet: returns
        self.assertTrue(self.gw.closing)
        ws.prepare_gate.set()  # the upgrade finishes AFTER shutdown returned
        await self.h.quiesce()
        self.assertEqual(self.gw.sessions_started, 0)  # asserted BEFORE any wait
        self.assertEqual(self.connects, [])
        resp = await asyncio.wait_for(task, 5)
        self.assertIs(resp, ws)
        self.assertEqual(ws.close_codes, [1001])  # fixed shutdown code, never a session
        request.transport.abort.assert_not_called()
        await self.h.quiesce()
        self.assertNothingStarted()
        again = await self.gw.handle(self.request())  # the door stays shut
        self.assertEqual(again.status, 409)

    async def test_r2_a_failed_close_after_a_late_upgrade_aborts_the_socket(self):
        ws = BlockedPrepareWS()
        ws.close_error = RuntimeError(CANARY)
        self.gw._ws_factory = lambda: ws
        request = self.request()
        task = self.own(asyncio.create_task(self.gw.handle(request)))
        await asyncio.wait_for(ws.prepare_started.wait(), 5)
        await self.gw.shutdown()
        ws.prepare_gate.set()
        await self.h.quiesce()
        self.assertEqual(self.gw.sessions_started, 0)  # asserted BEFORE any wait
        resp = await asyncio.wait_for(task, 5)
        self.assertIs(resp, ws)
        request.transport.abort.assert_called_once_with()
        self.assertNothingStarted()

    async def test_r2_a_cancel_after_shutdown_during_the_upgrade_leaks_nothing(self):
        ws = BlockedPrepareWS()
        self.gw._ws_factory = lambda: ws
        task = asyncio.create_task(self.gw.handle(self.request()))
        await asyncio.wait_for(ws.prepare_started.wait(), 5)
        await self.gw.shutdown()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.h.quiesce()
        self.assertNothingStarted()
        self.assertEqual(ws.close_codes, [])

    async def test_l2_a_second_session_scheduled_but_not_started_is_bounded_by_its_own_run(
        self,
    ):
        task1 = asyncio.create_task(self.gw.handle(self.request()))
        await self.h.quiesce()
        await self.h.feed({"type": "session.close"})
        await asyncio.wait_for(task1, 5)
        await until(lambda: not self.gw.busy)
        old_run = self.gw._run_task
        self.assertTrue(old_run is None or old_run.done())
        stuck = StuckSession()
        self.gw._session_factory = lambda device, connect: stuck
        self.devices.append(FakeDeviceLink(self.h.clock))
        task2 = asyncio.create_task(self.gw.handle(self.request()))
        await asyncio.sleep(0)  # custody is scheduled; it has NOT started yet
        self.assertTrue(self.gw.busy)
        self.assertIsNotNone(self.gw._custody_task)
        self.assertFalse(self.gw._custody_task.done())
        self.assertEqual(stuck.cancelled, 0)
        self.assertIsNot(self.gw._run_task, old_run)  # reset for THIS session
        t0 = self.h.clock.now()
        shutdown = asyncio.create_task(self.gw.shutdown())
        await self.h.quiesce()
        await self.h.clock.advance(
            16.1
        )  # the session's own remaining bound, not 60 + 16
        await asyncio.wait_for(shutdown, 2)
        await asyncio.wait_for(task2, 2)
        self.assertLess(self.h.clock.now() - t0, 20.0)
        self.assertEqual(stuck.cancelled, 1)
        self.assertEqual(stuck.shutdowns, 1)
        self.assertTrue(self.gw.forced_cancel)
        self.assertFalse(self.gw.busy)

    async def test_l1_a_raw_core_refusal_is_reported_as_the_core_named_it(self):
        """``Reconciliation.send_outcome`` is the RAW core name; PetSession may say uncertain."""
        self.h.ops.results["confirm"] = core.CommandResult(
            TURN, "refused", "journal_error", False
        )
        task = await self.to_blocked_confirm()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.h.quiesce()
        self.release.set()
        await until(lambda: not self.gw.busy)
        self.assertEqual(
            (self.gw.last_outcome.send_outcome, self.gw.last_outcome.source),
            ("refused", "result"),
        )
        self.assertEqual(self.h.ops.count("confirm"), 1)

    async def test_sf2_an_unknown_confirm_outcome_name_is_uncertain(self):
        self.h.ops.results["confirm"] = core.CommandResult(TURN, "weird", "x", False)
        task = await self.to_blocked_confirm()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.h.quiesce()
        self.release.set()
        await until(lambda: not self.gw.busy)
        self.assertEqual(
            (self.gw.last_outcome.send_outcome, self.gw.last_outcome.source),
            ("uncertain", "result"),
        )
        self.assertNotIn(
            "weird", repr(self.gw.last_outcome)
        )  # the unknown name is not kept

    async def test_mf4_a_run_that_raises_aborts_the_device_once_and_frees_only_when_idle(
        self,
    ):
        spy = DeviceSpy(self.h.clock)
        self.devices[:] = [spy]
        self.gw._session_factory = lambda device, connect: RaisingSession()
        entered, release = threading.Event(), threading.Event()

        def slow() -> None:
            entered.set()
            release.wait(5)

        self.h.runner.submit("status", slow)  # the worker is busy with a sync call
        await until(entered.is_set)
        resp = await asyncio.wait_for(self.gw.handle(self.request()), 5)
        self.assertIs(resp, self.sockets[0])  # a fixed outcome: nothing is raised
        self.assertEqual(spy.abort_calls, 1)
        self.assertTrue(self.gw.busy)  # not freed before the runner is idle
        self.assertIsNone(self.gw.last_outcome)
        release.set()
        await until(lambda: not self.gw.busy)
        self.assertEqual(spy.abort_calls, 1)
        self.assertEqual(self.gw.last_outcome.source, "session")
        self.assertEqual(self.h.runner.depth, 0)

    async def test_mf4_a_swallowed_first_cancel_gets_a_second_one_within_the_bound(
        self,
    ):
        spy = DeviceSpy(self.h.clock)
        self.devices[:] = [spy]
        session = SwallowingSession()
        self.gw._session_factory = lambda device, connect: session
        task = self.own(asyncio.create_task(self.gw.handle(self.request())))
        await self.h.quiesce()
        t0 = self.h.clock.now()
        shutdown = self.own(asyncio.create_task(self.gw.shutdown()))
        await self.h.quiesce()
        await self.h.clock.advance(16.1)  # the bound: first cancel
        self.assertEqual(session.cancelled, 1)
        self.assertFalse(shutdown.done())  # swallowed: shutdown keeps waiting, bounded
        self.assertEqual(spy.abort_calls, 0)
        await self.h.clock.advance(15.1)  # CLOSE_DEADLINE_S later: the second cancel
        await self.h.quiesce()
        self.assertEqual(session.cancelled, 2)  # asserted BEFORE any wait
        await asyncio.wait_for(shutdown, 2)
        await asyncio.wait_for(task, 2)
        self.assertEqual(session.cancelled, 2)
        self.assertEqual(spy.abort_calls, 1)  # exactly one device abort
        self.assertLess(self.h.clock.now() - t0, 60.0)  # never the full cap again
        self.assertTrue(self.gw.forced_cancel)
        self.assertFalse(self.gw.busy)
        self.assertEqual(self.h.runner.depth, 0)


# =================================================================================
# serve startup / shutdown with REAL CredentialStore, Journal and Bridge on temp state
# =================================================================================
OTHER_REF = "device-" + "cd" * 16


class StateFixture:
    """A private temp state root (owner-only, outside Git) with real store/journal."""

    def __init__(self, base: Path) -> None:
        self.base = base

    def write(self, name: str, data: bytes | str, mode: int = 0o600) -> Path:
        path = self.base / name
        raw = data.encode("utf-8") if isinstance(data, str) else data
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            os.fchmod(fd, mode)
            os.write(fd, raw)
        finally:
            os.close(fd)
        return path

    def build(
        self,
        *,
        cfg: dict[str, Any] | None = None,
        attach: bool = True,
        attach_changes: dict[str, Any] | None = None,
        bind_session: str | None = SESSION_ID,
        state_mode: int = 0o700,
        with_journal: bool = True,
        with_auth: bool = True,
        cert_mode: int = 0o600,
        key_text: str = KEY_CANARY,
        extra_refs: bool = False,
    ) -> Path:
        state = self.base / "state"
        state.mkdir(mode=0o700)
        if with_auth:
            with auth.CredentialStore(state / "auth") as store:
                if attach:
                    store.save_credential(cred())
                    store.save_client_id(CLIENT_ID, CRED_REF)  # as a real attach does
                    store.save_attachment(attachment(**(attach_changes or {})))
                if extra_refs:
                    other = "cd" * 16
                    store.save_credential(
                        auth.Credential(
                            OTHER_REF,
                            cred().origin,
                            other,
                            "amc_" + other + "." + "T" * 64,
                        )
                    )
                    store.save_attachment(attachment(credential_ref=OTHER_REF))
        if with_journal:
            with Journal(state / "journal") as journal:
                if bind_session is not None:
                    bridge = core.Bridge(
                        journal,
                        mock.Mock(),
                        clock=FakeClock(),
                        new_id=IdFactory(700),
                    )
                    bridge.select_session(
                        host_id=HOST_IDENTITY,
                        host_instance_id=INSTANCE,
                        session_id=bind_session,
                        client_id=CLIENT_ID,
                        credential_ref=CRED_REF,
                    )
        os.chmod(state, state_mode)
        self.write("cert.pem", b"not a certificate", cert_mode)
        self.write("key.pem", b"not a key")
        self.write("provider.key", key_text)
        doc = config_doc(self.base, **(cfg or {}))
        return self.write("gw.json", json.dumps(doc))

    def stores_reopen(self) -> None:
        """The flock-owning stores can be opened again: nothing leaked."""
        state = self.base / "state"
        if (state / "journal").is_dir():
            with Journal(state / "journal"):
                pass
        if (state / "auth").is_dir():
            with auth.CredentialStore(state / "auth"):
                pass


def runner_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name == "pet-core-runner"]


class Spies:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.gateways = []
        self.ssl_calls: list[tuple[Any, ...]] = []
        self.site_calls: list[tuple[Any, ...]] = []
        self.provider_calls = 0
        self.client_calls = 0
        self.unified: Any = None  # a scripted client for the one startup attach
        self.closer_calls = 0
        self.ssl_error: BaseException | None = None
        self.site_error: BaseException | None = None
        self.wait_error: BaseException | None = None
        self.wait_gate: asyncio.Event | None = None
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

    gateways: list[Any]

    async def provider(self, _key: Any, *, timeout: float) -> Any:
        self.provider_calls += 1
        raise AssertionError("no provider dial in an offline startup test")

    def client(self) -> Any:
        self.client_calls += 1
        if self.unified is not None:
            return self.unified
        raise AssertionError("no Unified client in an offline startup test")

    def ssl_factory(self, cert: Path, key: Path) -> ssl.SSLContext:
        self.ssl_calls.append((cert, key))
        if self.ssl_error is not None:
            raise self.ssl_error
        return self.ctx

    async def start_site(self, handler: Any, cfg: Any, ctx: Any) -> Any:
        self.site_calls.append((handler, cfg, ctx))
        self.gateways.append(handler.__self__)  # the _Gateway behind the handler
        self.events.append("site")
        if self.site_error is not None:
            raise self.site_error

        async def closer() -> None:
            self.closer_calls += 1
            self.events.append("closer")

        return closer

    async def wait(self) -> None:
        self.events.append("wait")
        if self.wait_error is not None:
            raise self.wait_error
        if self.wait_gate is not None:
            await self.wait_gate.wait()

    def deps(self, clock: Any) -> gsv.ServeDeps:
        return gsv.ServeDeps(
            client_factory=self.client,
            ssl_factory=self.ssl_factory,
            start_site=self.start_site,
            provider_connect=self.provider,
            wait_for_shutdown=self.wait,
            clock=lambda: clock,
            new_id=IdFactory(900),
        )


class ServeStartupTest(OfflineAsyncCase):
    # -- the same host restarted: follow its new process instance ---------------------
    def _binding_record(self) -> dict[str, Any]:
        raw = (self.base / "state/journal/binding.json").read_bytes()
        return Journal._decode(raw)

    def _stored_instance(self) -> str:
        with auth.CredentialStore(self.base / "state/auth") as store:
            return store.load_attachment(CRED_REF).host_instance_id

    async def test_same_host_restart_is_followed_and_the_session_rebound(self):
        path = self.fx.build()
        before = self._binding_record()
        self.spies.unified = FakeClient(
            json_response(attach_ok(hostInstanceId=INSTANCE_2))
        )
        self.assertEqual(await self.serve(path), 0)
        self.assertEqual(self.err.getvalue(), "")
        self.assertEqual(len(self.spies.unified.calls), 1)  # one POST, no retry
        self.assertEqual(self.spies.unified.bodies()[0]["clientId"], CLIENT_ID)
        self.assertEqual(self._stored_instance(), INSTANCE_2)
        after = self._binding_record()
        self.assertEqual(after["binding"]["host_instance_id"], INSTANCE_2)
        for field in ("host_id", "session_id", "client_id", "credential_ref"):
            self.assertEqual(after["binding"][field], before["binding"][field])
        self.assertGreater(after["epoch"], before["epoch"])  # an old turn is stale
        frozen = self.spies.gateways[0]._ops._binding
        self.assertEqual(frozen.host_instance_id, INSTANCE_2)
        self.assertEqual(self.spies.provider_calls, 0)
        self.assertEqual(runner_threads(), [])
        self.fx.stores_reopen()

    async def test_same_host_restart_keeps_an_uncertain_command_untouched(self):
        path = self.fx.build()
        state = self.base / "state"
        command_name = "cmd-12345678-1234-4234-8234-123456789abc.json"
        with Journal(state / "journal") as journal:
            binding = journal.get("binding.json")
            journal.put(command_name, {
                "state": "UNCERTAIN", "binding": binding["binding"], "epoch": 0,
                "created_at": time.time(), "updated_at": time.time(), "event_ids": [],
                "reason": "transport_exception",
            })
        before = (state / "journal" / command_name).read_bytes()
        self.spies.unified = FakeClient(
            json_response(attach_ok(hostInstanceId=INSTANCE_2))
        )
        self.assertEqual(await self.serve(path), 0)
        self.assertEqual((state / "journal" / command_name).read_bytes(), before)
        self.assertEqual(len(self.spies.unified.calls), 1)  # the attach; never a send

    async def test_unchanged_instance_rewrites_nothing(self):
        path = self.fx.build()
        state = self.base / "state"
        attachment_file = state / "auth" / f"attachment-{CRED_REF}.json"
        before = (attachment_file.read_bytes(), self._binding_record()["binding"])
        self.spies.unified = FakeClient(json_response(attach_ok()))
        self.assertEqual(await self.serve(path), 0)
        self.assertEqual(
            (attachment_file.read_bytes(), self._binding_record()["binding"]), before
        )
        self.assertEqual(len(self.spies.unified.calls), 1)

    async def test_a_different_host_identity_is_refused_and_changes_nothing(self):
        path = self.fx.build()
        binding_before = self._binding_record()["binding"]
        self.spies.unified = FakeClient(
            json_response(attach_ok(hostIdentity="6" * 64, hostInstanceId=INSTANCE_2))
        )
        self.assertFailed(await self.serve(path), "core_binding_required")
        self.assertEqual(self.spies.site_calls, [])
        self.assertEqual(self._stored_instance(), INSTANCE)
        self.assertEqual(self._binding_record()["binding"], binding_before)

    async def test_unreachable_or_refusing_host_starts_on_the_stored_pin(self):
        for number, response in enumerate(
            (OSError(CANARY), json_response({}, 401), json_response({}, 503),
             json_response({"clientId": CLIENT_ID}))
        ):
            with self.subTest(case=number):
                self.base = Path(
                    os.path.realpath(tempfile.mkdtemp(prefix="pet-gw-u3-"))
                )
                self.addCleanup(shutil.rmtree, self.base, True)
                self.fx = StateFixture(self.base)
                self.spies, self.out, self.err = Spies(), io.StringIO(), io.StringIO()
                path = self.fx.build()
                binding_before = self._binding_record()["binding"]
                self.spies.unified = FakeClient(response)
                self.assertEqual(await self.serve(path), 0)
                self.assertNotIn(CANARY, self.err.getvalue() + self.out.getvalue())
                self.assertEqual(len(self.spies.unified.calls), 1)  # never retried
                self.assertEqual(self._stored_instance(), INSTANCE)
                self.assertEqual(self._binding_record()["binding"], binding_before)

    async def test_existing_external_auth_is_used_without_copy_or_rebinding(self):
        path = self.fx.build()
        state = self.base / "state"
        external = self.base / "shared-auth"
        (state / "auth").rename(external)  # synthetic fixture layout only
        before = {p.name: p.read_bytes() for p in external.iterdir()}
        binding_before = Journal._decode((state / "journal/binding.json").read_bytes())["binding"]
        doc = config_doc(self.base, auth_root=str(external))
        path.write_text(json.dumps(doc))
        self.assertEqual(await self.serve(path), 0)
        self.assertEqual({p.name: p.read_bytes() for p in external.iterdir()}, before)
        self.assertFalse((state / "auth").exists())
        binding_after = Journal._decode((state / "journal/binding.json").read_bytes())["binding"]
        self.assertEqual(binding_after, binding_before)
        self.assertNothingDialled(attach_attempts=1)
        self.assertEqual(runner_threads(), [])
        with auth.CredentialStore(external):
            pass  # same existing flock is released

    async def test_external_auth_refuses_symlink_and_nonprivate_directory(self):
        path = self.fx.build()
        state = self.base / "state"
        original = state / "auth"
        link = self.base / "auth-link"
        link.symlink_to(original, target_is_directory=True)
        for target in (link, original):
            if target == original:
                original.chmod(0o755)
            path.write_text(json.dumps(config_doc(self.base, auth_root=str(target))))
            self.assertEqual(await self.serve(path), 1)
            self.assertEqual(self.err.getvalue(), "error: core_binding_required\n")
            self.err = io.StringIO()
            self.assertEqual(self.spies.site_calls, [])
            self.assertNothingDialled()
        original.chmod(0o700)
        self.fx.stores_reopen()

    async def test_external_auth_keeps_all_frozen_binding_checks(self):
        path = self.fx.build()
        state = self.base / "state"
        external = self.base / "shared-auth"
        (state / "auth").rename(external)
        path.write_text(json.dumps(config_doc(self.base, auth_root=str(external))))
        original = Journal._decode((state / "journal/binding.json").read_bytes())
        changes = {
            "host_id": "6" * 64,
            "host_instance_id": "12345678-1234-4234-8234-123456789abd",
            "session_id": "sess-other-0a1b2c3d",
            "client_id": "D" * 32,
            "credential_ref": OTHER_REF,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                with Journal(state / "journal") as journal:
                    journal.put("binding.json", {
                        **original, "binding": {**original["binding"], field: value}
                    })
                self.assertEqual(await self.serve(path), 1)
                self.assertEqual(self.err.getvalue(), "error: core_binding_required\n")
                self.err = io.StringIO()
                self.assertEqual(self.spies.site_calls, [])
                self.assertNothingDialled()

    async def test_external_auth_does_not_clear_or_replay_an_uncertain_record(self):
        path = self.fx.build()
        state = self.base / "state"
        external = self.base / "shared-auth"
        (state / "auth").rename(external)
        path.write_text(json.dumps(config_doc(self.base, auth_root=str(external))))
        command_name = "cmd-12345678-1234-4234-8234-123456789abc.json"
        with Journal(state / "journal") as journal:
            binding = journal.get("binding.json")
            journal.put(command_name, {
                "state": "UNCERTAIN", "binding": binding["binding"], "epoch": 0,
                "created_at": time.time(), "updated_at": time.time(), "event_ids": [],
                "reason": "transport_exception",
            })
        before = (state / "journal" / command_name).read_bytes()
        self.assertEqual(await self.serve(path), 0)
        self.assertEqual((state / "journal" / command_name).read_bytes(), before)
        self.assertEqual(
            Journal._decode(before)["state"], "UNCERTAIN"
        )
        self.assertNothingDialled(attach_attempts=1)
        self.assertEqual(runner_threads(), [])

    async def test_external_auth_missing_or_same_as_journal_never_creates_a_store(self):
        path = self.fx.build()
        for external in (self.base / "missing-auth", self.base / "state/journal"):
            path.write_text(json.dumps(config_doc(self.base, auth_root=str(external))))
            self.assertEqual(await self.serve(path), 1)
            self.assertEqual(self.err.getvalue(), "error: core_binding_required\n")
            self.err = io.StringIO()
            self.assertEqual(self.spies.site_calls, [])
            self.assertNothingDialled()
        self.assertFalse((self.base / "missing-auth").exists())
        self.fx.stores_reopen()

    async def asyncSetUp(self) -> None:
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="pet-gw-u3-")))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.fx = StateFixture(self.base)
        self.spies = Spies()
        self.clock = FakeLoopClock()
        self.out = io.StringIO()
        self.err = io.StringIO()

    async def serve(self, path: Path, env: dict[str, str] | None = None) -> int:
        return await gsv._serve(
            path, env or {}, self.out, self.err, self.spies.deps(self.clock)
        )

    def assertFailed(self, code: int, name: str) -> None:
        self.assertEqual(code, 1)
        self.assertEqual(self.err.getvalue(), f"error: {name}\n")
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(runner_threads(), [])
        self.assertNotIn(KEY_CANARY, self.err.getvalue())
        self.fx.stores_reopen()

    def assertNothingDialled(self, *, attach_attempts: int = 0) -> None:
        """No provider dial.  A fully validated startup makes ONE attach attempt."""
        self.assertEqual(self.spies.provider_calls, 0)
        self.assertEqual(self.spies.client_calls, attach_attempts)

    async def test_t_srv_startup_each_failure_has_its_fixed_code_before_any_later_step(
        self,
    ):
        cases: list[tuple[str, dict[str, Any], str, int]] = [
            ("config", dict(cfg={"port": 80}), "config_invalid", 0),
            ("cert", dict(cert_mode=0o644), "cert_missing", 0),
            ("provider key", dict(key_text="short"), "provider_key_missing", 0),
            ("no state", dict(state_mode=0o755), "core_binding_required", 0),
            ("no journal dir", dict(with_journal=False), "core_binding_required", 0),
            (
                "no auth dir",
                dict(with_auth=False, bind_session=None),
                "core_binding_required",
                0,
            ),
            ("no attachment", dict(attach=False), "credential_missing", 0),
            ("no binding", dict(bind_session=None), "core_binding_required", 0),
            (
                "other session",
                dict(bind_session="sess-other-0a1b2c3d"),
                "core_binding_required",
                0,
            ),
            (
                "other credential ref",
                dict(cfg={"credential_ref": OTHER_REF}, extra_refs=True),
                "core_binding_required",
                0,
            ),
            (
                "host identity",
                dict(attach_changes={"host_identity": "6" * 64}),
                "core_binding_required",
                0,
            ),
            (
                "host instance",
                dict(
                    attach_changes={
                        "host_instance_id": "12345678-1234-4234-8234-123456789abd"
                    }
                ),
                "core_binding_required",
                0,
            ),
            (
                "client id",
                dict(attach_changes={"client_id": "D" * 32}),
                "core_binding_required",
                0,
            ),
            (
                "uppercase short8",
                dict(
                    cfg={"session_id": "sess-fixture-0A1B2C3D"},
                    bind_session="sess-fixture-0A1B2C3D",
                ),
                "core_binding_required",
                0,
            ),
        ]
        for name, options, code, _ in cases:
            with self.subTest(case=name):
                self.base = Path(
                    os.path.realpath(tempfile.mkdtemp(prefix="pet-gw-u3-"))
                )
                self.addCleanup(shutil.rmtree, self.base, True)
                self.fx = StateFixture(self.base)
                self.spies = Spies()
                self.out, self.err = io.StringIO(), io.StringIO()
                path = self.fx.build(**options)
                if name == "config":
                    path.chmod(0o600)
                exit_code = await self.serve(path)
                self.assertFailed(exit_code, code)
                self.assertEqual(self.spies.ssl_calls, [])  # not reached
                self.assertEqual(self.spies.site_calls, [])
                self.assertNothingDialled()

    async def test_t_srv_startup_ok_listens_once_and_stops_cleanly(self):
        path = self.fx.build()
        self.spies.wait_gate = None
        code = await self.serve(path)
        self.assertEqual(code, 0)
        self.assertEqual(
            self.out.getvalue().splitlines(),
            [
                "gateway: listening on 10.1.2.3:8941 for session ...0a1b2c3d",
                "gateway: stopped",
            ],
        )
        self.assertEqual(self.err.getvalue(), "")
        for secret in (KEY_CANARY, GOOD_TOKEN, GOOD_HASH, SESSION_ID, HOST_IDENTITY):
            self.assertNotIn(secret, self.out.getvalue() + self.err.getvalue())
        ((_handler, cfg, ctx),) = self.spies.site_calls
        self.assertEqual((cfg.bind_host, cfg.port), ("10.1.2.3", 8941))
        self.assertIs(ctx, self.spies.ctx)
        self.assertEqual(
            self.spies.ssl_calls,
            [(self.base / "cert.pem", self.base / "key.pem")],
        )
        self.assertEqual(self.spies.events, ["site", "wait", "closer"])
        self.assertNothingDialled(attach_attempts=1)  # no device: no provider dial
        self.assertEqual(runner_threads(), [])  # joined
        self.fx.stores_reopen()  # journal/store closed after the runner

    async def test_t_srv_startup_freezes_exactly_the_six_binding_fields(self):
        path = self.fx.build()
        self.assertEqual(await self.serve(path), 0)
        (gw,) = self.spies.gateways
        binding = gw._ops._binding
        self.assertEqual(
            sorted(f.name for f in __import__("dataclasses").fields(binding)),
            sorted(
                [
                    "binding_id",
                    "host_id",
                    "host_instance_id",
                    "session_id",
                    "client_id",
                    "credential_ref",
                ]
            ),
        )
        self.assertEqual(
            (
                binding.host_id,
                binding.host_instance_id,
                binding.session_id,
                binding.client_id,
                binding.credential_ref,
            ),
            (HOST_IDENTITY, INSTANCE, SESSION_ID, CLIENT_ID, CRED_REF),
        )
        self.assertEqual(gw._settings.short8, SHORT8)
        self.assertEqual(self.spies.client_calls, 1)  # the one startup attach

    async def test_t_srv_runner_exit_cleanup_ssl_factory_failure_closes_everything(
        self,
    ):
        for failure in (
            gsv.StartupError("cert_missing"),
            RuntimeError(CANARY),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.base = Path(
                    os.path.realpath(tempfile.mkdtemp(prefix="pet-gw-u3-"))
                )
                self.addCleanup(shutil.rmtree, self.base, True)
                self.fx = StateFixture(self.base)
                self.spies, self.out, self.err = Spies(), io.StringIO(), io.StringIO()
                self.spies.ssl_error = failure
                code = await self.serve(self.fx.build())
                self.assertFailed(code, "cert_missing")
                self.assertNotIn(CANARY, self.err.getvalue())
                self.assertEqual(self.spies.site_calls, [])
                self.assertNothingDialled(attach_attempts=1)

    async def test_t_srv_runner_exit_cleanup_start_site_failure_joins_the_runner(self):
        self.spies.site_error = RuntimeError(CANARY)
        code = await self.serve(self.fx.build())
        self.assertFailed(code, "listen_failed")
        self.assertNotIn(CANARY, self.err.getvalue())
        self.assertEqual(self.spies.closer_calls, 0)  # there was nothing to close
        self.assertEqual(self.spies.events, ["site"])
        self.assertNothingDialled(attach_attempts=1)

    async def test_t_srv_runner_exit_cleanup_an_exception_while_serving_still_cleans_up(
        self,
    ):
        self.spies.wait_error = RuntimeError(CANARY)
        with self.assertRaises(RuntimeError):
            await self.serve(self.fx.build())
        self.assertEqual(self.spies.events, ["site", "wait", "closer"])
        self.assertEqual(runner_threads(), [])
        self.fx.stores_reopen()

    async def test_t_srv_runner_exit_cleanup_cancellation_while_serving_cleans_up(self):
        self.spies.wait_gate = asyncio.Event()
        task = asyncio.create_task(self.serve(self.fx.build()))
        await until(lambda: "wait" in self.spies.events)
        self.assertEqual(
            len(runner_threads()), 1
        )  # the worker is running while serving
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.spies.events, ["site", "wait", "closer"])
        self.assertEqual(runner_threads(), [])
        self.fx.stores_reopen()

    async def test_t_srv_runner_exit_cleanup_shutdown_waits_for_a_session_before_closing(
        self,
    ):
        self.spies.wait_gate = asyncio.Event()
        task = asyncio.create_task(self.serve(self.fx.build()))
        await until(lambda: "wait" in self.spies.events)
        (gw,) = self.spies.gateways
        release = threading.Event()
        entered = threading.Event()

        def slow_stop() -> None:
            entered.set()
            release.wait(5)

        gw._runner.submit("status", slow_stop)  # the worker is busy with a sync call
        await until(entered.is_set)
        self.spies.wait_gate.set()  # shutdown requested
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())  # runner not idle: nothing is closed yet
        with self.assertRaises(JournalError):  # the journal is still held by the worker
            Journal(self.base / "state" / "journal").close()
        with self.assertRaises(auth.AuthError):  # ... and so is the credential store
            auth.CredentialStore(self.base / "state" / "auth").close()
        release.set()
        self.assertEqual(await asyncio.wait_for(task, 5), 0)
        self.assertEqual(runner_threads(), [])
        self.fx.stores_reopen()

    # -- final repair v1: SF3 precedence, SF4 closer failure, MF5 stop and close order
    async def test_sf3_two_defects_report_the_earlier_startup_step(self):
        cases: list[tuple[str, dict[str, Any], str, bool]] = [
            (
                "config before cert",
                dict(cfg={"port": 80}, cert_mode=0o644),
                "config_invalid",
                False,
            ),
            (
                "cert before provider key",
                dict(cert_mode=0o644, key_text="short"),
                "cert_missing",
                False,
            ),
            (
                "provider key before state root",
                dict(key_text="short", state_mode=0o755),
                "provider_key_missing",
                False,
            ),
            (
                "state root before credential",
                dict(state_mode=0o755, attach=False),
                "core_binding_required",
                False,
            ),
            (
                "credential before binding",
                dict(attach=False, bind_session=None),
                "credential_missing",
                False,
            ),
        ]
        for name, options, code, _ in cases:
            with self.subTest(case=name):
                self.base = Path(
                    os.path.realpath(tempfile.mkdtemp(prefix="pet-gw-u3-"))
                )
                self.addCleanup(shutil.rmtree, self.base, True)
                self.fx = StateFixture(self.base)
                self.spies, self.out, self.err = Spies(), io.StringIO(), io.StringIO()
                exit_code = await self.serve(self.fx.build(**options))
                self.assertFailed(exit_code, code)
                self.assertEqual(self.spies.ssl_calls, [])
                self.assertEqual(self.spies.site_calls, [])
                self.assertNothingDialled()

    async def test_sf4_a_failing_site_closer_still_closes_runner_journal_and_store(
        self,
    ):
        self.spies.wait_gate = asyncio.Event()
        real_start = self.spies.start_site

        async def start_site(handler: Any, cfg: Any, ctx: Any) -> Any:
            await real_start(handler, cfg, ctx)

            async def bad_closer() -> None:
                self.spies.events.append("closer")
                raise RuntimeError(CANARY)

            return bad_closer

        self.spies.start_site = start_site  # type: ignore[method-assign]
        task = asyncio.create_task(self.serve(self.fx.build()))
        await until(lambda: "wait" in self.spies.events)
        self.spies.wait_gate.set()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(task, 5)
        self.assertEqual(self.spies.events, ["site", "wait", "closer"])
        self.assertEqual(runner_threads(), [])
        self.fx.stores_reopen()  # the runner was closed and BOTH stores were released

    def _record_close_events(self, events: list[str]) -> None:
        real_journal, real_store = Journal.close, auth.CredentialStore.close

        def journal_close(this: Any) -> None:
            events.append("journal_close")
            real_journal(this)

        def store_close(this: Any) -> None:
            events.append("store_close")
            real_store(this)

        for target, replacement in (
            (Journal, journal_close),
            (auth.CredentialStore, store_close),
        ):
            patcher = mock.patch.object(target, "close", replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _spy_runner(self, gw: Any) -> list[tuple[str, bool, bool]]:
        """Record every ``runner.run`` (op, urgent, retain) and the ``aclose`` timeline."""
        events = self.spies.events
        calls: list[tuple[str, bool, bool]] = []
        real_run, real_aclose = gw._runner.run, gw._runner.aclose

        async def run(op: str, fn: Any, **kw: Any) -> Any:
            calls.append((op, kw.get("urgent"), kw.get("retain")))
            return await real_run(op, fn, **kw)

        async def aclose() -> None:
            events.append("aclose")
            await real_aclose()
            events.append("aclosed")

        gw._runner.run, gw._runner.aclose = run, aclose
        return calls

    async def _run_quick_session(self, gw: Any) -> None:
        gw._ws_factory = FakeServerWS
        gw._device_factory = lambda ws, transport: FakeDeviceLink(self.clock)
        gw._session_factory = lambda device, connect: QuickSession()
        resp = await asyncio.wait_for(gw.handle(request_for()), 5)
        self.assertTrue(resp.prepared)
        await until(lambda: not gw.busy)
        self.assertEqual(gw.sessions_started, 1)

    async def test_mf5_a_session_that_ran_gets_one_urgent_unretained_stop_in_order(
        self,
    ):
        self.spies.wait_gate = asyncio.Event()
        path = self.fx.build()
        self._record_close_events(self.spies.events)
        task = asyncio.create_task(self.serve(path))
        await until(lambda: "wait" in self.spies.events)
        (gw,) = self.spies.gateways
        calls = self._spy_runner(gw)
        stop_fn = lambda: self.spies.events.append("stop")  # noqa: E731 - on the worker
        gw._ops.stop = stop_fn
        await self._run_quick_session(gw)
        self.spies.wait_gate.set()
        self.assertEqual(await asyncio.wait_for(task, 5), 0)
        self.assertEqual(
            calls, [("follow_host_restart", False, False), ("stop", True, False)]
        )  # ONE session-boundary follow, then ONE urgent, unretained stop
        self.assertEqual(
            self.spies.events,
            [
                "site",
                "wait",
                "closer",  # the site is closed first
                "stop",  # then the local core stop
                "aclose",  # then the runner is idled
                "aclosed",
                "journal_close",  # and only then the stores
                "store_close",
            ],
        )
        self.assertEqual(runner_threads(), [])

    async def test_mf5_without_a_session_there_is_no_stop(self):
        self.spies.wait_gate = asyncio.Event()
        path = self.fx.build()
        self._record_close_events(self.spies.events)
        task = asyncio.create_task(self.serve(path))
        await until(lambda: "wait" in self.spies.events)
        (gw,) = self.spies.gateways
        calls = self._spy_runner(gw)
        gw._ops.stop = lambda: self.spies.events.append("stop")
        self.spies.wait_gate.set()
        self.assertEqual(await asyncio.wait_for(task, 5), 0)
        self.assertEqual(calls, [])
        self.assertEqual(
            self.spies.events,
            [
                "site",
                "wait",
                "closer",
                "aclose",
                "aclosed",
                "journal_close",
                "store_close",
            ],
        )

    async def test_mf5_an_interrupted_runner_close_leaves_both_stores_open(self):
        self.spies.wait_gate = asyncio.Event()
        path = self.fx.build()
        self._record_close_events(self.spies.events)
        task = asyncio.create_task(self.serve(path))
        await until(lambda: "wait" in self.spies.events)
        (gw,) = self.spies.gateways
        release, entered = threading.Event(), threading.Event()

        def slow() -> None:
            entered.set()
            release.wait(5)

        gw._runner.submit("status", slow)  # the worker is busy: aclose cannot finish
        await until(entered.is_set)
        self.spies.wait_gate.set()
        await until(lambda: "closer" in self.spies.events)
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())  # parked in runner.aclose
        task.cancel()  # interrupted while waiting for the worker
        with self.assertRaises(asyncio.CancelledError):
            await task
        try:
            self.assertNotIn("journal_close", self.spies.events)
            self.assertNotIn("store_close", self.spies.events)
            with self.assertRaises(
                JournalError
            ):  # both stay locked: no premature close
                Journal(self.base / "state" / "journal").close()
            with self.assertRaises(auth.AuthError):
                auth.CredentialStore(self.base / "state" / "auth").close()
        finally:
            release.set()  # settle the worker, then own and close what was left open
            await until(lambda: runner_threads() == [])
            gw._ops._bridge._journal.close()
            gw._ops._store.close()
        self.fx.stores_reopen()

    async def test_serve_deps_defaults_are_the_production_functions(self):
        from pet_bridge import transport, wire

        deps = gsv.ServeDeps()
        self.assertIs(deps.client_factory, transport.HttpsClient)
        self.assertIs(deps.ssl_factory, gsv.build_server_ssl_context)
        self.assertIs(deps.start_site, gsv._start_site)
        self.assertIs(deps.provider_connect, gsv.connect_provider)
        self.assertIs(deps.wait_for_shutdown, gsv._wait_for_signals)
        self.assertIs(deps.clock, gc.RealClock)
        self.assertTrue(wire.is_canonical_uuid4(deps.new_id()))
        self.assertNotEqual(deps.new_id(), deps.new_id())


class ServeMainSyncTest(ServerOfflineCase):
    def test_serve_main_runs_the_whole_flow_and_returns_the_exit_code(self):
        fx = StateFixture(self.base)
        path = fx.build()
        spies = Spies()
        out, err = io.StringIO(), io.StringIO()
        code = gsv.serve_main(
            path,
            env={},
            stdout=out,
            stderr=err,
            deps=spies.deps(FakeLoopClock()),
        )
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().count("gateway:"), 2)
        self.assertEqual(runner_threads(), [])
        fx.stores_reopen()

    def test_serve_main_reports_a_startup_error_and_exit_1(self):
        out, err = io.StringIO(), io.StringIO()
        code = gsv.serve_main(
            self.base / "missing.json", env={}, stdout=out, stderr=err
        )
        self.assertEqual(
            (code, out.getvalue(), err.getvalue()), (1, "", "error: config_invalid\n")
        )


# =================================================================================
# Source hygiene
# =================================================================================
class SourceHygieneTest(ServerOfflineCase):
    def tree(self) -> ast.Module:
        return ast.parse(SERVER_PY.read_text(encoding="utf-8"))

    def test_t_srv_privacy_no_logging_no_env_reads_no_raw_socket_and_prints_only_in_serve(
        self,
    ):
        tree = self.tree()
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        self.assertFalse(
            roots
            & {"logging", "traceback", "warnings", "socket", "subprocess", "threading"}
        )
        self.assertTrue({"aiohttp", "asyncio", "ssl"} <= roots)
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        }
        self.assertFalse(names & {"environ", "getenv", "environb", "getpass"})
        printers: set[str] = set()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for call in ast.walk(fn):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id == "print"
                    ):
                        printers.add(fn.name)
        # the two metadata lines of startup/shutdown and the fixed ``error: <code>`` line
        self.assertEqual(printers, {"_serve", "_startup"})

    def test_t_srv_privacy_no_exception_text_is_formatted_into_output(self):
        source = SERVER_PY.read_text(encoding="utf-8")
        for forbidden in (
            "traceback",
            "format_exc",
            "exc_info",
            "0.0.0.0",
            "allow_redirects=True",
        ):
            self.assertNotIn(forbidden, source)
        self.assertNotIn(
            "wss://", source
        )  # the provider URL lives only in gateway_provider

    def test_the_public_surface_is_exactly_the_plan_plus_the_two_link_error_names(self):
        self.assertEqual(
            set(gsv.__all__),
            {
                "STARTUP_CODES",
                "StartupError",
                "Secret",
                "AuthRegistry",
                "check_private_file",
                "load_config",
                "load_provider_key",
                "build_server_ssl_context",
                "verify_bearer",
                "Rejection",
                "check_handshake",
                "reject_response",
                "LinkError",
                "AiohttpDeviceLink",
                "ProviderError",
                "AiohttpProviderLink",
                "connect_provider",
                "ServeDeps",
                "serve_main",
            },
        )
        for name in gsv.__all__:
            self.assertTrue(hasattr(gsv, name), name)


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
