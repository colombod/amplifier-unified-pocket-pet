"""Gateway server: private config/keys, TLS, the handshake guard, aiohttp links and ``serve``.

This is the ONLY module that imports aiohttp (and, with ``gateway_core``, asyncio).  It is
implemented for a real run but every behaviour here is tested offline with spies and
``make_mocked_request``; nothing in the tests listens, dials or reads a real key.

Device handshake (``check_handshake``), strictly in this order, first failure wins, nothing
before it touches the provider or the WebSocket upgrade::

    1 method != GET                      405 method_not_allowed
    2 raw path != /v1/pet/live           404 not_found     (no 3xx, no normalisation)
    3 Host != this listener's literal   421 misdirected   (exact string, exactly one header)
    4 Origin / Sec-WebSocket-Protocol present, or a duplicated Authorization / Upgrade /
      Connection / Sec-WebSocket-Key / Sec-WebSocket-Version      400 bad_request
    5 no peer address                    400 bad_request;  per-IP failure limit  429 rate_limited
    6 bearer not one of the stored SHA-256 hashes      401 unauthorized (counts as a failure)

Loopback peers get no exemption.  Every rejection is a fixed JSON body with no Location and
no echo of any request value. Past the guard ONE client slot is taken without waiting
(409 busy otherwise), the same-host restart follow settles on CoreRunner, then the upgrade
is prepared and an owned custody task runs the ``PetSession``.
The slot is freed only when the worker thread is idle again and the confirm job (if any) has
been reconciled exactly once; a cancelled request never frees it early.

Adapters: ``AiohttpDeviceLink`` / ``AiohttpProviderLink`` abort their transport on a
cancellation or a close error (aiohttp's own close wait is outside its cancellation
handlers) and raise fixed errors only; no aiohttp exception object, header or body is ever
formatted. Nothing here uses ``logging``. The optional event sink receives explicit
metadata only; stdout/stderr retain the fixed startup/shutdown messages.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import errno
import hashlib
import hmac
import os
import re
import signal
import socket
import ssl
import stat
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, TypeVar

import aiohttp
from aiohttp import web
from aiohttp.http_websocket import WebSocketError

from . import auth, core, transport
from . import gateway_provider as gp
from . import gateway_state as gs
from .gateway_core import (
    CoreRunner,
    DeviceLink,
    DeviceMessage,
    PetSession,
    ProviderError,
    ProviderLink,
    ProviderMessage,
    RealClock,
    RunnerError,
    SessionOutcome,
    SessionSettings,
    UnifiedOps,
)
from .gateway_events import EventLog, EventLogError
from .journal import BINDING_NAME, Journal, JournalError

T = TypeVar("T")

STARTUP_CODES = frozenset(
    {
        "config_invalid",
        "core_binding_required",
        "credential_missing",
        "cert_missing",
        "provider_key_missing",
        "listen_failed",
        "event_log_invalid",
    }
)

_MAX_CONFIG_BYTES = 16384
_MAX_KEY_BYTES = 1024
_READ_CEILING = 1 << 20
_KEY_RE = re.compile(r"[\x21-\x7e]{20,512}")
_BEARER_RE = re.compile(r"Bearer ([A-Za-z0-9_-]{43})")
_DUMMY_DIGEST = hashlib.sha256(b"pet-gateway-dummy-token").hexdigest().encode("ascii")
_RUNNER_SHUTDOWN_S = 2.0
_TAILNET_RETRY_S = 10.0
_PEER_MAX = 64
_REJECTIONS = {
    "method_not_allowed": 405,
    "not_found": 404,
    "misdirected": 421,
    "bad_request": 400,
    "rate_limited": 429,
    "unauthorized": 401,
    "busy": 409,
    "internal_error": 500,
    "core_binding_required": 503,
}
_SINGLE_HEADERS = (
    "Authorization",
    "Upgrade",
    "Connection",
    "Sec-WebSocket-Key",
    "Sec-WebSocket-Version",
)


# -- errors ----------------------------------------------------------------------------
class StartupError(Exception):
    """A fixed startup refusal: only ``code`` (one of ``STARTUP_CODES``) is ever carried."""

    def __init__(self, code: str) -> None:
        if code not in STARTUP_CODES:
            raise ValueError("unknown startup code")
        super().__init__(code)
        self.code = code

    def __str__(self) -> str:
        return self.code


class LinkError(Exception):
    """A device-link failure: the fixed code ``device_link`` and nothing else."""

    def __init__(self, code: str = "device_link") -> None:
        super().__init__(code)
        self.code = code

    def __str__(self) -> str:
        return self.code


class Secret:
    """A held secret: hidden from repr/str/format, never pickled or copied."""

    __slots__ = ("_v",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("a secret is text")
        self._v = value

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "Secret(<hidden>)"

    def __str__(self) -> str:
        return "Secret(<hidden>)"

    def __format__(self, spec: str) -> str:
        return format("Secret(<hidden>)", spec)

    def __reduce__(self) -> Any:
        raise TypeError("a secret cannot be pickled")

    def __reduce_ex__(self, protocol: Any) -> Any:
        raise TypeError("a secret cannot be pickled")


# -- private files, config, key, TLS ---------------------------------------------------
class _PrivateFileError(OSError):
    """Any private-file refusal; carries no path or content."""


def _file_state(info: os.stat_result) -> tuple[int, ...]:
    """What must not change on the open file while it is read (never the access time)."""
    return (info.st_size, info.st_mode, info.st_uid, info.st_mtime_ns, info.st_ctime_ns)


def check_private_file(
    path: Path, *, max_bytes: int | None, read: bool
) -> bytes | None:
    """Open ``path`` safely and (optionally) read it.

    Absolute, final component not a symlink, a regular file owned by this user with no
    group/other permission bit, within ``max_bytes``.  Raises ``OSError`` for every failure
    (callers map it to their own fixed code).  Never blocks on a FIFO.

    A read is refused when fewer bytes arrive than the size seen at open, or when the open
    file's size, mode, owner or modification/change time differs after the read: a file
    that is being rewritten is not accepted as a shorter or mixed text.  This is a
    before/after comparison, not a lock; a change that leaves all of those equal is not
    detected.
    """
    try:
        text = os.fspath(path)
        if not os.path.isabs(text):
            raise _PrivateFileError("not_absolute")
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(text, flags)
    except ValueError:
        raise _PrivateFileError("bad_path") from None
    try:
        info = os.fstat(fd)
        limit = _READ_CEILING if max_bytes is None else max_bytes
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or (max_bytes is not None and info.st_size > max_bytes)
            or (read and info.st_size > limit)
        ):
            raise _PrivateFileError("unsafe_file")
        if not read:
            return None
        data = b""
        while len(data) < info.st_size:
            chunk = os.read(fd, info.st_size - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) != info.st_size or _file_state(os.fstat(fd)) != _file_state(info):
            raise _PrivateFileError("changed_during_read")
        return data
    finally:
        os.close(fd)


def load_config(path: Path) -> gs.GatewayConfig:
    """Load the explicit private gateway config.  Fixed ``StartupError`` codes only."""
    failure = "config_invalid"
    try:
        path = Path(path)
        raw = check_private_file(path, max_bytes=_MAX_CONFIG_BYTES, read=True) or b""
        doc = gs.strict_loads(raw, max_bytes=_MAX_CONFIG_BYTES, max_depth=8)
        return gs.parse_config(doc)
    except gs.GatewayProtocolError as err:
        if err.code == "core_binding_required":
            failure = "core_binding_required"
    except (OSError, ValueError, TypeError, UnicodeError):
        pass
    raise StartupError(failure)


def load_env_config(path: Path) -> tuple[gs.GatewayConfig, Secret | None]:
    """Load the private ``.env`` spelling of the gateway config and its inline key, if any.

    The same private-file rule, size cap, ``parse_config`` validation and fixed
    ``StartupError`` codes as ``load_config``.  Nothing read from the file is ever
    formatted: the returned key is a ``Secret`` and the errors carry a code only.
    """
    failure = "config_invalid"
    try:
        raw = check_private_file(Path(path), max_bytes=_MAX_CONFIG_BYTES, read=True) or b""
        doc, inline = gs.env_config_document(raw.decode("utf-8"))
        return gs.parse_config(doc), None if inline is None else Secret(inline)
    except gs.GatewayProtocolError as err:
        if err.code == "core_binding_required":
            failure = "core_binding_required"
    except (OSError, ValueError, TypeError, UnicodeError):
        pass
    raise StartupError(failure)


def _env_file_provider_key(cfg: gs.GatewayConfig, inline: Secret | None) -> Secret:
    """The provider key of an env-file start: the named key file or the file's own value.

    The process environment is never consulted: the only variable ``load_provider_key``
    can see is the one the private file itself supplied.
    """
    own = {} if inline is None else {gs.ENV_PROVIDER_KEY: inline.reveal()}
    return load_provider_key(cfg, own)


def load_provider_key(cfg: gs.GatewayConfig, env: Mapping[str, str]) -> Secret:
    """The provider key from the explicit 0600 file or the NAMED variable of ``env`` only."""
    value: Any = None
    try:
        if cfg.provider_key_file is not None:
            raw = check_private_file(
                Path(cfg.provider_key_file), max_bytes=_MAX_KEY_BYTES, read=True
            )
            value = (raw or b"").decode("utf-8").removesuffix("\n")  # ONE trailing LF
        elif cfg.provider_key_env is not None:
            value = env.get(cfg.provider_key_env)
    except (OSError, ValueError, UnicodeError):
        value = None
    if isinstance(value, str) and _KEY_RE.fullmatch(value) is not None:
        return Secret(value)
    raise StartupError("provider_key_missing")


def _refuse_password() -> bytes:
    raise ValueError("password_refused")


def build_server_ssl_context(cert: Path, key: Path) -> ssl.SSLContext:
    """A TLS >= 1.2 server context from two private files; never prompts, no compression."""
    try:
        check_private_file(cert, max_bytes=None, read=False)
        check_private_file(key, max_bytes=None, read=False)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.options |= ssl.OP_NO_COMPRESSION
        ctx.load_cert_chain(cert, key, password=_refuse_password)
        return ctx
    except (OSError, ValueError, TypeError, ssl.SSLError):
        pass
    raise StartupError("cert_missing")


# -- authentication --------------------------------------------------------------------
class AuthRegistry:
    """Per-IP failed-authentication limiter: fixed cap, fixed window, fails closed.

    A window starts at an IP's first failure and is NOT extended; at ``limit`` failures the
    IP is refused until the window ends.  Expired entries are purged on every access.  When
    ``cap`` entries are live an unknown IP is refused and nothing is ever evicted.
    """

    def __init__(
        self,
        *,
        cap: int = gs.AUTH_REGISTRY_CAP,
        limit: int = gs.AUTH_FAIL_LIMIT,
        window: float = gs.AUTH_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cap, self._limit, self._window, self._clock = cap, limit, window, clock
        self._entries: dict[str, list[float]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"AuthRegistry(entries={len(self._entries)})"

    def _purge(self) -> float:
        now = self._clock()
        for ip in [
            k for k, (_n, t0) in self._entries.items() if now >= t0 + self._window
        ]:
            del self._entries[ip]
        return now

    def admit(self, ip: str) -> bool:
        self._purge()
        entry = self._entries.get(ip)
        if entry is None:
            return len(self._entries) < self._cap
        return entry[0] < self._limit

    def record_failure(self, ip: str) -> None:
        now = self._purge()
        entry = self._entries.get(ip)
        if entry is not None:
            entry[0] += 1
        elif len(self._entries) < self._cap:
            self._entries[ip] = [1, now]


def _canonical_token(value: str) -> str | None:
    match = _BEARER_RE.fullmatch(value)
    if match is None:
        return None
    token = match.group(1)
    try:
        raw = base64.urlsafe_b64decode(token + "=")
    except ValueError:
        return None
    again = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return token if len(raw) == 32 and again == token else None


def verify_bearer(values: list[str], hashes: tuple[str, ...]) -> bool:
    """True for exactly one canonical 43-character bearer whose SHA-256 is configured.

    EVERY configured hash is compared for every input (a malformed or missing token
    compares a fixed dummy digest), so the work does not depend on the token.
    """
    token = _canonical_token(values[0]) if len(values) == 1 else None
    candidate = (
        _DUMMY_DIGEST
        if token is None
        else hashlib.sha256(token.encode("ascii")).hexdigest().encode("ascii")
    )
    matched = False
    for stored in hashes:
        if hmac.compare_digest(candidate, stored.encode("utf-8")):
            matched = True
    return token is not None and matched


@dataclass(frozen=True)
class Rejection:
    status: int
    code: str


def _peer_ip(request: web.BaseRequest) -> str | None:
    conn = request.transport
    if conn is None:
        return None
    peer = conn.get_extra_info("peername")
    if (
        isinstance(peer, tuple)
        and peer
        and isinstance(peer[0], str)
        and 0 < len(peer[0]) <= _PEER_MAX
    ):
        return peer[0]
    return None


def check_handshake(
    request: web.BaseRequest, cfg: gs.GatewayConfig, registry: AuthRegistry
) -> Rejection | None:
    """The ordered device handshake guard (module docstring).  ``None`` means authenticated."""
    if request.method != "GET":
        return Rejection(405, "method_not_allowed")
    if request.raw_path != gs.ROUTE:
        return Rejection(404, "not_found")
    headers = request.headers
    expected_host = cfg.host_header
    if cfg.tailnet_bind_host is not None:
        conn = request.transport
        local = conn.get_extra_info("sockname") if conn is not None else None
        if local == (cfg.tailnet_bind_host, cfg.port):
            expected_host = f"{cfg.tailnet_fqdn}:{cfg.port}"
        elif local != (cfg.bind_host, cfg.port):
            return Rejection(421, "misdirected")
    if headers.getall("Host", []) != [expected_host]:
        return Rejection(421, "misdirected")
    if (
        "Origin" in headers
        or "Sec-WebSocket-Protocol" in headers
        or any(len(headers.getall(name, [])) > 1 for name in _SINGLE_HEADERS)
    ):
        return Rejection(400, "bad_request")
    ip = _peer_ip(request)
    if ip is None:
        return Rejection(400, "bad_request")
    if not registry.admit(ip):
        return Rejection(429, "rate_limited")
    if not verify_bearer(headers.getall("Authorization", []), cfg.pet_token_sha256):
        registry.record_failure(ip)
        return Rejection(401, "unauthorized")
    return None


def reject_response(rej: Rejection) -> web.Response:
    """A fixed JSON rejection: no Location, no WWW-Authenticate, nothing reflected."""
    if _REJECTIONS.get(rej.code) != rej.status:
        raise ValueError("unknown rejection")
    resp = web.Response(
        status=rej.status,
        body=b'{"error":"' + rej.code.encode("ascii") + b'"}',
        content_type="application/json",
        headers={"Connection": "close"},
    )
    resp.force_close()
    return resp


# -- adapters --------------------------------------------------------------------------
_SHUTDOWN_CLOSE_CODE = 1001  # "going away": the gateway is stopping


def _cut_socket(transport: Any) -> None:
    """Cut a socket now (idempotent, never raises)."""
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.abort()


async def _guarded(
    awaitable: Awaitable[T],
    abort: Callable[[], Awaitable[None]],
    error: Callable[[str], Exception],
    code: str,
) -> T:
    """Await ``awaitable``; on cancellation or any failure abort FIRST, then re-raise.

    A cancellation is re-raised as itself, any other failure as a fixed ``error(code)``
    raised outside the handler so it carries no cause or context text.
    """
    try:
        return await awaitable
    except asyncio.CancelledError:
        await abort()
        raise
    except Exception:  # noqa: BLE001 - mapped to a fixed error below; no text is kept
        pass
    except BaseException:
        await abort()
        raise
    await abort()
    raise error(code)


class AiohttpDeviceLink:
    """The device WebSocket as a ``DeviceLink``; ``request_transport`` is the abort handle."""

    def __init__(self, ws: Any, request_transport: Any) -> None:
        self._ws = ws
        self._transport = request_transport

    def __repr__(self) -> str:
        return "AiohttpDeviceLink(<hidden>)"

    def _abort_transport(self) -> None:
        _cut_socket(self._transport)

    async def _abort(self) -> None:
        self._abort_transport()

    def abort_if_open(self) -> None:
        """Cut the socket unless the WebSocket closed cleanly (idempotent, never raises)."""
        if not self._ws.closed:
            self._abort_transport()

    async def send_text(self, text: str) -> None:
        await _guarded(self._ws.send_str(text), self._abort, LinkError, "device_link")

    async def receive(self) -> DeviceMessage:
        failed = False
        msg: Any = None
        try:
            msg = await self._ws.receive()
        except Exception:  # noqa: BLE001 - fixed LinkError below; no text is kept
            failed = True
        if failed:
            self._abort_transport()
            raise LinkError("device_link")
        kind = msg.type
        if kind == aiohttp.WSMsgType.TEXT:
            return DeviceMessage("text", msg.data)
        if kind in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.ERROR,
        ):
            return DeviceMessage("closed")
        return DeviceMessage("binary")

    async def close(self, code: int) -> None:
        await _guarded(self._ws.close(code=code), self._abort, LinkError, "device_link")


async def _abort_quietly(connector: Any, session: Any) -> None:
    """Abort every socket now (``abort_ssl``), then close the session; never raises."""
    with contextlib.suppress(Exception):
        await connector.close(abort_ssl=True)
    if session is not None:
        with contextlib.suppress(Exception):
            await session.close()


class AiohttpProviderLink:
    """The provider WebSocket as a ``ProviderLink``; owns its session and connector."""

    def __init__(self, ws: Any, session: Any, connector: Any) -> None:
        self._ws = ws
        self._session = session
        self._connector = connector
        self._closed = False
        self._abort_task: asyncio.Future[None] | None = None

    def __repr__(self) -> str:
        return "AiohttpProviderLink(<hidden>)"

    async def _abort(self) -> None:
        task = self._abort_task
        if task is None:
            task = self._abort_task = asyncio.ensure_future(
                _abort_quietly(self._connector, self._session)
            )
        await asyncio.shield(task)

    async def send_text(self, text: str) -> None:
        await _guarded(
            self._ws.send_str(text), self._abort, ProviderError, "provider_link"
        )

    async def receive(self) -> ProviderMessage:
        failed = False
        msg: Any = None
        try:
            msg = await self._ws.receive()
        except Exception:  # noqa: BLE001 - fixed ProviderError below; no text is kept
            failed = True
        if failed:
            await self._abort()
            raise ProviderError("provider_link")
        kind = msg.type
        if kind == aiohttp.WSMsgType.TEXT:
            return ProviderMessage("text", msg.data)
        if kind == aiohttp.WSMsgType.BINARY:
            return ProviderMessage("invalid")  # fixed classification; discard bytes
        if kind == aiohttp.WSMsgType.ERROR and isinstance(msg.data, WebSocketError):
            # Locally detected frame/UTF-8 parser error, not a peer close code.
            # Never retain its message or code.
            return ProviderMessage("invalid")
        if kind == aiohttp.WSMsgType.CLOSE:
            return ProviderMessage("closed", None, self._ws.close_code == 1000)
        # CLOSED is aiohttp's own end-of-stream marker: a bare EOF / connection loss also
        # arrives as CLOSED with close_code 1000 and NO close frame, so it proves nothing.
        return ProviderMessage("closed", None, False)  # closed/closing/other error

    async def _close_all(self) -> None:
        await self._ws.close()
        await self._session.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await _guarded(self._close_all(), self._abort, ProviderError, "provider_link")


# -- provider dial ---------------------------------------------------------------------
async def _refuse_redirect(_session: Any, _ctx: Any, _params: Any) -> None:
    """Trace hook: raised BEFORE aiohttp follows any redirect, even without a Location."""
    raise ProviderError("provider_redirect")


def _client_ssl() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    return ctx


class _Dial:
    __slots__ = ("connector", "session", "ws")

    def __init__(self) -> None:
        self.connector: Any = None
        self.session: Any = None
        self.ws: Any = None


async def _dial(dial: _Dial, key: Secret, timeout: float) -> AiohttpProviderLink:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(_refuse_redirect)
    attempted = False

    async def upgrade_once(request: aiohttp.ClientRequest,
                           handler: aiohttp.ClientHandlerType) -> aiohttp.ClientResponse:
        # aiohttp retries a disconnected GET, including a WebSocket upgrade.
        # We cannot know if the provider saw the first request. This middleware
        # runs before each connect/send attempt; its latch lives for one dial.
        nonlocal attempted
        if attempted or loop.time() >= deadline:
            raise ProviderError("provider_connect")
        attempted = True
        return await handler(request)

    dial.connector = aiohttp.TCPConnector(ssl=_client_ssl())
    dial.session = aiohttp.ClientSession(
        connector=dial.connector,
        trust_env=False,
        trace_configs=[trace],
        middlewares=(upgrade_once,),
        timeout=aiohttp.ClientTimeout(
            total=None, connect=timeout, sock_connect=timeout
        ),
    )
    async with asyncio.timeout_at(deadline):
        dial.ws = await dial.session.ws_connect(
            gp.PROVIDER_URL,
            headers={"Authorization": "Bearer " + key.reveal()},
            max_msg_size=gs.MAX_PROVIDER_FRAME_BYTES,
            compress=0,
            autoping=True,
        )
    return AiohttpProviderLink(dial.ws, dial.session, dial.connector)


async def _discard(dial: _Dial) -> None:
    if dial.connector is None:
        return
    closer = asyncio.ensure_future(_abort_quietly(dial.connector, dial.session))
    await asyncio.shield(closer)


async def connect_provider(key: Secret, *, timeout: float) -> AiohttpProviderLink:
    """Dial the fixed GPT-Live URL with verified TLS and only the bearer header.

    The whole handshake is bounded by ``timeout``; a redirect is refused before it is
    followed.  Whatever was created is aborted on cancellation or any failure (including a
    connect result that arrives after a swallowed cancel); a failure is a fixed
    ``ProviderError`` with no aiohttp text, cause or context.
    """
    dial = _Dial()
    failure = "provider_connect"
    try:
        link = await _dial(dial, key, timeout)
        task = asyncio.current_task()
        if task is None or not task.cancelling():
            return link
        raise asyncio.CancelledError  # a cancel was swallowed below us: close the late result
    except asyncio.CancelledError:
        await _discard(dial)
        raise
    except ProviderError:
        await _discard(dial)
        raise
    except aiohttp.WSServerHandshakeError as err:
        # Only explicit upstream authentication/authorization refusals are
        # definitive. Never retain or forward the exception, status or headers.
        if type(err.status) is int and err.status in (401, 403):
            failure = "provider_auth"
        elif type(err.status) is int and err.status == 101:
            # aiohttp rejected the 101 upgrade's WebSocket headers/accept value.
            failure = "provider_protocol"
    except Exception:  # noqa: BLE001 - fixed ProviderError below; no aiohttp text is kept
        pass
    except BaseException:
        await _discard(dial)
        raise
    await _discard(dial)
    raise ProviderError(failure)


# -- site ------------------------------------------------------------------------------
class _NullLogger:
    """Absorbs aiohttp's server logging so nothing request-shaped is ever formatted."""

    def debug(self, *_a: Any, **_k: Any) -> None: ...
    def info(self, *_a: Any, **_k: Any) -> None: ...
    def warning(self, *_a: Any, **_k: Any) -> None: ...
    def error(self, *_a: Any, **_k: Any) -> None: ...
    def exception(self, *_a: Any, **_k: Any) -> None: ...
    def critical(self, *_a: Any, **_k: Any) -> None: ...
    def log(self, *_a: Any, **_k: Any) -> None: ...

    def isEnabledFor(self, _level: int) -> bool:  # the logging API's own name
        return False


