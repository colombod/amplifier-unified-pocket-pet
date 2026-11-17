"""Offline tests for the private ``.env`` spelling of the gateway config.

Synthetic values only; no listener, dial, provider, Unified call or real key.  These
are offline evidence: not a running service and not a second machine.

Also here, because both config spellings share them: the private-file reader's
changed-during-read refusal, the flushed startup lines, and the documented no-overwrite
copy command (run in real shells against temporary synthetic files only).
"""

# pyright: reportArgumentType=false, reportOptionalMemberAccess=false, reportPrivateUsage=false
from __future__ import annotations

import dataclasses
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any
from unittest import mock

from pet_bridge import cli
from pet_bridge import gateway_server as gsv
from pet_bridge import gateway_state as gs
from pet_bridge.gateway_state import GatewayProtocolError

from .fakes import OfflineTestCase
from .gatewayfakes import KEY_CANARY, FakeLoopClock
from .test_gateway_server import (
    ServerOfflineCase,
    Spies,
    StateFixture,
    config_doc,
    runner_threads,
)

BRIDGE = Path(gsv.__file__).resolve().parents[1]
EXAMPLE = BRIDGE / ".env.example"
UNIT = BRIDGE / "deploy" / "pocket-gateway.service"
DEPLOY_README = BRIDGE / "deploy" / "README.md"
GUIDE = BRIDGE.parent / "docs" / "team-prototype.md"

CANARY = "ENVCANARY-Zq7-do-not-print"
INLINE = "POCKET_GATEWAY_PROVIDER_KEY"
# Pinned independently of the production table: a renamed variable must fail here.
JSON_TO_ENV = {
    "version": "POCKET_GATEWAY_CONFIG_VERSION",
    "state_root": "POCKET_GATEWAY_STATE_ROOT",
    "auth_root": "POCKET_GATEWAY_AUTH_ROOT",
    "credential_ref": "POCKET_GATEWAY_CREDENTIAL_REF",
    "session_id": "POCKET_GATEWAY_SESSION_ID",
    "session_label": "POCKET_GATEWAY_SESSION_LABEL",
    "bind_host": "POCKET_GATEWAY_BIND_HOST",
    "port": "POCKET_GATEWAY_PORT",
    "host_header": "POCKET_GATEWAY_HOST_HEADER",
    "tailnet_bind_host": "POCKET_GATEWAY_TAILNET_BIND_HOST",
    "tailnet_fqdn": "POCKET_GATEWAY_TAILNET_FQDN",
    "tls_cert_file": "POCKET_GATEWAY_TLS_CERT_FILE",
    "tls_key_file": "POCKET_GATEWAY_TLS_KEY_FILE",
    "pet_token_sha256": "POCKET_GATEWAY_PET_TOKEN_SHA256",
    "model": "POCKET_GATEWAY_MODEL",
    "voice": "POCKET_GATEWAY_VOICE",
    "provider_key_file": "POCKET_GATEWAY_PROVIDER_KEY_FILE",
}
OTHER_HASH = "c" * 64


def env_text(doc: dict[str, Any], *, inline_key: str | None = None) -> str:
    """The ``.env`` spelling of a JSON config document (test-side, explicit names)."""
    lines = ["# synthetic test settings", ""]
    for key, value in doc.items():
        if key == "provider_key_env":
            continue
        text = ",".join(value) if key == "pet_token_sha256" else str(value)
        lines.append(f"{JSON_TO_ENV[key]}={text}")
    if inline_key is not None:
        lines.append(f"{INLINE}={inline_key}")
    return "\n".join(lines) + "\n"


def full_doc(base: Path, **changes: Any) -> dict[str, Any]:
    """Every optional field set, so the field-for-field comparison covers them all."""
    return config_doc(
        base,
        auth_root=str(base / "shared-auth"),
        tailnet_bind_host="100.64.12.34",
        tailnet_fqdn="gateway.example.test",
        model="gpt-live-2",
        voice="cedar",
        pet_token_sha256=[config_doc(base)["pet_token_sha256"][0], OTHER_HASH],
        **changes,
    )


def replace_line(text: str, name: str, new: str | None) -> str:
    """Replace (or drop, with ``None``) the one line assigning ``name``."""
    lines = text.split("\n")
    hits = [i for i, line in enumerate(lines) if line.startswith(name + "=")]
    assert len(hits) == 1, name
    if new is None:
        del lines[hits[0]]
    else:
        lines[hits[0]] = new
    return "\n".join(lines)


def fields_of(cfg: gs.GatewayConfig) -> dict[str, Any]:
    return {f.name: getattr(cfg, f.name) for f in dataclasses.fields(cfg)}


