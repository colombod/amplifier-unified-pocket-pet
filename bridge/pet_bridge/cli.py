"""Command line for pairing, attaching, listing, binding and serving (stdlib only).

Commands: ``pair-start``, ``pair-poll``, ``pair-abandon``, ``attach``, ``list``,
``bind SESSION_ID`` and ``serve --config ABSOLUTE_PATH [--event-log ABSOLUTE_PATH]``.
``serve --env-file ABSOLUTE_PATH`` takes the same settings from a private ``.env`` file
instead (exactly one of the two).  The explicit private gateway config is required (no
default, never the default config root) and the gateway module is imported lazily inside
that branch, so every other command stays standard library only.  There is deliberately
still NO send, token or resume command: a token never enters through argv or the
process environment (the ``.env`` file is read as a private file, never exported),
and ``bind`` only records an explicit selection in the private journal (it sends
nothing and does not arm the bridge).  ``--help`` works offline without opening any
config.

``list --all-work [--scope roots|delegates|all]`` opts into a bounded read-only
observation catalog. It neither binds nor changes the default list/preflight path.

Output is limited to the approval address and code, instructions, safe labels, short
ids (the last 8 characters) and metadata.  Failures print ``error: <fixed code>`` only:
never argv, a response body, a token, a traceback or an exception message.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, Any, NoReturn

from . import auth, companion, transport, wire
from .core import Bridge, BridgeError
from .journal import Journal, JournalError

DEFAULT_NAME = "pet-bridge"
_HINTS = {
    "auth_start_unknown": (
        "A previous pairing start has an unknown result. Stop and inspect it before recovery. "
        "pair-abandon is local only and does not cancel the server request."
    ),
    "auth_attempt_exists": (
        "A pairing attempt already exists. Preserve unresolved state and inspect it. "
        "pair-abandon is local only and does not cancel the server request."
    ),
    "auth_no_credential": "No paired device. Run pair-start, approve in the browser, then pair-poll.",
    "not_attached": "Run attach first.",
    "credential_required": "Several credentials exist. Choose one with --credential.",
    "auth_instance_changed": (
        "The host instance changed. This bridge cannot continue with that instance. "
        "Nothing was changed."
    ),
    "list_incomplete": "The list was cut short. Pin the session in Unified so it is listed.",
    "session_not_listed": "That exact session id is not in the fresh list. Pin it in Unified if it is idle.",
    "companion_observation_unsupported": (
        "This host does not advertise the supported observation-v1 session resource. "
        "Nothing was changed."
    ),
}


class _Failure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Usage(Exception):
    pass


def _make_parser(out: IO[str]) -> argparse.ArgumentParser:
    class Parser(argparse.ArgumentParser):
        def error(self, message: str) -> NoReturn:  # never echo the arguments
            raise _Usage

        def _print_message(self, message: str, file: Any = None) -> None:
            if message:
                out.write(message)

    parser = Parser(
        prog="pet-bridge",
        allow_abbrev=False,
        description="Pair with a Unified host and bind ONE explicit existing session. "
        "Only 'serve' runs a service. In conversation mode, recognized requests are "
        "sent to the bound session without a device confirmation tap.",
    )
    commands = parser.add_subparsers(dest="command", parser_class=Parser)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(
            name, help=help_text, description=help_text, allow_abbrev=False
        )
        return sub

    start = add(
        "pair-start", "Begin pairing; the owner approves in a signed-in browser."
    )
    start.add_argument(
        "--origin", required=True, help="https://host[:port] of the Unified host"
    )
    start.add_argument(
        "--name", default=DEFAULT_NAME, help="device label shown to the owner"
    )
    add("pair-poll", "Check the pairing ONCE.")
    add("pair-abandon", "Forget the local pairing attempt (local only).")
    for name, text in (
        ("attach", "Attach as a restricted native client and pin the host."),
        ("list", "List sessions (short ids and safe labels only)."),
        ("bind", "Bind the bridge to ONE exact session id (sends nothing)."),
    ):
        sub = add(name, text)
        sub.add_argument(
            "--credential", default=None, help="paired credential reference"
        )
        if name == "bind":
            sub.add_argument(
                "session_id", metavar="SESSION_ID", help="the exact full session id"
            )
        if name == "list":
            sub.add_argument(
                "--all-work", action="store_true",
                help="read all authorized activity/pins using observation-v1 (no binding)",
            )
            sub.add_argument(
                "--scope", choices=("roots", "delegates", "all"), default=None,
                help="observation scope; requires --all-work (default: roots)",
            )
    serve = add("serve", "Run the voice gateway from an explicit private config.")
    source = serve.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--config",
        metavar="PATH",
        help="ABSOLUTE path of the private gateway JSON config (no default)",
    )
    source.add_argument(
        "--env-file",
        metavar="PATH",
        help="ABSOLUTE path of the private owner-only gateway .env file (no default)",
    )
    serve.add_argument(
        "--event-log", metavar="PATH",
        help="optional ABSOLUTE metadata JSONL path in an owner-only directory",
    )
    return parser


def _ensure_private_root(root: Path) -> None:
    """Create ``root`` (one level, parent must exist) or require it to be private."""
    try:
        os.mkdir(root, 0o700)
        os.chmod(root, 0o700)
    except FileExistsError:
        pass
    except OSError:
        raise auth.AuthError("auth_root_invalid") from None
    try:
        info = os.lstat(root)
    except OSError:
        raise auth.AuthError("auth_root_invalid") from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise auth.AuthError("auth_root_invalid")


_KNOWN_STATUSES = frozenset(
    {
        "idle",
        "working",
        "running",
        "stopping",
        "error",
        "stopped",
        "interrupted",
        "unknown",
    }
)


def _status_label(status: str) -> str:
    """A fixed printable label: only exact known statuses; anything else is 'other'.

    The raw status stays untouched in ``companion`` (strict idle preflight); only this
    terminal view is allow-listed so a hostile host string never reaches the screen.
    """
    return status if status in _KNOWN_STATUSES else "other"


def _short(session_id: str) -> str:
    return session_id[-8:]


def _credential(store: auth.CredentialStore, given: str | None) -> str:
    if given is not None:
        return given
    refs = store.credential_refs()
    if not refs:
        raise auth.AuthError("auth_no_credential")
    if len(refs) > 1:
        raise _Failure("credential_required")
    return refs[0]


class _Context:
    def __init__(
        self,
        out: IO[str],
        store: auth.CredentialStore,
        root: Path,
        factory: Callable[[], Any],
        clock: Callable[[], float],
        new_id: Callable[[], str],
    ) -> None:
        self.out, self.store, self.root = out, store, root
        self.factory, self.clock, self.new_id = factory, clock, new_id

    def say(self, *lines: str) -> None:
        for line in lines:
            self.out.write(line + "\n")

    # -- commands --------------------------------------------------------------
    def pair_start(self, origin: str, name: str) -> int:
        prompt = auth.pair_start(
            self.store, self.factory(), origin, name, now=self.clock
        )
        remaining = max(0, int(prompt.expires_at - self.clock()))
        self.say(
            "Open this address in the browser where you are signed in to Unified:",
            prompt.url,
            "Approve only if this code matches: " + prompt.code,
            f"The request expires in {remaining} seconds. The browser owner approves; "
            "this tool never approves.",
            "Then run: pet-bridge pair-poll",
        )
        return 0

    def pair_poll(self) -> int:
        result = auth.pair_poll(self.store, self.factory(), self.clock)
        wait = math.ceil(result.retry_after or 0)
        if result.status == "approved":
            self.say(
                f"Approved. Credential reference: {result.credential_ref}",
                "Next: pet-bridge attach",
            )
        elif result.status == "pending":
            self.say(f"Not approved yet. Try again in {wait} seconds.")
        elif result.status == "too_early":
            self.say(f"Too early. Try again in {wait} seconds.")
        else:
            reason = {
                "denied": "The request was denied.",
                "revoked": "The request was revoked.",
                "expired": "The request expired.",
                "invalid_proof": "The server rejected the local proof.",
            }[result.status]
            self.say(f"{reason} ({result.status}) Start again with pair-start.")
            return 1
        return 0

    def pair_abandon(self) -> int:
        existed = auth.pair_abandon(self.store)
        self.say(
            "Pairing attempt abandoned locally. The server may keep it until it expires."
            if existed
            else "No pairing attempt to abandon (local only)."
        )
        return 0

    def attach(self, credential: str | None) -> int:
        ref = _credential(self.store, credential)
        result = auth.attach(self.store, self.factory(), ref)
        self.say(
            f"Attached. Credential: {result.credential_ref}",
            f"Host: {result.host_identity[:8]}  Instance: {result.host_instance_id[:8]}",
        )
        return 0

    def _listing(
        self, credential: str | None
    ) -> tuple[auth.Attachment, companion.SessionList]:
        ref = _credential(self.store, credential)
        attachment = self.store.load_attachment(ref)
        if attachment is None:
            raise _Failure("not_attached")
        return attachment, companion.list_sessions(self.factory(), attachment)

    def list(
        self, credential: str | None, *, all_work: bool = False, scope: str = "roots"
    ) -> int:
        listing: companion.SessionCatalog | companion.SessionList
        if all_work:
            ref = _credential(self.store, credential)
            attachment = self.store.load_attachment(ref)
            if attachment is None:
                raise _Failure("not_attached")
            listing = companion.list_all_work(self.factory(), attachment, scope=scope)
        else:
            _, listing = self._listing(credential)
        state = "complete" if listing.complete else "INCOMPLETE (cap reached)"
        if isinstance(listing, companion.SessionCatalog):
            self.say(
                f"All-work sessions (scope={scope}; activity=all; pinned=all): "
                f"{state}; {len(listing.sessions)} listed; {listing.pages} pages"
            )
        else:
            self.say(
                f"Sessions: {state}; revision {listing.revision}; {len(listing.sessions)} listed"
            )
        for row in listing.sessions:
            flags = ",".join(
                n
                for n, on in (
                    ("execution", row.execution),
                    ("preparing", row.preparing),
                    ("loading", row.loading),
                )
                if on
            )
            role = f"{row.role:<8} " if all_work and row.role is not None else ""
            self.say(
                f"  {_short(row.id)}  {_status_label(row.status):<10} {flags or '-':<10} "
                f"{role}{row.title or '(untitled)'}"
            )
        return 0

    def bind(self, credential: str | None, session_id: str) -> int:
        attachment, listing = self._listing(credential)
        if not listing.complete:
            raise _Failure("list_incomplete")
        row = next((r for r in listing.sessions if r.id == session_id), None)
        if row is None:
            raise _Failure("session_not_listed")
        with Journal(self.root / "journal") as journal:
            bridge = Bridge(
                journal,
                transport.HttpsTransport(self.store, self.factory),
                clock=self.clock,
                new_id=self.new_id,
            )
            bridge.select_session(
                host_id=attachment.host_identity,
                host_instance_id=attachment.host_instance_id,
                session_id=session_id,
                client_id=attachment.client_id,
                credential_ref=attachment.credential_ref,
            )
        self.say(
            f"Bound to session ...{_short(row.id)} ({row.title or 'untitled'}). Status: {_status_label(row.status)}.",
            "Nothing was sent. Idle was not verified, and the bridge stays disarmed until resumed.",
        )
        return 0


def main(
    argv: list[str],
    *,
    env: Mapping[str, str],
    stdout: IO[str],
    stderr: IO[str],
    root: Path | None = None,
    clientfactory: Callable[[], Any] | None = None,
    clock: Callable[[], float] = time.time,
    new_id: Callable[[], str] | None = None,
    serve_deps: Any = None,
) -> int:
    """Run one command; return its exit status (0 ok, 1 failure, 2 usage).

    ``serve_deps`` is handed to the gateway untouched (a test seam; ``None`` = real).
    """
    parser = _make_parser(stdout)
    try:
        args = parser.parse_args(argv)
        if args.command is None:
            raise _Usage
        if args.command == "bind" and not wire.valid_session_id(args.session_id):
            raise _Usage
        if args.command == "list" and args.scope is not None and not args.all_work:
            raise _Usage
        if args.command == "serve" and not Path(
            args.env_file if args.config is None else args.config
        ).is_absolute():
            raise _Usage
        if (args.command == "serve" and args.event_log is not None
                and not Path(args.event_log).is_absolute()):
            raise _Usage
        ref = getattr(args, "credential", None)
        if ref is not None and re.fullmatch(r"device-[0-9a-f]{32}", ref) is None:
            raise _Usage
    except _Usage:
        stderr.write("error: usage (run with --help)\n")
        return 2
    except SystemExit as exc:  # --help
        return exc.code if isinstance(exc.code, int) else 0
    try:
        if args.command == "serve":
            return _serve(args, env, stdout, stderr, serve_deps)
        return _run(
            args,
            env,
            stdout,
            root,
            clientfactory or transport.HttpsClient,
            clock,
            new_id,
        )
    except Exception as exc:  # noqa: BLE001 - only a fixed code ever leaves this function
        code = getattr(exc, "code", None)
        known = isinstance(
            exc,
            _Failure
            | auth.AuthError
            | companion.CompanionError
            | BridgeError
            | JournalError,
        )
        code = code if known and isinstance(code, str) else "internal"
        stderr.write(f"error: {code}\n")
        hint = _HINTS.get(code)
        if hint:
            stderr.write(hint + "\n")
        return 1


def _serve(
    args: argparse.Namespace,
    env: Mapping[str, str],
    out: Any,
    err: Any,
    deps: Any,
) -> int:
    """The ONLY place the gateway is imported (lazily): never the default root or store."""
    from . import gateway_server

    event_log = Path(args.event_log) if args.event_log is not None else None
    if args.config is None:  # the private .env spelling of the same settings
        return gateway_server.serve_main(
            Path(args.env_file), env=env, stdout=out, stderr=err, deps=deps,
            event_log_path=event_log, env_file=True,
        )
    return gateway_server.serve_main(
        Path(args.config), env=env, stdout=out, stderr=err, deps=deps,
        event_log_path=event_log,
    )


def _run(
    args: argparse.Namespace,
    env: Mapping[str, str],
    out: IO[str],
    root: Path | None,
    factory: Callable[[], Any],
    clock: Callable[[], float],
    new_id: Callable[[], str] | None,
) -> int:
    config = Path(root) if root is not None else auth.default_root(env)
    _ensure_private_root(config)
    with auth.CredentialStore(config / "auth") as store:
        ctx = _Context(
            out, store, config, factory, clock, new_id or (lambda: str(uuid.uuid4()))
        )
        if args.command == "pair-start":
            return ctx.pair_start(args.origin, args.name)
        if args.command == "pair-poll":
            return ctx.pair_poll()
        if args.command == "pair-abandon":
            return ctx.pair_abandon()
        if args.command == "attach":
            return ctx.attach(args.credential)
        if args.command == "list":
            return ctx.list(
                args.credential, all_work=args.all_work, scope=args.scope or "roots"
            )
        return ctx.bind(args.credential, args.session_id)