def _new_ws() -> web.WebSocketResponse:
    return web.WebSocketResponse(
        max_msg_size=gs.MAX_DEVICE_FRAME_BYTES,
        compress=False,
        autoping=True,
        protocols=(),
    )


async def _settle_owned(awaitable: Awaitable[T]) -> T:
    """Keep custody of startup/cleanup through repeated caller cancellation.

    Let the owned operation finish before cleanup or returning cancellation to
    the caller. Never leave an unobserved task behind.
    """
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                cancelled = True
        except BaseException:
            break
    if cancelled:
        with contextlib.suppress(BaseException):
            task.result()
        raise asyncio.CancelledError
    return task.result()


class _BindAddressAbsent(OSError):
    """Only the actual bind reported EADDRNOTAVAIL; no request data is retained."""


class _OwnedSite:
    """One IPv4 TLS listener, owning its socket before bind and server before listen."""

    def __init__(
        self,
        runner: web.ServerRunner,
        host: str,
        port: int,
        *,
        ssl_context: ssl.SSLContext,
        reuse_address: bool,
        reuse_port: bool,
    ) -> None:
        if reuse_port:
            raise ValueError("reuse_port is not supported")
        self._runner = runner
        self._host = host
        self._port = port
        self._ssl_context = ssl_context
        self._reuse_address = reuse_address
        self._socket: socket.socket | None = None
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket = sock
        if self._reuse_address:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self._host, self._port))
        except OSError as err:
            if err.errno == errno.EADDRNOTAVAIL:
                raise _BindAddressAbsent from None
            raise
        server = self._runner.server
        if server is None:
            raise RuntimeError("runner is not set up")
        # Keep the bound raw socket even if create_server fails before returning.
        # Never begin listening until the returned server has an owner.
        self._server = await asyncio.get_running_loop().create_server(
            server, sock=sock, ssl=self._ssl_context, backlog=128, start_serving=False
        )
        await self._server.start_serving()

    async def stop(self) -> None:
        try:
            if self._server is not None:
                self._server.close()
        finally:
            if self._socket is not None:
                self._socket.close()


