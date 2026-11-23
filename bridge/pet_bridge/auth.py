"""Private credential store, browser-approved pairing and native attach (stdlib only).

Secrets (the pairing proof and the companion token) live ONLY in small private files
(``0600`` in a ``0700`` directory outside Git).  They never appear in argv, the
environment, logs, ``repr``, error text or traceback chains.  Errors carry a fixed
``code`` (and, for HTTP, the numeric status) and nothing else.

``CredentialStore`` is a single-process store behind a non-blocking ``flock``.  Its
records are the ONLY allow-listed names ``attempt.json``, ``client-id.json``,
``credential-device-<32hex>.json`` and ``attachment-device-<32hex>.json``.  Every
write is an exclusive ``O_NOFOLLOW`` temp, full write, ``fsync``, ``replace`` and a
directory ``fsync``.  ``auth_io_error`` means the previous value is untouched;
``auth_persist_unknown`` means the rename happened and durability is unknown.

Pairing never approves anything: the owner approves in a signed-in browser.  A start
whose outcome is unknown blocks every later start until the operator abandons it
locally.  Source facts come from reading installed Unified 0.80.1; they are not a
live wire qualification.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from . import transport, wire
from .transport import Origin, TransportError

APP_DIRNAME = "amplifier-unified-pet-bridge"
SCHEMA = 1
MAX_RECORD_BYTES = 64 * 1024
MAX_RECORDS_LIMIT = 64
MAX_STALE_TEMPS = 64

ERROR_CODES = frozenset(
    {
        "auth_config_invalid",
        "auth_root_invalid",
        "auth_locked",
        "auth_closed",
        "auth_record_invalid",
        "auth_record_corrupt",
        "auth_record_too_large",
        "auth_unsafe_record",
        "auth_full",
        "auth_io_error",
        "auth_persist_unknown",
        "auth_origin_invalid",
        "auth_name_invalid",
        "auth_start_unknown",
        "auth_attempt_exists",
        "auth_no_attempt",
        "auth_no_credential",
        "auth_credential_conflict",
        "auth_client_bound",
        "auth_start_rejected",
        "auth_request_failed",
        "auth_response_invalid",
        "auth_http_status",
        "auth_unauthorized",
        "auth_host_mismatch",
        "auth_instance_changed",
    }
)


class AuthError(Exception):
    """A fixed ``code`` (and an HTTP ``status`` where one exists); never any payload."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        if code not in ERROR_CODES:  # pragma: no cover - programming error guard
            code = "auth_io_error"
        super().__init__(code)
        self.code = code
        self.status = status

    def __str__(self) -> str:
        return self.code


# -- strict formats -----------------------------------------------------------------
_RECORD_NAME_RE = re.compile(
    r"(?:attempt|client-id|(?:credential|attachment)-device-[0-9a-f]{32})\.json"
)
_STALE_TMP_RE = re.compile(r"\.tmp-[1-9][0-9]{0,9}-[0-9a-f]{32}")
_TOKEN_RE = re.compile(r"amc_([0-9a-f]{32})\.[A-Za-z0-9_-]{64}")
_REF_RE = re.compile(r"device-([0-9a-f]{32})")
_DEVICE_RE = re.compile(r"[0-9a-f]{32}")
_PROOF_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_CLIENT_RE = re.compile(r"[A-Za-z0-9_-]{1,100}")
_IDENTITY_RE = re.compile(r"[0-9a-f]{64}")
_CODE_RE = re.compile(
    r"[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}-[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}"
)
_STATES = frozenset({"PREPARED", "ATTEMPTED", "START_UNKNOWN", "STARTED"})
_FLAGS = os.O_CLOEXEC | os.O_NOFOLLOW


def _invalid() -> AuthError:
    return AuthError("auth_record_invalid")


def _is_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _canonical_origin(value: Any) -> str:
    try:
        origin = Origin.parse(value)
    except TransportError:
        raise _invalid() from None
    if origin.url != value:
        raise _invalid()
    return origin.url


def _check_name(name: Any) -> str:
    if type(name) is not str or not 1 <= len(name) <= 80 or not name.strip():
        raise _invalid()
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise _invalid()
    return name


