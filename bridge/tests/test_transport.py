"""Offline tests for the HTTPS transport: origin, routes, strict JSON, bound send.

No real network: the socket guard from ``OfflineTestCase`` is active and the client
is always a recording fake.  These tests are not a Unified wire qualification.
"""

from __future__ import annotations

import json
import unittest

from pet_bridge import transport
from pet_bridge.transport import (
    Origin,
    Response,
    TransportError,
    route_attach,
    route_companion,
    route_detail,
    route_pair_start,
    route_pair_token,
    route_send,
    route_sessions_observation,
    strict_object,
)

from .fakes import OfflineTestCase

SID = "sess-abc_123"


class OriginTest(OfflineTestCase):
    def test_accepts_https_host_with_optional_port_and_trailing_slash(self):
        a = Origin.parse("https://Unified.Example.ts.net")
        self.assertEqual(
            (a.host, a.port, a.url),
            ("unified.example.ts.net", 443, "https://unified.example.ts.net"),
        )
        b = Origin.parse("https://host.example:8941/")
        self.assertEqual(
            (b.host, b.port, b.url), ("host.example", 8941, "https://host.example:8941")
        )
        # an explicit :443 is the same origin as no port
        self.assertEqual(
            Origin.parse("https://host.example:443"),
            Origin.parse("https://host.example"),
        )

    def test_rejects_everything_else(self):
        bad = [
            "http://host.example",
            "HTTPS://host.example",
            "https://",
            "https:///",
            "host.example",
            "https://user@host.example",
            "https://user:pw@host.example",
            "https://host.example/path",
            "https://host.example//",
            "https://host.example/?a=1",
            "https://host.example?a=1",
            "https://host.example#frag",
            "https://host.example:0",
            "https://host.example:65536",
            "https://host.example:08941",
            "https://host.example:+443",
            "https://host.example:",
            "https://host.example:44 3",
            "https://host.example:4_43",
            "https://[::1]",
            "https://host.example\n",
            "https://host.example\x00",
            "https://host .example",
            "https://host.example\\evil",
            "https://host..example",
            "https://.host.example",
            "https://host.example.",
            "https://-host.example",
            "https://hos_t.example",
            "https://h\u00f6st.example",
            "https://" + "a" * 64 + ".example",
            "https://" + ".".join(["abcdefghi"] * 30),
            "https://host.example%2f",
            "https://host.example:443:443",
            "",
            None,
            7,
            b"https://host.example",
        ]
        for value in bad:
            with self.subTest(value=value):
                exc = raised(Origin.parse, value)
                self.assertIsInstance(exc, TransportError)
                self.assertEqual(exc.code, "origin_invalid")
                self.assertFalse(exc.maybe_sent)

    def test_error_never_echoes_the_input(self):
        marker = "https://user:SECRETMARKER@host.example"
        exc = raised(Origin.parse, marker)
        self.assertNotIn("SECRETMARKER", str(exc) + repr(exc) + repr(exc.args))