class _SiteHandle:
    """Owned listener cleanup plus optional Tailnet readiness and failure signal."""

    def __init__(
        self,
        close: Callable[[], Awaitable[None]],
        begin_close: Callable[[], None],
    ) -> None:
        self._close = close
        self.begin_close = begin_close
        self.failure = asyncio.Event()
        self.tailnet_state = "none"
        self.on_tailnet_ready: Callable[[], None] | None = None

    async def __call__(self) -> None:
        self.begin_close()
        await self._close()


async def _start_site(
    handler: Callable[[web.BaseRequest], Awaitable[web.StreamResponse]],
    cfg: gs.GatewayConfig,
    ssl_ctx: ssl.SSLContext,
) -> _SiteHandle:
    """Listen on the configured private addresses; return an async closer.

    A low-level ``web.Server`` (no router, no middleware, no access log, a null logger); the
    shutdown timeout is set on the runner. One runner owns both sites. Only an
    unavailable optional bind permits LAN-only admission and a bounded-interval
    retry. Other failures or ambiguous cleanup close admission.
    """
    ready = False
    stopping = False
    retry_task: asyncio.Task[None] | None = None
    handle: _SiteHandle

    async def gated(request: web.BaseRequest) -> web.StreamResponse:
        if not ready:
            return reject_response(Rejection(503, "core_binding_required"))
        if cfg.tailnet_bind_host is not None and handle.tailnet_state != "ready":
            conn = request.transport
            local = conn.get_extra_info("sockname") if conn is not None else None
            if local == (cfg.tailnet_bind_host, cfg.port):
                return reject_response(Rejection(503, "core_binding_required"))
        return await handler(request)

    options: dict[str, Any] = {"access_log": None, "logger": _NullLogger()}
    server = web.Server(gated, **options)
    runner = web.ServerRunner(server, shutdown_timeout=_RUNNER_SHUTDOWN_S)
    sites: list[_OwnedSite] = []

    async def cleanup() -> None:
        failure: Exception | None = None
        if retry_task is not None:
            await asyncio.gather(retry_task, return_exceptions=True)
        for site in sites:
            try:
                await site.stop()
            except Exception as err:  # noqa: BLE001 - still close the other sites
                if failure is None:
                    failure = err
        try:
            await runner.cleanup()
        except Exception as err:  # noqa: BLE001 - keep the earliest cleanup failure
            if failure is None:
                failure = err
        if failure is not None:
            raise failure

    def begin_close() -> None:
        nonlocal ready, stopping
        ready = False
        stopping = True
        if retry_task is not None and not retry_task.done():
            retry_task.cancel()

    async def close() -> None:
        await _settle_owned(cleanup())

    handle = _SiteHandle(close, begin_close)

    def new_site(address: str) -> _OwnedSite:
        site = _OwnedSite(
            runner, address, cfg.port, ssl_context=ssl_ctx,
            reuse_address=True, reuse_port=False,
        )
        sites.append(site)
        return site

    async def start_tailnet() -> bool:
        assert cfg.tailnet_bind_host is not None
        site = new_site(cfg.tailnet_bind_host)
        try:
            await site.start()
        except _BindAddressAbsent:
            # Only discard the failed site after its socket has been closed.
            await site.stop()
            sites.remove(site)
            return False
        return True

    async def retry_attempt() -> bool:
        nonlocal ready
        try:
            return await start_tailnet()
        except Exception:  # noqa: BLE001 - latch before a cancelled waiter can discard it
            ready = False
            handle.tailnet_state = "failed"
            handle.failure.set()
            raise

    async def retry_tailnet() -> None:
        nonlocal ready
        try:
            while not stopping:
                await asyncio.sleep(_TAILNET_RETRY_S)
                if stopping:
                    return
                if await _settle_owned(retry_attempt()):
                    if not stopping:
                        handle.tailnet_state = "ready"
                        if handle.on_tailnet_ready is not None:
                            handle.on_tailnet_ready()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - fixed failure, no exception text
            ready = False
            handle.tailnet_state = "failed"
            handle.failure.set()

    try:
        await _settle_owned(runner.setup())
        await _settle_owned(new_site(cfg.bind_host).start())
        if cfg.tailnet_bind_host is not None:
            if await _settle_owned(start_tailnet()):
                handle.tailnet_state = "ready"
            else:
                handle.tailnet_state = "retrying"
                retry_task = asyncio.create_task(retry_tailnet())
        ready = True
    except BaseException:
        begin_close()
        await _settle_owned(cleanup())
        raise

    return handle