def _check_ref(ref: Any) -> str:
    if type(ref) is not str or _REF_RE.fullmatch(ref) is None:
        raise _invalid()
    return ref


# -- records ------------------------------------------------------------------------
@dataclass(frozen=True)
class Credential:
    """A paired device credential.  ``token`` is hidden from ``repr``."""

    ref: str
    origin: str
    device_id: str
    token: str = field(repr=False)

    def validate(self) -> Self:
        _canonical_origin(self.origin)
        match = _TOKEN_RE.fullmatch(self.token) if type(self.token) is str else None
        if (
            _check_ref(self.ref) != "device-" + str(self.device_id)
            or type(self.device_id) is not str
            or _DEVICE_RE.fullmatch(self.device_id) is None
            or match is None
            or match.group(1) != self.device_id
        ):
            raise _invalid()
        return self


@dataclass(frozen=True)
class Attachment:
    """Origin/device/client/host binding.  The in-memory ``token`` is never persisted."""

    credential_ref: str
    origin: str
    client_id: str
    host_identity: str
    host_instance_id: str
    token: str = field(default="", repr=False, compare=False)

    def validate(self) -> Self:
        _check_ref(self.credential_ref)
        _canonical_origin(self.origin)
        if (
            type(self.client_id) is not str
            or _CLIENT_RE.fullmatch(self.client_id) is None
            or type(self.host_identity) is not str
            or _IDENTITY_RE.fullmatch(self.host_identity) is None
            or not wire.is_canonical_uuid4(self.host_instance_id)
        ):
            raise _invalid()
        return self


@dataclass(frozen=True)
class ClientBinding:
    client_id: str
    credential_ref: str


@dataclass(frozen=True)
class Attempt:
    """One pairing attempt.  ``verifier`` (the proof) is hidden from ``repr``."""

    state: str
    origin: str
    name: str
    verifier: str = field(repr=False)
    pairing_id: str | None = None
    code: str | None = None
    expires_at: float | None = None
    interval: int | None = None
    next_poll_at: float | None = None

    def validate(self) -> Self:
        _canonical_origin(self.origin)
        _check_name(self.name)
        if (
            self.state not in _STATES
            or type(self.verifier) is not str
            or _PROOF_RE.fullmatch(self.verifier) is None
        ):
            raise _invalid()
        started = (
            self.pairing_id,
            self.code,
            self.expires_at,
            self.interval,
            self.next_poll_at,
        )
        if self.state != "STARTED":
            if any(v is not None for v in started):
                raise _invalid()
            return self
        if (
            type(self.pairing_id) is not str
            or _PROOF_RE.fullmatch(self.pairing_id) is None
            or type(self.code) is not str
            or _CODE_RE.fullmatch(self.code) is None
            or not _is_number(self.expires_at)
            or type(self.interval) is not int
            or not 1 <= self.interval <= 60
            or not _is_number(self.next_poll_at)
        ):
            raise _invalid()
        return self


@dataclass(frozen=True)
class PairPrompt:
    """What the owner needs: the approval address and the code to compare.  No proof."""

    url: str
    code: str
    expires_at: float


@dataclass(frozen=True)
class PollResult:
    """``status`` is pending, approved, denied, revoked, expired, invalid_proof or too_early."""

    status: str
    retry_after: float | None = None
    credential_ref: str | None = None


# -- strict JSON ----------------------------------------------------------------------
def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _strict_json(value: Any, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if value is None or type(value) in (bool, int, str):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_strict_json(v, depth + 1) for v in value)
    if type(value) is dict:
        return all(
            type(k) is str and _strict_json(v, depth + 1) for k, v in value.items()
        )
    return False


def _decode_record(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicates,
        )
    except (ValueError, RecursionError):
        raise AuthError("auth_record_corrupt") from None
    if type(value) is not dict:
        raise AuthError("auth_record_corrupt")
    return value


def default_root(env: Mapping[str, str]) -> Path:
    """``$XDG_CONFIG_HOME/<app>`` when absolute, else ``$HOME/.config/<app>``.

    Reads only the mapping it is given, never the process environment.
    """
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / APP_DIRNAME
    home = env.get("HOME")
    if home and os.path.isabs(home):
        return Path(home) / ".config" / APP_DIRNAME
    raise AuthError("auth_root_invalid")


