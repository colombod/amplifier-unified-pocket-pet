"""Private one-shot HTTPS worker (stdlib only, self-contained).

Run by the supervising parent as ``[sys.executable, "-I", <this file>]``; it must
not import the ``pet_bridge`` package (it is not installed).  It reads ONE strict
JSON request from stdin, performs exactly one exchange with TLS verification and
writes ONE JSON line to stdout.  It never follows a redirect, never retries, never
logs and never touches the disk.  Failures are fixed codes: no message, header or
body text is ever copied into an error.

Request  ``{"v":1,"host","port","method","target","headers","body"(base64),
"max_request","max_response","timeout"}``.
Result   ``{"v":1,"ok":true,"status":N,"body"(base64)}`` or
``{"v":1,"ok":false,"code":CODE}``.

Offline tests drive ``exchange``/``run`` with a fake connector.  This file is not a
live qualification of any server.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import http.client
import json
import math
import re
import signal
import socket
import ssl
import sys
from collections.abc import Callable
from typing import Any, BinaryIO

IPC_VERSION = 1
MAX_REQUEST_BODY = 16 * 1024
MAX_RESPONSE_BODY = 1024 * 1024
MAX_TIMEOUT = 60.0
HARD_ALARM_SECONDS = 15
HARDEN_FAILED_EXIT = 70
MAX_TARGET = 2048
MAX_HEADER_VALUE = 4096

HEADER_ALLOW = frozenset(
    {"Authorization", "X-Amplifier-Client", "Content-Type", "Accept"}
)
RESULT_CODES = frozenset(
    {
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
    }
)

_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_TARGET_RE = re.compile(r"/[\x21-\x22\x24-\x7e]*")  # printable ASCII, no space, no '#'
_LENGTH_RE = re.compile(r"[0-9]{1,10}")
_REQUEST_KEYS = frozenset(
    {
        "v",
        "host",
        "port",
        "method",
        "target",
        "headers",
        "body",
        "max_request",
        "max_response",
        "timeout",
    }
)


def _b64_len(raw: int) -> int:
    return 4 * ((raw + 2) // 3)


def ipc_request_cap(max_request: int) -> int:
    """Largest request IPC document for a route body cap (base64 plus headers)."""
    return _b64_len(max_request) + 8192


def ipc_response_cap(max_response: int) -> int:
    """Largest result IPC document for a route response cap (base64 plus framing)."""
    return _b64_len(max_response) + 1024


class WorkerError(Exception):
    """Carries a fixed result code only."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    def __str__(self) -> str:
        return self.code


def _invalid() -> WorkerError:
    return WorkerError("request_invalid")


def _printable(value: Any, limit: int) -> bool:
    return (
        type(value) is str
        and len(value) <= limit
        and all(0x20 <= ord(ch) <= 0x7E for ch in value)
    )


def _valid_host(host: Any) -> bool:
    return (
        type(host) is str
        and host.isascii()
        and 0 < len(host) <= 253
        and all(_HOST_LABEL.fullmatch(label) for label in host.split("."))
    )