# =================================================================================
# The pure translation: text -> the document parse_config validates
# =================================================================================
class EnvDocumentTest(OfflineTestCase):
    base = Path("/synthetic")

    def test_the_variable_names_are_exactly_the_config_keys_plus_the_inline_key(self):
        self.assertEqual(gs.ENV_PROVIDER_KEY, INLINE)
        self.assertEqual(gs.ENV_CONFIG_NAMES, frozenset(JSON_TO_ENV.values()) | {INLINE})
        # every JSON config key has a variable; the only other JSON key names the inline one
        self.assertEqual(
            set(JSON_TO_ENV) | {"provider_key_env"},
            set(gs._CONFIG_REQUIRED | gs._CONFIG_OPTIONAL),
        )

    def test_the_document_is_the_json_document_and_the_key_is_returned_apart(self):
        doc = full_doc(self.base)
        built, inline = gs.env_config_document(env_text(doc))
        self.assertEqual(built, doc)  # same keys, same types, same values
        self.assertIsNone(inline)
        self.assertIs(type(built["port"]), int)
        self.assertIs(type(built["version"]), int)

        keyed = full_doc(self.base, provider_key_file=..., provider_key_env=INLINE)
        built, inline = gs.env_config_document(env_text(keyed, inline_key=KEY_CANARY))
        self.assertEqual(built, keyed)
        self.assertEqual(inline, KEY_CANARY)
        self.assertNotIn(KEY_CANARY, repr(built))  # the document names the variable only

    def test_comments_blank_lines_and_inner_spaces_are_accepted(self):
        doc = config_doc(self.base, session_label="Pocket assistant two")
        text = "\n   \n# a comment = with an equals sign\n" + env_text(doc) + "\n\n"
        self.assertEqual(gs.env_config_document(text)[0], doc)
        self.assertEqual(gs.env_config_document(env_text(doc).rstrip("\n"))[0], doc)

    def test_every_malformed_text_is_config_invalid_and_quotes_nothing(self):
        good = env_text(config_doc(self.base))
        port = "POCKET_GATEWAY_PORT"
        label = "POCKET_GATEWAY_SESSION_LABEL"
        cases = {
            "unknown name": good + f"POCKET_GATEWAY_EXTRA={CANARY}\n",
            "unprefixed name": good + f"PORT={CANARY}\n",
            "json key as name": good + f"bind_host={CANARY}\n",
            "duplicate name": good + f"{label}={CANARY}\n",
            "duplicate identical": good + good.split("\n")[2] + "\n",
            "empty value": replace_line(good, label, f"{label}="),
            "no equals": good + f"{CANARY}\n",
            "export prefix": replace_line(good, label, f"export {label}={CANARY}"),
            "space before equals": replace_line(good, label, f"{label} ={CANARY}"),
            "space after equals": replace_line(good, label, f"{label}= {CANARY}"),
            "trailing space": replace_line(good, label, f"{label}={CANARY} "),
            "double quoted": replace_line(good, label, f'{label}="{CANARY}"'),
            "single quoted": replace_line(good, label, f"{label}='{CANARY}'"),
            "inline comment": replace_line(good, label, f"{label}={CANARY} # note"),
            "indented entry": replace_line(good, label, f"  {label}={CANARY}"),
            "indented comment": good + f"  # {CANARY}\n",
            "crlf": good.replace("\n", "\r\n"),
            "tab": replace_line(good, label, f"{label}={CANARY}\tx"),
            "nul": replace_line(good, label, f"{label}={CANARY}\x00"),
            "bom": "\ufeff" + good,
            "lowercase name": good + f"pocket_gateway_model={CANARY}\n",
            "port not a number": replace_line(good, port, f"{port}={CANARY}"),
            "port leading zero": replace_line(good, port, f"{port}=08941"),
            "port signed": replace_line(good, port, f"{port}=+8941"),
            "port float": replace_line(good, port, f"{port}=8941.0"),
            "port too long": replace_line(good, port, f"{port}=894100"),
            "version not a number": replace_line(
                good, "POCKET_GATEWAY_CONFIG_VERSION",
                f"POCKET_GATEWAY_CONFIG_VERSION={CANARY}"),
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(GatewayProtocolError) as ctx:
                    gs.env_config_document(text)
                err = ctx.exception
                self.assertEqual((err.code, str(err), err.args), (
                    "config_invalid", "config_invalid", ("config_invalid",)))
                self.assertNotIn("CANARY", repr(err))
                self.assertIsNone(err.__cause__)
        for bad in (None, b"POCKET_GATEWAY_PORT=8941\n", 1, ["x"], {"a": 1}):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(GatewayProtocolError) as ctx:
                    gs.env_config_document(bad)
                self.assertEqual(ctx.exception.code, "config_invalid")

    def test_well_formed_but_wrong_values_are_refused_by_the_one_validator(self):
        # The translation accepts these lines; parse_config (the JSON validator) refuses
        # them.  There is no second, weaker check on the .env path.
        good = env_text(config_doc(self.base))
        cases = {
            "wildcard bind": ("POCKET_GATEWAY_BIND_HOST", "0.0.0.0"),
            "loopback bind": ("POCKET_GATEWAY_BIND_HOST", "127.0.0.1"),
            "public bind": ("POCKET_GATEWAY_BIND_HOST", "8.8.8.8"),
            "low port": ("POCKET_GATEWAY_PORT", "443"),
            "high port": ("POCKET_GATEWAY_PORT", "65536"),
            "version two": ("POCKET_GATEWAY_CONFIG_VERSION", "2"),
            "relative state": ("POCKET_GATEWAY_STATE_ROOT", "relative/state"),
            "dotted path": ("POCKET_GATEWAY_TLS_KEY_FILE", "/a/../key.pem"),
            "bad credential": ("POCKET_GATEWAY_CREDENTIAL_REF", "device-xyz"),
            "short hash": ("POCKET_GATEWAY_PET_TOKEN_SHA256", "ab" * 31),
            "uppercase hash": ("POCKET_GATEWAY_PET_TOKEN_SHA256", "AB" * 32),
            "repeated hash": ("POCKET_GATEWAY_PET_TOKEN_SHA256", OTHER_HASH + "," + OTHER_HASH),
            "spaced hashes": ("POCKET_GATEWAY_PET_TOKEN_SHA256", OTHER_HASH + ", " + "d" * 64),
            "empty hash item": ("POCKET_GATEWAY_PET_TOKEN_SHA256", OTHER_HASH + ","),
            "host header fqdn": ("POCKET_GATEWAY_HOST_HEADER", "gateway.example.test:8941"),
            "long label": ("POCKET_GATEWAY_SESSION_LABEL", "x" * 41),
            "relative key file": ("POCKET_GATEWAY_PROVIDER_KEY_FILE", "provider.key"),
        }
        for name, (variable, value) in cases.items():
            with self.subTest(case=name):
                doc, _ = gs.env_config_document(
                    replace_line(good, variable, f"{variable}={value}"))
                with self.assertRaises(GatewayProtocolError) as ctx:
                    gs.parse_config(doc)
                self.assertEqual(str(ctx.exception), "config_invalid")


# =================================================================================
# load_env_config: private file + the same validation and codes as load_config
# =================================================================================
class LoadEnvConfigTest(ServerOfflineCase):
    def put(self, text: str | bytes, mode: int = 0o600, name: str = "gw.env") -> Path:
        return self.write(name, text, mode)

    def refused(self, path: Path) -> gsv.StartupError:
        with self.assertRaises(gsv.StartupError) as ctx:
            gsv.load_env_config(path)
        err = ctx.exception
        self.assertEqual(str(err), err.code)
        self.assertEqual(err.args, (err.code,))
        self.assertNotIn("CANARY", repr(err) + str(err))
        self.assertIsNone(err.__cause__)
        self.assertIsNone(err.__context__)
        return err

    def test_the_env_config_equals_the_json_config_field_for_field(self):
        for name, doc in {
            "required only": config_doc(self.base),
            "every optional field": full_doc(self.base),
        }.items():
            with self.subTest(case=name):
                from_json = gsv.load_config(
                    self.write(f"{name[:3]}.json", json.dumps(doc)))
                from_env, inline = gsv.load_env_config(
                    self.put(env_text(doc), name=f"{name[:3]}.env"))
                self.assertIsNone(inline)
                self.assertEqual(fields_of(from_env), fields_of(from_json))
                self.assertEqual(from_env, from_json)
                self.assertEqual(repr(from_env), repr(from_json))
                self.assertEqual(
                    {f.name for f in dataclasses.fields(from_env)},
                    (set(doc) - {"version"}) | {
                        "session_id_short", "model", "voice", "provider_key_file",
                        "provider_key_env", "auth_root", "tailnet_bind_host",
                        "tailnet_fqdn"},
                )

    def test_the_inline_key_equals_the_json_named_variable_and_is_a_hidden_secret(self):
        doc = full_doc(self.base, provider_key_file=..., provider_key_env=INLINE)
        from_json = gsv.load_config(self.write("k.json", json.dumps(doc)))
        from_env, inline = gsv.load_env_config(
            self.put(env_text(doc, inline_key=KEY_CANARY)))
        self.assertEqual(fields_of(from_env), fields_of(from_json))
        self.assertIsInstance(inline, gsv.Secret)
        self.assertEqual(inline.reveal(), KEY_CANARY)
        for text in (repr(inline), str(inline), f"{inline}", repr(from_env), str(from_env),
                     repr((from_env, inline))):
            self.assertNotIn(KEY_CANARY, text)
        self.assertIsNone(from_env.provider_key_file)
        # the same provider-key loader accepts it, from the file's own value only
        self.assertEqual(
            gsv._env_file_provider_key(from_env, inline).reveal(), KEY_CANARY)

    def test_every_content_failure_is_one_fixed_startup_code(self):
        good = env_text(config_doc(self.base))
        label = "POCKET_GATEWAY_SESSION_LABEL"
        key_file = "POCKET_GATEWAY_PROVIDER_KEY_FILE"
        cases: dict[str, tuple[str | bytes, str]] = {
            "unknown": (good + f"POCKET_GATEWAY_EXTRA={CANARY}\n", "config_invalid"),
            "duplicated": (good + f"{label}={CANARY}\n", "config_invalid"),
            "malformed": (good + f"{CANARY}\n", "config_invalid"),
            "quoted": (replace_line(good, label, f'{label}="{CANARY}"'), "config_invalid"),
            "empty file": ("", "config_invalid"),
            "comments only": (f"# {CANARY}\n", "config_invalid"),
            "both key sources": (good + f"{INLINE}={KEY_CANARY}\n", "config_invalid"),
            "no key source": (replace_line(good, key_file, None), "config_invalid"),
            "wildcard bind": (
                replace_line(good, "POCKET_GATEWAY_BIND_HOST",
                             "POCKET_GATEWAY_BIND_HOST=0.0.0.0"),
                "config_invalid",
            ),
            "tailnet half set": (
                good + "POCKET_GATEWAY_TAILNET_BIND_HOST=100.64.12.34\n",
                "config_invalid",
            ),
            "uppercase short8": (
                replace_line(good, "POCKET_GATEWAY_SESSION_ID",
                             "POCKET_GATEWAY_SESSION_ID=sess-fixture-0A1B2C3D"),
                "core_binding_required",
            ),
            "oversize": (good + "# " + "x" * 17000 + "\n", "config_invalid"),
            "bad utf8": (b"\xff\xfe" + good.encode(), "config_invalid"),
            "crlf": (good.replace("\n", "\r\n"), "config_invalid"),
            "json document": (json.dumps(config_doc(self.base)), "config_invalid"),
        }
        for required in sorted(gs._CONFIG_REQUIRED):
            cases[f"missing {required}"] = (
                replace_line(good, JSON_TO_ENV[required], None), "config_invalid")
        for index, (name, (text, code)) in enumerate(cases.items()):
            with self.subTest(case=name):
                err = self.refused(self.put(text, name=f"c{index}.env"))
                self.assertEqual(err.code, code)

    def test_the_env_file_must_be_a_private_regular_file_owned_by_the_user(self):
        text = env_text(config_doc(self.base))
        good = self.put(text, name="good.env")
        self.assertEqual(gsv.load_env_config(good)[0].port, 8941)
        link = self.base / "link.env"
        os.symlink(good, link)
        fifo = self.base / "fifo.env"
        os.mkfifo(fifo, 0o600)
        cases = {
            "symlink": link,
            "group readable": self.put(text, mode=0o640, name="g.env"),
            "other readable": self.put(text, mode=0o604, name="o.env"),
            "group writable": self.put(text, mode=0o620, name="w.env"),
            "world everything": self.put(text, mode=0o666, name="a.env"),
            "relative": Path("good.env"),
            "missing": self.base / "none.env",
            "directory": self.base,
            "fifo": fifo,
        }
        for name, path in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.refused(path).code, "config_invalid")
        for mode in (0o600, 0o400):
            with self.subTest(mode=oct(mode)):
                os.chmod(good, mode)
                self.assertEqual(gsv.load_env_config(good)[0].port, 8941)
        with mock.patch.object(gsv.os, "geteuid", return_value=os.geteuid() + 1):
            self.assertEqual(self.refused(good).code, "config_invalid")  # another owner

    def test_a_refused_file_is_never_parsed(self):
        loose = self.put(env_text(config_doc(self.base)), mode=0o644)
        with (
            mock.patch.object(gs, "env_config_document",
                              side_effect=AssertionError("parsed")) as translate,
            mock.patch.object(gs, "parse_config", side_effect=AssertionError("parsed")),
        ):
            self.assertEqual(self.refused(loose).code, "config_invalid")
        translate.assert_not_called()

    def test_the_env_path_validates_with_parse_config_itself(self):
        doc = full_doc(self.base)
        path = self.put(env_text(doc))
        with mock.patch.object(gs, "parse_config", wraps=gs.parse_config) as parse:
            gsv.load_env_config(path)
        parse.assert_called_once_with(doc)  # the very document the JSON path validates