def _check_ancestors(root: Path) -> None:
    if not root.is_absolute() or "." in root.parts or ".." in root.parts:
        raise AuthError("auth_root_invalid")
    for path in (root, *root.parents):
        if os.path.lexists(path / ".git"):
            raise AuthError("auth_root_invalid")
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            if path == root:
                continue
            raise AuthError("auth_root_invalid") from None
        except OSError:
            raise AuthError("auth_root_invalid") from None
        if stat.S_ISLNK(mode):
            raise AuthError("auth_root_invalid")
    if not stat.S_ISDIR(os.lstat(root.parent).st_mode):
        raise AuthError("auth_root_invalid")


class CredentialStore:
    """Private, bounded, single-process record store.  Not thread-safe."""

    def __init__(self, root: Path, max_records: int = 16) -> None:
        if type(max_records) is not int or not 1 <= max_records <= MAX_RECORDS_LIMIT:
            raise AuthError("auth_config_invalid")
        self._max = max_records
        self._dirfd = -1
        self._lockfd = -1
        self._closed = True
        try:
            root = Path(root)
            _check_ancestors(root)
            self._open_directory(root)
            self._open_lock()
            self._remove_stale_temps()
        except AuthError:
            self._release()
            raise
        except OSError:
            self._release()
            raise AuthError("auth_root_invalid") from None
        self._closed = False

    # -- setup ---------------------------------------------------------------------
    def _open_directory(self, root: Path) -> None:
        created = False
        try:
            os.mkdir(root, 0o700)
            created = True
        except FileExistsError:
            pass
        self._dirfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | _FLAGS)
        if created:
            os.fchmod(self._dirfd, 0o700)
        st = os.fstat(self._dirfd)
        if (
            not stat.S_ISDIR(st.st_mode)
            or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) != 0o700
        ):
            raise AuthError("auth_root_invalid")

    def _open_lock(self) -> None:
        created = True
        try:
            self._lockfd = os.open(
                ".lock",
                os.O_RDWR | os.O_CREAT | os.O_EXCL | _FLAGS,
                0o600,
                dir_fd=self._dirfd,
            )
        except FileExistsError:
            created = False
            try:
                self._lockfd = os.open(".lock", os.O_RDWR | _FLAGS, dir_fd=self._dirfd)
            except OSError:
                raise AuthError("auth_root_invalid") from None
        if created:
            os.fchmod(self._lockfd, 0o600)
        st = os.fstat(self._lockfd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.geteuid()
            or st.st_nlink != 1
            or stat.S_IMODE(st.st_mode) != 0o600
        ):
            raise AuthError("auth_root_invalid")
        try:
            fcntl.flock(self._lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AuthError("auth_locked") from None

    def _remove_stale_temps(self) -> None:
        """Remove exact generated temps of a crashed writer (the lock is held)."""
        try:
            names = sorted(
                n for n in os.listdir(self._dirfd) if _STALE_TMP_RE.fullmatch(n)
            )
        except OSError:
            raise AuthError("auth_io_error") from None
        if len(names) > MAX_STALE_TEMPS:
            raise AuthError("auth_root_invalid")
        for name in names:
            try:
                st = os.stat(name, dir_fd=self._dirfd, follow_symlinks=False)
            except OSError:
                raise AuthError("auth_root_invalid") from None
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.geteuid()
                or stat.S_IMODE(st.st_mode) != 0o600
                or st.st_nlink != 1
                or st.st_size > MAX_RECORD_BYTES
            ):
                raise AuthError("auth_root_invalid")
        for name in names:
            try:
                os.unlink(name, dir_fd=self._dirfd)
            except FileNotFoundError:
                continue
            except OSError:
                raise AuthError("auth_root_invalid") from None

    def _release(self) -> None:
        for attr in ("_lockfd", "_dirfd"):
            fd = getattr(self, attr)
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
                setattr(self, attr, -1)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._release()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _live(self) -> None:
        if self._closed:
            raise AuthError("auth_closed")

    # -- raw records -----------------------------------------------------------------
    @staticmethod
    def _valid_name(name: Any) -> str:
        if type(name) is not str or _RECORD_NAME_RE.fullmatch(name) is None:
            raise _invalid()
        return name

    def names(self) -> list[str]:
        self._live()
        try:
            listing = os.listdir(self._dirfd)
        except OSError:
            raise AuthError("auth_io_error") from None
        return sorted(n for n in listing if _RECORD_NAME_RE.fullmatch(n))

    def _read(self, name: str) -> dict[str, Any] | None:
        self._live()
        self._valid_name(name)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | _FLAGS, dir_fd=self._dirfd)
        except FileNotFoundError:
            return None
        except OSError:
            raise AuthError("auth_unsafe_record") from None
        try:
            data = self._read_private(fd)
        except OSError:
            raise AuthError("auth_io_error") from None
        finally:
            os.close(fd)
        return _decode_record(data)

    @staticmethod
    def _read_private(fd: int) -> bytes:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) != 0o600
            or st.st_nlink != 1
        ):
            raise AuthError("auth_unsafe_record")
        if st.st_size > MAX_RECORD_BYTES:
            raise AuthError("auth_record_too_large")
        chunks: list[bytes] = []
        remaining = MAX_RECORD_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_RECORD_BYTES:
            raise AuthError("auth_record_too_large")
        return data

    def _write(self, name: str, record: Any) -> None:
        self._live()
        self._valid_name(name)
        if type(record) is not dict or not _strict_json(record):
            raise _invalid()
        try:
            data = json.dumps(
                record,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        except (TypeError, ValueError, RecursionError):
            raise _invalid() from None
        if len(data) > MAX_RECORD_BYTES:
            raise AuthError("auth_record_too_large")
        names = self.names()
        if name not in names and len(names) >= self._max:
            raise AuthError("auth_full")
        self._commit(name, data)

    def _commit(self, name: str, data: bytes) -> None:
        tmp = f".tmp-{os.getpid()}-{uuid.uuid4().hex}"
        fd = -1
        try:
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FLAGS,
                0o600,
                dir_fd=self._dirfd,
            )
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
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=self._dirfd)
            raise AuthError("auth_io_error") from None
        try:
            os.fsync(self._dirfd)
        except OSError:
            raise AuthError("auth_persist_unknown") from None

    def _delete(self, name: str) -> None:
        self._live()
        self._valid_name(name)
        try:
            os.unlink(name, dir_fd=self._dirfd)
        except FileNotFoundError:
            return
        except OSError:
            raise AuthError("auth_io_error") from None
        try:
            os.fsync(self._dirfd)
        except OSError:
            raise AuthError("auth_persist_unknown") from None

    # -- typed records ---------------------------------------------------------------
    def _typed(self, name: str, kind: str, keys: set[str]) -> dict[str, Any] | None:
        record = self._read(name)
        if record is None:
            return None
        if (
            set(record) != keys | {"schema", "kind"}
            or record["schema"] != SCHEMA
            or type(record["schema"]) is not int
            or record["kind"] != kind
        ):
            raise AuthError("auth_record_corrupt")
        return record

    def _decode(self, build: Callable[[], Any]) -> Any:
        try:
            return build()
        except AuthError as exc:
            if exc.code == "auth_record_invalid":
                raise AuthError("auth_record_corrupt") from None
            raise

    def credential_refs(self) -> list[str]:
        refs = []
        for name in self.names():
            match = re.fullmatch(r"credential-(device-[0-9a-f]{32})\.json", name)
            if match:
                refs.append(match.group(1))
        return refs

    def credential(self, ref: str) -> Credential | None:
        name = f"credential-{_check_ref(ref)}.json"
        record = self._typed(
            name, "credential", {"credential_ref", "origin", "device_id", "token"}
        )
        if record is None:
            return None

        def build() -> Credential:
            loaded = Credential(
                record["credential_ref"],
                record["origin"],
                record["device_id"],
                record["token"],
            ).validate()
            if loaded.ref != ref:
                raise _invalid()
            return loaded

        return self._decode(build)

    def save_credential(self, credential: Credential) -> None:
        credential.validate()
        self._write(
            f"credential-{credential.ref}.json",
            {
                "schema": SCHEMA,
                "kind": "credential",
                "credential_ref": credential.ref,
                "origin": credential.origin,
                "device_id": credential.device_id,
                "token": credential.token,
            },
        )

    def attachment_record(self, ref: str) -> Attachment | None:
        """The persisted attachment WITHOUT the token."""
        name = f"attachment-{_check_ref(ref)}.json"
        record = self._typed(
            name,
            "attachment",
            {
                "credential_ref",
                "origin",
                "client_id",
                "host_identity",
                "host_instance_id",
            },
        )
        if record is None:
            return None

        def build() -> Attachment:
            loaded = Attachment(
                record["credential_ref"],
                record["origin"],
                record["client_id"],
                record["host_identity"],
                record["host_instance_id"],
            ).validate()
            if loaded.credential_ref != ref:
                raise _invalid()
            return loaded

        return self._decode(build)

    def save_attachment(self, attachment: Attachment) -> None:
        attachment.validate()
        self._write(
            f"attachment-{attachment.credential_ref}.json",
            {
                "schema": SCHEMA,
                "kind": "attachment",
                "credential_ref": attachment.credential_ref,
                "origin": attachment.origin,
                "client_id": attachment.client_id,
                "host_identity": attachment.host_identity,
                "host_instance_id": attachment.host_instance_id,
            },
        )

    def load_attachment(self, ref: str) -> Attachment | None:
        """The attachment joined with its credential's token (in memory only)."""
        record = self.attachment_record(ref)
        credential = self.credential(ref)
        if record is None or credential is None:
            return None
        if record.origin != credential.origin:
            raise AuthError("auth_record_corrupt")
        return dataclasses.replace(record, token=credential.token)

    def client_binding(self) -> ClientBinding | None:
        record = self._typed(
            "client-id.json", "client", {"client_id", "credential_ref"}
        )
        if record is None:
            return None

        def build() -> ClientBinding:
            if (
                type(record["client_id"]) is not str
                or _CLIENT_RE.fullmatch(record["client_id"]) is None
            ):
                raise _invalid()
            return ClientBinding(
                record["client_id"], _check_ref(record["credential_ref"])
            )

        return self._decode(build)

    def save_client_id(self, client_id: str, credential_ref: str) -> None:
        if type(client_id) is not str or _CLIENT_RE.fullmatch(client_id) is None:
            raise _invalid()
        self._write(
            "client-id.json",
            {
                "schema": SCHEMA,
                "kind": "client",
                "client_id": client_id,
                "credential_ref": _check_ref(credential_ref),
            },
        )

    def attempt(self) -> Attempt | None:
        record = self._typed(
            "attempt.json",
            "attempt",
            {
                "state",
                "origin",
                "name",
                "verifier",
                "pairing_id",
                "code",
                "expires_at",
                "interval",
                "next_poll_at",
            },
        )
        if record is None:
            return None
        return self._decode(
            lambda: Attempt(
                record["state"],
                record["origin"],
                record["name"],
                record["verifier"],
                record["pairing_id"],
                record["code"],
                record["expires_at"],
                record["interval"],
                record["next_poll_at"],
            ).validate()
        )

    def save_attempt(self, attempt: Attempt) -> None:
        attempt.validate()
        self._write(
            "attempt.json",
            {
                "schema": SCHEMA,
                "kind": "attempt",
                "state": attempt.state,
                "origin": attempt.origin,
                "name": attempt.name,
                "verifier": attempt.verifier,
                "pairing_id": attempt.pairing_id,
                "code": attempt.code,
                "expires_at": attempt.expires_at,
                "interval": attempt.interval,
                "next_poll_at": attempt.next_poll_at,
            },
        )

    def delete_attempt(self) -> None:
        self._delete("attempt.json")


