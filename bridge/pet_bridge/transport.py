"""HTTPS transport for the host bridge (stdlib only).

Defence in depth, all offline-tested; none of it is a live Unified qualification:

* ``Origin`` accepts only ``https://<ASCII hostname>[:port][/]``.
* Callers use fixed ``route_*`` constructors, so no free URL,
  unlisted query, encoded slash or dot-dot reaches the network.
* ``HttpsClient`` runs ONE supervised child process per exchange (see
  ``_https_worker``); it never follows a redirect and never retries.
* ``HttpsTransport`` is the core ``Transport``: every binding field is checked
  against the stored credential/attachment BEFORE anything is sent.

Errors carry a fixed code, never a body, header, token or URL.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import math
import os
import re
import selectors
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import _https_worker as worker
from . import wire

PAIR_CAP = 16 * 1024
COMPANION_CAP = 256 * 1024
DETAIL_CAP = 1024 * 1024
SESSIONS_OBSERVATION_RESOURCE = "/api/sessions?view=observation"
DEFAULT_TIMEOUT = 10.0
# The host answers a send only after dispatching it, which for a dormant conversation
# includes starting its runtime: observed to exceed 10 s. Giving up early leaves a
# delivered command recorded as uncertain, which then blocks every later send.
SEND_TIMEOUT = 60.0
REAP_TIMEOUT = 2.0

ERROR_CODES = frozenset(
    {
        "transport_error",
        "origin_invalid",
        "route_invalid",
        "binding_invalid",
        "body_invalid",
        "credential_missing",
        "request_invalid",
        "response_invalid",
        "response_too_large",
        "content_type_invalid",
        "redirect_refused",
        "timeout",
        "dns_error",
        "connect_error",
        "tls_error",
        "tls_verify_failed",
        "http_protocol_error",
        "worker_error",
        "worker_failed",
        "worker_flood",
        "worker_ipc_invalid",
        "spawn_failed",
        "busy",
        "child_faulted",
        "child_unreaped",
    }
)


class TransportError(Exception):
    """A transport failure identified by a fixed ``code`` and metadata only.

    ``maybe_sent`` is False only when nothing can have reached the network.
    ``route`` is a fixed route name, never a URL.
    """

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        route: str | None = None,
        maybe_sent: bool = False,
    ) -> None:
        if code not in ERROR_CODES:
            code = "transport_error"
        super().__init__(code)
        self.code = code
        self.status = status
        self.route = route
        self.maybe_sent = maybe_sent

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = field(repr=False)


# -- origin -------------------------------------------------------------------
_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_PORT_RE = re.compile(r"[1-9][0-9]{0,4}")


@dataclass(frozen=True)
class Origin:
    """A normalised ``https`` origin: lower-case ASCII host and an explicit port."""

    host: str
    port: int = 443

    @classmethod
    def parse(cls, value: Any) -> Origin:
        if isinstance(value, Origin):
            return value
        if (
            type(value) is not str
            or not value.isascii()
            or not value.startswith("https://")
        ):
            raise TransportError("origin_invalid")
        rest = value[len("https://") :]
        if rest.endswith("/"):
            rest = rest[:-1]
        if (
            not rest
            or any(ch in rest for ch in "/?#@\\%[] \t\r\n\x00")
            or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in rest)
        ):
            raise TransportError("origin_invalid")
        host, sep, port_text = rest.partition(":")
        port = 443
        if sep:
            if _PORT_RE.fullmatch(port_text) is None or int(port_text) > 65535:
                raise TransportError("origin_invalid")
            port = int(port_text)
        labels = host.split(".")
        if (
            not host
            or len(host) > 253
            or any(_LABEL_RE.fullmatch(label) is None for label in labels)
        ):
            raise TransportError("origin_invalid")
        return cls(host.lower(), port)

    @property
    def url(self) -> str:
        return f"https://{self.host}" + ("" if self.port == 443 else f":{self.port}")


# -- routes -------------------------------------------------------------------
_SESSION_ID_MAX = 128
_BEFORE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")


@dataclass(frozen=True)
class Route:
    """One allow-listed request shape.  Built only by the ``route_*`` functions."""

    name: str
    method: str
    path: str
    query: tuple[tuple[str, str], ...]
    max_request: int
    max_response: int

    @property
    def target(self) -> str:
        if not self.query:
            return self.path
        pairs = "&".join(f"{k}={quote(v, safe='')}" for k, v in self.query)
        return f"{self.path}?{pairs}"


def _session(value: Any) -> str:
    if not wire.valid_session_id(value) or type(value) is not str:
        raise TransportError("route_invalid")
    return value


def _cursor(value: Any) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 1024
        or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in value)
    ):
        raise TransportError("route_invalid")
    return value


def route_pair_start() -> Route:
    return Route("pair_start", "POST", "/api/devices/pair", (), PAIR_CAP, PAIR_CAP)


def route_pair_token() -> Route:
    return Route(
        "pair_token", "POST", "/api/devices/pair/token", (), PAIR_CAP, PAIR_CAP
    )


def route_attach() -> Route:
    return Route("attach", "POST", "/api/clients/attach", (), PAIR_CAP, PAIR_CAP)


def route_send(session_id: Any) -> Route:
    sid = _session(session_id)
    return Route(
        "send",
        "POST",
        f"/api/sessions/{quote(sid, safe='')}/commands",
        (),
        PAIR_CAP,
        PAIR_CAP,
    )


def route_sessions_observation(
    *, scope: Any = "roots", limit: Any = 200, cursor: Any = None
) -> Route:
    """All activity/pins, explicit root/delegate scope; never an advertised URL proxy."""
    if (
        type(scope) is not str
        or scope not in {"roots", "delegates", "all"}
        or type(limit) is not int
        or not 1 <= limit <= 200
    ):
        raise TransportError("route_invalid")
    query = [
        ("view", "observation"),
        ("scope", scope),
        ("activity", "all"),
        ("pinned", "all"),
        ("limit", str(limit)),
    ]
    if cursor is not None:
        query.append(("cursor", _cursor(cursor)))
    route = Route(
        "sessions_observation", "GET", "/api/sessions", tuple(query), 0, COMPANION_CAP
    )
    # Percent-encoding an opaque cursor can expand it beyond the worker's wire cap.
    if len(route.target) > worker.MAX_TARGET:
        raise TransportError("route_invalid")
    return route


def route_detail(session_id: Any, before: Any = None) -> Route:
    query = [("sessionId", _session(session_id)), ("part", "messages")]
    if before is not None:
        if type(before) is not str or _BEFORE_RE.fullmatch(before) is None:
            raise TransportError("route_invalid")
        query.append(("before", before))
    return Route(
        "detail", "GET", "/api/conversation/detail", tuple(query), 0, DETAIL_CAP
    )


# -- strict JSON ----------------------------------------------------------------
def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _finite(value: Any, depth: int) -> bool:
    if depth > 32:
        return False
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_finite(v, depth + 1) for v in value)
    if type(value) is dict:
        return all(_finite(v, depth + 1) for v in value.values())
    return True


def strict_object(body: Any) -> dict[str, Any]:
    """Decode ONE strict JSON object: UTF-8, no duplicate keys, finite numbers."""
    try:
        if type(body) is not bytes:
            raise ValueError("not bytes")
        value = json.loads(
            body.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicates,
        )
        if type(value) is not dict or not _finite(value, 0):
            raise ValueError("not a finite object")
        return value
    except (ValueError, RecursionError):
        raise TransportError("response_invalid") from None


# -- supervised HTTPS client ------------------------------------------------------
_CHILD_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}
_FAILURE_CODES = worker.RESULT_CODES


class _ChildSlot:
    """ONE global, non-reentrant slot: held until the child is reaped."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = "free"
        self.proc: Any = None

    def acquire(self, route: str) -> None:
        with self._lock:
            if self.state == "faulted":
                raise TransportError("child_faulted", route=route)
            if self.state == "busy":
                raise TransportError("busy", route=route)
            self.state = "busy"

    def release(self) -> None:
        with self._lock:
            self.state, self.proc = "free", None

    def fault(self, proc: Any) -> None:
        with self._lock:
            self.state, self.proc = "faulted", proc

    def faulted_proc(self) -> Any:
        """The faulted child handle (read under the lock), or None when not faulted."""
        with self._lock:
            return self.proc if self.state == "faulted" else None

    def status(self) -> tuple[str, int | None]:
        with self._lock:
            pid = getattr(self.proc, "pid", None) if self.state == "faulted" else None
            return self.state, pid