# =================================================================================
# serve: the whole startup from the private .env file alone
# =================================================================================
class ServeEnvFileTest(ServerOfflineCase):
    def setUp(self) -> None:
        super().setUp()
        self.fx = StateFixture(self.base)
        self.json_path = self.fx.build()
        self.doc = config_doc(self.base)

    def serve(self, path: Path, *, env_file: bool = True, env: dict[str, str] | None = None,
              log: Path | None = None) -> tuple[int, str, str, Spies]:
        spies = Spies()
        out, err = io.StringIO(), io.StringIO()
        kwargs: dict[str, Any] = {"env_file": True} if env_file else {}
        code = gsv.serve_main(
            path, env=env or {}, stdout=out, stderr=err,
            deps=spies.deps(FakeLoopClock()), event_log_path=log, **kwargs,
        )
        self.assertEqual(runner_threads(), [])
        self.fx.stores_reopen()
        return code, out.getvalue(), err.getvalue(), spies

    def test_the_gateway_starts_from_the_env_file_alone_like_the_json_config(self):
        j_code, j_out, j_err, j_spies = self.serve(self.json_path, env_file=False)
        env_path = self.write("gw.env", env_text(self.doc))
        os.unlink(self.json_path)  # the .env file alone is enough
        e_code, e_out, e_err, e_spies = self.serve(env_path)
        self.assertEqual((e_code, e_err), (0, ""))
        self.assertEqual((e_code, e_out, e_err), (j_code, j_out, j_err))
        self.assertEqual(
            e_out.splitlines(),
            ["gateway: listening on 10.1.2.3:8941 for session ...0a1b2c3d",
             "gateway: stopped"],
        )
        self.assertEqual(e_spies.events, ["site", "wait", "closer"])
        self.assertEqual(e_spies.events, j_spies.events)
        self.assertEqual(e_spies.site_calls[0][1], j_spies.site_calls[0][1])  # same config
        self.assertEqual(e_spies.ssl_calls, j_spies.ssl_calls)
        self.assertEqual(e_spies.gateways[0]._key.reveal(), KEY_CANARY)  # from the key file
        self.assertEqual((e_spies.provider_calls, e_spies.client_calls), (0, 1))

    def test_an_inline_key_reaches_the_gateway_hidden_and_appears_in_no_output(self):
        os.unlink(self.base / "provider.key")
        doc = config_doc(self.base, provider_key_file=...)
        env_path = self.write("gw.env", env_text(doc, inline_key=KEY_CANARY))
        log = self.base / "events.jsonl"
        code, out, err, spies = self.serve(env_path, log=log)
        self.assertEqual((code, err), (0, ""))
        gateway = spies.gateways[0]
        self.assertIsInstance(gateway._key, gsv.Secret)
        self.assertEqual(gateway._key.reveal(), KEY_CANARY)
        events = log.read_text(encoding="utf-8")
        self.assertIn("gateway_start", events)
        for text in (out, err, events, repr(gateway._cfg), repr(gateway._key),
                     repr(spies.site_calls[0][1])):
            self.assertNotIn(KEY_CANARY, text)
        # The key, credential reference, token hash, full conversation id and paths reach
        # no stream or log.  The one line of startup metadata is the JSON path's own: the
        # listen address and the last eight characters of the conversation id.
        for private in (doc["credential_ref"], doc["session_id"], doc["pet_token_sha256"][0],
                        doc["state_root"], doc["tls_key_file"], doc["tls_cert_file"],
                        str(env_path), INLINE):
            for text in (out, err, events):
                self.assertNotIn(private, text)
        self.assertEqual(
            out.splitlines(),
            ["gateway: listening on 10.1.2.3:8941 for session ...0a1b2c3d",
             "gateway: stopped"],
        )

    def test_the_process_environment_is_never_a_key_source_for_an_env_file_start(self):
        other = "sk-" + "E" * 40
        inline_doc = config_doc(self.base, provider_key_file=...)
        inline_path = self.write("inline.env", env_text(inline_doc, inline_key=KEY_CANARY))
        code, _, err, spies = self.serve(inline_path, env={INLINE: other})
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(spies.gateways[0]._key.reveal(), KEY_CANARY)  # the file's value

        no_key = replace_line(env_text(self.doc), "POCKET_GATEWAY_PROVIDER_KEY_FILE", None)
        code, out, err, spies = self.serve(
            self.write("nokey.env", no_key), env={INLINE: other, "PROVIDER_KEY": other})
        self.assertEqual((code, out, err), (1, "", "error: config_invalid\n"))
        self.assertEqual(spies.site_calls, [])

        (self.base / "provider.key").unlink()  # the named key file is gone
        code, out, err, spies = self.serve(
            self.write("file.env", env_text(self.doc)), env={INLINE: other})
        self.assertEqual((code, out, err), (1, "", "error: provider_key_missing\n"))
        self.assertEqual(spies.site_calls, [])
        self.assertNotIn(other, out + err)

    def test_a_malformed_inline_key_is_the_fixed_provider_key_code(self):
        doc = config_doc(self.base, provider_key_file=...)
        path = self.write("short.env", env_text(doc, inline_key="short-" + CANARY[:6]))
        code, out, err, spies = self.serve(path)
        self.assertEqual((code, out, err), (1, "", "error: provider_key_missing\n"))
        self.assertEqual(spies.site_calls, [])

    def test_every_refusal_happens_before_listening_and_echoes_nothing(self):
        good = env_text(self.doc)
        label = "POCKET_GATEWAY_SESSION_LABEL"
        contents = {
            "unknown": good + f"POCKET_GATEWAY_EXTRA={CANARY}\n",
            "missing": replace_line(good, "POCKET_GATEWAY_BIND_HOST", None),
            "duplicated": good + f"{label}={CANARY}\n",
            "malformed": good + f"{CANARY}={CANARY}\n",
            "bad value": replace_line(good, "POCKET_GATEWAY_STATE_ROOT",
                                      f"POCKET_GATEWAY_STATE_ROOT={CANARY}"),
            "secret then junk": good + f"{INLINE}={KEY_CANARY}\n{CANARY}\n",
        }
        paths = {name: self.write(f"r{i}.env", text)
                 for i, (name, text) in enumerate(contents.items())}
        paths["group readable"] = self.write(
            "loose.env", good + f"# {CANARY}\n", mode=0o640)
        paths["missing file"] = self.base / (CANARY + ".env")
        paths["json given as env"] = self.json_path
        for index, (name, path) in enumerate(paths.items()):
            with self.subTest(case=name):
                log = self.base / f"events-{index}.jsonl"
                code, out, err, spies = self.serve(path, log=log)
                self.assertEqual((code, out, err), (1, "", "error: config_invalid\n"))
                self.assertEqual(spies.events, [])  # no site, no wait
                self.assertEqual((spies.site_calls, spies.ssl_calls), ([], []))
                self.assertEqual((spies.client_calls, spies.provider_calls), (0, 0))
                events = log.read_text(encoding="utf-8")
                rows = [json.loads(line) for line in events.splitlines()]
                self.assertEqual([row["event"] for row in rows],
                                 ["gateway_start", "gateway_stop"])
                self.assertEqual(rows[-1]["code"], "config_invalid")
                for text in (out, err, events):
                    self.assertNotIn("CANARY", text)
                    self.assertNotIn(KEY_CANARY, text)

    def test_an_env_file_given_as_json_config_is_refused_too(self):
        env_path = self.write("gw.env", env_text(self.doc))
        code, out, err, spies = self.serve(env_path, env_file=False)
        self.assertEqual((code, out, err), (1, "", "error: config_invalid\n"))
        self.assertEqual(spies.site_calls, [])

    def test_the_json_path_is_called_exactly_as_before(self):
        # serve_main's JSON call shape is unchanged: no new required argument.
        spies = Spies()
        out, err = io.StringIO(), io.StringIO()
        code = gsv.serve_main(self.json_path, env={}, stdout=out, stderr=err,
                              deps=spies.deps(FakeLoopClock()))
        self.assertEqual((code, err.getvalue()), (0, ""))
        self.assertEqual(spies.events, ["site", "wait", "closer"])
        with mock.patch.object(gsv, "load_env_config",
                               side_effect=AssertionError("env loader used")):
            self.assertEqual(self.serve(self.json_path, env_file=False)[0], 0)