async def _wait_for_signals() -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    installed: list[int] = []
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        await stop.wait()
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)


def _new_uuid() -> str:
    return str(uuid.uuid4())


@dataclass
class ServeDeps:
    """Every seam of ``serve``: production defaults; tests inject spies."""

    client_factory: Callable[[], Any] = transport.HttpsClient
    ssl_factory: Callable[[Path, Path], ssl.SSLContext] = build_server_ssl_context
    start_site: Callable[..., Awaitable[Callable[[], Awaitable[None]]]] = _start_site
    provider_connect: Callable[..., Awaitable[ProviderLink]] = connect_provider
    wait_for_shutdown: Callable[[], Awaitable[None]] = _wait_for_signals
    clock: Callable[[], Any] = RealClock
    new_id: Callable[[], str] = _new_uuid


# -- the gateway: one client slot, owned custody ----------------------------------------
@dataclass(frozen=True)
class Reconciliation:
    """Metadata only: the confirm outcome as the core reported it (never text).

    ``send_outcome`` is the RAW core name (``accepted`` / ``rejected`` / ``uncertain`` /
    ``refused``, anything else becomes ``uncertain``).  A core ``refused`` means the core
    refused before any send, for ANY reason; ``PetSession`` is stricter and reports a
    refusal whose reason is not on its known-unsent list as ``uncertain``, so for one job
    ``SessionOutcome.send_outcome`` and this field may differ (this one never claims more).
    """

    send_outcome: str | None
    source: str  # session | result | unavailable | exception
    conversation: SessionOutcome | None = None