_SLOT = _ChildSlot()


def child_slot_status() -> tuple[str, int | None]:
    """``("free" | "busy" | "faulted", pid)``; the pid is set only when faulted."""
    return _SLOT.status()


def _reap(proc: Any) -> bool:
    """Close the pipes, kill a live child and wait a bounded time.  True if reaped."""
    if proc is None:
        return True
    for stream in (getattr(proc, "stdin", None), getattr(proc, "stdout", None)):
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.close()
    if proc.poll() is not None:
        return True
    with contextlib.suppress(OSError):
        proc.kill()
    try:
        proc.wait(timeout=REAP_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False
    return True


def release_faulted_child() -> bool:
    """Owner action: retry killing and reaping a faulted child; free the slot if it worked."""
    proc = _SLOT.faulted_proc()
    if proc is None or not _reap(proc):
        return False
    _SLOT.release()
    return True


def _reset_child_slot_for_tests() -> None:
    _SLOT.release()


def _invalid_ipc(route: Route) -> TransportError:
    return TransportError("worker_ipc_invalid", route=route.name, maybe_sent=True)


def _parse_ipc(data: bytes, route: Route) -> Response:
    """Strictly parse the child's single result line."""
    if data.count(b"\n") != 1 or not data.endswith(b"\n"):
        raise _invalid_ipc(route)
    try:
        doc = strict_object(data[:-1])
    except TransportError:
        raise _invalid_ipc(route) from None
    if (
        type(doc.get("v")) is not int
        or doc["v"] != worker.IPC_VERSION
        or type(doc.get("ok")) is not bool
    ):
        raise _invalid_ipc(route)
    if not doc["ok"]:
        code = doc.get("code")
        if set(doc) != {"v", "ok", "code"} or code not in _FAILURE_CODES:
            raise _invalid_ipc(route)
        raise TransportError(
            code, route=route.name, maybe_sent=code != "request_invalid"
        )
    status, body = doc.get("status"), doc.get("body")
    if (
        set(doc) != {"v", "ok", "status", "body"}
        or type(status) is not int
        or not 200 <= status <= 599
        or 300 <= status < 400
        or type(body) is not str
    ):
        raise _invalid_ipc(route)
    try:
        raw = base64.b64decode(body.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise _invalid_ipc(route) from None
    if len(raw) > route.max_response:
        raise TransportError("response_too_large", route=route.name, maybe_sent=True)
    return Response(status, raw)


def _build_request(
    origin: Origin,
    route: Route,
    headers: Mapping[str, str],
    body: bytes,
    timeout: float,
) -> bytes:
    def bad() -> TransportError:
        return TransportError("request_invalid", route=route.name)

    if (
        type(body) is not bytes
        or len(body) > route.max_request
        or (route.method == "GET" and body)
        or not isinstance(headers, Mapping)
    ):
        raise bad()
    for name, value in headers.items():
        if (
            name not in worker.HEADER_ALLOW
            or type(value) is not str
            or len(value) > worker.MAX_HEADER_VALUE
            or any(not 0x20 <= ord(ch) <= 0x7E for ch in value)
        ):
            raise bad()
    document = {
        "v": worker.IPC_VERSION,
        "host": origin.host,
        "port": origin.port,
        "method": route.method,
        "target": route.target,
        "headers": dict(headers),
        "body": base64.b64encode(body).decode("ascii"),
        "max_request": route.max_request,
        "max_response": route.max_response,
        "timeout": timeout,
    }
    try:
        worker.validate_request(document)
    except worker.WorkerError:
        raise bad() from None
    return json.dumps(document, separators=(",", ":")).encode("ascii")


class HttpsClient:
    """One supervised, stdlib-only child process per exchange.

    ``request`` returns ``Response(status, body)`` for every HTTP status except a
    redirect (refused).  The deadline is monotonic and starts BEFORE the spawn, so it
    covers DNS, TLS, headers and body.  Worst case after a failure is the deadline
    plus ``REAP_TIMEOUT`` of kill/wait; the spawn syscall itself and an uninterruptible
    DNS lookup inside the child are OS-limited, so no hard real-time bound is claimed.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        send_timeout: float | None = None,
        worker_command: list[str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        _spawn: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        if type(timeout) not in (int, float) or not 0 < timeout <= worker.MAX_TIMEOUT:
            raise TransportError("request_invalid")
        self._timeout = float(timeout)
        # An explicit ``timeout`` alone keeps bounding every route (as before); only the
        # default client gives the send route its longer allowance.
        if send_timeout is None:
            send_timeout = SEND_TIMEOUT if timeout == DEFAULT_TIMEOUT else timeout
        if (type(send_timeout) not in (int, float)
                or not 0 < send_timeout <= worker.MAX_TIMEOUT):
            raise TransportError("request_invalid")
        self._send_timeout = float(send_timeout)
        self._command = list(
            worker_command
            or [sys.executable, "-I", str(Path(worker.__file__).resolve())]
        )
        self._mono = monotonic
        self._spawn = _spawn

    def request(
        self,
        origin: Origin,
        route: Route,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> Response:
        if type(origin) is not Origin or type(route) is not Route:
            raise TransportError("request_invalid")
        timeout = self._send_timeout if route.name == "send" else self._timeout
        document = _build_request(origin, route, headers or {}, body, timeout)
        _SLOT.acquire(route.name)
        proc = None
        failure: BaseException | None = None
        result: Response | None = None
        try:
            deadline = self._mono() + timeout
            proc = self._start(route)
            result = self._supervise(proc, document, route, deadline)
        except BaseException as exc:  # noqa: BLE001 - cleanup first, then re-raise
            failure = exc
        # The slot is settled in ``finally`` so that ANY error (including a
        # BaseException such as KeyboardInterrupt) raised by kill/wait can never leave
        # it busy with an untracked child.  An unconfirmed child faults the slot with
        # its exact handle; only a confirmed reap frees it.  A real worker does not
        # fork, so grandchildren / process groups are intentionally not handled.
        reaped = False
        cleanup_error: BaseException | None = None
        try:
            reaped = _reap(proc)
        except BaseException as exc:  # noqa: BLE001 - settle the slot, then re-raise
            cleanup_error = exc
        finally:
            if reaped:
                _SLOT.release()
            else:
                _SLOT.fault(proc)
        if cleanup_error is not None and not isinstance(cleanup_error, Exception):
            raise cleanup_error  # an interrupt during cleanup still stops the caller
        if not reaped and (failure is None or isinstance(failure, TransportError)):
            raise TransportError(
                "child_unreaped", route=route.name, maybe_sent=True
            ) from None
        if failure is not None:
            raise failure
        assert result is not None
        return result

    def _start(self, route: Route) -> Any:
        try:
            return self._spawn(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=dict(_CHILD_ENV),
                close_fds=True,
                cwd="/",
                start_new_session=True,
            )
        except OSError:
            raise TransportError("spawn_failed", route=route.name) from None

    def _supervise(
        self, proc: Any, document: bytes, route: Route, deadline: float
    ) -> Response:
        data = self._exchange_bytes(proc, document, route, deadline)
        remaining = deadline - self._mono()
        try:
            proc.wait(timeout=max(remaining, 0.0))
        except subprocess.TimeoutExpired:
            raise TransportError("timeout", route=route.name, maybe_sent=True) from None
        if self._mono() >= deadline:
            raise TransportError("timeout", route=route.name, maybe_sent=True)
        if proc.returncode != 0:
            raise TransportError("worker_failed", route=route.name, maybe_sent=True)
        return _parse_ipc(data, route)

    def _exchange_bytes(
        self, proc: Any, document: bytes, route: Route, deadline: float
    ) -> bytes:
        """Write the request and read until EOF, nonblocking, within the deadline."""
        pipes = _Pipes(proc, document, route)
        with selectors.DefaultSelector() as sel:
            sel.register(pipes.rfd, selectors.EVENT_READ)
            sel.register(pipes.wfd, selectors.EVENT_WRITE)
            while True:
                remaining = deadline - self._mono()
                if remaining <= 0:
                    raise pipes.fail("timeout")
                for key, _events in sel.select(min(remaining, 0.2)):
                    if key.fd == pipes.wfd and pipes.pending:
                        pipes.write_some(sel)
                    elif key.fd == pipes.rfd and pipes.read_some():
                        return b"".join(pipes.chunks)


class _Pipes:
    """Nonblocking request writer and capped result reader for one child."""

    def __init__(self, proc: Any, document: bytes, route: Route) -> None:
        self.proc, self.route = proc, route
        self.wfd, self.rfd = proc.stdin.fileno(), proc.stdout.fileno()
        os.set_blocking(self.wfd, False)
        os.set_blocking(self.rfd, False)
        self.pending = memoryview(document)
        self.chunks: list[bytes] = []
        self.total = 0
        self.cap = worker.ipc_response_cap(route.max_response)

    def fail(self, code: str) -> TransportError:
        return TransportError(code, route=self.route.name, maybe_sent=True)

    def write_some(self, sel: selectors.BaseSelector) -> None:
        try:
            self.pending = self.pending[os.write(self.wfd, self.pending[:65536]) :]
        except BlockingIOError:
            return
        except OSError:  # the child went away; its exit status decides
            self.pending = self.pending[len(self.pending) :]
        if not self.pending:
            sel.unregister(self.wfd)
            with contextlib.suppress(OSError, ValueError):
                self.proc.stdin.close()

    def read_some(self) -> bool:
        """True at EOF; raises on a flood or a read error."""
        try:
            chunk = os.read(self.rfd, 65536)
        except BlockingIOError:
            return False
        except OSError:
            raise self.fail("worker_ipc_invalid") from None
        if not chunk:
            return True
        self.total += len(chunk)
        if self.total > self.cap:
            raise self.fail("worker_flood")
        self.chunks.append(chunk)
        return False


# -- the core Transport ---------------------------------------------------------------
_REF_RE = re.compile(r"device-[0-9a-f]{32}")
_SEND_ARGS = frozenset({"text", "via", "preserveDraft", "attachmentIds"})


def _check_send_body(body: Any) -> None:
    """Exact ``conversation.send`` body shape; anything else is refused before the network."""

    def bad() -> TransportError:
        return TransportError("body_invalid", route="send")

    if type(body) is not bytes or len(body) > PAIR_CAP:
        raise bad()
    try:
        doc = strict_object(body)
    except TransportError:
        raise bad() from None
    allowed = {"id", "action", "args", "expectedRevision"}
    args = doc.get("args")
    revision = doc.get("expectedRevision", 0)
    if (
        not {"id", "action", "args"} <= set(doc) <= allowed
        or doc["action"] != "conversation.send"
        or not wire.is_canonical_uuid4(doc["id"])
        or type(revision) is not int
        or revision < 0
        or type(args) is not dict
        or set(args) != _SEND_ARGS
        or args["via"] != "chat"
        or args["preserveDraft"] is not True
        or args["attachmentIds"] != []
    ):
        raise bad()
    try:
        wire.check_text(args["text"])
    except wire.WireError:
        raise bad() from None


class HttpsTransport:
    """The core ``Transport``: validate every binding field, THEN send once.

    ``store`` supplies ``load_attachment(ref)`` (an object with ``origin``,
    ``client_id``, ``host_identity``, ``host_instance_id`` and ``token``).  Any mismatch
    raises ``TransportError(maybe_sent=False)``: nothing reached the network.  Statuses
    (including 401/403/409/504) are returned raw for the core to interpret; there is
    no automatic pairing and no retry.
    """

    def __init__(self, store: Any, client_factory: Callable[[], Any]) -> None:
        self._store = store
        self._factory = client_factory

    def send(
        self, binding: Any, method: str, path: str, body: bytes
    ) -> tuple[int, bytes]:
        values = self._binding_values(binding)
        try:
            route = route_send(values["session_id"])
        except TransportError:
            raise TransportError("binding_invalid", route="send") from None
        if method != "POST" or path != route.path:
            raise TransportError("binding_invalid", route="send")
        _check_send_body(body)
        try:
            attachment = self._store.load_attachment(values["credential_ref"])
        except Exception:  # noqa: BLE001 - unreadable store: fixed code, no detail
            attachment = None
        if attachment is None:
            raise TransportError("credential_missing", route="send")
        if (
            attachment.client_id != values["client_id"]
            or attachment.host_identity != values["host_id"]
            or attachment.host_instance_id != values["host_instance_id"]
        ):
            raise TransportError("binding_invalid", route="send")
        origin = Origin.parse(attachment.origin)
        headers = {
            "Authorization": "Bearer " + attachment.token,
            "X-Amplifier-Client": attachment.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        response = self._factory().request(origin, route, headers=headers, body=body)
        return response.status, response.body

    @staticmethod
    def _binding_values(binding: Any) -> dict[str, str]:
        names = (
            "host_id",
            "host_instance_id",
            "session_id",
            "client_id",
            "credential_ref",
        )
        try:
            values = {name: getattr(binding, name) for name in names}
        except AttributeError:
            raise TransportError("binding_invalid", route="send") from None
        if (
            any(type(v) is not str for v in values.values())
            or _REF_RE.fullmatch(values["credential_ref"]) is None
        ):
            raise TransportError("binding_invalid", route="send")
        return values