# =================================================================================
# The startup lines are visible at once on a block-buffered (non-tty) stream
# =================================================================================
class PipeStream:
    """A text stream over a real pipe, buffered like Python's stdout under a service."""

    def __init__(self, case: Any) -> None:
        self._read_fd, write_fd = os.pipe()
        os.set_blocking(self._read_fd, False)
        self.stream = open(write_fd, "w", buffering=8192, encoding="utf-8")  # noqa: SIM115
        case.addCleanup(os.close, self._read_fd)
        case.addCleanup(self.close)
        case.assertFalse(self.stream.isatty())
        case.assertFalse(self.stream.line_buffering)
        # the premise: an unflushed line does NOT reach the reader of this stream
        self.stream.write("premise: buffered\n")
        case.assertEqual(self.arrived(), "")
        self.stream.flush()
        case.assertEqual(self.arrived(), "premise: buffered\n")

    def arrived(self) -> str:
        """Everything the other end of the pipe can read right now."""
        data = b""
        while True:
            try:
                chunk = os.read(self._read_fd, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            data += chunk
        return data.decode("utf-8")

    def close(self) -> None:
        if not self.stream.closed:
            self.stream.close()


class StartupLineFlushTest(ServerOfflineCase):
    LISTENING = "gateway: listening on 10.1.2.3:8941 for session ...0a1b2c3d\n"

    def setUp(self) -> None:
        super().setUp()
        self.fx = StateFixture(self.base)

    def run_gateway(self, path: Path, stdout: Any, *, env_file: bool,
                    retrying: bool = False) -> tuple[int, list[str], str]:
        """Serve once; return (exit code, what a reader saw WHILE it ran, stderr)."""
        spies = Spies()
        deps = spies.deps(FakeLoopClock())
        seen: list[str] = []

        async def wait() -> None:  # the gateway is up and waiting for a signal
            seen.append(stdout.arrived() if isinstance(stdout, PipeStream) else "")

        deps.wait_for_shutdown = wait
        if retrying:
            async def close() -> None:
                return None

            async def start_site(_handler: Any, _cfg: Any, _ctx: Any) -> Any:
                handle = gsv._SiteHandle(close, lambda: None)
                handle.tailnet_state = "retrying"
                return handle

            deps.start_site = start_site
        err = io.StringIO()
        code = gsv.serve_main(
            path, env={}, stderr=err, deps=deps, env_file=env_file,
            stdout=stdout.stream if isinstance(stdout, PipeStream) else stdout,
        )
        self.assertEqual(runner_threads(), [])
        self.fx.stores_reopen()
        return code, seen, err.getvalue()

    def test_a_real_interpreter_does_not_line_buffer_stdout_on_a_pipe(self):
        # Why the flush matters: without PYTHONUNBUFFERED (the unit does not set it), a
        # service's stdout is a pipe and Python holds printed lines until the buffer fills.
        result = subprocess.run(
            [sys.executable, "-B", "-c",
             "import sys; print(sys.stdout.isatty(), sys.stdout.line_buffering)"],
            env={"PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True,
            timeout=10, check=False,
        )
        self.assertEqual((result.returncode, result.stdout), (0, "False False\n"))

    def test_the_listening_line_reaches_a_pipe_reader_while_the_gateway_runs(self):
        json_path = self.fx.build()
        env_path = self.write("gw.env", env_text(config_doc(self.base)))
        for name, path, env_file in (("json", json_path, False), ("env", env_path, True)):
            with self.subTest(config=name):
                pipe = PipeStream(self)
                code, seen, err = self.run_gateway(path, pipe, env_file=env_file)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(seen, [self.LISTENING])  # before any shutdown or exit
                pipe.close()
                self.assertEqual(pipe.arrived(), "gateway: stopped\n")

    def test_the_tailnet_retrying_line_is_flushed_with_it(self):
        path = self.fx.build(cfg={"tailnet_bind_host": "100.64.12.34",
                                  "tailnet_fqdn": "gateway.example.test"})
        pipe = PipeStream(self)
        code, seen, err = self.run_gateway(path, pipe, env_file=False, retrying=True)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            seen, [self.LISTENING + "gateway: Tailnet address unavailable; retrying\n"])

    def test_a_failing_flush_never_stops_a_listening_gateway(self):
        class Unflushable(io.StringIO):
            def flush(self) -> None:
                raise BrokenPipeError("synthetic")

        out = Unflushable()
        code, seen, err = self.run_gateway(self.fx.build(), out, env_file=False)
        self.assertEqual((code, err, seen), (0, "", [""]))  # it reached the wait
        self.assertEqual(out.getvalue(), self.LISTENING + "gateway: stopped\n")


# =================================================================================
# A private file that changes while it is read is refused (JSON, .env and key file)
# =================================================================================
class ChangedDuringReadTest(ServerOfflineCase):
    """The change happens after the file is opened and checked, before its bytes are read.

    A before/after comparison, not a lock: only the changes exercised here are shown to
    be refused.
    """

    OLD_NS = 1_600_000_000 * 10**9  # a fixed old mtime, so a rewrite always moves it
    CHANGES = ("truncated to nothing", "truncated to a prefix", "appended to",
               "rewritten at the same size", "permissions changed")

    def during_read(self, action: Any) -> Any:
        real_read = os.read
        fired: list[int] = []

        def read(fd: int, count: int) -> bytes:
            if not fired:
                fired.append(1)
                action()
            return real_read(fd, count)

        return mock.patch.object(gsv.os, "read", side_effect=read)

    def change(self, name: str, path: Path) -> Any:
        size = os.stat(path).st_size

        def rewrite_same_size() -> None:
            with open(path, "r+b") as handle:
                handle.write(b"#" * size)

        def append() -> None:
            with open(path, "ab") as handle:
                handle.write(b"# appended\n")

        return {
            "truncated to nothing": lambda: os.truncate(path, 0),
            "truncated to a prefix": lambda: os.truncate(path, size // 2),
            "appended to": append,
            "rewritten at the same size": rewrite_same_size,
            "permissions changed": lambda: os.chmod(path, 0o400),
        }[name]

    def fresh(self, name: str, text: str | bytes) -> Path:
        path = self.write(name, text)
        os.utime(path, ns=(self.OLD_NS, self.OLD_NS))
        return path

    def test_every_exercised_change_is_refused_by_all_three_readers(self):
        json_text = json.dumps(config_doc(self.base))
        env = env_text(config_doc(self.base))
        for index, name in enumerate(self.CHANGES):
            with self.subTest(change=name, reader="json config"):
                path = self.fresh(f"j{index}.json", json_text)
                with self.during_read(self.change(name, path)):
                    with self.assertRaises(gsv.StartupError) as ctx:
                        gsv.load_config(path)
                self.assertEqual(ctx.exception.code, "config_invalid")
                self.assertIsNone(ctx.exception.__cause__)
            with self.subTest(change=name, reader="env file"):
                path = self.fresh(f"e{index}.env", env)
                with self.during_read(self.change(name, path)):
                    with self.assertRaises(gsv.StartupError) as ctx:
                        gsv.load_env_config(path)
                self.assertEqual(ctx.exception.code, "config_invalid")
                self.assertIsNone(ctx.exception.__cause__)
            with self.subTest(change=name, reader="provider key file"):
                path = self.fresh(f"k{index}.key", KEY_CANARY + "\n")
                cfg = gs.parse_config(config_doc(self.base, provider_key_file=str(path)))
                with self.during_read(self.change(name, path)):
                    with self.assertRaises(gsv.StartupError) as ctx:
                        gsv.load_provider_key(cfg, {})
                self.assertEqual(ctx.exception.code, "provider_key_missing")
            with self.subTest(change=name, reader="check_private_file"):
                path = self.fresh(f"r{index}.bin", b"x" * 64)
                with self.during_read(self.change(name, path)):
                    with self.assertRaises(OSError):
                        gsv.check_private_file(path, max_bytes=4096, read=True)

    def test_an_env_file_cut_down_to_a_valid_prefix_is_refused(self):
        # The reported case: the prefix is a complete, valid settings text on its own
        # (only the optional Tailnet lines are lost), yet it must not be accepted.
        doc = config_doc(self.base, tailnet_bind_host="100.64.12.34",
                         tailnet_fqdn="gateway.example.test")
        text = env_text(doc)
        cut = len(text[:text.index("POCKET_GATEWAY_TAILNET_BIND_HOST=")].encode("utf-8"))
        prefix_cfg = gs.parse_config(gs.env_config_document(text.encode()[:cut].decode())[0])
        self.assertIsNone(prefix_cfg.tailnet_bind_host)  # valid, and silently different
        path = self.fresh("cut.env", text)
        with self.during_read(lambda: os.truncate(path, cut)):
            with self.assertRaises(gsv.StartupError) as ctx:
                gsv.load_env_config(path)
        self.assertEqual(ctx.exception.code, "config_invalid")
        self.assertEqual(gsv.load_env_config(path)[0], prefix_cfg)  # once it is at rest

    def test_a_json_config_that_grows_into_a_different_valid_document_is_refused(self):
        # A JSON text followed by spaces is still valid JSON, so without the comparison
        # the first bytes of a file being extended would be accepted.
        text = json.dumps(config_doc(self.base))
        path = self.fresh("grow.json", text)

        def extend() -> None:
            with open(path, "ab") as handle:
                handle.write(b" " * 64)

        with self.during_read(extend):
            with self.assertRaises(gsv.StartupError) as ctx:
                gsv.load_config(path)
        self.assertEqual(ctx.exception.code, "config_invalid")
        self.assertEqual(gsv.load_config(path).port, 8941)  # once it is at rest

    def test_an_unchanged_file_and_a_concurrent_reader_are_still_accepted(self):
        text = env_text(config_doc(self.base))
        path = self.fresh("still.env", text)
        with self.during_read(lambda: None):
            self.assertEqual(gsv.load_env_config(path)[0].port, 8941)
        with self.during_read(lambda: Path(path).read_bytes()):  # reading is not a change
            self.assertEqual(gsv.load_env_config(path)[0].port, 8941)
        json_path = self.fresh("still.json", json.dumps(config_doc(self.base)))
        with self.during_read(lambda: Path(json_path).read_bytes()):
            self.assertEqual(gsv.load_config(json_path).port, 8941)
        # the check without a read (the TLS files) takes no second look
        with mock.patch.object(gsv.os, "fstat", wraps=os.fstat) as fstat:
            self.assertIsNone(gsv.check_private_file(path, max_bytes=None, read=False))
        self.assertEqual(fstat.call_count, 1)

    def test_a_file_changed_while_read_never_reaches_the_listener(self):
        fx = StateFixture(self.base)
        json_path = fx.build()
        os.utime(json_path, ns=(self.OLD_NS, self.OLD_NS))
        env_path = self.fresh("gw.env", env_text(config_doc(self.base)))
        for name, path, env_file in (("json", json_path, False), ("env", env_path, True)):
            with self.subTest(config=name):
                spies = Spies()
                out, err = io.StringIO(), io.StringIO()

                def append(target: Path = path) -> None:
                    with open(target, "ab") as handle:
                        handle.write(b"\n")

                with self.during_read(append):
                    code = gsv.serve_main(path, env={}, stdout=out, stderr=err,
                                          deps=spies.deps(FakeLoopClock()),
                                          env_file=env_file)
                self.assertEqual((code, out.getvalue(), err.getvalue()),
                                 (1, "", "error: config_invalid\n"))
                self.assertEqual(spies.events, [])
        fx.stores_reopen()


# =================================================================================
# The documented copy command creates the private file exclusively
# =================================================================================
def copy_command(document: Path) -> tuple[list[str], str]:
    """(the two assignment lines, the command body) of the documented copy snippet."""
    blocks = document.read_text(encoding="utf-8").split("```sh\n")[1:]
    found = [textwrap.dedent(block.split("```", 1)[0]) for block in blocks]
    (snippet,) = [block for block in found if "set -C" in block]
    lines = snippet.splitlines()
    return lines[:2], "\n".join(lines[2:]) + "\n"


class CopyCommandTest(OfflineTestCase):
    SHELLS = tuple(shell for shell in ("/bin/sh", "/bin/bash") if Path(shell).exists())
    EXISTS = "Stopped: the destination already exists. Nothing was copied or changed."
    INCOMPLETE = "Stopped: the copy did not complete. No existing file was overwritten."
    FILLED = b"POCKET_GATEWAY_SESSION_LABEL=synthetic filled-in file\n"

    def setUp(self) -> None:
        super().setUp()
        self.assertTrue(self.SHELLS)
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="pet-env-copy-")))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.body = copy_command(DEPLOY_README)[1]
        self.count = 0

    def folder(self) -> Path:
        self.count += 1
        path = self.root / f"private dir {self.count}"  # a space: the quoting must hold
        path.mkdir(mode=0o700)
        return path

    def run_shell(self, shell: str, script: str, dest: Path,
                  source: Path = EXAMPLE) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [shell, "-c", script],
            env={"PATH": os.defpath, "PET_ENV_EXAMPLE": str(source),
                 "PET_ENV_FILE": str(dest)},
            capture_output=True, text=True, timeout=10, check=False,
        )

    def test_both_documents_give_the_same_command_and_no_plain_copy(self):
        readme_head, readme_body = copy_command(DEPLOY_README)
        guide_head, guide_body = copy_command(GUIDE)
        self.assertEqual(readme_body, guide_body)  # one form, used consistently
        self.assertEqual(readme_head, [
            'PET_ENV_EXAMPLE="/ABSOLUTE/REPOSITORY/bridge/.env.example"',
            'PET_ENV_FILE="/ABSOLUTE/private/.env"'])
        self.assertEqual(guide_head, [
            'PET_ENV_EXAMPLE=".env.example"', 'PET_ENV_FILE="$XDG_CONFIG_HOME/.env"'])
        self.assertIn(
            '( umask 077 && set -C && cat "$PET_ENV_EXAMPLE" > "$PET_ENV_FILE" )', readme_body)
        plain = re.compile(r"\s*(cp|mv|install|chmod)\s.*\.env")
        for document in (DEPLOY_README, GUIDE, EXAMPLE):
            lines = document.read_text(encoding="utf-8").splitlines()
            with self.subTest(document=document.name):
                self.assertEqual([line for line in lines if plain.match(line)], [])
        for document in (DEPLOY_README, GUIDE):
            text = " ".join(document.read_text(encoding="utf-8").split())
            with self.subTest(document=document.name):
                # each message is in the command AND explained in the text around it
                self.assertGreaterEqual(text.count(self.EXISTS), 2)
                self.assertGreaterEqual(text.count(self.INCOMPLETE), 2)
                self.assertIn("not with `cp`", text)

    def test_an_absent_destination_is_created_private_with_the_example_bytes(self):
        for shell in self.SHELLS:
            for prefix in ("", "umask 022\n", "umask 000\n"):
                with self.subTest(shell=shell, prefix=prefix.strip()):
                    dest = self.folder() / ".env"
                    result = self.run_shell(shell, prefix + self.body, dest)
                    self.assertEqual(
                        (result.returncode, result.stdout, result.stderr),
                        (0, "Created a new private settings file.\n", ""))
                    self.assertEqual(dest.read_bytes(), EXAMPLE.read_bytes())
                    info = os.lstat(dest)
                    self.assertTrue(stat.S_ISREG(info.st_mode))
                    self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                    # running it again is the "already exists" refusal, byte for byte
                    again = self.run_shell(shell, prefix + self.body, dest)
                    self.assertNotEqual(again.returncode, 0)
                    self.assertEqual((again.stdout, again.stderr), ("", self.EXISTS + "\n"))
                    self.assertEqual(dest.read_bytes(), EXAMPLE.read_bytes())

    def obstacles(self, folder: Path) -> dict[str, Path]:
        """Things already at a destination; ``filled.env`` holds the bytes to protect."""
        filled = folder / "filled.env"
        filled.write_bytes(self.FILLED)
        os.chmod(filled, 0o640)  # not ours to tighten: the mode must survive too
        to_file = folder / "link-to-file.env"
        to_file.symlink_to(filled)
        dangling = folder / "dangling.env"
        dangling.symlink_to(folder / "not-there.env")
        to_dir = folder / "link-to-dir.env"
        to_dir.symlink_to(folder, target_is_directory=True)
        fifo = folder / "fifo.env"
        os.mkfifo(fifo, 0o600)
        directory = folder / "dir.env"
        directory.mkdir()
        return {
            "existing filled-in file": filled,
            "symlink to a filled-in file": to_file,
            "dangling symlink": dangling,
            "symlink to a directory": to_dir,
            "fifo": fifo,
            "directory": directory,
        }

    def assert_untouched(self, folder: Path, before: list[str]) -> None:
        self.assertEqual(sorted(os.listdir(folder)), before)  # nothing created or removed
        self.assertEqual((folder / "filled.env").read_bytes(), self.FILLED)
        self.assertEqual(stat.S_IMODE((folder / "filled.env").stat().st_mode), 0o640)
        for link in ("link-to-file.env", "dangling.env", "link-to-dir.env"):
            self.assertTrue((folder / link).is_symlink())
        self.assertFalse((folder / "not-there.env").exists())
        self.assertEqual(os.listdir(folder / "dir.env"), [])

    def test_anything_already_at_the_destination_is_refused_and_left_untouched(self):
        for shell in self.SHELLS:
            folder = self.folder()
            cases = self.obstacles(folder)
            before = sorted(os.listdir(folder))
            for name, dest in cases.items():
                with self.subTest(shell=shell, case=name):
                    result = self.run_shell(shell, self.body, dest)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual((result.stdout, result.stderr), ("", self.EXISTS + "\n"))
                    self.assert_untouched(folder, before)

    def test_the_redirect_alone_is_exclusive_without_the_check(self):
        # What the check cannot close: something appears after it.  The no-overwrite
        # redirect must itself fail and leave the other file alone.  (A FIFO is left out:
        # a shell may open one for writing, which overwrites nothing but can block.)
        match = re.search(r"^elif (\( .* \)); then$", self.body, re.MULTILINE)
        self.assertIsNotNone(match)
        redirect = match.group(1) + "\n"
        for shell in self.SHELLS:
            folder = self.folder()
            cases = self.obstacles(folder)
            before = sorted(os.listdir(folder))
            for name in ("existing filled-in file", "symlink to a filled-in file",
                         "dangling symlink", "symlink to a directory", "directory"):
                with self.subTest(shell=shell, case=name):
                    result = self.run_shell(shell, redirect, cases[name])
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assert_untouched(folder, before)
            with self.subTest(shell=shell, case="clear path"):
                created = folder / "new.env"  # and it does create when the way is clear
                self.assertEqual(self.run_shell(shell, redirect, created).returncode, 0)
                self.assertEqual(created.read_bytes(), EXAMPLE.read_bytes())
                self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o600)

    def test_a_failed_copy_reports_incomplete_and_overwrites_nothing(self):
        for shell in self.SHELLS:
            with self.subTest(shell=shell, case="missing destination directory"):
                dest = self.folder() / "absent" / ".env"
                result = self.run_shell(shell, self.body, dest)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.endswith(self.INCOMPLETE + "\n"))
                self.assertFalse(dest.parent.exists())
            with self.subTest(shell=shell, case="missing example"):
                folder = self.folder()
                dest = folder / ".env"
                result = self.run_shell(shell, self.body, dest, folder / "no-example")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.endswith(self.INCOMPLETE + "\n"))
                # the documented leftover: a new, empty, owner-only file to inspect
                self.assertEqual(dest.read_bytes(), b"")
                self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)

    def test_a_created_copy_is_a_file_the_gateway_accepts_as_private(self):
        dest = self.folder() / ".env"
        self.assertEqual(self.run_shell(self.SHELLS[0], self.body, dest).returncode, 0)
        # owner-only and regular: the reader gets as far as the unfilled placeholders
        self.assertEqual(
            gsv.check_private_file(dest, max_bytes=16384, read=True), EXAMPLE.read_bytes())
        with self.assertRaises(gsv.StartupError) as ctx:
            gsv.load_env_config(dest)
        self.assertEqual(ctx.exception.code, "config_invalid")