class RouteTest(OfflineTestCase):
    def test_fixed_routes_and_caps(self):
        self.assertEqual(
            (
                route_pair_start().method,
                route_pair_start().path,
                route_pair_start().query,
            ),
            ("POST", "/api/devices/pair", ()),
        )
        self.assertEqual(route_pair_token().path, "/api/devices/pair/token")
        self.assertEqual(route_attach().path, "/api/clients/attach")
        send = route_send(SID)
        self.assertEqual(
            (send.method, send.path), ("POST", f"/api/sessions/{SID}/commands")
        )
        for route in (route_pair_start(), route_pair_token(), route_attach(), send):
            self.assertEqual(
                (route.max_request, route.max_response), (16 * 1024, 16 * 1024)
            )
        self.assertEqual(route_companion().max_response, 256 * 1024)
        self.assertEqual(route_detail(SID).max_response, 1024 * 1024)
        for route in (route_companion(), route_detail(SID)):
            self.assertEqual((route.method, route.max_request), ("GET", 0))

    def test_companion_query_is_restricted_and_encoded(self):
        self.assertEqual(route_companion().target, "/api/companion")
        route = route_companion(
            sessions_cursor="ab.c-d_e", items_cursor="x+y/z", client_id="cid_1-A"
        )
        self.assertEqual(
            route.target,
            "/api/companion?sessionsCursor=ab.c-d_e&itemsCursor=x%2By%2Fz&clientId=cid_1-A",
        )
        for kwargs in (
            {"sessions_cursor": ""},
            {"sessions_cursor": "a\nb"},
            {"sessions_cursor": "a" * 1025},
            {"sessions_cursor": 5},
            {"items_cursor": "\x7f"},
            {"client_id": "bad id"},
            {"client_id": "a" * 101},
            {"client_id": ""},
        ):
            with self.subTest(kwargs=kwargs):
                exc = raised(route_companion, **kwargs)
                self.assertIsInstance(exc, TransportError)
                self.assertEqual(exc.code, "route_invalid")
        # percent and ampersand can never smuggle a second parameter
        route = route_companion(sessions_cursor="a&clientId=evil%2F")
        self.assertEqual(route.target.count("="), 1)
        self.assertNotIn("&clientId", route.target)

    def test_detail_route_is_messages_only_with_string_before(self):
        self.assertEqual(
            route_detail(SID).target,
            f"/api/conversation/detail?sessionId={SID}&part=messages",
        )
        self.assertEqual(
            route_detail(SID, before="m-1:2").target,
            f"/api/conversation/detail?sessionId={SID}&part=messages&before=m-1%3A2",
        )
        for before in (5, "", "a b", "../x", "a/b", "a" * 201, "\n"):
            with self.subTest(before=before):
                self.assertEqual(
                    raised(route_detail, SID, before=before).code, "route_invalid"
                )

    def test_session_ids_must_be_exact_safe_ids(self):
        for bad in (
            "",
            "a/b",
            "..",
            ".",
            "a%2Fb",
            "a%2fb",
            "../x",
            "a b",
            "a?b",
            "a#b",
            "-x",
            ".x",
            "a" * 129,
            5,
            None,
            "a\n",
            "é",
        ):
            for ctor in (route_send, route_detail):
                with self.subTest(ctor=ctor.__name__, value=bad):
                    exc = raised(ctor, bad)
                    self.assertIsInstance(exc, TransportError)
                    self.assertEqual(exc.code, "route_invalid")

    def test_there_are_no_other_route_constructors(self):
        names = {n for n in dir(transport) if n.startswith("route_")}
        self.assertEqual(
            names,
            {
                "route_pair_start",
                "route_pair_token",
                "route_attach",
                "route_send",
                "route_companion",
                "route_detail",
                "route_sessions_observation",
            },
        )


class ObservationRouteTest(OfflineTestCase):
    def test_fixed_all_activity_route_and_worker_compatibility(self):
        for scope in ("roots", "delegates", "all"):
            route = route_sessions_observation(scope=scope, limit=17)
            self.assertEqual(
                route.target,
                "/api/sessions?view=observation&scope=" + scope
                + "&activity=all&pinned=all&limit=17",
            )
            self.assertEqual(
                (route.name, route.method, route.max_request, route.max_response),
                ("sessions_observation", "GET", 0, 256 * 1024),
            )
            document = transport._build_request(
                Origin.parse("https://host.example"), route, {}, b"", 10.0
            )
            # Actual supervised worker validator, without spawning or connecting.
            validated = transport.worker.validate_request(json.loads(document))
            self.assertEqual(validated["target"], route.target)
            self.assertEqual(validated["body"], b"")

    def test_scope_limit_and_cursor_validation_is_strict(self):
        bad = [
            {"scope": x}
            for x in ("root", "ALL", "", "all&workspace=/secret", None, 1, [])
        ] + [
            {"limit": x} for x in (True, False, 0, -1, 201, "200", 1.0, None)
        ] + [
            {"cursor": x} for x in ("", "a b", "\n", "\x7f", "é", "x" * 1025, 1, [])
        ]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs):
                exc = raised(route_sessions_observation, **kwargs)
                self.assertIsInstance(exc, TransportError)
                self.assertEqual(exc.code, "route_invalid")
                self.assertFalse(exc.maybe_sent)
        for limit in (1, 200):
            self.assertIn(("limit", str(limit)), route_sessions_observation(limit=limit).query)

    def test_cursor_cannot_inject_query_and_encoded_worker_limit_is_enforced(self):
        from urllib.parse import parse_qs, urlsplit

        cursor = "a&scope=all&workspace=/private?x=1%2F+#"
        route = route_sessions_observation(cursor=cursor)
        query = parse_qs(urlsplit(route.target).query)
        self.assertEqual(query["cursor"], [cursor])
        self.assertEqual(query["scope"], ["roots"])
        self.assertNotIn("workspace", query)
        self.assertNotIn("#", route.target)
        self.assertEqual(
            raised(route_sessions_observation, cursor="%" * 1024).code, "route_invalid"
        )
        route = route_sessions_observation(cursor="a" * 1024)
        transport._build_request(Origin.parse("https://host.example"), route, {}, b"", 10)