_OUTCOMES = frozenset({"accepted", "rejected", "uncertain", "refused"})


class _Gateway:
    """Handler state: the single client slot and the custody of its session."""

    def __init__(
        self,
        cfg: gs.GatewayConfig,
        key: Secret,
        registry: AuthRegistry,
        runner: CoreRunner,
        ops: UnifiedOps,
        deps: ServeDeps,
        *,
        ws_factory: Callable[[], Any] | None = None,
        device_factory: Callable[[Any, Any], DeviceLink] | None = None,
        session_factory: Callable[[Any, Callable[[], Awaitable[Any]]], Any]
        | None = None,
        follow_host_restart: Callable[[], tuple[str, str] | None] | None = None,
        event_log: EventLog | None = None,
    ) -> None:
        self._cfg = cfg
        self._key = key
        self._registry = registry
        self._runner = runner
        self._ops = ops
        self._deps = deps
        self._clock = deps.clock()
        self._settings = SessionSettings(
            cfg.model, cfg.voice, cfg.session_label, cfg.session_id_short
        )
        self._ws_factory = ws_factory or _new_ws
        self._device_factory = device_factory or AiohttpDeviceLink
        self._session_factory = session_factory
        self._busy = False
        self._closing = False
        self._active: Any = None
        self._custody_task: asyncio.Future[None] | None = None
        self._run_task: asyncio.Future[Any] | None = None
        self._custody_t0 = 0.0
        self._sessions = 0
        self._last: Reconciliation | None = None
        self._forced = False
        self._follow_host_restart = follow_host_restart
        self._follow_task: asyncio.Task[Any] | None = None
        self._event_log = event_log

    def __repr__(self) -> str:
        return "_Gateway(<hidden>)"

    # -- read-only metadata
    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def sessions_started(self) -> int:
        return self._sessions

    @property
    def last_outcome(self) -> Reconciliation | None:
        return self._last

    @property
    def forced_cancel(self) -> bool:
        return self._forced

    # -- sessions
    async def _connect(self, *, remaining: Callable[[], float] | None = None) -> Any:
        # Custody owns the single slot until the prior runner has settled. Recheck
        # its durable send state immediately before any paid provider connection.
        # This explicitly named job may reconcile an exact positively proven input.
        safe = False
        try:
            safe = await self._runner.run(
                "pre_dial_admission", self._ops.pre_dial_admission, urgent=False, retain=False
            )
        except Exception:  # noqa: BLE001 - failure is a fixed local policy refusal
            pass
        if safe is not True:
            raise ProviderError("provider_policy") from None
        if self._closing:
            raise asyncio.CancelledError
        await self._event("provider_dial", outcome="attempt", code=None)
        try:
            # Both admission and event-log I/O may have suspended. Revalidate
            # at the paid boundary rather than relying on timer dispatch order.
            timeout = gs.START_DEADLINE_S if remaining is None else remaining()
            if timeout <= 0:
                raise ProviderError("provider_policy")
            link = await self._deps.provider_connect(self._key, timeout=timeout)
        except BaseException as err:
            code = err.code if isinstance(err, ProviderError) else "provider_connect"
            await self._event("provider_dial", outcome="failed", code=code)
            raise
        # No new suspension after acquiring a link and before handing ownership
        # to PetSession: cancellation there would strand the paid connection.
        await self._event("provider_dial", outcome="open", code=None, wait=False)
        return link

    async def _log(self, write: Callable[[], None], *, wait: bool = True) -> None:
        # O_NONBLOCK does not bound regular-file writes. Runtime metadata I/O,
        # including logging rejected connections during a call, belongs off the
        # asyncio thread just like journal I/O. Admission is bounded/nonblocking;
        # an unavailable runner may drop optional metadata, never block capture.
        with contextlib.suppress(RunnerError):
            ticket = self._runner.submit("event_log", write, retain=False)
            if wait:
                await ticket.wait()

    async def _event(self, event: str, *, wait: bool = True, **facts: Any) -> None:
        sink = self._event_log
        if sink is not None:
            await self._log(lambda: sink.write(event, **facts), wait=wait)

    async def _reject(self, rejection: Rejection) -> web.Response:
        await self._event("device_connect", outcome="refused", code=rejection.code,
                          status=rejection.status)
        return reject_response(rejection)

    async def _follow(self) -> None:
        assert self._follow_host_restart is not None
        changed = await self._runner.run(
            "follow_host_restart", self._follow_host_restart, urgent=False, retain=False
        )
        if changed is not None:
            old, new = changed
            await self._event("host_restart_follow", old_instance=old[:8], new_instance=new[:8])

    def _abandon_follow(self, task: asyncio.Task[Any]) -> None:
        """Cancelled HTTP waiter: the queued job, not that waiter, owns the slot."""
        if not task.cancelled():
            task.exception()
        self._busy = False

    def _new_session(self, device: Any) -> PetSession:
        async def connect() -> Any:
            return await self._connect(remaining=session.provider_start_remaining)

        session = PetSession(
            device, connect, self._runner, self._ops, self._settings, self._deps.clock()
        )
        return session

    async def handle(self, request: web.BaseRequest) -> web.StreamResponse:
        """One device request: guard, upgrade, take the slot, own the session's custody."""
        ws: Any = None
        session: Any = None
        try:
            rejection = check_handshake(request, self._cfg, self._registry)
            if rejection is not None:
                return await self._reject(rejection)
            ws = self._ws_factory()
            if not ws.can_prepare(request).ok:
                return await self._reject(Rejection(400, "bad_request"))
            if self._closing or self._busy:
                return await self._reject(Rejection(409, "busy"))
            self._busy = True
            run_done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            owned = False
            try:
                if self._follow_host_restart is not None:
                    # No session can submit a core job until this ONE FIFO job has
                    # settled. Shield it: cancelling HTTP cannot release its custody.
                    self._follow_task = asyncio.create_task(self._follow())
                    try:
                        await asyncio.shield(self._follow_task)
                    except (StartupError, core.BridgeError):
                        return await self._reject(Rejection(503, "core_binding_required"))
                    if self._closing:
                        return await self._reject(Rejection(409, "busy"))
                await ws.prepare(request)
                # Preserve the no-suspension upgrade-to-custody boundary.
                await self._event("device_connect", outcome="accepted", code=None, status=101,
                                  wait=False)
                if self._closing:
                    # Shutdown began while the upgrade was pending and returned with nothing
                    # to settle: no device link, session, dial or core job may start now.
                    await self._refuse_late_upgrade(ws, request)
                    return ws
                device = self._device_factory(ws, request.transport)
                session = (self._new_session(device) if self._session_factory is None
                           else self._session_factory(device, self._connect))
                self._active = session
                self._sessions += 1
                self._custody_t0 = self._clock.now()
                # The run is created HERE, with the custody, so a shutdown in the one-step
                # window before the custody starts bounds THIS session's run (never a stale
                # reference to a previous one) and the custody has nothing to wait for blindly.
                run = self._run_task = asyncio.ensure_future(session.run())
                self._custody_task = asyncio.ensure_future(
                    self._custody(session, device, run, run_done)
                )
                owned = True
            finally:
                if not owned:
                    follow = self._follow_task
                    if follow is not None and not follow.done():
                        follow.add_done_callback(self._abandon_follow)
                    else:
                        self._busy = False
                    self._active = None
            await asyncio.shield(run_done)
            return ws
        except asyncio.CancelledError:
            if session is not None:
                await (
                    session.request_shutdown()
                )  # the owned custody task keeps the slot
            raise
        except Exception:  # noqa: BLE001 - a fixed response; no exception text reaches a client
            if ws is not None and getattr(ws, "prepared", False):
                with contextlib.suppress(Exception):
                    await ws.close(code=1011)
                return ws
            return await self._reject(Rejection(500, "internal_error"))

    @staticmethod
    async def _refuse_late_upgrade(ws: Any, request: web.BaseRequest) -> None:
        """Close an upgraded socket with the fixed shutdown code; abort if that fails."""
        try:
            await ws.close(code=_SHUTDOWN_CLOSE_CODE)
        except asyncio.CancelledError:
            _cut_socket(request.transport)
            raise
        except Exception:  # noqa: BLE001 - fixed behaviour: cut the socket, keep no text
            _cut_socket(request.transport)

    async def _custody(
        self,
        session: Any,
        device: Any,
        run: asyncio.Future[Any],
        run_done: asyncio.Future[None],
    ) -> None:
        """Wait for the session's run, then (always) settle: idle runner, reconcile, free."""
        try:
            try:
                await run
            except Exception:  # noqa: BLE001 - a failed run closed its own links already
                pass  # the detail is in session.outcome; nothing is kept here
            finally:
                abort = getattr(device, "abort_if_open", None)
                if callable(abort):
                    abort()
                if not run_done.done():
                    run_done.set_result(None)
        finally:
            await self._settle(session)

    async def _settle(self, session: Any) -> None:
        await self._runner.wait_idle()
        self._last = self._reconcile(session)
        if self._event_log is not None:
            outcome = self._last.conversation or session.outcome
            await self._log(lambda: self._event_log.session_end(outcome))
        self._active = None
        self._busy = False

    def _reconcile(self, session: Any) -> Reconciliation:
        """Take the in-flight confirm job's REAL outcome exactly once; never resend."""
        outcome = session.outcome
        job = outcome.confirm_job
        def recorded(
            name: str | None, source: str, classified: str | None = None,
        ) -> Reconciliation:
            summary = None
            if isinstance(outcome, SessionOutcome) and outcome.conversation:
                summary = outcome
                if job is not None:
                    state = classified or "uncertain"
                    summary = dataclasses.replace(
                        outcome, send_outcome=state, confirm_job=None,
                        requests_sent=outcome.requests_sent + (state != "refused"),
                        requests_accepted=outcome.requests_accepted + (state == "accepted"),
                        requests_uncertain=outcome.requests_uncertain + (state == "uncertain"),
                        requests_refused=outcome.requests_refused + (
                            state in ("refused", "rejected")
                        ),
                        refused_confirm=outcome.refused_confirm + (
                            state in ("refused", "rejected")
                        ),
                    )
            return Reconciliation(name, source, summary)

        if job is None:
            return recorded(outcome.send_outcome, "session")
        try:
            result = self._runner.take_result(job)
        except RunnerError:
            return recorded("uncertain", "unavailable")
        except Exception:  # noqa: BLE001 - the job raised: uncertain, and no text is kept
            return recorded("uncertain", "exception")
        name = getattr(result, "outcome", None)
        classified, _reason = PetSession._classify_confirm(True, result)
        return recorded(name if name in _OUTCOMES else "uncertain", "result", classified)

    # -- shutdown
    def _shutdown_bound(self) -> float:
        """The wait for a session's run: its own remaining cap, never an extension."""
        if (self._active is not None
                and getattr(self._active.outcome, "conversation", False)):
            # The old 60 s cap applies only to one-turn mode. PetSession itself
            # reserves close time within the conversation's 600 s total cap.
            cap = self._custody_t0 + gs.AWAIT_START_S + gs.CONVERSE_LIMIT_S
            return max(0.0, min(gs.CLOSE_DEADLINE_S + 1.0, cap - self._clock.now()))
        cap = self._custody_t0 + gs.AWAIT_START_S + gs.SESSION_CAP_S
        return max(0.0, min(gs.CLOSE_DEADLINE_S + 1.0, cap - self._clock.now()))

    async def _bounded(self, task: asyncio.Future[Any], seconds: float) -> None:
        if seconds <= 0 or task.done():
            return
        timer = asyncio.ensure_future(self._clock.sleep(seconds))
        try:
            await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)

    async def shutdown(self) -> None:
        """Close the door, stop the active session, wait until it is fully settled."""
        self._closing = True
        if self._follow_task is not None:
            # Never cancel the job or free its store underneath it.
            await asyncio.wait({self._follow_task})
            if not self._follow_task.cancelled():
                self._follow_task.exception()
        custody, run, session = self._custody_task, self._run_task, self._active
        if custody is not None and not custody.done():
            if session is not None:
                await session.request_shutdown()
            if run is not None and not run.done():
                await self._bounded(run, self._shutdown_bound())
                if not run.done():
                    self._forced = True
                    run.cancel()  # PetSession cleans up inside its own hard deadline
                    await self._bounded(run, gs.CLOSE_DEADLINE_S)
                    if not run.done():
                        run.cancel()  # a second cancel aborts that cleanup
            await asyncio.wait({custody})
        if custody is not None and custody.done() and not custody.cancelled():
            custody.exception()  # retrieved: nothing is left unobserved
        if self._busy and self._active is not None:
            await self._settle(
                self._active
            )  # a cancelled custody still gets reconciled