# =================================================================================
# CLI: serve --env-file
# =================================================================================
class CliEnvFileTest(ServerOfflineCase):
    def run_cli(self, *argv: str, **kwargs: Any) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(list(argv), env=kwargs.pop("env", {}), stdout=out, stderr=err,
                        **kwargs)
        return code, out.getvalue(), err.getvalue()

    def test_env_file_is_forwarded_and_the_json_call_is_unchanged(self):
        env = {"UNRELATED": "value"}
        deps = object()
        with mock.patch.object(gsv, "serve_main", return_value=7) as serve:
            code, out, err = self.run_cli(
                "serve", "--env-file", "/private/pocket-gateway.env",
                "--event-log", "/private/events", env=env, serve_deps=deps)
            self.assertEqual((code, out, err), (7, "", ""))
            args, kwargs = serve.call_args
            self.assertEqual(args, (Path("/private/pocket-gateway.env"),))
            self.assertEqual(set(kwargs), {"env", "stdout", "stderr", "deps",
                                           "event_log_path", "env_file"})
            self.assertIs(kwargs["env_file"], True)
            self.assertIs(kwargs["env"], env)
            self.assertIs(kwargs["deps"], deps)
            self.assertEqual(kwargs["event_log_path"], Path("/private/events"))

            serve.reset_mock()
            self.assertEqual(self.run_cli("serve", "--config", "/private/gw.json")[0], 7)
            args, kwargs = serve.call_args
            self.assertEqual(args, (Path("/private/gw.json"),))
            self.assertEqual(set(kwargs), {"env", "stdout", "stderr", "deps",
                                           "event_log_path"})

    def test_exactly_one_absolute_source_is_required_and_nothing_is_echoed(self):
        cases = (
            ["serve"],
            ["serve", "--env-file"],
            ["serve", "--env-file", ""],
            ["serve", "--env-file", "relative.env"],
            ["serve", "--env-file", "./" + CANARY + ".env"],
            ["serve", "--env-file=" + CANARY],
            ["serve", "--env", "/abs/gw.env"],  # abbreviations are not accepted
            ["serve", "--env-file", "/abs/gw.env", "--config", "/abs/gw.json"],
            ["serve", "--config", "/abs/gw.json", "--env-file", "/abs/gw.env"],
            ["serve", "--env-file", "/abs/gw.env", "--config", CANARY],
            ["serve", "--env-file", "/abs/gw.env", "extra"],
            ["serve", "--env-file", "/abs/gw.env", "--event-log", "relative"],
            ["serve", "--env-file", "/abs/gw.env", "--provider-key", CANARY],
        )
        with mock.patch.object(gsv, "serve_main", return_value=0) as serve:
            for argv in cases:
                with self.subTest(argv=argv[1:3]):
                    code, out, err = self.run_cli(*argv)
                    self.assertEqual((code, out), (2, ""))
                    self.assertEqual(err, "error: usage (run with --help)\n")
            serve.assert_not_called()

    def test_serve_help_names_both_sources(self):
        code, out, err = self.run_cli("serve", "--help")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("--config", out)
        self.assertIn("--env-file", out)

    def test_the_cli_reaches_the_real_serve_from_an_env_file(self):
        fx = StateFixture(self.base)
        fx.build()
        env_path = self.write("gw.env", env_text(config_doc(self.base)))
        spies = Spies()
        code, out, err = self.run_cli(
            "serve", "--env-file", str(env_path), serve_deps=spies.deps(FakeLoopClock()))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            out.splitlines(),
            ["gateway: listening on 10.1.2.3:8941 for session ...0a1b2c3d",
             "gateway: stopped"],
        )
        self.assertEqual(spies.events, ["site", "wait", "closer"])

    def test_the_cli_refuses_a_loose_or_missing_env_file_with_a_fixed_code(self):
        StateFixture(self.base).build()
        loose = self.write("loose.env", env_text(config_doc(self.base)), mode=0o644)
        for path in (loose, self.base / "absent.env"):
            with self.subTest(path=path.name):
                spies = Spies()
                code, out, err = self.run_cli(
                    "serve", "--env-file", str(path),
                    serve_deps=spies.deps(FakeLoopClock()))
                self.assertEqual((code, out, err), (1, "", "error: config_invalid\n"))
                self.assertEqual(spies.events, [])
                self.assertFalse(re.search(r"Traceback|loose|absent", err))


