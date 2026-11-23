"""Offline doubles for the auth, companion and CLI tests (stdlib only)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pet_bridge.transport import Response

from .fakes import OfflineTestCase

ORIGIN_URL = "https://unified.example.ts.net:8941"
PAIRING_ID = "I" * 43
VERIFIER = "V" * 43
CLIENT_ID = "C" * 32
DEVICE_ID = "ab" * 16
CRED_REF = "device-" + DEVICE_ID
TOKEN = "amc_" + DEVICE_ID + "." + "T" * 64
HOST_IDENTITY = "5" * 64
INSTANCE = "12345678-1234-4234-8234-123456789abc"
INSTANCE_2 = "12345678-1234-4234-8234-123456789abd"


def rand(n: int) -> str:
    return {32: VERIFIER, 24: CLIENT_ID}[n]


def raised_any(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Whatever ``call`` raised (typed loosely for attribute access), else None."""
    try:
        call(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001
        return exc
    return None


def challenge_of(verifier: str) -> str:
    import base64

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def json_response(doc: Any, status: int = 200) -> Response:
    return Response(status, json.dumps(doc).encode())


def pair_ok(**changes: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": PAIRING_ID,
        "code": "ABCD-2345",
        "verificationPath": "/devices/pair?id=" + PAIRING_ID,
        "expiresIn": 600,
        "interval": 5,
    }
    doc.update(changes)
    return {k: v for k, v in doc.items() if v is not ...}


def approved(**changes: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "status": "approved",
        "token": TOKEN,
        "deviceID": DEVICE_ID,
        "scope": "companion-v1",
    }
    doc.update(changes)
    return {k: v for k, v in doc.items() if v is not ...}


def attach_ok(**changes: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "clientId": CLIENT_ID,
        "protocolVersion": 1,
        "hostInstanceId": INSTANCE,
        "hostIdentity": HOST_IDENTITY,
        "reconnect": "snapshot",
        "capabilities": {},
        "transports": ["http"],
    }
    doc.update(changes)
    return {k: v for k, v in doc.items() if v is not ...}


class FakeClient:
    """Scripted ``request`` client.  Records every call; never touches a socket."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.before: Callable[[dict[str, Any]], None] | None = None

    def request(self, origin, route, *, headers=None, body=b""):
        call = {
            "origin": origin,
            "route": route,
            "headers": dict(headers or {}),
            "body": body,
        }
        self.calls.append(call)
        if self.before is not None:
            self.before(call)
        if not self.script:
            raise AssertionError("unscripted request")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(call)
        return item

    def bodies(self) -> list[Any]:
        return [json.loads(c["body"]) if c["body"] else None for c in self.calls]


class AuthCase(OfflineTestCase):
    """A private temporary store root outside any Git checkout."""

    def setUp(self) -> None:
        super().setUp()
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="pet-auth-test-")))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = self.base / "auth"

    def open_store(self, **kwargs: Any):
        from pet_bridge.auth import CredentialStore

        store = CredentialStore(self.root, **kwargs)
        self.addCleanup(store.close)
        return store