def _make_handler(
    gateway: _Gateway,
) -> Callable[[web.BaseRequest], Awaitable[web.StreamResponse]]:
    return gateway.handle


# -- serve -----------------------------------------------------------------------------
@dataclass
class _Held:
    """What startup has opened so far, released in the one safe order."""

    store: Any = None
    journal: Any = None
    runner: CoreRunner | None = None
    gateway: _Gateway | None = None
    ops: UnifiedOps | None = None
    closer: Callable[[], Awaitable[None]] | None = None
    event_log: EventLog | None = None


def _check_tls_files(cfg: gs.GatewayConfig) -> tuple[Path, Path]:
    cert, key = Path(cfg.tls_cert_file), Path(cfg.tls_key_file)
    try:
        check_private_file(cert, max_bytes=None, read=False)
        check_private_file(key, max_bytes=None, read=False)
    except OSError:
        pass
    else:
        return cert, key
    raise StartupError("cert_missing")


def _check_state_root(cfg: gs.GatewayConfig) -> Path:
    root = Path(cfg.state_root)
    store_root = Path(cfg.auth_root) if cfg.auth_root is not None else root / "auth"
    try:
        paths = (root, root / "journal", store_root)
        infos = [os.lstat(path) for path in paths]
        ok = all(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700
            for info in infos
        ) and (
            (infos[1].st_dev, infos[1].st_ino)
            != (infos[2].st_dev, infos[2].st_ino)
        )
    except OSError:
        ok = False
    if not ok:
        raise StartupError("core_binding_required")
    return root