# =================================================================================
# The tracked placeholder file and the documents that point at it
# =================================================================================
class EnvExampleTest(ServerOfflineCase):
    ASSIGN = re.compile(r"(#?)([A-Z][A-Z0-9_]*)=(.*)")
    # Not machine-specific or secret: the only values the example may state literally.
    LITERAL = {
        "POCKET_GATEWAY_CONFIG_VERSION": "1",
        "POCKET_GATEWAY_SESSION_LABEL": "Pocket assistant",
        "POCKET_GATEWAY_PORT": "9443",
        "POCKET_GATEWAY_MODEL": gs.DEFAULT_MODEL,
        "POCKET_GATEWAY_VOICE": gs.DEFAULT_VOICE,
    }

    def entries(self) -> list[tuple[bool, str, str, str]]:
        """(active, name, value, the line directly above) for every assignment line."""
        lines = EXAMPLE.read_text(encoding="utf-8").split("\n")
        found = []
        for index, line in enumerate(lines):
            match = self.ASSIGN.fullmatch(line)
            if match is not None:
                found.append((match.group(1) == "", match.group(2), match.group(3),
                              lines[index - 1]))
        return found

    def test_it_lists_every_setting_once_with_a_one_line_comment(self):
        entries = self.entries()
        names = [name for _, name, _, _ in entries]
        self.assertEqual(sorted(names), sorted(gs.ENV_CONFIG_NAMES))  # all, exactly once
        for _, name, _, above in entries:
            with self.subTest(name=name):
                self.assertRegex(above, r"^# \S.{20,}$")  # its own one-line comment
        active = {name for on, name, _, _ in entries if on}
        self.assertEqual(
            active,
            {JSON_TO_ENV[key] for key in gs._CONFIG_REQUIRED}
            | {"POCKET_GATEWAY_PROVIDER_KEY_FILE"},  # exactly one key source is active
        )

    def test_it_holds_placeholders_only(self):
        text = EXAMPLE.read_text(encoding="utf-8")
        self.assertTrue(text.isascii())
        self.assertNotIn("\r", text)
        for _, name, value, _ in self.entries():
            with self.subTest(name=name):
                if name in self.LITERAL:
                    self.assertEqual(value, self.LITERAL[name])
                else:
                    self.assertRegex(value, r"^<[a-z][A-Za-z0-9_-]*>(?:[:/][a-z0-9-]+)?$")
        # nothing that looks like a real address, hash, credential, key or home path
        for pattern in (
            r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", r"[0-9a-f]{32}", r"device-[0-9a-f]",
            r"sk-[A-Za-z0-9]", r"\.ts\.net", r"/home/", r"/Users/", r"/mnt/",
            r"[0-9a-f]{8}-[0-9a-f]{4}-",
        ):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, text))

    def test_the_unfilled_example_cannot_start_a_gateway(self):
        copy = self.write("unfilled.env", EXAMPLE.read_bytes())
        with self.assertRaises(gsv.StartupError) as ctx:
            gsv.load_env_config(copy)
        self.assertEqual(ctx.exception.code, "config_invalid")

    def filled(self, doc: dict[str, Any], *, uncomment: bool, inline: str | None) -> str:
        """The example with each placeholder replaced by a synthetic value from ``doc``."""
        by_name = {JSON_TO_ENV[k]: (",".join(v) if isinstance(v, list) else str(v))
                   for k, v in doc.items() if k in JSON_TO_ENV}
        out = []
        for line in EXAMPLE.read_text(encoding="utf-8").split("\n"):
            match = self.ASSIGN.fullmatch(line)
            if match is None:
                out.append(line)
                continue
            active, name = match.group(1) == "", match.group(2)
            if name == INLINE:
                out.append(f"{name}={inline}" if inline is not None else line)
            elif name == "POCKET_GATEWAY_PROVIDER_KEY_FILE" and inline is not None:
                out.append("#" + line)
            elif active or (uncomment and name in by_name):
                out.append(f"{name}={by_name[name]}")
            else:
                out.append(line)
        return "\n".join(out)

    def test_a_filled_in_copy_loads_and_equals_the_json_config(self):
        cases = {
            "as shipped": (config_doc(self.base, port=9443, host_header="10.1.2.3:9443",
                                      session_label="Pocket assistant"), False, None),
            "all optional": (full_doc(self.base), True, None),
            "inline key": (full_doc(self.base, provider_key_file=...,
                                    provider_key_env=INLINE), True, KEY_CANARY),
        }
        for index, (name, (doc, uncomment, inline)) in enumerate(cases.items()):
            with self.subTest(case=name):
                text = self.filled(doc, uncomment=uncomment, inline=inline)
                cfg, key = gsv.load_env_config(self.write(f"filled{index}.env", text))
                self.assertEqual(fields_of(cfg), fields_of(gs.parse_config(doc)))
                self.assertEqual(None if key is None else key.reveal(), inline)

    def test_the_example_is_not_ignored_but_a_filled_copy_name_is(self):
        rules = (BRIDGE.parent / ".gitignore").read_text(encoding="utf-8").splitlines()
        for rule in (".env", ".env.*", "!.env.example"):
            self.assertIn(rule, rules)
        self.assertLess(rules.index(".env.*"), rules.index("!.env.example"))