# -- pairing and attach ------------------------------------------------------------------
_JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}
_COMPANION_SCOPE = "companion-v1"


def _clock(now: Callable[[], float] | float | None) -> float:
    value = time.time() if now is None else now() if callable(now) else now
    if not _is_number(value):
        raise AuthError("auth_config_invalid")
    return float(value)


def _parse_origin(origin: Any) -> Origin:
    try:
        return Origin.parse(origin)
    except TransportError:
        raise AuthError("auth_origin_invalid") from None


def _object(response: transport.Response, code: str) -> dict[str, Any]:
    try:
        return transport.strict_object(response.body)
    except TransportError:
        raise AuthError(code) from None


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _mark_unknown(store: CredentialStore, attempt: Attempt) -> None:
    """Best effort: an ATTEMPTED record is recovered as unknown anyway."""
    with contextlib.suppress(AuthError):
        store.save_attempt(dataclasses.replace(attempt, state="START_UNKNOWN"))


def _valid_start(doc: dict[str, Any]) -> tuple[str, str, int, int] | None:
    if set(doc) != {"id", "code", "verificationPath", "expiresIn", "interval"}:
        return None
    ident, code, path = doc["id"], doc["code"], doc["verificationPath"]
    expires, interval = doc["expiresIn"], doc["interval"]
    if (
        type(ident) is not str
        or _PROOF_RE.fullmatch(ident) is None
        or type(code) is not str
        or _CODE_RE.fullmatch(code) is None
        or path != "/devices/pair?id=" + ident
        or type(expires) is not int
        or not 1 <= expires <= 600
        or type(interval) is not int
        or not 1 <= interval <= 60
    ):
        return None
    return ident, code, expires, interval