def _open_store(held: _Held, root: Path, cfg: gs.GatewayConfig) -> Any:
    """The credential store and the attachment of ``credential_ref`` (token not kept)."""
    attachment: Any = None
    try:
        held.store = auth.CredentialStore(
            Path(cfg.auth_root) if cfg.auth_root is not None else root / "auth"
        )
        attachment = held.store.load_attachment(cfg.credential_ref)
    except (auth.AuthError, OSError):
        attachment = None
    if attachment is None:
        raise StartupError("credential_missing")
    return attachment


def _frozen_binding(
    journal: Journal, cfg: gs.GatewayConfig, attachment: Any
) -> core.Binding:
    """The journal's binding record, which must match the config and the attachment."""
    names = {field.name for field in dataclasses.fields(core.Binding)}
    try:
        record = journal.get(BINDING_NAME)
        raw = record.get("binding") if isinstance(record, dict) else None
        if (
            isinstance(raw, dict)
            and set(raw) == names
            and all(isinstance(v, str) for v in raw.values())
            and raw["session_id"] == cfg.session_id
            and raw["credential_ref"] == cfg.credential_ref
            and raw["host_id"] == attachment.host_identity
            and raw["host_instance_id"] == attachment.host_instance_id
            and raw["client_id"] == attachment.client_id
        ):
            return core.Binding(**raw)
    except (JournalError, TypeError, ValueError):
        pass
    raise StartupError("core_binding_required")


def _open_bridge(held: _Held, deps: ServeDeps, binding: core.Binding) -> core.Bridge:
    bridge: Any = None
    try:
        bridge = core.Bridge(
            held.journal,
            transport.HttpsTransport(held.store, deps.client_factory),
            clock=time.time,
            new_id=deps.new_id,
        )
        live = bridge.status()
        if (
            live.get("bound") is True
            and live.get("binding_id") == binding.binding_id
            and live.get("session_id") == binding.session_id
            and live.get("host_id") == binding.host_id
        ):
            return bridge
    except (core.BridgeError, JournalError, OSError):
        pass
    raise StartupError("core_binding_required")


def _follow_host_restart(
    held: _Held,
    cfg: gs.GatewayConfig,
    deps: ServeDeps,
    bridge: core.Bridge,
    binding: core.Binding,
) -> core.Binding:
    """Follow a restart of the SAME host; returns the binding to freeze.

    The host mints a new process instance id on every restart (each update restarts
    it), so a pinned instance goes stale without anything being wrong.  This runs
    only after the stored attachment and journal binding have passed every strict
    check.  One attach POST with the existing restricted credential and client id;
    only the volatile instance may change.  A different stable host identity is
    refused.  Any other failure (host unreachable, unexpected reply) changes nothing:
    a stale pin is then still refused at preflight, before anything is sent.

    When the instance did change, the same session is re-selected on it.  That
    advances the epoch, so a turn begun against the old instance is stale and a
    command already attempted keeps its recorded outcome; nothing is replayed.
    """
    try:
        refreshed = auth.refresh_instance(
            held.store,
            deps.client_factory(),
            cfg.credential_ref,
            expected_instance_id=binding.host_instance_id,
        )
    except auth.AuthError as err:
        if err.code == "auth_host_mismatch":
            raise StartupError("core_binding_required") from None
        return binding
    except Exception:  # noqa: BLE001 - best effort; the strict checks still apply
        return binding
    if refreshed.host_instance_id == binding.host_instance_id:
        return binding
    try:
        return bridge.select_session(
            host_id=refreshed.host_identity,
            host_instance_id=refreshed.host_instance_id,
            session_id=cfg.session_id,
            client_id=refreshed.client_id,
            credential_ref=cfg.credential_ref,
        )
    except (core.BridgeError, JournalError, OSError):
        raise StartupError("core_binding_required") from None


async def _wait_for_gateway_shutdown(
    wait: Callable[[], Awaitable[None]], handle: _SiteHandle
) -> None:
    """Wait for shutdown or a fatal optional-listener error, owning both waiters."""
    shutdown = asyncio.ensure_future(wait())
    failure = asyncio.create_task(handle.failure.wait())
    try:
        await asyncio.wait((shutdown, failure), return_when=asyncio.FIRST_COMPLETED)
        if handle.failure.is_set():
            raise StartupError("listen_failed")
        await shutdown
    finally:
        for task in (shutdown, failure):
            if not task.done():
                task.cancel()
        await asyncio.gather(shutdown, failure, return_exceptions=True)
    if handle.failure.is_set():
        raise StartupError("listen_failed")