class StrictObjectTest(OfflineTestCase):
    def test_accepts_one_plain_object(self):
        self.assertEqual(
            strict_object(b'{"a": 1, "b": [true, null, "x"]}'),
            {"a": 1, "b": [True, None, "x"]},
        )

    def test_rejects_non_objects_and_malformed_scalars(self):
        bad = [
            b"",
            b"[]",
            b"1",
            b"null",
            b'"x"',
            b"{",
            b"{} x",
            b"{}{}",
            b'{"a":1,"a":2}',
            b'{"a":NaN}',
            b'{"a":Infinity}',
            b'{"a":-Infinity}',
            b'{"a":1e999}',
            b"\xff\xfe{}",
            b'{"a":"\xff"}',
            b"{'a':1}",
            b'{"a":' + b"[" * 200 + b"]" * 200 + b"}",
            "{}".encode("utf-16"),
        ]
        for body in bad:
            with self.subTest(body=body[:20]):
                exc = raised(strict_object, body)
                self.assertIsInstance(exc, TransportError)
                self.assertEqual(exc.code, "response_invalid")

    def test_non_bytes_rejected_and_error_does_not_echo(self):
        self.assertEqual(raised(strict_object, "{}").code, "response_invalid")
        exc = raised(strict_object, b'{"token":"SECRETMARKER" x')
        self.assertNotIn("SECRETMARKER", str(exc) + repr(exc))
        self.assertIsNone(exc.__cause__)


class ErrorTest(OfflineTestCase):
    def test_error_is_fixed_metadata_only(self):
        exc = TransportError("timeout", status=None, route="companion", maybe_sent=True)
        self.assertEqual(str(exc), "timeout")
        self.assertEqual(
            (exc.code, exc.status, exc.route, exc.maybe_sent),
            ("timeout", None, "companion", True),
        )
        unknown = TransportError("not-a-real-code")
        self.assertEqual(unknown.code, "transport_error")

    def test_response_is_plain_status_and_bytes(self):
        r = Response(200, b"{}")
        self.assertEqual((r.status, r.body), (200, b"{}"))
        self.assertEqual(json.loads(r.body), {})


# =====================================================================================
# HttpsTransport: binding validation BEFORE any network (zero sends on mismatch).
# =====================================================================================
import ast  # noqa: E402
from pathlib import Path  # noqa: E402

from pet_bridge import wire  # noqa: E402
from pet_bridge.auth import Attachment, Credential  # noqa: E402
from pet_bridge.core import Binding  # noqa: E402
from pet_bridge.transport import HttpsTransport  # noqa: E402

from .authfakes import (  # noqa: E402
    CLIENT_ID,
    CRED_REF,
    DEVICE_ID,
    HOST_IDENTITY,
    INSTANCE,
    INSTANCE_2,
    ORIGIN_URL,
    TOKEN,
    AuthCase,
    FakeClient,
    json_response,
    raised_any,
)

raised = raised_any
COMMAND_ID = "12345678-1234-4234-8234-1234567890ab"


def binding(**changes):
    values = {
        "binding_id": "22345678-1234-4234-8234-1234567890ab",
        "host_id": HOST_IDENTITY,
        "host_instance_id": INSTANCE,
        "session_id": SID,
        "client_id": CLIENT_ID,
        "credential_ref": CRED_REF,
    }
    values.update(changes)
    return Binding(**values)