def pair_start(
    store: CredentialStore,
    client: Any,
    origin: Any,
    name: Any,
    *,
    now: Callable[[], float] | float | None = None,
    rand: Callable[[int], str] | None = None,
) -> PairPrompt:
    """Begin pairing: ONE POST, proof persisted first.  The owner approves in a browser.

    An outcome that cannot be known (``ATTEMPTED`` after a crash, a lost response, a
    malformed 200, a 5xx) leaves a ``START_UNKNOWN`` attempt that blocks every new
    start until ``pair_abandon``; the start is never retried.
    """
    target = _parse_origin(origin)
    if type(name) is not str:
        raise AuthError("auth_name_invalid")
    try:
        label = _check_name(name.strip())
    except AuthError:
        raise AuthError("auth_name_invalid") from None
    existing = store.attempt()
    if existing is not None and existing.state != "PREPARED":
        raise AuthError(
            "auth_attempt_exists"
            if existing.state == "STARTED"
            else "auth_start_unknown"
        )
    verifier = (rand or secrets.token_urlsafe)(32)
    if type(verifier) is not str or _PROOF_RE.fullmatch(verifier) is None:
        raise AuthError("auth_config_invalid")
    prepared = Attempt("PREPARED", target.url, label, verifier)
    store.save_attempt(prepared)
    attempted = dataclasses.replace(prepared, state="ATTEMPTED")
    store.save_attempt(attempted)
    body = json.dumps({"name": label, "challenge": _challenge(verifier)}).encode(
        "ascii"
    )
    try:
        response = client.request(
            target, transport.route_pair_start(), headers=dict(_JSON_HEADERS), body=body
        )
    except TransportError as exc:
        if not exc.maybe_sent:
            store.delete_attempt()
            raise AuthError("auth_request_failed") from None
        _mark_unknown(store, attempted)
        raise AuthError("auth_start_unknown") from None
    except Exception:  # noqa: BLE001 - outcome unknown: block a restart, show nothing
        _mark_unknown(store, attempted)
        raise AuthError("auth_start_unknown") from None
    except BaseException:
        _mark_unknown(store, attempted)
        raise
    return _finish_start(store, attempted, target, response, now)


