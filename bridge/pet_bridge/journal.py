"""Private, bounded, single-process JSON journal (stdlib only).

One directory of small schema-1 records.  The directory and every record must be
private to the current user (0700 / 0600), must not be reachable through a
symlink, and must live outside any Git checkout.  A non-blocking ``flock`` keeps
a single process in charge.  Every write is an atomic temp-file replace with full
write, ``fsync`` and a directory ``fsync``.

Failure semantics callers must respect:

* an error *before* the rename leaves the previous value untouched;
* ``journal_persist_unknown`` means the rename happened but the directory sync
  failed: the new value is visible and may or may not survive a crash.  It is
  never presented as a rollback.

Startup stale-temp cleanup (narrow, bridge-owned namespace only): after the
exclusive ``flock`` is held and before the journal is live, entries whose name is
EXACTLY a generated temp (``.tmp-<positive pid>-<lowercase uuid4 hex>``) are
removed through the open directory fd.  Every matching entry is validated first
(``lstat`` only: regular file, effective-UID owner, mode 0600, one link, size
<= ``MAX_RECORD_BYTES``; symlinks are not followed and contents are never read) and
the number of matches is capped, all BEFORE anything is deleted.  An unsafe,
replaced or over-cap match is preserved and fails the open with a fixed code.
Near-miss names, records, ``.lock`` and anything unknown are never touched, and no
PID is probed: only the lock decides that no other instance is writing.  This
assumes the private-directory / cooperating-lock threat model; it does not
promise protection against a hostile process of the same user.

Errors carry a fixed code and nothing else, so payload text cannot leak.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, Self

SCHEMA = 1
MAX_RECORD_BYTES = 64 * 1024
MAX_RECORDS_LIMIT = 4096
MAX_STALE_TEMPS = 64

BINDING_NAME = "binding.json"
_NAME_RE = re.compile(
    r"^(?:binding|cmd-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.json$"
)
_LOCK_NAME = ".lock"
_TMP_PREFIX = ".tmp-"
_STALE_TMP_RE = re.compile(
    r"\.tmp-[1-9][0-9]{0,9}-"
    r"[0-9a-f]{8}[0-9a-f]{4}4[0-9a-f]{3}[89ab][0-9a-f]{3}[0-9a-f]{12}"
)

_FLAGS = os.O_CLOEXEC | os.O_NOFOLLOW

ERROR_CODES = frozenset(
    {
        "journal_config_invalid",
        "journal_root_invalid",
        "journal_locked",
        "journal_closed",
        "journal_name_invalid",
        "journal_record_invalid",
        "journal_record_too_large",
        "journal_record_corrupt",
        "journal_unsafe_record",
        "journal_full",
        "journal_io_error",
        "journal_persist_unknown",
        "journal_temp_unsafe",
        "journal_temp_overflow",
        "journal_temp_cleanup_failed",
    }
)


class JournalError(Exception):
    """A journal failure identified only by a stable ``code``."""

    def __init__(self, code: str) -> None:
        if code not in ERROR_CODES:  # pragma: no cover - programming error guard
            code = "journal_io_error"
        super().__init__(code)
        self.code = code

    def __str__(self) -> str:
        return self.code


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _strict_json(value: Any, depth: int) -> bool:
    """Only plain JSON types, string keys, finite numbers, bounded depth."""
    if depth > 16:
        return False
    if value is None or type(value) in (bool, int, str):
        return True
    if type(value) is float:
        return value == value and value not in (float("inf"), float("-inf"))
    if type(value) is list:
        return all(_strict_json(item, depth + 1) for item in value)
    if type(value) is dict:
        return all(
            type(k) is str and _strict_json(v, depth + 1) for k, v in value.items()
        )
    return False


def _check_ancestors(root: Path) -> None:
    if not root.is_absolute() or ".." in root.parts or "." in root.parts:
        raise JournalError("journal_root_invalid")
    for path in (root, *root.parents):
        if os.path.lexists(path / ".git"):
            raise JournalError("journal_root_invalid")
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            if path == root:
                continue
            raise JournalError("journal_root_invalid") from None
        except OSError:
            raise JournalError("journal_root_invalid") from None
        if stat.S_ISLNK(mode):
            raise JournalError("journal_root_invalid")
    if not stat.S_ISDIR(os.lstat(root.parent).st_mode):
        raise JournalError("journal_root_invalid")


class Journal:
    """Bounded private record store.  Not thread-safe; callers serialise access."""

    def __init__(self, root: Path, max_records: int = 256) -> None:
        if type(max_records) is not int or not 1 <= max_records <= MAX_RECORDS_LIMIT:
            raise JournalError("journal_config_invalid")
        self._max = max_records
        self._dirfd = -1
        self._lockfd = -1
        self._tmp: set[str] = set()
        self._closed = True
        try:
            root = Path(root)
            _check_ancestors(root)
            self._open_directory(root)
            self._open_lock()
            self._remove_stale_temps()
        except JournalError:
            self._release()
            raise
        except OSError:
            self._release()
            raise JournalError("journal_root_invalid") from None
        self._closed = False

    # -- setup -----------------------------------------------------------------
    def _open_directory(self, root: Path) -> None:
        created = False
        try:
            os.mkdir(root, 0o700)
            created = True
        except FileExistsError:
            pass
        self._dirfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | _FLAGS)
        st = os.fstat(self._dirfd)
        if created:
            os.fchmod(self._dirfd, 0o700)
            st = os.fstat(self._dirfd)
        if (
            not stat.S_ISDIR(st.st_mode)
            or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) != 0o700
        ):
            raise JournalError("journal_root_invalid")

    def _open_lock(self) -> None:
        created = True
        try:
            self._lockfd = os.open(
                _LOCK_NAME,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | _FLAGS,
                0o600,
                dir_fd=self._dirfd,
            )
        except FileExistsError:
            created = False
            try:
                self._lockfd = os.open(
                    _LOCK_NAME, os.O_RDWR | _FLAGS, dir_fd=self._dirfd
                )
            except OSError:
                raise JournalError("journal_root_invalid") from None
        except OSError:
            raise JournalError("journal_root_invalid") from None
        if created:
            os.fchmod(self._lockfd, 0o600)
        st = os.fstat(self._lockfd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.geteuid()
            or st.st_nlink != 1
            or stat.S_IMODE(st.st_mode) != 0o600
        ):
            raise JournalError("journal_root_invalid")
        try:
            fcntl.flock(self._lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise JournalError("journal_locked") from None

    def _remove_stale_temps(self) -> None:
        """Remove exact generated temps left by a crashed process (lock is held)."""
        try:
            listing = os.listdir(self._dirfd)
        except OSError:
            raise JournalError("journal_io_error") from None
        names = sorted(n for n in listing if _STALE_TMP_RE.fullmatch(n))
        if len(names) > MAX_STALE_TEMPS:
            raise JournalError("journal_temp_overflow")
        identities = {name: self._temp_identity(name) for name in names}
        for name in names:
            if identities[name] is None:  # vanished since the listing: nothing to do
                continue
            if self._temp_identity(name) != identities[name]:
                raise JournalError("journal_temp_unsafe")
            try:
                os.unlink(name, dir_fd=self._dirfd)
            except FileNotFoundError:
                continue
            except OSError:
                raise JournalError("journal_temp_cleanup_failed") from None
        if any(identities.values()):
            try:
                os.fsync(self._dirfd)
            except OSError:
                raise JournalError("journal_temp_cleanup_failed") from None

    def _temp_identity(self, name: str) -> tuple[int, int] | None:
        """``(dev, inode)`` of a SAFE stale temp, ``None`` if gone; else a fault.

        Uses ``lstat`` semantics only: nothing is opened, followed or read.
        """
        try:
            st = os.stat(name, dir_fd=self._dirfd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError:
            raise JournalError("journal_temp_cleanup_failed") from None
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) != 0o600
            or st.st_nlink != 1
            or st.st_size > MAX_RECORD_BYTES
        ):
            raise JournalError("journal_temp_unsafe")
        return (st.st_dev, st.st_ino)

    # -- helpers ---------------------------------------------------------------
    def _release(self) -> None:
        for fd_name in ("_lockfd", "_dirfd"):
            fd = getattr(self, fd_name)
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
                setattr(self, fd_name, -1)

    def _live(self) -> None:
        if self._closed:
            raise JournalError("journal_closed")

    @staticmethod
    def _valid_name(name: Any) -> str:
        if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
            raise JournalError("journal_name_invalid")
        return name

    def _record_names(self) -> list[str]:
        try:
            listing = os.listdir(self._dirfd)
        except OSError:
            raise JournalError("journal_io_error") from None
        return sorted(n for n in listing if _NAME_RE.fullmatch(n))

    # -- public API ------------------------------------------------------------
    @property
    def max_records(self) -> int:
        return self._max

    def names(self, prefix: str = "") -> list[str]:
        self._live()
        if not isinstance(prefix, str):
            raise JournalError("journal_name_invalid")
        return [n for n in self._record_names() if n.startswith(prefix)]

    def count(self) -> int:
        self._live()
        return len(self._record_names())

    def get(self, name: str) -> dict[str, Any] | None:
        self._live()
        self._valid_name(name)
        data = self._read_bytes(name)
        return None if data is None else self._decode(data)

    def _read_bytes(self, name: str) -> bytes | None:
        try:
            # O_NONBLOCK so a planted FIFO cannot hang the open; fstat rejects it.
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | _FLAGS, dir_fd=self._dirfd)
        except FileNotFoundError:
            return None
        except OSError:
            raise JournalError("journal_unsafe_record") from None
        try:
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.geteuid()
                or stat.S_IMODE(st.st_mode) != 0o600
                or st.st_nlink != 1
            ):
                raise JournalError("journal_unsafe_record")
            if st.st_size > MAX_RECORD_BYTES:
                raise JournalError("journal_record_too_large")
            chunks: list[bytes] = []
            remaining = MAX_RECORD_BYTES + 1
            while remaining > 0:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except OSError:
            raise JournalError("journal_io_error") from None
        finally:
            os.close(fd)
        data = b"".join(chunks)
        if len(data) > MAX_RECORD_BYTES:
            raise JournalError("journal_record_too_large")
        return data

    @staticmethod
    def _decode(data: bytes) -> dict[str, Any]:
        try:
            wrapper = json.loads(
                data.decode("utf-8"),
                parse_constant=_reject_constant,
                object_pairs_hook=_no_duplicates,
            )
        except (ValueError, RecursionError):
            raise JournalError("journal_record_corrupt") from None
        if (
            not isinstance(wrapper, dict)
            or set(wrapper) != {"schema", "record"}
            or type(wrapper["schema"]) is not int
            or wrapper["schema"] != SCHEMA
            or not isinstance(wrapper["record"], dict)
        ):
            raise JournalError("journal_record_corrupt")
        return wrapper["record"]

    def put(self, name: str, record: dict[str, Any]) -> None:
        self._live()
        self._valid_name(name)
        data = self._encode(record)
        if name not in self._record_names() and self.count() >= self._max:
            raise JournalError("journal_full")
        self._commit(name, data)

    def delete(self, name: str) -> None:
        self._live()
        self._valid_name(name)
        try:
            os.unlink(name, dir_fd=self._dirfd)
        except FileNotFoundError:
            return
        except OSError:
            raise JournalError("journal_io_error") from None
        try:
            os.fsync(self._dirfd)
        except OSError:
            raise JournalError("journal_persist_unknown") from None

    def close(self) -> None:
        if self._closed:
            return
        for tmp in sorted(self._tmp):
            self._unlink_own_temp(tmp)
        self._closed = True
        self._release()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- internals -------------------------------------------------------------
    @staticmethod
    def _encode(record: Any) -> bytes:
        if type(record) is not dict or not _strict_json(record, 0):
            raise JournalError("journal_record_invalid")
        try:
            text = json.dumps(
                {"schema": SCHEMA, "record": record},
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            data = text.encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise JournalError("journal_record_invalid") from None
        if len(data) > MAX_RECORD_BYTES:
            raise JournalError("journal_record_too_large")
        return data

    def _unlink_own_temp(self, tmp: str) -> None:
        if tmp not in self._tmp:
            return
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=self._dirfd)
        self._tmp.discard(tmp)

    def _commit(self, name: str, data: bytes) -> None:
        tmp = f"{_TMP_PREFIX}{os.getpid()}-{uuid.uuid4().hex}"
        fd = -1
        try:
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FLAGS,
                0o600,
                dir_fd=self._dirfd,
            )
            self._tmp.add(tmp)
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("no progress")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(tmp, name, src_dir_fd=self._dirfd, dst_dir_fd=self._dirfd)
        except OSError:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            self._unlink_own_temp(tmp)
            raise JournalError("journal_io_error") from None
        self._tmp.discard(tmp)
        try:
            os.fsync(self._dirfd)
        except OSError:
            raise JournalError("journal_persist_unknown") from None