def validate_request(req: Any) -> dict[str, Any]:
    """Strictly validate a decoded request; return it with the body as bytes."""
    if type(req) is not dict or set(req) != _REQUEST_KEYS or type(req["v"]) is not int:
        raise _invalid()
    if req["v"] != IPC_VERSION or not _valid_host(req["host"]):
        raise _invalid()
    port, method, target = req["port"], req["method"], req["target"]
    if type(port) is not int or not 1 <= port <= 65535 or method not in ("GET", "POST"):
        raise _invalid()
    if (
        type(target) is not str
        or len(target) > MAX_TARGET
        or _TARGET_RE.fullmatch(target) is None
    ):
        raise _invalid()
    headers = req["headers"]
    if type(headers) is not dict or not all(
        k in HEADER_ALLOW and _printable(v, MAX_HEADER_VALUE)
        for k, v in headers.items()
    ):
        raise _invalid()
    max_request, max_response, timeout = (
        req["max_request"],
        req["max_response"],
        req["timeout"],
    )
    if (
        type(max_request) is not int
        or not 0 <= max_request <= MAX_REQUEST_BODY
        or type(max_response) is not int
        or not 1 <= max_response <= MAX_RESPONSE_BODY
        or type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= MAX_TIMEOUT
    ):
        raise _invalid()
    if type(req["body"]) is not str:
        raise _invalid()
    try:
        body = base64.b64decode(req["body"].encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise _invalid() from None
    if len(body) > max_request or (method == "GET" and body):
        raise _invalid()
    return {**req, "body": body}


def make_context() -> ssl.SSLContext:
    """Default verification, hostname checking and TLS >= 1.2; never weakened."""
    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def connect(host: str, port: int, timeout: float, context: ssl.SSLContext) -> Any:
    return http.client.HTTPSConnection(host, port, timeout=timeout, context=context)


def _json_content_type(value: Any) -> bool:
    if type(value) is not str:
        return False
    parts = [part.strip().lower() for part in value.split(";")]
    if parts[0] != "application/json":
        return False
    return all(part == "charset=utf-8" for part in parts[1:]) and len(parts) <= 2


def _failure(code: str) -> dict[str, Any]:
    return {"ok": False, "code": code}


def _classify(exc: BaseException) -> str:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls_verify_failed"
    if isinstance(exc, ssl.SSLError):
        return "tls_error"
    if isinstance(exc, TimeoutError):  # socket.timeout is an alias of TimeoutError
        return "timeout"
    if isinstance(exc, socket.gaierror):
        return "dns_error"
    if isinstance(exc, OSError):
        return "connect_error"
    if isinstance(exc, http.client.HTTPException):
        return "http_protocol_error"
    return "worker_error"


def _read_response(response: Any, cap: int) -> dict[str, Any]:
    status = response.status
    if type(status) is not int or not 100 <= status <= 599:
        return _failure("response_invalid")
    if 300 <= status < 400:
        return _failure("redirect_refused")
    declared = response.getheader("Content-Length")
    if declared is not None:
        if _LENGTH_RE.fullmatch(declared) is None:
            return _failure("response_invalid")
        if int(declared) > cap:
            return _failure("response_too_large")
    data = response.read(cap + 1)
    if len(data) > cap:
        return _failure("response_too_large")
    if 200 <= status < 300 and not _json_content_type(
        response.getheader("Content-Type")
    ):
        return _failure("content_type_invalid")
    return {"ok": True, "status": status, "body": data}


def exchange(
    req: dict[str, Any], connector: Callable[..., Any] = connect
) -> dict[str, Any]:
    """Perform ONE exchange for a validated request.  Never raises."""
    conn = None
    try:
        conn = connector(req["host"], req["port"], req["timeout"], make_context())
        headers = {**req["headers"], "Connection": "close"}
        conn.request(
            req["method"], req["target"], body=req["body"] or None, headers=headers
        )
        return _read_response(conn.getresponse(), req["max_response"])
    except Exception as exc:  # noqa: BLE001 - mapped to a fixed code, text discarded
        return _failure(_classify(exc))
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


def _encode(result: dict[str, Any]) -> bytes:
    doc: dict[str, Any] = {"v": IPC_VERSION, "ok": result["ok"]}
    if result["ok"]:
        doc["status"] = result["status"]
        doc["body"] = base64.b64encode(result["body"]).decode("ascii")
    else:
        doc["code"] = (
            result["code"] if result["code"] in RESULT_CODES else "worker_error"
        )
    return json.dumps(doc, separators=(",", ":")).encode("ascii") + b"\n"


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _read_request(stdin: BinaryIO) -> dict[str, Any]:
    limit = ipc_request_cap(MAX_REQUEST_BODY)
    data = stdin.read(limit + 1)
    if type(data) is not bytes or not data or len(data) > limit:
        raise _invalid()
    try:
        document = json.loads(
            data.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicates,
        )
    except (ValueError, RecursionError):
        raise _invalid() from None
    return validate_request(document)


def run(
    stdin: BinaryIO, stdout: BinaryIO, connector: Callable[..., Any] = connect
) -> int:
    """Read one request, run one exchange, write one result line; exit status 0."""
    try:
        result = exchange(_read_request(stdin), connector)
    except WorkerError as exc:
        result = _failure(exc.code)
    stdout.write(_encode(result))
    stdout.flush()
    return 0


def harden(
    *,
    setrlimit: Callable[..., Any] | None = None,
    alarm: Callable[[int], Any] | None = None,
    set_signal: Callable[..., Any] | None = None,
) -> None:
    """First thing in the child: a finite self-kill alarm, then no core dumps.

    The alarm is armed FIRST so a failure below can never leave an unbounded child.
    A failure to disable core dumps is NOT swallowed (OSError, ValueError or
    ImportError propagates): a dump could contain the request's secrets.
    """
    (set_signal or signal.signal)(signal.SIGALRM, signal.SIG_DFL)
    (alarm or signal.alarm)(HARD_ALARM_SECONDS)
    import resource

    (setrlimit or resource.setrlimit)(resource.RLIMIT_CORE, (0, 0))


def main() -> int:
    try:
        harden()
    except (OSError, ValueError, ImportError):
        return HARDEN_FAILED_EXIT  # before any stdin read or stdout write
    return run(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
