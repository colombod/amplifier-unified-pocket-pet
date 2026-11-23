"""Documented PRIMARY provider fixtures, not minimal pet-facing legacy frames.

Only synthetic test inputs are constructed here. The production classifier never
adds fields. Parser missing-field tests must call it directly without this helper.
"""
from __future__ import annotations

from typing import Any


def provider_frame(event: dict[str, Any]) -> dict[str, Any]:
    """Complete shorthand fixture envelopes for tests focused on gateway behavior."""
    result = dict(event)
    kind = result.get("type")
    kinds = {
        "session.started", "session.closed", "session.commentary.appended",
        "session.input_transcript.delta", "session.delegation.created", "error",
    }
    if kind not in kinds:
        return result
    result.setdefault("event_id", "srv_fixture_1")
    if kind in ("session.started", "session.closed"):
        result.setdefault("session", {
            "id": "live_fixture_1", "expires_at": 2000000000,
            "model": "gpt-live-1", "status": "active",
            "audio": {"format": {"type": "audio/pcm", "rate": 24000},
                      "output": {"voice": "marin"}},
            "delegation": {"type": "client", "acknowledgement_delay_ms": 0},
            "store": False,
        })
    if kind == "session.closed":
        result.setdefault("reason", "close_requested")
        result.setdefault("usage", {"seconds": 1.0})
    if kind == "session.commentary.appended":
        result.setdefault("start_ms", 0)
        result.setdefault("end_ms", 0)
    if kind == "error":
        result.setdefault("error", {"type": "server_error", "code": "fixture", "message": "fixture"})
    return result