def _finish_start(
    store: CredentialStore,
    attempted: Attempt,
    target: Origin,
    response: Any,
    now: Callable[[], float] | float | None,
) -> PairPrompt:
    status = getattr(response, "status", None)
    if type(status) is int and 400 <= status <= 499:
        store.delete_attempt()
        raise AuthError("auth_start_rejected", status=status)
    parsed = None
    if status == 200:
        with contextlib.suppress(TransportError):
            parsed = _valid_start(transport.strict_object(response.body))
    if parsed is None:
        _mark_unknown(store, attempted)
        raise AuthError("auth_start_unknown")
    ident, code, expires_in, interval = parsed
    moment = _clock(now)
    started = dataclasses.replace(
        attempted,
        state="STARTED",
        pairing_id=ident,
        code=code,
        expires_at=moment + expires_in,
        interval=interval,
        next_poll_at=moment + interval,
    )
    store.save_attempt(started)
    return PairPrompt(
        target.url + "/devices/pair?id=" + ident, code, moment + expires_in
    )


def pair_abandon(store: CredentialStore) -> bool:
    """Forget the local attempt (and its proof).  Local only: it tells the server nothing."""
    if "attempt.json" not in store.names():
        return False
    store.delete_attempt()
    return True


def pair_poll(
    store: CredentialStore, client: Any, now: Callable[[], float] | float | None = None
) -> PollResult:
    """ONE poll of a started pairing, honouring the server interval and expiry."""
    attempt = store.attempt()
    if attempt is None:
        raise AuthError("auth_no_attempt")
    if attempt.state != "STARTED":
        raise AuthError("auth_start_unknown")
    assert attempt.expires_at is not None and attempt.next_poll_at is not None
    assert attempt.interval is not None and attempt.pairing_id is not None
    moment = _clock(now)
    if moment >= attempt.expires_at:
        store.delete_attempt()
        return PollResult("expired")
    if moment < attempt.next_poll_at:
        return PollResult("too_early", retry_after=attempt.next_poll_at - moment)
    store.save_attempt(
        dataclasses.replace(attempt, next_poll_at=moment + attempt.interval)
    )
    body = json.dumps({"id": attempt.pairing_id, "verifier": attempt.verifier}).encode(
        "ascii"
    )
    try:
        response = client.request(
            Origin.parse(attempt.origin),
            transport.route_pair_token(),
            headers=dict(_JSON_HEADERS),
            body=body,
        )
    except Exception:  # noqa: BLE001 - fixed error; a poll is safe to repeat later
        raise AuthError("auth_request_failed") from None
    return _poll_outcome(store, attempt, response)