async def _startup(
    held: _Held,
    config_path: Path,
    env: Mapping[str, str],
    stdout: TextIO,
    deps: ServeDeps,
    env_file: bool = False,
) -> None:
    """Every startup step in the plan's order; a failure never reaches a later step."""
    inline: Secret | None = None
    if env_file:
        cfg, inline = load_env_config(config_path)
    else:
        cfg = load_config(config_path)
    cert, key_path = _check_tls_files(cfg)
    if env_file:
        provider_key = _env_file_provider_key(cfg, inline)
    else:
        provider_key = load_provider_key(cfg, env)
    del inline
    root = _check_state_root(cfg)
    attachment = _open_store(held, root, cfg)
    try:
        held.journal = Journal(root / "journal")
    except (JournalError, OSError):
        raise StartupError("core_binding_required") from None
    binding = _frozen_binding(held.journal, cfg, attachment)
    del attachment
    bridge = _open_bridge(held, deps, binding)
    before = binding
    binding = _follow_host_restart(held, cfg, deps, bridge, binding)
    if held.event_log is not None and before.host_instance_id != binding.host_instance_id:
        held.event_log.write("host_restart_follow",
                            old_instance=before.host_instance_id[:8],
                            new_instance=binding.host_instance_id[:8])
    try:
        ctx = deps.ssl_factory(cert, key_path)
    except StartupError:
        raise
    except Exception:  # noqa: BLE001 - any factory failure is the fixed cert_missing
        ctx = None
    if ctx is None:
        raise StartupError("cert_missing")
    held.runner = CoreRunner()
    held.runner.start()
    held.ops = UnifiedOps(
        bridge,
        held.store,
        cfg.credential_ref,
        cfg.session_id,
        deps.client_factory,
        binding=binding,
    )
    held.gateway = _Gateway(
        cfg, provider_key, AuthRegistry(), held.runner, held.ops, deps,
        follow_host_restart=lambda: held.ops.follow_host_restart(
            lambda current: _follow_host_restart(held, cfg, deps, bridge, current)
        ),
        event_log=held.event_log,
    )
    try:
        held.closer = await deps.start_site(_make_handler(held.gateway), cfg, ctx)
    except Exception:  # noqa: BLE001 - any listen failure is the fixed listen_failed
        held.closer = None
    if held.closer is None:
        raise StartupError("listen_failed")
    addresses = f"{cfg.bind_host}:{cfg.port}"
    site_handle = held.closer if isinstance(held.closer, _SiteHandle) else None
    if cfg.tailnet_bind_host is not None and (
        site_handle is None or site_handle.tailnet_state == "ready"
    ):
        addresses += f", {cfg.tailnet_bind_host}:{cfg.port}"
    print(f"gateway: listening on {addresses} for session ...{cfg.session_id_short}", file=stdout)
    if site_handle is not None and site_handle.tailnet_state == "retrying":
        stdout.write("gateway: Tailnet address unavailable; retrying\n")

        def recovered() -> None:
            with contextlib.suppress(OSError, ValueError):
                stdout.write("gateway: Tailnet listener ready\n")
                stdout.flush()

        site_handle.on_tailnet_ready = recovered
    # A service's stdout is a pipe, which Python block-buffers: without this the lines
    # above would stay in the buffer until exit.  A failed flush never stops a gateway
    # that is already listening (as for the Tailnet line in ``recovered``).
    with contextlib.suppress(OSError, ValueError):
        stdout.flush()
    if site_handle is not None and cfg.tailnet_bind_host is not None:
        await _wait_for_gateway_shutdown(deps.wait_for_shutdown, site_handle)
    else:
        await deps.wait_for_shutdown()


async def _release(held: _Held) -> None:
    """Stop the session, close the site, stop the bridge, THEN idle the runner, THEN close.

    The journal and store are closed only after the worker thread has exited; if that wait
    is interrupted they are left to process exit rather than closed under a running call.
    """
    gateway, runner = held.gateway, held.runner
    idle = runner is None
    try:
        if isinstance(held.closer, _SiteHandle):
            held.closer.begin_close()
        if gateway is not None:
            await gateway.shutdown()
        if held.closer is not None:
            await held.closer()
        ran = gateway is not None and gateway.sessions_started > 0
        if ran and runner is not None and held.ops is not None:
            with contextlib.suppress(Exception):
                await runner.run("stop", held.ops.stop, urgent=True, retain=False)
    finally:
        try:
            if runner is not None:
                await runner.aclose()
                idle = True
        finally:
            if idle:
                try:
                    if held.journal is not None:
                        held.journal.close()
                finally:
                    if held.store is not None:
                        held.store.close()


async def _serve(
    config_path: Path,
    env: Mapping[str, str],
    stdout: TextIO,
    stderr: TextIO,
    deps: ServeDeps,
    event_log_path: Path | None = None,
    env_file: bool = False,
) -> int:
    held = _Held()
    failure: str | None = None
    try:
        if event_log_path is not None:
            try:
                held.event_log = EventLog(event_log_path, stderr)
                held.event_log.write("gateway_start", required=True)
            except EventLogError:
                raise StartupError("event_log_invalid") from None
        await _startup(held, config_path, env, stdout, deps, env_file)
    except StartupError as err:
        failure = err.code
    finally:
        try:
            await _release(held)
            if isinstance(held.closer, _SiteHandle) and held.closer.failure.is_set():
                failure = "listen_failed"
        finally:
            # Interrupted runner shutdown can leave a runtime log write alive.
            # As with the journal/store, never close its fd under that worker.
            if held.event_log is not None and (held.runner is None or held.runner.idle):
                held.event_log.write("gateway_stop", code=failure)
                held.event_log.close()
    if failure is not None:
        print("error: " + failure, file=stderr)
        return 1
    print("gateway: stopped", file=stdout)
    return 0


def serve_main(
    config_path: Path,
    *,
    env: Mapping[str, str],
    stdout: TextIO,
    stderr: TextIO,
    deps: ServeDeps | None = None,
    event_log_path: Path | None = None,
    env_file: bool = False,
) -> int:
    """Run the gateway from an explicit private config until SIGINT/SIGTERM.  Exit code.

    ``config_path`` is the private JSON config, or with ``env_file=True`` the private
    ``.env`` spelling of the same settings (``env`` is then not consulted).
    """
    return asyncio.run(
        _serve(Path(config_path), env, stdout, stderr, deps or ServeDeps(), event_log_path,
               env_file)
    )


__all__ = [
    "STARTUP_CODES",
    "AiohttpDeviceLink",
    "AiohttpProviderLink",
    "AuthRegistry",
    "LinkError",
    "ProviderError",
    "Rejection",
    "Secret",
    "ServeDeps",
    "StartupError",
    "build_server_ssl_context",
    "check_handshake",
    "check_private_file",
    "connect_provider",
    "load_config",
    "load_env_config",
    "load_provider_key",
    "reject_response",
    "serve_main",
    "verify_bearer",
]