def request_for(session_id=SID, text="hello", revision=None):
    return wire.send_command_request(
        session_id, COMMAND_ID, text, expected_revision=revision
    )


class SendCase(AuthCase):
    def setUp(self):
        super().setUp()
        self.store = self.open_store()
        self.store.save_credential(Credential(CRED_REF, ORIGIN_URL, DEVICE_ID, TOKEN))
        self.store.save_attachment(
            Attachment(CRED_REF, ORIGIN_URL, CLIENT_ID, HOST_IDENTITY, INSTANCE)
        )
        self.client = FakeClient(json_response({"accepted": True}))
        self.factory_calls = 0

        def factory():
            self.factory_calls += 1
            return self.client

        self.transport = HttpsTransport(self.store, factory)

    def assertZeroSends(self, exc, code):
        self.assertIsInstance(exc, TransportError)
        self.assertEqual(exc.code, code)
        self.assertFalse(exc.maybe_sent)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.factory_calls, 0)


class HttpsTransportSendTest(SendCase):
    def test_a_matching_binding_sends_exactly_one_authenticated_request(self):
        method, path, body = request_for(revision=7)
        status, reply = self.transport.send(binding(), method, path, body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(reply), {"accepted": True})
        (call,) = self.client.calls
        self.assertEqual(call["origin"], Origin.parse(ORIGIN_URL))
        self.assertEqual((call["route"].method, call["route"].path), ("POST", path))
        self.assertEqual(call["body"], body)
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(call["headers"]["X-Amplifier-Client"], CLIENT_ID)
        self.assertEqual(
            set(call["headers"]),
            {"Authorization", "X-Amplifier-Client", "Content-Type", "Accept"},
        )

    def test_error_statuses_are_returned_raw_for_the_core_to_interpret(self):
        for status in (401, 403, 404, 409, 429, 500, 504):
            with self.subTest(status=status):
                self.client = FakeClient(
                    Response(status, b'{"error":"x","deliveryUnknown":true}')
                )
                got = self.transport.send(binding(), *request_for())
                self.assertEqual(got, (status, b'{"error":"x","deliveryUnknown":true}'))
                self.assertEqual(len(self.client.calls), 1)  # never a retry

    def test_unicode_text_round_trips_as_utf8_bytes(self):
        method, path, body = request_for(text="h\u00e9llo \u2603")
        self.transport.send(binding(), method, path, body)
        self.assertEqual(self.client.calls[0]["body"], body)

    def test_any_binding_mismatch_sends_nothing(self):
        method, path, body = request_for()
        cases = {
            "unknown credential ref": (
                {"credential_ref": "device-" + "ef" * 16},
                "credential_missing",
            ),
            "client id": ({"client_id": "D" * 32}, "binding_invalid"),
            "host identity": ({"host_id": "6" * 64}, "binding_invalid"),
            "host instance": ({"host_instance_id": INSTANCE_2}, "binding_invalid"),
        }
        for name, (changes, code) in cases.items():
            with self.subTest(name):
                exc = raised_any(
                    self.transport.send, binding(**changes), method, path, body
                )
                self.assertZeroSends(exc, code)

    def test_session_mismatch_sends_nothing(self):
        other = request_for(session_id="sess-other-9")
        exc = raised_any(self.transport.send, binding(), *other)
        self.assertZeroSends(exc, "binding_invalid")

    def test_path_and_method_must_be_the_exact_send_route(self):
        _method, path, body = request_for()
        for bad_method, bad_path in (
            ("PUT", path),
            ("GET", path),
            ("post", path),
            ("POST", path + "?x=1"),
            ("POST", path + "/"),
            ("POST", path.replace("/commands", "/stop")),
            ("POST", path.replace(SID, SID + "%2F..")),
            ("POST", "/api/actions"),
            ("POST", "/api/sessions/%2e%2e/commands"),
            ("POST", "https://evil.example" + path),
            ("POST", path + "#frag"),
            ("POST", "//evil.example" + path),
            ("POST", ""),
            ("POST", None),
        ):
            with self.subTest(method=bad_method, path=bad_path):
                exc = raised_any(
                    self.transport.send, binding(), bad_method, bad_path, body
                )
                self.assertZeroSends(exc, "binding_invalid")

    def test_malformed_bodies_send_nothing(self):
        method, path, good = request_for()
        doc = json.loads(good)

        def variant(**changes):
            copy = json.loads(good)
            for key, value in changes.items():
                if value is ...:
                    copy.pop(key, None)
                else:
                    copy[key] = value
            return json.dumps(copy).encode()

        def args(**changes):
            copy = json.loads(good)
            for key, value in changes.items():
                if value is ...:
                    copy["args"].pop(key, None)
                else:
                    copy["args"][key] = value
            return json.dumps(copy).encode()

        bodies = {
            "empty": b"",
            "not json": b"nope",
            "array": b"[]",
            "not bytes": "text",
            "dup keys": b'{"id":"a","id":"b"}',
            "nan": good.replace(b'"hello"', b"NaN"),
            "bad utf8": b"\xff\xfe",
            "too large": good + b" " * (16 * 1024),
            "action": variant(action="session.create"),
            "action missing": variant(action=...),
            "id not uuid": variant(id="not-a-uuid"),
            "id uuid1": variant(id="12345678-1234-1234-8234-1234567890ab"),
            "id missing": variant(id=...),
            "extra top key": variant(sessionId=SID),
            "revision str": variant(expectedRevision="7"),
            "revision negative": variant(expectedRevision=-1),
            "revision bool": variant(expectedRevision=True),
            "args not dict": variant(args=[]),
            "args extra key": args(sessionId=SID),
            "args via": args(via="voice"),
            "args draft": args(preserveDraft=False),
            "args attachments": args(attachmentIds=["a"]),
            "args text int": args(text=5),
            "args text missing": args(text=...),
        }
        self.assertEqual(doc["args"]["via"], "chat")
        for name, body in bodies.items():
            with self.subTest(name):
                exc = raised_any(self.transport.send, binding(), method, path, body)
                self.assertZeroSends(exc, "body_invalid")

    def test_missing_attachment_or_credential_sends_nothing(self):
        method, path, body = request_for()
        self.store._delete(f"attachment-{CRED_REF}.json")
        self.assertZeroSends(
            raised_any(self.transport.send, binding(), method, path, body),
            "credential_missing",
        )

    def test_tampered_store_is_a_zero_send_failure(self):
        method, path, body = request_for()
        record = self.root / f"attachment-{CRED_REF}.json"
        record.write_bytes(b"corrupt")
        exc = raised_any(self.transport.send, binding(), method, path, body)
        self.assertZeroSends(exc, "credential_missing")

    def test_client_failure_propagates_as_maybe_sent_and_is_not_retried(self):
        self.client = FakeClient(
            TransportError("timeout", route="send", maybe_sent=True)
        )
        exc = raised_any(self.transport.send, binding(), *request_for())
        self.assertEqual((exc.code, exc.maybe_sent), ("timeout", True))
        self.assertEqual(len(self.client.calls), 1)

    def test_errors_never_echo_the_token_or_body(self):
        exc = raised_any(
            self.transport.send,
            binding(client_id="D" * 32),
            *request_for(text="TEXTSECRET"),
        )
        self.assertNotIn(TOKEN, str(exc) + repr(exc))
        self.assertNotIn("TEXTSECRET", str(exc) + repr(exc))

    def test_binding_must_be_a_core_binding_with_a_valid_ref(self):
        exc = raised_any(self.transport.send, object(), *request_for())
        self.assertZeroSends(exc, "binding_invalid")