def _poll_outcome(
    store: CredentialStore, attempt: Attempt, response: Any
) -> PollResult:
    status = response.status
    if status == 200:
        doc = _object(response, "auth_response_invalid")
        state = doc.get("status")
        if state == "pending" and set(doc) == {"status"}:
            return PollResult("pending", retry_after=float(attempt.interval or 0))
        if state in ("denied", "revoked") and set(doc) == {"status"}:
            store.delete_attempt()
            return PollResult(state)
        if state == "approved":
            return _approved(store, attempt, doc)
        raise AuthError("auth_response_invalid")
    if status in (403, 410):
        store.delete_attempt()
        return PollResult("invalid_proof" if status == 403 else "expired")
    if status == 429:
        return PollResult("too_early", retry_after=float(attempt.interval or 0))
    raise AuthError("auth_http_status", status=status)


def _approved(
    store: CredentialStore, attempt: Attempt, doc: dict[str, Any]
) -> PollResult:
    if (
        set(doc) != {"status", "token", "deviceID", "scope"}
        or doc["scope"] != _COMPANION_SCOPE
    ):
        raise AuthError("auth_response_invalid")
    device_id = doc["deviceID"]
    try:
        credential = Credential(
            "device-" + str(device_id), attempt.origin, device_id, doc["token"]
        ).validate()
    except AuthError:
        raise AuthError("auth_response_invalid") from None
    existing = store.credential(credential.ref)
    if existing is None:
        store.save_credential(credential)  # durable BEFORE the proof is deleted
    elif existing != credential:
        raise AuthError("auth_credential_conflict")
    store.delete_attempt()
    return PollResult("approved", credential_ref=credential.ref)


def load_attachment(store: CredentialStore, ref: str) -> Attachment | None:
    """The stored attachment of ``ref`` joined with its token, for authenticated calls."""
    return store.load_attachment(ref)


def _valid_attach(doc: dict[str, Any], client_id: str) -> tuple[str, str] | None:
    identity, instance = doc.get("hostIdentity"), doc.get("hostInstanceId")
    version = doc.get("protocolVersion")
    if (
        doc.get("clientId") != client_id
        or type(version) is not int
        or version != 1
        or type(identity) is not str
        or _IDENTITY_RE.fullmatch(identity) is None
        or not wire.is_canonical_uuid4(instance)
    ):
        return None
    return identity, str(instance)


def _client_id_for(
    store: CredentialStore,
    ref: str,
    existing: Attachment | None,
    rand: Callable[[int], str] | None,
) -> str:
    """The stable client id of this credential, persisted BEFORE any POST."""
    binding = store.client_binding()
    if binding is not None and binding.credential_ref != ref:
        raise AuthError("auth_client_bound")
    client_id = (
        binding.client_id if binding else existing.client_id if existing else None
    )
    if client_id is None:
        client_id = (rand or secrets.token_urlsafe)(24)
        if type(client_id) is not str or _CLIENT_RE.fullmatch(client_id) is None:
            raise AuthError("auth_config_invalid")
    if binding is None:
        store.save_client_id(client_id, ref)
    return client_id


