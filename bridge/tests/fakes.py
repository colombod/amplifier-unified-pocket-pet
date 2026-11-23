"""Offline test doubles shared by the bridge tests (stdlib only)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
import uuid
from collections.abc import Callable
from pathlib import Path
from unittest import mock

SESSION = "sess-abc_123"
OTHER_SESSION = "sess-other-9"

# Opaque credential identifiers: exactly "device-" + 32 lowercase hex characters.
CRED_1 = "device-" + "1" * 32
CRED_2 = "device-" + "2" * 32
CRED_9 = "device-" + "9" * 32


def raised(call, *args, **kwargs) -> BaseException | None:
    """Run ``call`` and return what it raised (any exception), else ``None``.

    Lets a test assert on the exception TYPE so a wrong exception is an assertion
    failure with a message, not an unexplained error.
    """
    try:
        call(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 - the whole point of the helper
        return exc
    return None


class OfflineTestCase(unittest.TestCase):
    """Every socket entry point raises, so no test can reach a network."""

    def setUp(self) -> None:
        super().setUp()

        def refuse(*_args, **_kwargs):
            raise AssertionError("network access attempted in an offline test")

        for target in (
            "socket.socket.connect",
            "socket.socket.connect_ex",
            "socket.create_connection",
            "socket.getaddrinfo",
        ):
            patcher = mock.patch(target, side_effect=refuse)
            patcher.start()
            self.addCleanup(patcher.stop)

    def journal_root(self) -> Path:
        """A fresh private journal root in a temp dir outside any Git checkout."""
        base = Path(os.path.realpath(tempfile.mkdtemp(prefix="pet-bridge-test-")))
        self.addCleanup(shutil.rmtree, base, True)
        return base / "journal"


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class IdFactory:
    """Deterministic canonical UUID4 values."""

    def __init__(self, start: int = 1) -> None:
        self.n = start

    def __call__(self) -> str:
        value = str(uuid.UUID(int=self.n, version=4))
        self.n += 1
        return value


class FakeTransport:
    """Scripted transport. Never touches a socket; records every call."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple] = []
        self.on_send: Callable[[], None] | None = None

    def send(self, binding, method, path, body):
        self.calls.append((binding, method, path, body))
        if self.on_send is not None:
            self.on_send()
        if not self.responses:
            raise AssertionError("unscripted transport call")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def accepted_body(
    session_id: str = SESSION, command_id: str | None = None, **extra
) -> bytes:
    body = {"accepted": True, "delivery": "accepted", "sessionId": session_id}
    if command_id is not None:
        body["inputId"] = command_id
    body.update(extra)
    return json.dumps(body).encode()


def user_row(
    command_id: str,
    generation: str | None = "gen-1",
    phase: str = "done",
    basis: str = "generation_finished",
    **extra,
) -> dict:
    status = {"version": 1, "inputId": command_id, "phase": phase, "basis": basis}
    if generation is not None:
        status["generationId"] = generation
    if phase in ("done", "failed"):
        status["endedAt"] = 1.5
    row = {
        "id": f"m-{command_id[-4:]}",
        "role": "user",
        "text": "hello",
        "via": "chat",
        "inputId": command_id,
        "inputStatus": status,
    }
    row.update(extra)
    return row


def assistant_row(generation: str | None, text: str, **extra) -> dict:
    row = {
        "id": "a-" + uuid.uuid4().hex[:6],
        "role": "assistant",
        "text": text,
        "via": "chat",
    }
    if generation is not None:
        row["generationId"] = generation
    row.update(extra)
    return row


def page(items: list, *, offset: int = 0, total: int | None = None, **extra) -> dict:
    result = {
        "items": items,
        "offset": offset,
        "total": len(items) if total is None else total,
        "before": None,
        "userOffset": 0,
        "sourceOffset": offset,
    }
    result.update(extra)
    return result
