"""Offline tests for the bridge command line (scripted client, temp config root).

Nothing here reads the real HOME or environment tokens, opens a socket, or sends a
message.  The ``bind`` command only records a selection in the private journal.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import types
import unicodedata
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pet_bridge
from pet_bridge import auth, cli, transport
from pet_bridge.core import Bridge
from pet_bridge.journal import Journal

from .authfakes import (
    CLIENT_ID,
    CRED_REF,
    HOST_IDENTITY,
    INSTANCE,
    ORIGIN_URL,
    PAIRING_ID,
    TOKEN,
    VERIFIER,
    AuthCase,
    FakeClient,
    approved,
    attach_ok,
    json_response,
    pair_ok,
    raised_any,
)
from .fakes import FakeClock, IdFactory
from .test_companion import catalog_bootstrap, cpage, lpage, srow

CANARY = "CANARY-ARGV-SECRET-81f0"
FULL_ID = "0a1b2c3d-1111-4222-8333-44445555aaaa"


class CliCase(AuthCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.base / "cfg"
        self.clock = FakeClock(5000.0)
        self.client = FakeClient()

    def run_cli(self, *argv: str, client: FakeClient | None = None, **kwargs: Any):
        used = client or self.client
        out, err = io.StringIO(), io.StringIO()
        kwargs.setdefault("env", {})
        kwargs.setdefault("root", self.cfg)
        code = cli.main(
            list(argv),
            stdout=out,
            stderr=err,
            clientfactory=lambda: used,
            clock=self.clock,
            new_id=IdFactory(),
            **kwargs,
        )
        return code, out.getvalue(), err.getvalue()

    def pair_and_attach(self) -> None:
        self.client.script = [json_response(pair_ok())]
        with mock.patch.object(
            auth.secrets, "token_urlsafe", lambda n: {32: VERIFIER, 24: CLIENT_ID}[n]
        ):
            self.assertEqual(self.run_cli("pair-start", "--origin", ORIGIN_URL)[0], 0)
            self.clock.advance(5)
            self.client.script = [json_response(approved())]
            self.assertEqual(self.run_cli("pair-poll")[0], 0)
            self.client.script = [json_response(attach_ok())]
            self.assertEqual(self.run_cli("attach")[0], 0)

    def assertNoSecrets(self, *texts: str) -> None:
        for text in texts:
            for secret in (TOKEN, VERIFIER, CANARY, "T" * 20, "V" * 20):
                self.assertNotIn(secret, text)

    def fake_gateway(self, result: int = 0) -> mock.Mock:
        """A stand-in ``pet_bridge.gateway_server`` so a stdlib-only run never needs aiohttp.

        Both ``sys.modules`` and the package attribute are replaced (and restored), so a
        real module imported earlier by another test cannot leak into the lazy import.
        """
        serve = mock.Mock(return_value=result)
        fake = types.ModuleType("pet_bridge.gateway_server")
        fake.serve_main = serve  # type: ignore[attr-defined]
        modules = mock.patch.dict(sys.modules, {"pet_bridge.gateway_server": fake})
        attribute = mock.patch.object(pet_bridge, "gateway_server", fake, create=True)
        modules.start()
        self.addCleanup(modules.stop)
        attribute.start()
        self.addCleanup(
            attribute.stop
        )  # runs first (LIFO): the attribute, then the dict
        return serve

    @contextlib.contextmanager
    def gateway_absent(self) -> Iterator[None]:
        """Make ``from . import gateway_server`` fail like a missing dependency would.

        ``sys.modules`` gets ``None`` for the module AND the package attribute (which an
        earlier test's real import may have left) is removed; both are restored on exit.
        """
        name = "pet_bridge.gateway_server"
        absent = object()
        module = sys.modules.get(name, absent)
        attribute = pet_bridge.__dict__.get("gateway_server", absent)
        sys.modules[name] = None  # type: ignore[assignment]
        pet_bridge.__dict__.pop("gateway_server", None)
        try:
            yield
        finally:
            if module is absent:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module  # type: ignore[assignment]
            if attribute is absent:
                pet_bridge.__dict__.pop("gateway_server", None)
            else:
                pet_bridge.__dict__["gateway_server"] = attribute


class HelpAndUsageTest(CliCase):
    def test_help_works_offline_without_opening_the_config(self):
        missing = self.base / "no-parent" / "cfg"
        with mock.patch.object(
            auth, "CredentialStore", side_effect=AssertionError("opened config")
        ):
            for argv in (
                ["-h"],
                ["--help"],
                ["pair-start", "-h"],
                ["pair-poll", "--help"],
                ["pair-abandon", "-h"],
                ["attach", "-h"],
                ["list", "-h"],
                ["bind", "-h"],
                ["serve", "-h"],
                ["serve", "--help"],
            ):
                with self.subTest(argv=argv):
                    code, out, err = self.run_cli(*argv, root=missing)
                    self.assertEqual((code, err), (0, ""))
                    self.assertIn("usage:", out)
        self.assertFalse(missing.parent.exists())
        _, out, _ = self.run_cli("-h")
        for command in (
            "pair-start",
            "pair-poll",
            "pair-abandon",
            "attach",
            "list",
            "bind",
            "serve",
        ):
            self.assertIn(command, out)

    def test_the_top_level_help_makes_the_truthful_service_and_send_claim(self):
        _, out, _ = self.run_cli("--help")
        text = " ".join(
            out.split()
        )  # the help wraps lines; the sentence is what counts
        self.assertNotIn("Nothing here sends a message", text)
        self.assertNotIn("starts a service", text)
        self.assertIn(
            "Only 'serve' runs a service. In conversation mode, recognized requests are "
            "sent to the bound session without a device confirmation tap.",
            text,
        )
        self.assertNotIn("only after an explicit human confirm on the device", text)

    def test_serve_help_never_imports_the_gateway(self):
        serve = self.fake_gateway()
        code, out, err = self.run_cli("serve", "--help")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("--config", out)
        serve.assert_not_called()

    def test_there_is_no_send_token_resume_or_run_command(self):
        for argv in (["send", "hello"], ["token"], ["resume"], ["run"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("error: usage", err)
        self.assertFalse(self.cfg.exists())

    def test_serve_requires_an_explicit_config(self):
        serve = self.fake_gateway()
        cases = (
            ["serve"],
            ["serve", "--config"],
            ["serve", "--config", "relative.json"],
            ["serve", "--config", "./relative.json"],
            ["serve", "--config", ""],
            ["serve", "--config=" + CANARY + "/x.json"],
            ["serve", "--conf", "/abs/gateway.json"],  # abbreviations are not accepted
            ["serve", "--config", "/abs/gateway.json", "extra"],
            ["serve", "--config", "/abs/a.json", "--config", "rel.json"],
            ["serve", "--token", CANARY],
            ["serve", "--credential", "device-" + "1" * 32],
        )
        for argv in cases:
            with self.subTest(argv=argv[:2]):
                code, out, err = self.run_cli(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("error: usage", err)
                self.assertNotIn(CANARY, out + err)
        serve.assert_not_called()
        self.assertFalse(self.cfg.exists())  # no directory, no default root

    def test_serve_wires_the_absolute_config_env_streams_and_deps_through(self):
        serve = self.fake_gateway(7)
        path = self.base / "gateway.json"  # absolute; deliberately never created
        env = {"PET_PROVIDER_KEY_NAME": "carried-by-identity"}
        deps = object()
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            ["serve", "--config", str(path)],
            env=env,
            stdout=out,
            stderr=err,
            serve_deps=deps,
        )
        self.assertEqual(code, 7)  # the gateway's own exit status, unchanged
        serve.assert_called_once()
        args, kwargs = serve.call_args
        self.assertEqual(args, (path,))
        self.assertIsInstance(args[0], Path)
        self.assertEqual(set(kwargs), {"env", "stdout", "stderr", "deps", "event_log_path"})
        self.assertIsNone(kwargs["event_log_path"])
        self.assertIs(kwargs["env"], env)
        self.assertIs(kwargs["stdout"], out)
        self.assertIs(kwargs["stderr"], err)
        self.assertIs(kwargs["deps"], deps)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))  # cli adds nothing
        self.assertFalse(path.exists())
        self.assertFalse(self.cfg.exists())

    def test_serve_defaults_to_no_deps_and_returns_zero(self):
        serve = self.fake_gateway(0)
        path = self.base / "gateway.json"
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            ["serve", "--config", str(path)], env={}, stdout=out, stderr=err
        )
        self.assertEqual(code, 0)
        self.assertIsNone(serve.call_args.kwargs["deps"])

    def test_serve_never_touches_the_default_root_or_the_credential_store(self):
        serve = self.fake_gateway(0)
        boom = AssertionError("the serve path touched the default config root")
        with (
            mock.patch.object(auth, "default_root", side_effect=boom),
            mock.patch.object(auth, "CredentialStore", side_effect=boom),
            mock.patch.object(cli, "_ensure_private_root", side_effect=boom),
            mock.patch.object(cli, "_run", side_effect=boom),
            mock.patch.object(transport, "HttpsClient", side_effect=boom),
        ):
            code = cli.main(
                ["serve", "--config", str(self.base / "gateway.json")],
                env={"HOME": "/should/not/be/used", "XDG_CONFIG_HOME": "/nope"},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(code, 0)
        serve.assert_called_once()

    def test_a_failing_gateway_leaves_only_a_fixed_code_and_no_traceback(self):
        serve = self.fake_gateway()
        serve.side_effect = RuntimeError("SECRET-DETAIL " + CANARY)
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            ["serve", "--config", str(self.base / "gateway.json")],
            env={},
            stdout=out,
            stderr=err,
        )
        self.assertEqual((code, out.getvalue()), (1, ""))
        self.assertEqual(err.getvalue(), "error: internal\n")

    def test_an_unimportable_gateway_is_a_fixed_internal_error_and_both_imports_restore(
        self,
    ):
        name = "pet_bridge.gateway_server"
        absent = object()
        module_before = sys.modules.get(name, absent)
        attribute_before = pet_bridge.__dict__.get("gateway_server", absent)
        out, err = io.StringIO(), io.StringIO()
        with self.gateway_absent():
            self.assertIsNone(sys.modules[name])
            self.assertNotIn("gateway_server", pet_bridge.__dict__)
            code = cli.main(
                ["serve", "--config", str(self.base / "gateway.json")],
                env={},
                stdout=out,
                stderr=err,
            )
        self.assertEqual((code, out.getvalue()), (1, ""))
        self.assertEqual(err.getvalue(), "error: internal\n")  # no module name, no text
        self.assertIs(sys.modules.get(name, absent), module_before)  # both restored
        self.assertIs(
            pet_bridge.__dict__.get("gateway_server", absent), attribute_before
        )

    def test_the_docstring_states_serve_and_still_denies_send_token_resume(self):
        doc = cli.__doc__ or ""
        self.assertIn("serve --config ABSOLUTE_PATH", doc)
        self.assertNotIn("NO send, serve", doc)
        for word in ("send", "token", "resume"):
            self.assertIn(word, doc)

    def test_usage_errors_never_echo_the_arguments(self):
        cases = [
            ["--token", CANARY],
            ["pair-start", "--origin", ORIGIN_URL, "--token", CANARY],
            ["pair-start", "--origin"],
            ["pair-start"],
            ["bind"],
            ["bind", "a", "b", CANARY],
            ["attach", "--credential"],
            ["list", CANARY],
            ["pair-poll", CANARY],
            [CANARY],
            [],
            ["--origin=" + CANARY],
            ["pair-st", "--origin", ORIGIN_URL],  # abbreviations are not accepted
        ]
        for argv in cases:
            with self.subTest(argv=argv[:2]):
                code, out, err = self.run_cli(*argv)
                self.assertEqual(code, 2)
                self.assertNotIn(CANARY, out + err)
                self.assertIn("error: usage", err)
        self.assertFalse(self.cfg.exists())

    def test_module_entry_point_prints_help_without_touching_the_config(self):
        bridge_dir = Path(cli.__file__).resolve().parent.parent
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "pet_bridge", "--help"],
            capture_output=True,
            text=True,
            cwd=bridge_dir,
            env={"PYTHONDONTWRITEBYTECODE": "1", "HOME": "/nonexistent-home"},
            timeout=30,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("pair-start", proc.stdout)
        self.assertIn("serve", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_existing_commands_stay_stdlib_only_and_the_gateway_is_never_imported(self):
        bridge_dir = Path(cli.__file__).resolve().parent.parent
        snippet = (
            "import io, json, sys\n"
            "from pet_bridge import cli\n"
            "out, err = io.StringIO(), io.StringIO()\n"
            "codes = [\n"
            "    cli.main(['--help'], env={}, stdout=out, stderr=err),\n"
            "    cli.main(['serve', '--help'], env={}, stdout=out, stderr=err),\n"
            "    cli.main(['serve', '--config', 'relative.json'], env={}, stdout=out, stderr=err),\n"
            "    cli.main(['serve'], env={}, stdout=out, stderr=err),\n"
            "]\n"
            "bad = sorted(\n"
            "    name for name in sys.modules\n"
            "    if name == 'aiohttp' or name.startswith('aiohttp.')\n"
            "    or name.startswith('pet_bridge.gateway_')\n"
            ")\n"
            "print(json.dumps({'codes': codes, 'bad': bad}))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-B", "-c", snippet],
            capture_output=True,
            text=True,
            cwd=bridge_dir,
            env={"PYTHONDONTWRITEBYTECODE": "1", "HOME": "/nonexistent-home"},
            timeout=30,
            check=False,
        )
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        result = json.loads(proc.stdout)
        self.assertEqual(result, {"codes": [0, 0, 2, 2], "bad": []})

    def test_the_gateway_import_exists_only_inside_the_serve_branch(self):
        tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
        inside_functions: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                inside_functions.update(id(n) for n in ast.walk(node))
        lazy = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Import | ast.ImportFrom):
                continue
            names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            if any("gateway" in n or "aiohttp" in n for n in names):
                self.assertIn(
                    id(node), inside_functions, "a module-level gateway import"
                )
                lazy += 1
        self.assertEqual(lazy, 1)

    def test_the_package_init_imports_no_gateway_module(self):
        tree = ast.parse(Path(pet_bridge.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = [a.name for a in node.names] + [
                    getattr(node, "module", "") or ""
                ]
                for name in names:
                    self.assertNotIn("gateway", name)
                    self.assertNotIn("aiohttp", name)
        doc = " ".join((pet_bridge.__doc__ or "").split())
        self.assertIn(
            "Gateway modules (gateway_state, gateway_provider, gateway_core, "
            "gateway_server) are imported explicitly; this package never imports "
            "them, so the stdlib core imports without aiohttp.",
            doc,
        )

    def test_selected_session_is_never_hard_coded(self):
        package = Path(cli.__file__).parent
        for path in package.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("0a1b2c3d", text, path.name)
            self.assertNotIn("5555aaaa", text, path.name)


class PairingFlowTest(CliCase):
    def test_pair_start_prints_only_the_url_code_and_instructions(self):
        self.client.script = [json_response(pair_ok())]
        with mock.patch.object(auth.secrets, "token_urlsafe", lambda n: VERIFIER):
            code, out, err = self.run_cli(
                "pair-start", "--origin", ORIGIN_URL, "--name", "my pet"
            )
        self.assertEqual((code, err), (0, ""))
        self.assertIn(ORIGIN_URL + "/devices/pair?id=" + PAIRING_ID, out)
        self.assertIn("ABCD-2345", out)
        self.assertIn("600", out)
        self.assertIn("never approves", out)
        self.assertNoSecrets(out, err)
        self.assertEqual(stat.S_IMODE(os.lstat(self.cfg).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(self.cfg / "auth").st_mode), 0o700)
        self.assertEqual(len(self.client.calls), 1)

    def test_second_start_is_blocked_and_abandon_unblocks_it(self):
        self.client.script = [json_response(pair_ok())]
        self.assertEqual(self.run_cli("pair-start", "--origin", ORIGIN_URL)[0], 0)
        code, out, err = self.run_cli("pair-start", "--origin", ORIGIN_URL)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("error: auth_attempt_exists", err)
        self.assertEqual(len(self.client.calls), 1)
        code, out, _ = self.run_cli("pair-abandon")
        self.assertEqual(code, 0)
        self.assertIn("locally", out)
        self.client.script = [json_response(pair_ok())]
        self.assertEqual(self.run_cli("pair-start", "--origin", ORIGIN_URL)[0], 0)

    def test_poll_statuses_and_credential_reference_output(self):
        self.client.script = [json_response(pair_ok())]
        self.run_cli("pair-start", "--origin", ORIGIN_URL)
        code, out, _ = self.run_cli("pair-poll")  # too early: no request
        self.assertEqual(code, 0)
        self.assertIn("5", out)
        self.assertEqual(len(self.client.calls), 1)
        self.clock.advance(5)
        self.client.script = [json_response({"status": "pending"})]
        code, out, _ = self.run_cli("pair-poll")
        self.assertEqual(code, 0)
        self.assertIn("Not approved yet", out)
        self.clock.advance(5)
        self.client.script = [json_response(approved())]
        code, out, err = self.run_cli("pair-poll")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(CRED_REF, out)
        self.assertNoSecrets(out, err)

    def test_terminal_poll_results_exit_nonzero(self):
        for body, word in (
            (json_response({"status": "denied"}), "denied"),
            (Response403(), "proof"),
        ):
            self.run_cli("pair-abandon")
            self.client.script = [json_response(pair_ok())]
            self.run_cli("pair-start", "--origin", ORIGIN_URL)
            self.clock.advance(5)
            self.client.script = [body]
            code, out, _ = self.run_cli("pair-poll")
            self.assertEqual(code, 1)
            self.assertIn(word, out)

    def test_origin_errors_have_fixed_codes_only(self):
        code, out, err = self.run_cli(
            "pair-start", "--origin", "http://x.example/?t=" + CANARY
        )
        self.assertEqual((code, out), (1, ""))
        self.assertIn("error: auth_origin_invalid", err)
        self.assertNotIn(CANARY, err)

    def test_transport_and_unexpected_failures_never_leak_text_or_tracebacks(self):
        self.client.script = [RuntimeError(CANARY)]
        code, out, err = self.run_cli("pair-start", "--origin", ORIGIN_URL)
        self.assertEqual(code, 1)
        self.assertIn("error: auth_start_unknown", err)
        self.assertNotIn("Traceback", out + err)
        self.assertNoSecrets(out, err)

        def broken_factory():
            raise RuntimeError(CANARY)

        out2, err2 = io.StringIO(), io.StringIO()
        self.run_cli("pair-abandon")
        code = cli.main(
            ["pair-start", "--origin", ORIGIN_URL],
            env={},
            stdout=out2,
            stderr=err2,
            root=self.cfg,
            clientfactory=broken_factory,
            clock=self.clock,
            new_id=IdFactory(),
        )
        self.assertEqual(code, 1)
        self.assertIn("error: internal", err2.getvalue())
        self.assertNotIn(CANARY, out2.getvalue() + err2.getvalue())
        self.assertNotIn("Traceback", err2.getvalue())


class Response403:
    status = 403
    body = b"Invalid device proof."


class AttachListBindTest(CliCase):
    def setUp(self) -> None:
        super().setUp()
        self.pair_and_attach()

    def listing(self, rows=None, nxt=None):
        rows = (
            rows
            if rows is not None
            else [
                srow(FULL_ID, title="Selected\x1b[31m one"),
                srow("sess-other-9", status="working", execution=True),
            ]
        )
        return lpage(rows, nxt)

    def test_attach_output_is_metadata_only(self):
        self.client.script = [json_response(attach_ok())]
        code, out, err = self.run_cli("attach")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(CRED_REF, out)
        self.assertIn(HOST_IDENTITY[:8], out)
        self.assertIn(INSTANCE[:8], out)
        self.assertNoSecrets(out, err)
        self.assertNotIn(HOST_IDENTITY, out)

    def test_changed_instance_always_fails_with_the_fixed_hint(self):
        self.pair_and_attach()
        path = self.cfg / "auth" / f"attachment-{CRED_REF}.json"
        before = path.read_bytes()
        other = "12345678-1234-4234-8234-123456789abd"
        self.client.script = [json_response(attach_ok(hostInstanceId=other))]
        code, out, err = self.run_cli("attach")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("error: auth_instance_changed", err)
        self.assertIn(
            "The host instance changed. This bridge cannot continue with that "
            "instance. Nothing was changed.",
            err,
        )
        self.assertNotIn("accept", err.lower())
        self.assertEqual(path.read_bytes(), before)

    def test_the_instance_override_flag_does_not_exist(self):
        self.pair_and_attach()
        self.client.script = [json_response(attach_ok())]
        code, out, err = self.run_cli("attach", "--accept-new-instance")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("error: usage", err)
        self.assertEqual(self.client.script, [json_response(attach_ok())])  # unused
        _, helptext, _ = self.run_cli("attach", "-h")
        self.assertNotIn("accept", helptext.lower())
        self.assertNotIn("instance", helptext.lower())

    def test_list_prints_short_ids_safe_titles_and_completeness(self):
        self.client.script = [self.listing()]
        code, out, err = self.run_cli("list")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("5555aaaa", out)
        self.assertNotIn(FULL_ID, out)  # only the short label is printed
        self.assertIn("idle", out)
        self.assertIn("working", out)
        self.assertNotIn("\x1b", out)
        self.assertIn("complete", out.lower())
        self.assertNoSecrets(out, err)
        self.client.script = [lpage([srow("s1")], "c1")] + [
            lpage([srow(f"s{i}")], f"c{i}") for i in range(2, 6)
        ]
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("INCOMPLETE", out)

    def test_list_failure_is_a_fixed_code(self):
        self.client.script = [json_response({"error": "x"}, 503)]
        code, out, err = self.run_cli("list")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("error: companion_observation_busy", err)

    def test_bind_records_the_exact_listed_session_and_sends_nothing(self):
        before = len(self.client.calls)
        self.client.script = [self.listing()]
        code, out, err = self.run_cli("bind", FULL_ID)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("5555aaaa", out)
        self.assertIn("Nothing was sent", out)
        self.assertNotIn("\x1b", out)
        new_calls = self.client.calls[before:]
        self.assertEqual(
            [(c["route"].method, c["route"].path) for c in new_calls],
            [("GET", "/api/companion")],
        )
        with Journal(self.cfg / "journal") as journal:
            bridge = Bridge(
                journal, mock.Mock(), clock=self.clock, new_id=IdFactory(500)
            )
            self.assertTrue(bridge.status()["bound"])
            raw = journal.get("binding.json")
        assert raw is not None
        self.assertNotIn(TOKEN, repr(raw))
        self.assertEqual(raw["binding"]["session_id"], FULL_ID)
        self.assertEqual(raw["binding"]["credential_ref"], CRED_REF)
        self.assertEqual(raw["binding"]["client_id"], CLIENT_ID)
        self.assertEqual(raw["binding"]["host_id"], HOST_IDENTITY)
        self.assertEqual(raw["binding"]["host_instance_id"], INSTANCE)

    HOSTILE_STATUSES = (
        "\x1b[31mred",
        "\x1b]0;owned\x07",
        "work\ning",
        "\x9b31m",
        "\u202eevil",
        "idle\x1b[0m",
        "IDLE",
        "idle ",
        "x" * 5000,
    )

    def assertPlainOutput(self, text: str) -> None:
        for ch in text:
            self.assertTrue(
                ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf"),
                repr(ch),
            )
        self.assertLess(len(text), 2000)

    def test_list_prints_only_a_fixed_label_for_a_hostile_status(self):
        for status in self.HOSTILE_STATUSES:
            with self.subTest(status=status[:12]):
                self.client.script = [
                    self.listing(
                        [srow(FULL_ID, status=status), srow("s-ok", "working")]
                    )
                ]
                code, out, err = self.run_cli("list")
                self.assertEqual((code, err), (0, ""))
                self.assertPlainOutput(out)
                row = next(ln for ln in out.splitlines() if "5555aaaa" in ln)
                self.assertEqual(row.split()[1], "other")
                self.assertIn("working", out)  # known statuses stay readable

    def test_bind_prints_only_a_fixed_label_for_a_hostile_status(self):
        for status in self.HOSTILE_STATUSES:
            with self.subTest(status=status[:12]):
                self.client.script = [self.listing([srow(FULL_ID, status=status)])]
                code, out, err = self.run_cli("bind", FULL_ID)
                self.assertEqual((code, err), (0, ""))
                self.assertPlainOutput(out)
                self.assertNotIn(status, out)
                self.assertIn("Status: other.", out)

    def test_every_known_status_is_printed_as_itself(self):
        known = (
            "idle",
            "working",
            "running",
            "stopping",
            "error",
            "stopped",
            "interrupted",
            "unknown",
        )
        rows = [srow(f"s{i}", status=name) for i, name in enumerate(known)]
        self.client.script = [self.listing(rows)]
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        for name in known:
            self.assertIn(name, out)
        self.assertNotIn("other", out)

    def test_bind_a_busy_session_does_not_infer_anything_from_idle(self):
        self.client.script = [
            self.listing([srow(FULL_ID, status="working", execution=True)])
        ]
        code, out, _ = self.run_cli("bind", FULL_ID)
        self.assertEqual(code, 0)
        self.assertIn("working", out)

    def test_bind_refuses_unlisted_incomplete_or_malformed_ids(self):
        self.client.script = [self.listing([srow("sess-other-9")])]
        code, _, err = self.run_cli("bind", FULL_ID)
        self.assertEqual(code, 1)
        self.assertIn("error: session_not_listed", err)
        self.client.script = [lpage([srow("s1")], "c1")] + [
            lpage([srow(f"s{i}")], f"c{i}") for i in range(2, 6)
        ]
        code, _, err = self.run_cli("bind", "s1")
        self.assertEqual(code, 1)
        self.assertIn("error: list_incomplete", err)
        for bad in ("bad id", "a/b", "", ".."):
            code, out, err = self.run_cli("bind", bad)
            self.assertEqual(code, 2)
        # a token pasted as an id is still never echoed
        self.client.script = [self.listing([srow("sess-other-9")])]
        code, out, err = self.run_cli("bind", TOKEN)
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, out + err)
        self.assertFalse((self.cfg / "journal").exists())

    def test_bind_prefix_or_short_id_is_not_accepted(self):
        for partial in (FULL_ID[:8], "5555aaaa", FULL_ID[:-1]):
            self.client.script = [self.listing()]
            code, _, err = self.run_cli("bind", partial)
            self.assertEqual(code, 1)
            self.assertIn("session_not_listed", err)

    def test_bind_does_not_arm_an_existing_journal(self):
        self.client.script = [self.listing()]
        self.assertEqual(self.run_cli("bind", FULL_ID)[0], 0)
        self.client.script = [self.listing()]
        self.assertEqual(self.run_cli("bind", FULL_ID)[0], 0)
        with Journal(self.cfg / "journal") as journal:
            bridge = Bridge(
                journal, mock.Mock(), clock=self.clock, new_id=IdFactory(900)
            )
            self.assertTrue(
                bridge.status()["stopped"]
            )  # disarmed until a human resumes

    def test_commands_that_need_an_attachment_say_so(self):
        for command in (["list"], ["bind", FULL_ID]):
            fresh = self.base / ("fresh" + command[0])
            client = FakeClient()
            code, _, err = self.run_cli(*command, root=fresh, client=client)
            self.assertEqual(code, 1)
            self.assertIn("error: auth_no_credential", err)
            self.assertEqual(client.calls, [])

    def test_multiple_credentials_require_an_explicit_choice(self):
        other = "cd" * 16
        with auth.CredentialStore(self.cfg / "auth") as store:
            store.save_credential(
                auth.Credential(
                    "device-" + other,
                    ORIGIN_URL,
                    other,
                    "amc_" + other + "." + "S" * 64,
                )
            )
        code, _, err = self.run_cli("list")
        self.assertEqual(code, 1)
        self.assertIn("error: credential_required", err)
        self.client.script = [self.listing()]
        self.assertEqual(self.run_cli("list", "--credential", CRED_REF)[0], 0)
        code, _, err = self.run_cli("list", "--credential", "bogus")
        self.assertEqual(code, 2)


class AllWorkListTest(CliCase):
    def setUp(self):
        super().setUp()
        self.pair_and_attach()  # synthetic store/HTTP only
        self.client.calls.clear()

    def test_explicit_catalog_prints_dormant_unpinned_rows_and_never_mutates_journal(self):
        self.client.script = [
            catalog_bootstrap(),
            cpage([srow(FULL_ID, title="Dormant\u202e\x1b\n" + "x" * 150)]),
        ]
        before = {p.name: p.read_bytes() for p in (self.cfg / "auth").iterdir()}
        with (
            mock.patch.object(cli, "Journal", side_effect=AssertionError("opened journal")),
            mock.patch.object(cli, "Bridge", side_effect=AssertionError("opened core")),
            mock.patch.object(auth, "attach", side_effect=AssertionError("reattached")),
        ):
            code, out, err = self.run_cli("list", "--all-work")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("All-work sessions (scope=roots; activity=all; pinned=all): complete", out)
        self.assertIn("5555aaaa", out)
        self.assertNotIn(FULL_ID, out)
        self.assertIn("root", out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\u202e", out)
        self.assertLess(len(out), 300)
        self.assertNotIn("workspace", out.lower())  # no invented paths or grouping
        self.assertNotIn("pin the session", out.lower())
        self.assertNoSecrets(out, err)
        self.assertFalse((self.cfg / "journal").exists())
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.cfg / "auth").iterdir()})
        self.assertEqual(
            [(c["route"].method, c["route"].path) for c in self.client.calls],
            [("GET", "/api/companion"), ("GET", "/api/sessions")],
        )

    def test_scope_all_and_delegates_are_explicit(self):
        for scope in ("all", "delegates"):
            with self.subTest(scope=scope):
                self.client.script = [
                    catalog_bootstrap(), cpage([{**srow("delegate-12345678"), "role": "delegate"}])
                ]
                code, out, err = self.run_cli("list", "--all-work", "--scope", scope)
                self.assertEqual((code, err), (0, ""))
                self.assertIn("scope=" + scope, out)
                self.assertIn("delegate", out)
                self.assertEqual(dict(self.client.calls[-1]["route"].query)["scope"], scope)

    def test_cap_is_visible_incomplete_not_a_pin_requirement(self):
        self.client.script = [catalog_bootstrap()] + [cpage([], f"c{i}") for i in range(5)]
        code, out, err = self.run_cli("list", "--all-work")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("INCOMPLETE (cap reached)", out)
        self.assertIn("5 pages", out)
        self.assertNotIn("Pin", out + err)
        self.assertFalse((self.cfg / "journal").exists())

    def test_later_409_exposes_no_partial_catalog(self):
        self.client.script = [
            catalog_bootstrap(), cpage([srow(FULL_ID)], "c1"),
            transport.Response(409, b"SECRET-DETAIL"),
        ]
        code, out, err = self.run_cli("list", "--all-work")
        self.assertEqual((code, out, err), (1, "", "error: companion_snapshot_changed\n"))
        self.assertEqual(len(self.client.calls), 3)

    def test_unauthorized_or_unsupported_resource_has_no_fallback_or_auth_post(self):
        for status, code in ((401, "companion_unauthorized"), (403, "companion_unauthorized"),
                             (404, "companion_observation_unsupported"),
                             (503, "companion_observation_busy")):
            with self.subTest(status=status):
                self.client.calls.clear()
                self.client.script = [catalog_bootstrap(), transport.Response(status, b"SECRET")]
                result, out, err = self.run_cli("list", "--all-work")
                self.assertEqual((result, out), (1, ""))
                self.assertIn("error: " + code, err)
                self.assertNotIn("SECRET", err)
                self.assertNotIn("Pin", err)
                self.assertEqual(len(self.client.calls), 2)
                self.assertTrue(all(c["route"].method == "GET" for c in self.client.calls))
        self.assertFalse((self.cfg / "journal").exists())

    def test_all_work_options_do_not_silently_change_other_commands(self):
        for argv in (
            ("list", "--scope", "all"),
            ("list", "--all-work", "--scope", "all&workspace=/secret"),
            ("list", "--all-work", "--scope", CANARY),
            ("bind", FULL_ID, "--all-work"), ("attach", "--all-work"),
        ):
            with self.subTest(argv=argv[:2]):
                code, out, err = self.run_cli(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("error: usage", err)
                self.assertNoSecrets(out, err)
        self.assertEqual(self.client.calls, [])
        self.client.script = [lpage([srow("default-list")])]
        code, out, err = self.run_cli("list")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("Sessions: complete; revision 10;"))
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.calls[0]["route"].target, "/api/companion")

    def test_catalog_leaves_existing_binding_and_disarmed_state_unchanged(self):
        self.client.script = [lpage([srow(FULL_ID)])]
        self.assertEqual(self.run_cli("bind", FULL_ID)[0], 0)
        before = {p.name: p.read_bytes() for p in (self.cfg / "journal").iterdir()}
        self.client.calls.clear()
        self.client.script = [catalog_bootstrap(), cpage([srow("different-chat")])]
        with mock.patch.object(cli, "Journal", side_effect=AssertionError("opened journal")):
            self.assertEqual(self.run_cli("list", "--all-work")[0], 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.cfg / "journal").iterdir()})
        self.assertTrue(all(c["route"].method == "GET" for c in self.client.calls))

    def test_list_help_describes_opt_in_without_accessing_store_or_network(self):
        with mock.patch.object(auth, "CredentialStore", side_effect=AssertionError("store")):
            code, out, err = self.run_cli("list", "--help")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("--all-work", out)
        self.assertIn("--scope", out)
        self.assertIn("no binding", out)
        self.assertEqual(self.client.calls, [])


class DefaultRootTest(CliCase):
    def test_default_root_comes_from_the_injected_environment_only(self):
        xdg = self.base / "xdg"
        xdg.mkdir(mode=0o700)
        self.client.script = [json_response(pair_ok())]
        with mock.patch.dict(os.environ, {"HOME": "/should/not/be/used"}):
            code, _, _ = self.run_cli(
                "pair-start",
                "--origin",
                ORIGIN_URL,
                env={"XDG_CONFIG_HOME": str(xdg)},
                root=None,
            )
        self.assertEqual(code, 0)
        self.assertTrue(
            (xdg / "amplifier-unified-pet-bridge" / "auth" / "attempt.json").exists()
        )

    def test_unusable_environment_fails_with_a_fixed_code(self):
        code, out, err = self.run_cli("list", env={}, root=None)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("error: auth_root_invalid", err)

    def test_config_root_is_not_created_under_a_missing_parent(self):
        code, _, err = self.run_cli("list", root=self.base / "missing" / "cfg")
        self.assertEqual(code, 1)
        self.assertIn("error: auth_root_invalid", err)
        self.assertFalse((self.base / "missing").exists())

    def test_looser_config_root_is_refused(self):
        self.cfg.mkdir()
        os.chmod(self.cfg, 0o755)
        code, _, err = self.run_cli("list")
        self.assertEqual(code, 1)
        self.assertIn("error: auth_root_invalid", err)


class DefaultClientFactoryTest(CliCase):
    """With no injected factory the CLI builds the real ``HttpsClient`` (10 s)."""

    def test_the_default_factory_is_the_https_client_with_a_ten_second_timeout(self):
        real = transport.HttpsClient
        made: list[Any] = []

        class Stubbed(real):  # real constructor, but request() never leaves memory
            def request(self, origin, route, *, headers=None, body=b""):
                self.requested = getattr(self, "requested", 0) + 1
                return json_response(pair_ok())

        def factory(*args: Any, **kwargs: Any) -> Any:
            client = Stubbed(*args, **kwargs)
            made.append(client)
            return client

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(transport, "HttpsClient", factory):
            code = cli.main(
                ["pair-start", "--origin", ORIGIN_URL],
                env={},
                root=self.cfg,
                stdout=out,
                stderr=err,
                clock=self.clock,
                new_id=IdFactory(),
            )
        self.assertEqual(len(made), 1, "the default factory was not used once")
        self.assertEqual(made[0]._timeout, 10.0)
        self.assertEqual(made[0].requested, 1)
        self.assertEqual((code, err.getvalue()), (0, ""))


class CliOutputGuardTest(CliCase):
    def test_raised_errors_have_no_argv_in_repr(self):
        exc = raised_any(
            cli.main,
            ["list", CANARY],
            env={},
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertIsNone(exc)  # returns an exit code, never raises


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