class EnvDocumentsTest(OfflineTestCase):
    def test_the_unit_recommends_the_env_file_with_one_active_start_line(self):
        unit = UNIT.read_text(encoding="utf-8")
        active = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
        self.assertEqual(active, [
            'ExecStart=@PYTHON@ -B -m pet_bridge serve --env-file "@ENV_FILE@" '
            '--event-log "@EVENT_LOG@"'])
        self.assertIn(
            '#ExecStart=@PYTHON@ -B -m pet_bridge serve --config "@CONFIG@" '
            '--event-log "@EVENT_LOG@"', unit)  # the JSON alternative stays documented
        directives = [line.split("=", 1)[0] for line in unit.splitlines()
                      if "=" in line and not line.startswith("#")]
        self.assertNotIn("EnvironmentFile", directives)  # secrets are never exported
        # The start line and environment are not changed by the startup-line fix: no
        # PYTHONUNBUFFERED and no ``-u``.  The line is visible because the gateway flushes
        # it (StartupLineFlushTest), not because the unit unbuffers Python.
        self.assertEqual(
            [line for line in unit.splitlines() if line.startswith("Environment")],
            ["Environment=PYTHONDONTWRITEBYTECODE=1"])
        self.assertNotIn("PYTHONUNBUFFERED", unit)
        self.assertNotIn(KEY_CANARY, unit)

    def test_the_environmentfile_warning_gives_the_real_reason(self):
        unit = " ".join(
            line.lstrip("# ") for line in UNIT.read_text(encoding="utf-8").splitlines()
            if line.startswith("#"))
        readme = " ".join(DEPLOY_README.read_text(encoding="utf-8").split())
        for name, text in (("unit", unit), ("deploy README", readme)):
            with self.subTest(document=name):
                self.assertIn("EnvironmentFile=", text)
                self.assertIn("process environment", text)
                self.assertIn("/proc", text)
                self.assertIn("never reads its", text)  # ... key from the environment
                # with --env-file the gateway still checks the file: never say otherwise
                self.assertIsNone(re.search(r"skips? (that|the private-file) check", text))
        self.assertIn("opens and checks that file itself", unit)
        self.assertIn("does not remove that check", readme)

    def test_the_start_is_verified_by_evidence_tied_to_the_current_start(self):
        readme = " ".join(DEPLOY_README.read_text(encoding="utf-8").split())
        guide = " ".join(GUIDE.read_text(encoding="utf-8").split())
        for needed in (
            "### Check the current start", "ActiveEnterTimestamp", "-p InvocationID",
            '_SYSTEMD_INVOCATION_ID="$(systemctl --user show pocket-gateway.service '
            '-p InvocationID --value)"',
            "`gateway_start` record whose UTC `timestamp` is not earlier",
            "no `gateway_stop` after it", '--since "$PET_START_LOCAL"',
            "have not been exercised against a live unit",
        ):
            with self.subTest(readme=needed):
                self.assertIn(needed, readme)
        for needed in ("ActiveEnterTimestamp", "`gateway_start` event-log record",
                       "restricted to the current invocation", "Check the current start"):
            with self.subTest(guide=needed):
                self.assertIn(needed, guide)
        # the migration no longer tells the operator to look for the bare text
        self.assertNotIn("and the journal for `gateway: listening on`", readme)
        self.assertIn("that text alone proves nothing", readme)

    def test_the_documents_state_the_concurrent_edit_limit_without_overclaiming(self):
        for document in (DEPLOY_README, GUIDE):
            text = " ".join(document.read_text(encoding="utf-8").split())
            with self.subTest(document=document.name):
                self.assertIn("not a lock", text)
                self.assertIn("refusal of every concurrent edit is not claimed", text)

    START = re.compile(
        r"systemctl --user (?:start|restart|enable --now) pocket-gateway\.service")
    NOTE_LOCAL = "PET_START_LOCAL=\"$(date '+%Y-%m-%d %H:%M:%S')\""
    NOTE_UTC = "date -u '+%Y-%m-%dT%H:%M:%S+00:00'"

    def start_order_defects(self, procedure: str) -> list[str]:
        """Why a start procedure would note its time too late (empty when it is right)."""
        starts = [match.start() for match in self.START.finditer(procedure)]
        if len(starts) != 1:
            return ["expected exactly one start command"]
        defects = []
        for label, needle in (("local", self.NOTE_LOCAL), ("UTC", self.NOTE_UTC)):
            if procedure.count(needle) != 1:
                defects.append(f"the {label} time is not noted exactly once")
            elif procedure.index(needle) > starts[0]:
                defects.append(f"the {label} time is noted after the start")
            elif "```" in procedure[procedure.index(needle):starts[0]]:
                defects.append(f"the {label} time is not in the start's command block")
        return defects

    def readme_sections(self) -> dict[str, str]:
        readme = DEPLOY_README.read_text(encoding="utf-8")
        move, rest = readme.split(
            "\n### Move an existing JSON-configured service to `.env`\n", 1)[1].split(
            "\n## Install and start\n", 1)
        install, rest = rest.split("\n### Check the current start\n", 1)
        check, rest = rest.split("\n## Optional LAN and Tailnet listeners\n", 1)
        return {"move": move, "install": install, "check": check, "all": readme}

    def test_the_time_is_noted_before_the_start_command_in_both_start_paths(self):
        sections = self.readme_sections()
        for name in ("move", "install"):
            with self.subTest(path=name):
                self.assertEqual(self.start_order_defects(sections[name]), [])
        # The check itself never tells a reader who has already started to note the time,
        # and says what a late note would do.
        check = " ".join(sections["check"].split())
        self.assertNotIn("PET_START_LOCAL=", check)
        self.assertEqual(self.START.findall(check), [])
        self.assertIn("noted immediately **before** the start", check)
        self.assertIn("Do not run them once the service has started", check)
        # Two start procedures and the two entries of the operate list: a further start
        # command cannot be added without being placed in one of the checked procedures.
        self.assertEqual(len(self.START.findall(sections["all"])), 4)
        # The team guide starts nothing itself and gives the same order in words.
        guide = " ".join(GUIDE.read_text(encoding="utf-8").split())
        self.assertEqual(self.START.findall(guide), [])
        self.assertLess(guide.index("Before you start the service, note the time"),
                        guide.index("Afterwards do not rely on finding"))

    def test_the_ordering_check_rejects_a_time_noted_after_the_start(self):
        good = self.readme_sections()["install"]
        self.assertEqual(self.start_order_defects(good), [])
        start = self.START.search(good).group(0)
        block = f"{self.NOTE_LOCAL}\n   {self.NOTE_UTC}\n"
        self.assertEqual(good.count(block), 1)
        late = good.replace(block, "").replace(start, f"{start}\n   {block}")
        self.assertEqual(self.start_order_defects(late), [
            "the local time is noted after the start",
            "the UTC time is noted after the start"])
        # the previous shape: start here, note the time in a later command block
        separate = good.replace(block, "") + f"\n```sh\n{block}```\n"
        self.assertEqual(self.start_order_defects(separate), [
            "the local time is noted after the start",
            "the UTC time is noted after the start"])
        apart = good.replace(block, "") .replace(
            "   ```sh\n", f"   ```sh\n   {block}   ```\n\n   ```sh\n", 1)
        self.assertEqual(self.start_order_defects(apart), [
            "the local time is not in the start's command block",
            "the UTC time is not in the start's command block"])
        self.assertEqual(self.start_order_defects(good.replace(block, "")), [
            "the local time is not noted exactly once",
            "the UTC time is not noted exactly once"])
        self.assertEqual(self.start_order_defects("no command here"),
                         ["expected exactly one start command"])

    def test_a_clean_stop_is_not_described_as_a_restart_trigger(self):
        check = " ".join(self.readme_sections()["check"].split())
        self.assertIn("A `null` code is a clean stop", check)
        self.assertIn("`Restart=on-failure` does not start it again", check)
        self.assertIn("A code such as `config_invalid` is a failed start", check)
        self.assertNotIn("every 10 seconds", check)

    def test_sourcing_is_distinguished_from_exporting(self):
        readme = " ".join(DEPLOY_README.read_text(encoding="utf-8").split())
        self.assertIn("Systemd `EnvironmentFile=`, and dotenv tools that export what they "
                      "read, copy every value", readme)
        self.assertIn("A bare shell `source` is different: it sets shell variables, which "
                      "reach a process environment only if the file or the shell exports "
                      "them", readme)
        self.assertIn("Do not `source` the file anyway. It is not a shell script", readme)
        self.assertNotIn("a shell `source` or a dotenv tool", readme)  # the old conflation

    def test_the_copy_command_scope_is_stated_without_an_atomic_claim(self):
        for document in (DEPLOY_README, GUIDE):
            text = " ".join(document.read_text(encoding="utf-8").split())
            with self.subTest(document=document.name):
                self.assertIn("private and controlled only by you", text)
                self.assertIn("check followed by a redirect", text)
                self.assertRegex(text, r"not (one atomic step|an atomic guarantee)")
                self.assertRegex(text, r"FIFO\)?,? (for example, )?could make (the command|it) "
                                       r"block or fail")
                self.assertNotIn("is not overwritten either", text)

    def test_the_guides_show_the_env_setup_keep_json_and_claim_no_demonstration(self):
        readme = " ".join(DEPLOY_README.read_text(encoding="utf-8").split())
        guide = " ".join(GUIDE.read_text(encoding="utf-8").split())
        for name, text in (("deploy README", readme), ("team guide", guide)):
            with self.subTest(document=name):
                self.assertIn(".env.example", text)
                self.assertIn("--env-file", text)
                self.assertIn("--config", text)  # the JSON alternative is kept
                self.assertIn("has not been demonstrated on a running service or on a "
                              "second machine", text)
        self.assertIn("recommended", readme.lower())
        self.assertIn("@ENV_FILE@", readme)
        self.assertIn("@CONFIG@", readme)
        self.assertIn("Move an existing JSON-configured service to `.env`", readme)
        self.assertIn("create it with this command, not with `cp`", readme)


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
