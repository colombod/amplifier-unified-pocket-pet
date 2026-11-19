"""Optional append-only metadata log. No frame, request or exception serialization."""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import stat
from pathlib import Path
from typing import Any, TextIO


class EventLogError(Exception):
    """A fixed startup failure; never carries a path or OS error text."""


# Deliberately explicit: adding a SessionOutcome field must not silently log it.
_OUTCOME_FIELDS = (
    "final_state", "turn_started", "send_outcome", "reply_done", "error_code",
    "close_confirmed", "audio_in_bytes", "audio_out_bytes", "dropped_audio_bytes",
    "confirm_job", "close_origin", "conversation", "requests_submitted",
    "requests_sent", "requests_accepted", "requests_uncertain", "requests_refused",
    "replies_rendered", "conversation_end",
    # Counts and one duration rounded to 100 ms: no words, audio or identifiers.
    "delegations", "delegations_repeated", "transcript_segments",
    "transcript_segments_unused", "refused_pending", "refused_no_words",
    "refused_input_late", "refused_preflight", "refused_local", "refused_confirm",
    "refused_ended", "refused_ended_input_late", "input_wait_ms",
)
_EVENT_FIELDS = {
    "gateway_start": (),
    "gateway_stop": ("code",),
    "device_connect": ("outcome", "code", "status"),
    "provider_dial": ("outcome", "code"),
    "host_restart_follow": ("old_instance", "new_instance"),
    "session_end": _OUTCOME_FIELDS,
}


class EventLog:
    """Owner-only JSONL, held by fd; callers serialize access.

    Startup is fail-closed. Later I/O failure disables this sink with one fixed
    stderr warning, without disturbing custody of a possibly in-flight send.
    Runtime writes run on CoreRunner, not the asyncio thread. Startup/shutdown
    writes and close run only outside that worker's lifetime.
    """

    def __init__(self, path: Path, stderr: TextIO) -> None:
        self._fd: int | None = None
        self._stderr = stderr
        directory: int | None = None
        fd: int | None = None
        try:
            path = Path(path)
            if not path.is_absolute():
                raise ValueError
            directory = os.open(
                path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            parent = os.fstat(directory)
            if parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
                raise ValueError
            flags = os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                fd = os.open(path.name, flags | os.O_CREAT | os.O_EXCL, 0o600,
                             dir_fd=directory)
                os.fchmod(fd, 0o600)
            except FileExistsError:
                fd = os.open(path.name, flags, dir_fd=directory)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ValueError
            self._fd, fd = fd, None
        except (OSError, ValueError, TypeError):
            raise EventLogError("event_log_invalid") from None
        finally:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            if directory is not None:
                with contextlib.suppress(OSError):
                    os.close(directory)

    def write(self, event: str, *, required: bool = False, **facts: Any) -> None:
        if self._fd is None:
            return
        if event not in _EVENT_FIELDS or set(facts) != set(_EVENT_FIELDS[event]):
            raise ValueError("invalid event metadata")
        record = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "event": event, **facts,
        }
        data = (json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n").encode()
        try:
            while data:
                count = os.write(self._fd, data)
                if count <= 0:
                    raise OSError
                data = data[count:]
        except OSError:
            self.close()
            if required:
                raise EventLogError("event_log_invalid") from None
            with contextlib.suppress(OSError, ValueError):
                self._stderr.write("error: event_log_failed\n")

    def session_end(self, outcome: Any) -> None:
        # SessionOutcome is local metadata, never a provider/request-shaped object.
        from .gateway_core import SessionOutcome

        if isinstance(outcome, SessionOutcome):
            self.write("session_end", **{
                field: getattr(outcome, field) for field in _OUTCOME_FIELDS
            })

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)