def native_attach_payload(client_id: str) -> dict[str, Any]:
    """Public request shape; contains neither a credential nor a destination."""
    return {
        "clientId": client_id,
        "kind": "native",
        "protocolVersion": 1,
        "includeState": False,
    }


def _attach_post(client: Any, credential: Credential, client_id: str) -> Any:
    payload = native_attach_payload(client_id)
    headers = {**_JSON_HEADERS, "Authorization": "Bearer " + credential.token}
    try:
        response = client.request(
            Origin.parse(credential.origin),
            transport.route_attach(),
            headers=headers,
            body=json.dumps(payload).encode("ascii"),
        )
    except Exception:  # noqa: BLE001 - fixed error, no cause chain
        raise AuthError("auth_request_failed") from None
    if response.status in (401, 403):
        raise AuthError("auth_unauthorized", status=response.status)
    if response.status != 200:
        raise AuthError("auth_http_status", status=response.status)
    return response


def _pin_response(
    response: Any, client_id: str, existing: Attachment | None
) -> tuple[str, str]:
    """Pin the host from the response; a different host or instance ALWAYS fails."""
    pinned = _valid_attach(_object(response, "auth_response_invalid"), client_id)
    if pinned is None:
        raise AuthError("auth_response_invalid")
    identity, instance = pinned
    if existing is not None:
        if existing.host_identity != identity:
            raise AuthError("auth_host_mismatch")
        if existing.host_instance_id != instance:
            raise AuthError("auth_instance_changed")
    return identity, instance


def attach(
    store: CredentialStore,
    client: Any,
    ref: str,
    *,
    rand: Callable[[int], str] | None = None,
) -> Attachment:
    """Attach this bridge to the host as a restricted native client (one POST).

    The client id is persisted before the POST.  The host identity is PINNED from the
    response (never sent).  A different host identity or host instance ALWAYS
    fails and changes nothing; there is no override.
    """
    credential = store.credential(ref)
    if credential is None:
        raise AuthError("auth_no_credential")
    existing = store.attachment_record(ref)
    client_id = _client_id_for(store, ref, existing, rand)
    response = _attach_post(client, credential, client_id)
    identity, instance = _pin_response(response, client_id, existing)
    attachment = Attachment(ref, credential.origin, client_id, identity, instance)
    store.save_attachment(attachment)
    return dataclasses.replace(attachment, token=credential.token)


def refresh_instance(
    store: CredentialStore,
    client: Any,
    ref: str,
    *,
    expected_instance_id: str,
) -> Attachment:
    """Explicitly reattach an existing client after a same-host restart.

    This is never called by normal attach/startup. The operator must reconcile
    the old journal separately before admitting another turn. Only the volatile
    process instance may change; origin, credential, client and host stay pinned.
    One POST, no retry, and no persistence before a fully validated response.
    """
    existing = store.load_attachment(ref)
    if existing is None:
        raise AuthError("auth_no_credential")
    if (
        not wire.is_canonical_uuid4(expected_instance_id)
        or existing.host_instance_id != expected_instance_id
    ):
        raise AuthError("auth_instance_changed")
    credential = store.credential(ref)
    if credential is None:
        raise AuthError("auth_no_credential")
    client_binding = store.client_binding()
    if (
        client_binding is None
        or client_binding.client_id != existing.client_id
        or client_binding.credential_ref != ref
    ):
        raise AuthError("auth_client_bound")
    response = _attach_post(client, credential, existing.client_id)
    pinned = _valid_attach(_object(response, "auth_response_invalid"), existing.client_id)
    if pinned is None:
        raise AuthError("auth_response_invalid")
    identity, instance = pinned
    if identity != existing.host_identity:
        raise AuthError("auth_host_mismatch")
    refreshed = dataclasses.replace(existing, host_instance_id=instance)
    if instance != existing.host_instance_id:
        store.save_attachment(refreshed)
    return refreshed