PROCESS_NETWORK_ROOTS = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "urllib",
        "asyncio",
        "aiohttp",
        "subprocess",
        "selectors",
        "multiprocessing",
        "importlib",
        "ctypes",
        "pty",
        "socketserver",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "telnetlib",
        "xmlrpc",
    }
)
DYNAMIC_CALLS = frozenset({"__import__", "eval", "exec"})
OS_PROCESS_CALLS = frozenset(
    {"system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp"}
    | {f"exec{s}" for s in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")}
    | {f"spawn{s}" for s in ("l", "le", "lp", "lpe", "v", "ve", "vp", "vpe")}
)
NETWORK_CALLS = frozenset(
    {"Popen", "create_connection", "getaddrinfo", "urlopen"}
    # asyncio spellings of dial / listen / thread / subprocess capability (U2 repair A4)
    | {"open_connection", "start_server", "create_server", "to_thread"}
    | {"run_in_executor", "subprocess_exec", "subprocess_shell"}
    | {"create_subprocess_exec", "create_subprocess_shell"}
)


def scan_source(text: str) -> set[str]:
    """Findings for process / network / dynamic-execution capability in a source text.

    Test-only, purely syntactic and exact: imports (any form) of the roots above,
    dynamic ``__import__``/``eval``/``exec`` calls, ``os.<process call>`` by exact name
    (so ``obj.execute()`` is NOT a hit), ``Popen``/``create_connection``/
    ``getaddrinfo``/``urlopen`` calls under any spelling and any ``shell=`` keyword.
    ``urllib.parse`` alone is pure string handling (used by the accepted core ``wire``
    and by the transport) and is the one explicit exemption; every other ``urllib``
    form is a hit.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[
                    0
                ] in PROCESS_NETWORK_ROOTS and alias.name.split(".")[:2] != [
                    "urllib",
                    "parse",
                ]:
                    found.add(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                pure_parse = full == "urllib.parse" or node.module.split(".")[:2] == [
                    "urllib",
                    "parse",
                ]
                if (
                    node.module.split(".")[0] in PROCESS_NETWORK_ROOTS
                    and not pure_parse
                ) or (node.module == "os" and alias.name in OS_PROCESS_CALLS):
                    found.add(f"import {full}")
        elif isinstance(node, ast.Call):
            found.update(_call_findings(node))
    return found


def _call_findings(node: ast.Call) -> set[str]:
    found: set[str] = set()
    func = node.func
    if isinstance(func, ast.Name):
        if func.id in DYNAMIC_CALLS | OS_PROCESS_CALLS | NETWORK_CALLS:
            found.add(f"call {func.id}")
    elif isinstance(func, ast.Attribute):
        if (
            isinstance(func.value, ast.Name)
            and func.value.id == "os"
            and func.attr in OS_PROCESS_CALLS
        ):
            found.add(f"call os.{func.attr}")
        if func.attr in NETWORK_CALLS:
            found.add(f"call {func.attr}")
    if any(kw.arg == "shell" for kw in node.keywords):
        found.add("shell keyword")
    return found


# The ONLY reviewed capability surface.  A future gateway module is a NEW entry here,
# added deliberately after its own review; there is no blanket exemption.  The pure
# gateway_state / gateway_provider modules are classified with an EMPTY surface (no entry);
# gateway_core / gateway_server get their own reviewed entries in their own units.
REVIEWED_SURFACE = {
    "transport.py": {
        "import subprocess",
        "import selectors",
    },
    "_https_worker.py": {
        "import http.client",
        "import socket",
        "import ssl",
    },
    # gateway_core (U2): asyncio only.  threading is a non-capability stdlib root here;
    # the worker thread is the CoreRunner.  No socket/ssl/http/subprocess, no aiohttp.
    "gateway_core.py": {"import asyncio"},
    # gateway_server (U3): the ONLY aiohttp importer.  asyncio + ssl + aiohttp(+web) and
    # nothing else: no socket, http.client, subprocess, selectors, no dial/listen/executor
    # CALL spelling (the scanner pins those too).  The device/provider sockets are
    # created by aiohttp inside its own objects; this module never opens one itself.
    "gateway_server.py": {
        "import asyncio",
        "import ssl",
        "import aiohttp",
        "import aiohttp.web",
    },
}
CLASSIFIED_MODULES = frozenset(
    {
        "__init__.py",
        "__main__.py",
        "_https_worker.py",
        "auth.py",
        "cli.py",
        "companion.py",
        "core.py",
        "gateway_core.py",
        "gateway_events.py",  # local file I/O only; EMPTY process/network surface
        "gateway_provider.py",
        "gateway_server.py",
        "gateway_state.py",
        "journal.py",
        "reply.py",
        "transport.py",
        "wire.py",
    }
)


class NetworkModuleBoundaryTest(OfflineTestCase):
    """Process/network capability lives ONLY in the two reviewed modules."""

    package = Path(transport.__file__).parent

    def test_every_module_is_classified_and_only_the_reviewed_two_have_capability(self):
        names = {p.name for p in self.package.glob("*.py")}
        self.assertEqual(names, CLASSIFIED_MODULES)  # a new module must be classified
        for name in sorted(names):
            with self.subTest(module=name):
                found = scan_source((self.package / name).read_text(encoding="utf-8"))
                self.assertEqual(found, REVIEWED_SURFACE.get(name, set()))

    def test_subprocess_is_only_in_transport_and_socket_ssl_only_in_the_worker(self):
        parent = REVIEWED_SURFACE["transport.py"]
        child = REVIEWED_SURFACE["_https_worker.py"]
        self.assertFalse({"import socket", "import ssl", "import http.client"} & parent)
        self.assertFalse({"import subprocess", "import selectors"} & child)
        self.assertEqual(
            {n for n, f in REVIEWED_SURFACE.items() if "import subprocess" in f},
            {"transport.py"},
        )

    def test_the_scanner_detects_every_forbidden_form(self):
        hostile = (
            "import socket",
            "import ssl",
            "import http.client",
            "from http import client",
            "import asyncio",
            "import aiohttp",
            "import aiohttp.web",
            "from aiohttp import web",
            "import selectors",
            "import subprocess",
            "import urllib",
            "import urllib.request",
            "from urllib import request",
            "from urllib.request import urlopen",
            "from urllib.error import URLError",
            "import urllib.parse, urllib.request",
            "import multiprocessing",
            "from multiprocessing import Process",
            "import importlib",
            "from importlib import import_module",
            "__import__('socket')",
            "eval('1')",
            "exec('x = 1')",
            "import os\nos.system('x')",
            "import os\nos.popen('x')",
            "import os\nos.fork()",
            "import os\nos.execv('a', [])",
            "import os\nos.execvp('a', [])",
            "import os\nos.spawnl(0, 'a')",
            "import os\nos.posix_spawn('a', [], {})",
            "from os import system",
            "from os import fork",
            "fork()",
            "Popen(['x'])",
            "x.Popen(['x'])",
            "create_connection(('h', 1))",
            "s.create_connection(('h', 1))",
            "getaddrinfo('h', 1)",
            "s.getaddrinfo('h', 1)",
            "urlopen('x')",
            "u.urlopen('x')",
            "f(shell=True)",
        )
        for source in hostile:
            with self.subTest(source=source):
                self.assertTrue(scan_source(source), source)

    def test_the_scanner_detects_asyncio_network_process_and_executor_forms(self):
        # U2 repair A4: the asyncio spellings of connect / listen / thread / subprocess.
        wrapped = (
            "asyncio.open_connection('h', 1)",
            "asyncio.start_server(f, 'h', 1)",
            "loop.create_server(f)",
            "await asyncio.to_thread(f)",
            "loop.run_in_executor(None, f)",
            "loop.subprocess_exec(f, 'x')",
            "loop.subprocess_shell(f, 'x')",
            "asyncio.create_subprocess_exec('x')",
            "asyncio.create_subprocess_shell('x')",
        )
        for call in wrapped:
            source = f"async def g():\n    {call}\n"
            with self.subTest(call=call):
                found = scan_source(source)
                name = call.split("(")[0].split(".")[-1].split()[-1]
                self.assertIn(f"call {name}", found, call)

    def test_the_scanner_has_no_false_positive_on_innocent_calls(self):
        innocent = (
            "import json, os, re\nos.path.join('a', 'b')\nos.set_blocking(1, False)",
            "obj.execute()\nproc.executor()\nrunner.spawn_child()\nx.system_info()",
            "import hashlib, contextlib, fcntl, signal\nhashlib.sha256(b'')",
            "from urllib_like import thing\nfrom osmod import system",
            "def fork_it():\n    return 'ok'\nfork_it()",
            "open('x').read()",
            "from urllib.parse import quote\nimport urllib.parse\nfrom urllib import parse",
            "loop.call_soon_threadsafe(f)\nloop.call_later(1, f)\nsub.executor_name()",
        )
        for source in innocent:
            with self.subTest(source=source):
                self.assertEqual(scan_source(source), set(), source)

    def test_an_injected_capability_into_a_reviewed_or_pure_module_changes_the_surface(
        self,
    ):
        for name in ("core.py", "auth.py", "transport.py", "_https_worker.py"):
            original = (self.package / name).read_text(encoding="utf-8")
            for extra in (
                "from urllib import request",
                "import multiprocessing",
                "import os\nos.system('x')",
                "__import__('socket')",
            ):
                with self.subTest(module=name, extra=extra):
                    found = scan_source(original + "\n" + extra + "\n")
                    self.assertNotEqual(found, REVIEWED_SURFACE.get(name, set()))

    def test_gateway_capabilities_are_confined(self):
        # U3: aiohttp only in gateway_server; ssl only in the worker and the gateway server;
        # asyncio only in gateway_core and gateway_server; subprocess only in transport;
        # the pure gateway modules have an empty surface.
        findings = {
            name: scan_source((self.package / name).read_text(encoding="utf-8"))
            for name in sorted(CLASSIFIED_MODULES)
        }

        def holders(form: str) -> set[str]:
            return {name for name, found in findings.items() if form in found}

        self.assertEqual(holders("import aiohttp"), {"gateway_server.py"})
        self.assertEqual(holders("import aiohttp.web"), {"gateway_server.py"})
        self.assertEqual(
            holders("import ssl"), {"_https_worker.py", "gateway_server.py"}
        )
        self.assertEqual(
            holders("import asyncio"), {"gateway_core.py", "gateway_server.py"}
        )
        self.assertEqual(holders("import subprocess"), {"transport.py"})
        self.assertEqual(findings["gateway_state.py"], set())
        self.assertEqual(findings["gateway_provider.py"], set())
        for name, found in findings.items():  # no other aiohttp form anywhere
            extra = {f for f in found if f.startswith("import aiohttp")}
            self.assertLessEqual(extra, {"import aiohttp", "import aiohttp.web"}, name)

    def test_the_gateway_server_surface_is_pinned_exactly(self):
        # `from aiohttp import web` and `import aiohttp` are the reviewed spellings: any
        # other aiohttp name, socket/http/subprocess import or dial/listen/executor call
        # added to gateway_server changes the scanned surface and fails the pin above.
        original = (self.package / "gateway_server.py").read_text(encoding="utf-8")
        self.assertEqual(scan_source(original), REVIEWED_SURFACE["gateway_server.py"])
        for extra in (
            "import socket",
            "import subprocess",
            "import selectors",
            "import http.client",
            "import aiohttp.client",
            "from aiohttp import ClientSession",
            "from aiohttp.client import ClientSession",
            "from aiohttp import web, ClientSession",
            "import threading, multiprocessing",
            "loop.create_server(f)",
            "asyncio.open_connection('h', 1)",
            "asyncio.start_server(f, 'h', 1)",
            "await asyncio.to_thread(f)",
            "s.create_connection(('h', 1))",
            "s.getaddrinfo('h', 1)",
            "loop.run_in_executor(None, f)",
            "asyncio.create_subprocess_exec('x')",
            "import os\nos.system('x')",
            "__import__('socket')",
            "f(shell=True)",
        ):
            with self.subTest(extra=extra):
                found = scan_source(original + "\n" + extra + "\n")
                self.assertNotEqual(found, REVIEWED_SURFACE["gateway_server.py"])

    def test_no_shell_keyword_or_dynamic_call_anywhere_in_production(self):
        for path in sorted(self.package.glob("*.py")):
            found = scan_source(path.read_text(encoding="utf-8"))
            self.assertNotIn("shell keyword", found, path.name)
            self.assertFalse({f for f in found if f.startswith("call ")}, path.name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
