"""Offline tests for gateway_provider: exact GPT-Live wire builders and event classifier.

Pure functions, no mocks for them.  The provider start asserted here is the source-backed
shape the device's nested ``session.start`` (assistant_config.h:9-20) maps to when the host
builds its own: model, store false, 24 kHz PCM, voice, client delegation.  Offline evidence
only, never a live provider result.
"""
# pyright: reportArgumentType=false, reportCallIssue=false, reportOptionalMemberAccess=false
# (tests deliberately pass invalid types and read Optionals right after asserting them)

from __future__ import annotations

import ast
import base64
import dataclasses
import inspect
import json
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

import pet_bridge
from pet_bridge import gateway_provider as gp
from pet_bridge import gateway_state as gs
from pet_bridge import wire

from .fakes import OfflineTestCase
from .providerfixtures import provider_frame

TEXT_CANARY = "TRANSCRIPTCANARY-4471"
MESSAGE_CANARY = "PROVIDERMESSAGECANARY-9921"
DEVICE_INSTRUCTIONS_CANARY = "DEVICEINSTRUCTIONSCANARY-5512"


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def frame(**fields: Any) -> str:
    return json.dumps(provider_frame(fields))


class ProviderBuilderTest(OfflineTestCase):
    def test_start_has_host_owned_delegation_policy_not_device_authority(self):
        start = json.loads(gp.build_provider_start("gw_host", "gpt-live-1", "marin"))
        instructions = start["session"]["instructions"]
        for required in (
            "Backchannel policy:", "Interruption policy:", "Delegation policy:",
            "Backend tools:", "Generate <|SpawnThinking|> when:",
            "Do not guess", "confirmation", "host-supplied commentary",
        ):
            self.assertIn(required, instructions)
        self.assertNotIn(DEVICE_INSTRUCTIONS_CANARY, instructions)
        self.assertEqual(list(inspect.signature(gp.build_provider_start).parameters),
                         ["event_id", "model", "voice"])

    def test_t_prov_start_01_exact_provider_start_string(self):
        wire_start = gp.build_provider_start("gw_abc", "gpt-live-1", "marin")
        instructions = json.loads(wire_start)["session"]["instructions"]
        self.assertEqual(
            wire_start,
            '{"type":"session.start","event_id":"gw_abc","session":{"model":"gpt-live-1",'
            '"store":false,"audio":{"format":{"type":"audio/pcm","rate":24000},'
            '"output":{"voice":"marin"}},"delegation":{"type":"client",'
            '"acknowledgement_delay_ms":0},"instructions":'
            + json.dumps(instructions, ensure_ascii=False, separators=(",", ":")) + "}}",
        )

    def test_t_prov_start_is_the_minimal_source_backed_nested_shape(self):
        doc = json.loads(gp.build_provider_start("gw_abc", "gpt-live-2", "alloy"))
        self.assertEqual(list(doc), ["type", "event_id", "session"])
        session = doc["session"]
        self.assertEqual(list(session), ["model", "store", "audio", "delegation", "instructions"])
        self.assertEqual(session["model"], "gpt-live-2")
        self.assertIs(session["store"], False)
        self.assertEqual(
            session["audio"],
            {
                "format": {"type": "audio/pcm", "rate": 24000},
                "output": {"voice": "alloy"},
            },
        )
        self.assertEqual(
            session["delegation"], {"type": "client", "acknowledgement_delay_ms": 0}
        )
        self.assertIs(type(session["delegation"]["acknowledgement_delay_ms"]), int)

    def test_t_prov_start_ignores_device_fields_they_have_no_parameter_to_enter(self):
        params = list(inspect.signature(gp.build_provider_start).parameters)
        self.assertEqual(params, ["event_id", "model", "voice"])
        text = gp.build_provider_start("gw_abc", "gpt-live-1", "marin")
        for absent in (
            '"input"',
            '"responses"',
            '"reasoning"',
            "gpt-6",
            "Tachikoma",
            DEVICE_INSTRUCTIONS_CANARY,
        ):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, text)

    def test_t_prov_start_rejects_invalid_arguments(self):
        good = ("gw_abc", "gpt-live-1", "marin")
        for index, bad in (
            (0, ""),
            (0, "bad id"),
            (0, None),
            (0, "x" * 65),
            (1, "GPT"),
            (1, ""),
            (1, "-a"),
            (1, None),
            (2, "Marin"),
            (2, ""),
            (2, None),
            (2, "a b"),
        ):
            args = list(good)
            args[index] = bad
            with self.subTest(args=args), self.assertRaises(ValueError):
                gp.build_provider_start(*args)

    def test_t_prov_audio_forwards_the_validated_string_verbatim(self):
        encoded = b64(b"\x01\x02\x03\x04")
        self.assertEqual(
            gp.build_provider_audio(encoded),
            '{"type":"session.input_audio.append","audio":"AQIDBA=="}',
        )
        biggest = b64(b"\x00\x01" * 2400)
        self.assertEqual(json.loads(gp.build_provider_audio(biggest))["audio"], biggest)

    def test_t_prov_audio_rejects_invalid_audio(self):
        for value in (
            "",
            "!!!!",
            " AAAA",
            "-_8=",
            b64(b"\x00"),
            b64(b"\x00" * 3),
            b64(b"\x00" * 4802),
            None,
            5,
            b"AAAA",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gp.build_provider_audio(value)

    def test_t_prov_commentary_is_exact_and_carries_ids_and_content(self):
        self.assertEqual(
            gp.build_commentary("gw_e1", "del_1", "hello \u00e9"),
            '{"type":"session.commentary.append","event_id":"gw_e1",'
            '"delegation_id":"del_1","content":"hello \u00e9"}',
        )
        doc = json.loads(gp.build_commentary("gw_e1", "del_1", 'a "q"\nb'))
        self.assertEqual(doc["content"], 'a "q"\nb')

    def test_t_prov_commentary_content_is_1_to_480_utf8_bytes(self):
        gp.build_commentary("gw_e1", "d", "x" * 480)
        gp.build_commentary("gw_e1", "d", "\u00e9" * 240)
        gp.build_commentary("gw_e1", "d", "\U0001f600" * 120)
        for content in (
            "x" * 481,
            "\u00e9" * 241,
            "\U0001f600" * 121,
            "",
            None,
            5,
            b"x",
            "a\ud800",
        ):
            with (
                self.subTest(content=repr(content)[:20]),
                self.assertRaises(ValueError),
            ):
                gp.build_commentary("gw_e1", "d", content)

    def test_t_prov_commentary_requires_both_ids_in_event_id_format(self):
        for event_id, delegation_id in (
            ("", "d"),
            ("bad id", "d"),
            ("gw_e1", None),
            ("gw_e1", ""),
            ("gw_e1", "bad id"),
            (None, "d"),
            ("gw_e1", 5),
        ):
            with (
                self.subTest(event_id=event_id, delegation_id=delegation_id),
                self.assertRaises(ValueError),
            ):
                gp.build_commentary(event_id, delegation_id, "hi")

    def test_t_prov_close_is_exact(self):
        self.assertEqual(gp.build_provider_close(), '{"type":"session.close"}')

    def test_t_prov_new_event_id_is_gw_plus_uuid4_hex(self):
        value = gp.new_event_id()
        self.assertEqual(len(value), 35)
        self.assertRegex(value, r"^gw_[0-9a-f]{32}$")
        self.assertTrue(gs.is_event_id(value))
        self.assertEqual(len({gp.new_event_id() for _ in range(200)}), 200)
        fixed = uuid.UUID("12345678-1234-4234-8234-123456789abc")
        with mock.patch("uuid.uuid4", return_value=fixed):
            self.assertEqual(gp.new_event_id(), "gw_" + fixed.hex)

    def test_t_prov_no_proxy_module_exposes_exactly_the_four_builders_and_the_classifier(
        self,
    ):
        self.assertEqual(
            set(gp.__all__),
            {
                "PROVIDER_URL",
                "build_provider_start",
                "build_provider_audio",
                "build_commentary",
                "build_provider_close",
                "new_event_id",
                "ProviderEvent",
                "classify_provider_event",
            },
        )
        defined = {
            name
            for name, obj in vars(gp).items()
            if not name.startswith("_")
            and (inspect.isfunction(obj) or inspect.isclass(obj))
            and getattr(obj, "__module__", None) == gp.__name__
        }
        self.assertEqual(defined, set(gp.__all__) - {"PROVIDER_URL"})
        builders = [n for n in defined if n.startswith("build_")]
        self.assertEqual(
            sorted(builders),
            [
                "build_commentary",
                "build_provider_audio",
                "build_provider_close",
                "build_provider_start",
            ],
        )

    def test_t_prov_no_proxy_every_builder_emits_only_its_own_pinned_type(self):
        emitted = {
            json.loads(gp.build_provider_start("gw_a", "gpt-live-1", "marin"))["type"],
            json.loads(gp.build_provider_audio(b64(b"\x00\x01")))["type"],
            json.loads(gp.build_commentary("gw_a", "d", "x"))["type"],
            json.loads(gp.build_provider_close())["type"],
        }
        self.assertEqual(
            emitted,
            {
                "session.start",
                "session.input_audio.append",
                "session.commentary.append",
                "session.close",
            },
        )
        # No builder takes a free-form frame, type or payload argument.
        for builder in (
            gp.build_provider_start,
            gp.build_provider_audio,
            gp.build_commentary,
            gp.build_provider_close,
        ):
            for name in inspect.signature(builder).parameters:
                self.assertNotIn(
                    name,
                    {"frame", "payload", "type", "kwargs", "extra", "event", "data"},
                )

    def test_t_prov_url_is_the_pinned_constant_and_appears_only_in_this_module(self):
        self.assertEqual(gp.PROVIDER_URL, "wss://api.openai.com/v1/live/sessions")
        package = Path(pet_bridge.__file__).parent
        holders = {
            path.name
            for path in package.glob("*.py")
            if "api.openai.com" in path.read_text(encoding="utf-8")
        }
        self.assertEqual(holders, {"gateway_provider.py"})


class ProviderClassifierTest(OfflineTestCase):
    def test_required_payload_deletions_bypass_fixture_completion(self):
        for kind, missing in (
            ("session.started", "session"), ("session.closed", "session"),
            ("session.closed", "reason"), ("session.closed", "usage"), ("error", "error"),
        ):
            doc = provider_frame({"type": kind})
            del doc[missing]
            with self.subTest(kind=kind, missing=missing):
                self.assertEqual(gp.classify_provider_event(json.dumps(doc)).kind, "invalid")

    def test_documented_metadata_is_validated_not_retained(self):
        started = provider_frame({"type": "session.started"})
        started["session"]["instructions"] = MESSAGE_CANARY
        started["session"]["input"] = [{"content": MESSAGE_CANARY}]
        value = gp.classify_provider_event(json.dumps(started))
        self.assertEqual(value.kind, "started")
        self.assertNotIn(MESSAGE_CANARY, str(dataclasses.asdict(value)))
        for name in ("id", "expires_at", "model", "status"):
            doc = provider_frame({"type": "session.started"})
            del doc["session"][name]
            with self.subTest(missing=name):
                self.assertEqual(gp.classify_provider_event(json.dumps(doc)).kind, "invalid")
        for name, bad in (("expires_at", True), ("store", 1), ("status", "closed"),
                          ("input", {}), ("audio", []), ("delegation", {"type": "responses"})):
            doc = provider_frame({"type": "session.started"})
            doc["session"][name] = bad
            with self.subTest(name=name, bad=bad):
                self.assertEqual(gp.classify_provider_event(json.dumps(doc)).kind, "invalid")

    def test_documented_ack_envelope_and_timeline_refuse_malformed_values(self):
        base = provider_frame({"type": "session.commentary.appended", "client_event_id": "gw_reply"})
        self.assertEqual(gp.classify_provider_event(json.dumps(base)).kind, "ack")
        for name, bad in (("event_id", None), ("client_event_id", None),
                          ("start_ms", True), ("start_ms", -1), ("end_ms", -1)):
            with self.subTest(name=name, bad=bad):
                self.assertEqual(gp.classify_provider_event(json.dumps({**base, name: bad})).kind, "invalid")
        for name in ("event_id", "start_ms", "end_ms"):
            doc = dict(base)
            del doc[name]
            with self.subTest(missing=name):
                self.assertEqual(gp.classify_provider_event(json.dumps(doc)).kind, "invalid")

    def test_documented_close_validates_reason_and_finite_usage(self):
        for reason in ("close_requested", "expired", "content", "remote_hangup", "connection_lost"):
            event = gp.classify_provider_event(frame(type="session.closed", reason=reason))
            self.assertEqual((event.kind, event.close_reason), ("closed", reason))
        for change in ({"reason": []}, {"reason": "unknown"}, {"usage": {}},
                       {"usage": {"seconds": True}}, {"usage": {"seconds": -1}},
                       {"usage": {"seconds": "1"}}, {"usage": {"seconds": 1, "extra": 1}}):
            with self.subTest(change=change):
                self.assertEqual(gp.classify_provider_event(frame(type="session.closed", **change)).kind, "invalid")

    def test_documented_echoes_and_nested_error_fields_are_validated_and_redacted(self):
        self.assertEqual(gp.classify_provider_event(frame(
            type=wire.LIVE_TRANSCRIPT_DELTA, event_id="tx1", client_event_id="capture1",
            delta="hello", start_ms=0, end_ms=2)).kind, "transcript")
        error = gp.classify_provider_event(frame(type="error", error={
            "type": "invalid_request_error", "code": "unknown_parameter",
            "message": MESSAGE_CANARY, "param": "session.voice", "client_event_id": "gw_start",
        }))
        self.assertEqual((error.kind, error.code), ("error", "provider_error"))
        self.assertNotIn(MESSAGE_CANARY, repr(error))
        for bad in (True, None, [], 5):
            self.assertEqual(gp.classify_provider_event(frame(type="error", error={
                "type": "fixture", "code": bad, "message": MESSAGE_CANARY,
            })).kind, "invalid")

    def test_t_prov_classify_lifecycle_requires_documented_metadata(self):
        started = gp.classify_provider_event(frame(type="session.started"))
        self.assertEqual(started.kind, "started")
        self.assertEqual(started.session_id, "live_fixture_1")
        self.assertEqual(started.model, "gpt-live-1")
        self.assertEqual(gp.classify_provider_event(frame(type="session.closed")).kind, "closed")
        self.assertEqual(gp.classify_provider_event(b'{"type":"session.started"}').kind, "invalid")
        self.assertEqual(gp.classify_provider_event(b'{"type":"session.closed"}').kind, "invalid")
        # R3: an unknown key on a frame the gateway acts on is REJECTED, not ignored.
        self.assertEqual(
            gp.classify_provider_event(frame(type="session.closed", reason="x", n=1)),
            gp.ProviderEvent("invalid"),
        )
        self.assertEqual(
            gp.classify_provider_event(frame(type="session.started", session={})),
            gp.ProviderEvent("invalid"),
        )

    def test_t_prov_classify_audio_decodes_even_pcm_of_any_length_within_the_frame(
        self,
    ):
        for pcm in (
            b"\x01\x02",
            b"\x01\x02" * 2400,
            b"\x01\x02" * 50000,
            bytes(range(256)) * 4,
        ):
            with self.subTest(size=len(pcm)):
                event = gp.classify_provider_event(
                    frame(type="session.output_audio.delta", delta=b64(pcm))
                )
                self.assertEqual(event.kind, "audio")
                self.assertEqual(event.pcm, pcm)

    def test_t_prov_classify_invalid_audio(self):
        bad = {
            "odd length": b64(b"\x00\x01\x02"),
            "empty": "",
            "bad alphabet": "!!!!",
            "urlsafe": "-_8=",
            "whitespace": "AAAA\n",
            "bad padding": b64(b"\x00\x01\x02")[:-1],
            "one byte": b64(b"\x00"),
        }
        for label, value in bad.items():
            with self.subTest(label):
                event = gp.classify_provider_event(
                    frame(type="session.output_audio.delta", delta=value)
                )
                self.assertEqual(event.kind, "invalid_audio")
                self.assertIsNone(event.pcm)
        for raw in (
            frame(type="session.output_audio.delta"),
            frame(type="session.output_audio.delta", delta=None),
            frame(type="session.output_audio.delta", delta=5),
            frame(type="session.output_audio.delta", delta=["AA=="]),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(gp.classify_provider_event(raw).kind, "invalid_audio")

    def test_t_prov_classify_transcript_goes_through_the_wire_constants(self):
        self.assertEqual(wire.LIVE_TRANSCRIPT_DELTA, "session.input_transcript.delta")
        event = gp.classify_provider_event(
            frame(
                type=wire.LIVE_TRANSCRIPT_DELTA,
                event_id="t1",
                delta=TEXT_CANARY,
                start_ms=0,
                end_ms=500,
            )
        )
        self.assertEqual(
            (event.kind, event.event_id, event.text, event.start_ms, event.end_ms),
            ("transcript", "t1", TEXT_CANARY, 0, 500),
        )
        empty = gp.classify_provider_event(
            frame(
                type=wire.LIVE_TRANSCRIPT_DELTA,
                event_id="t2",
                delta="",
                start_ms=5,
                end_ms=5,
            )
        )
        self.assertEqual((empty.kind, empty.text), ("transcript", ""))

    def test_t_prov_classify_malformed_transcript_is_invalid_transcript(self):
        base = {
            "type": wire.LIVE_TRANSCRIPT_DELTA,
            "event_id": "t1",
            "delta": "x",
            "start_ms": 0,
            "end_ms": 5,
        }
        changes = {
            "missing start": {"start_ms": None},
            "end before start": {"start_ms": 9, "end_ms": 1},
            "bool ms": {"start_ms": True},
            "float ms": {"end_ms": 5.0},
            "negative ms": {"start_ms": -1},
            "bad event id": {"event_id": "bad id"},
            "missing event id": {"event_id": None},
            "delta not str": {"delta": 5},
            "delta too long": {"delta": "x" * 4001},
        }
        for label, change in changes.items():
            doc = {**base, **change}
            doc = {k: v for k, v in doc.items() if v is not None}
            with self.subTest(label):
                event = gp.classify_provider_event(json.dumps(doc))
                self.assertEqual(event.kind, "invalid_transcript")
                self.assertIsNone(event.text)

    def test_t_prov_classify_delegation_hint_and_malformed_delegation_is_ignored(self):
        self.assertEqual(wire.LIVE_DELEGATION_CREATED, "session.delegation.created")
        good = frame(
            type=wire.LIVE_DELEGATION_CREATED,
            offset_ms=10,
            delegation={"id": "d1", "target": "client", "type": "delegation"},
        )
        event = gp.classify_provider_event(good)
        self.assertEqual((event.kind, event.delegation_id), ("delegation", "d1"))
        bad_docs = (
            {"offset_ms": 10},
            {"offset_ms": 10, "delegation": "d1"},
            {
                "offset_ms": 10,
                "delegation": {"id": "d1", "target": "server", "type": "delegation"},
            },
            {
                "offset_ms": 10,
                "delegation": {"id": "d1", "target": "client", "type": "other"},
            },
            {
                "offset_ms": 10,
                "delegation": {
                    "id": "bad id",
                    "target": "client",
                    "type": "delegation",
                },
            },
            {"offset_ms": 10, "delegation": {"target": "client", "type": "delegation"}},
            {"delegation": {"id": "d1", "target": "client", "type": "delegation"}},
            {
                "offset_ms": True,
                "delegation": {"id": "d1", "target": "client", "type": "delegation"},
            },
        )
        for doc in bad_docs:
            with self.subTest(doc=doc):
                event = gp.classify_provider_event(
                    frame(type=wire.LIVE_DELEGATION_CREATED, **doc)
                )
                self.assertEqual(event, gp.ProviderEvent("ignored"))

    def test_t_prov_classify_ack_requires_a_valid_client_event_id_echo(self):
        event = gp.classify_provider_event(
            frame(
                type="session.commentary.appended",
                client_event_id="gw_e1",
            )
        )
        self.assertEqual((event.kind, event.client_event_id), ("ack", "gw_e1"))
        self.assertEqual(
            gp.classify_provider_event(frame(type="session.commentary.appended")).kind,
            "ack_uncorrelated",
        )
        for doc in (
            {"client_event_id": None},
            {"client_event_id": ""},
            {"client_event_id": "bad id"},
            {"client_event_id": 5},
            {"client_event_id": ["gw_e1"]},
        ):
            with self.subTest(doc=doc):
                event = gp.classify_provider_event(
                    frame(type="session.commentary.appended", **doc)
                )
                self.assertEqual(event.kind, "invalid")
                self.assertIsNone(event.client_event_id)

    def test_t_prov_classify_error_is_a_fixed_local_code_never_provider_text(self):
        # MF5: source-known shape is outer {type, error} with nested {type, code,
        # message}.  The provider's code and message are never retained anywhere.
        code_canary = "CODECANARY-3307"
        nested = gp.classify_provider_event(
            frame(
                type="error",
                error={
                    "type": "invalid_request_error",
                    "code": code_canary,
                    "message": MESSAGE_CANARY,
                },
            )
        )
        self.assertEqual((nested.kind, nested.code), ("error", "provider_error"))
        for code in ("bad code", "x" * 64, "", "a\nb", "caf\u00e9"):
            with self.subTest(code=code):
                event = gp.classify_provider_event(
                    frame(type="error", error={"type": "fixture", "code": code, "message": MESSAGE_CANARY})
                )
                self.assertEqual((event.kind, event.code), ("error", "provider_error"))
        bare = gp.classify_provider_event(frame(type="error"))
        self.assertEqual((bare.kind, bare.code), ("error", "provider_error"))
        for event in (nested, bare):
            for canary in (MESSAGE_CANARY, code_canary):
                self.assertNotIn(canary, repr(event))
                self.assertNotIn(canary, str(event))
                self.assertNotIn(canary, str(dataclasses.asdict(event)))
                self.assertNotIn(canary, str(event.code))

    def test_t_prov_classify_error_unknown_or_misshapen_keys_are_invalid(self):
        for doc in (
            {"code": "server_error", "message": MESSAGE_CANARY},  # not the source shape
            {"error": {"code": "x"}, "extra": 1},
            {"error": {"code": "x", "param": MESSAGE_CANARY}},
            {"error": "boom"},
            {"error": None},
            {"error": ["x"]},
        ):
            with self.subTest(doc=doc):
                event = gp.classify_provider_event(json.dumps({"type": "error", **doc}))
                self.assertEqual(event, gp.ProviderEvent("invalid"))

    def test_t_prov_classify_other_types_are_ignored(self):
        for event_type in (
            "session.output_transcript.delta",
            "session.input_transcript.done",
            "session.output_transcript.done",
            "response.event",
            "response.done",
            "session.done",
            "session.turn.final",
            "session.input_audio.committed",
            "session.commentary.append",
            "SESSION.STARTED",
            "session.started ",
            "",
            "unknown",
        ):
            with self.subTest(event_type=event_type):
                self.assertEqual(
                    gp.classify_provider_event(frame(type=event_type)),
                    gp.ProviderEvent("ignored"),
                )

    def test_t_prov_classify_undecodable_input_is_invalid_and_never_raises(self):
        # An unrecognised type carries the padding, so only size/depth decide the kind.
        head = '{"type":"session.noop","pad":"'
        oversize = head + "x" * (gs.MAX_PROVIDER_FRAME_BYTES - len(head) - 1) + '"}'
        self.assertEqual(len(oversize.encode("utf-8")), gs.MAX_PROVIDER_FRAME_BYTES + 1)
        exact = head + "x" * (gs.MAX_PROVIDER_FRAME_BYTES - len(head) - 2) + '"}'
        self.assertEqual(len(exact.encode("utf-8")), gs.MAX_PROVIDER_FRAME_BYTES)
        self.assertEqual(gp.classify_provider_event(exact).kind, "ignored")
        deep_ok = '{"type":"session.noop","a":' + "[" * 31 + "]" * 31 + "}"
        deep_bad = '{"type":"session.noop","a":' + "[" * 32 + "]" * 32 + "}"
        self.assertEqual(gp.classify_provider_event(deep_ok).kind, "ignored")
        corpus = [
            "",
            "{",
            "[]",
            "1",
            "null",
            '"s"',
            "{}",
            '{"type":1}',
            '{"type":null}',
            '{"type":["session.started"]}',
            '{"type":"session.started","type":"error"}',
            '{"type":"session.started","n":NaN}',
            '{"type":"session.started","n":1e999}',
            deep_bad,
            oversize,
            '{"type":"session.started","x":"\\ud800"}',
            "\ud800",
            b"\xff\xfe",
            b'{"type":"session.started"} x',
            b"",
            None,
            5,
            [],
            {},
            object(),
            "[" * 90000,
        ]
        for raw in corpus:
            with self.subTest(raw=repr(raw)[:40]):
                self.assertEqual(
                    gp.classify_provider_event(raw), gp.ProviderEvent("invalid")
                )

    def test_t_prov_no_done_the_classifier_has_no_done_or_final_kind(self):
        source = Path(gp.__file__).read_text(encoding="utf-8")
        kinds = set()
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ProviderEvent"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                kinds.add(node.args[0].value)
        self.assertEqual(
            kinds,
            {
                "started",
                "closed",
                "audio",
                "invalid_audio",
                "transcript",
                "invalid_transcript",
                "delegation",
                "ack",
                "ack_uncorrelated",
                "error",
                "ignored",
                "invalid",
            },
        )
        for kind in kinds:
            self.assertNotIn("done", kind)
            self.assertNotIn("final", kind)

    def test_t_prov_events_are_frozen_and_repr_hides_text_and_audio(self):
        pcm = b"AUDIOCANARY-77"
        event = gp.classify_provider_event(
            frame(
                type=wire.LIVE_TRANSCRIPT_DELTA,
                event_id="t1",
                delta=TEXT_CANARY,
                start_ms=0,
                end_ms=5,
            )
        )
        audio = gp.classify_provider_event(
            frame(type="session.output_audio.delta", delta=b64(pcm))
        )
        for item in (event, audio):
            shown = repr(item) + str(item)
            self.assertNotIn(TEXT_CANARY, shown)
            self.assertNotIn("AUDIOCANARY", shown)
            self.assertNotIn(b64(pcm), shown)
            with self.assertRaises(dataclasses.FrozenInstanceError):
                item.kind = "x"  # type: ignore[misc]
        self.assertEqual(audio.pcm, pcm)
        self.assertEqual(event.text, TEXT_CANARY)


# -- U1 corrections v2 (R3, canonical base64, near-prefix, chunk compat, pure-IO) -------
GOOD_DELEGATION = {"id": "d1", "target": "client", "type": "delegation"}
CANARY_KEY = "UNKNOWNKEYCANARY-6613"


def good_frames() -> dict[str, dict[str, Any]]:
    """One valid source-shaped frame per recognised provider type."""
    docs = {
        "session.started": {"type": "session.started"},
        "session.closed": {"type": "session.closed"},
        "session.output_audio.delta": {
            "type": "session.output_audio.delta",
            "delta": b64(b"\x00\x01"),
        },
        wire.LIVE_TRANSCRIPT_DELTA: {
            "type": wire.LIVE_TRANSCRIPT_DELTA,
            "event_id": "t1",
            "delta": "hi",
            "start_ms": 0,
            "end_ms": 5,
        },
        wire.LIVE_DELEGATION_CREATED: {
            "type": wire.LIVE_DELEGATION_CREATED,
            "offset_ms": 10,
            "delegation": dict(GOOD_DELEGATION),
        },
        "session.commentary.appended": {
            "type": "session.commentary.appended",
            "client_event_id": "gw_e1",
        },
        "error": {"type": "error", "error": {"type": "e", "code": "c", "message": "m"}},
    }
    return {kind: provider_frame(doc) for kind, doc in docs.items()}


REJECTED_KIND = {
    "session.started": "invalid",
    "session.closed": "invalid",
    "session.output_audio.delta": "invalid_audio",
    wire.LIVE_TRANSCRIPT_DELTA: "invalid_transcript",
    wire.LIVE_DELEGATION_CREATED: "ignored",
    "session.commentary.appended": "invalid",
    "error": "invalid",
}


def noncanonical(canonical: str) -> str:
    """The same decoded bytes with a different, non-zero pad-bit last character."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    stripped = canonical.rstrip("=")
    flipped = alphabet[alphabet.index(stripped[-1]) ^ 1]
    variant = stripped[:-1] + flipped + canonical[len(stripped) :]
    assert variant != canonical
    assert base64.b64decode(variant, validate=True) == base64.b64decode(canonical)
    return variant


class ProviderStrictKeysTest(OfflineTestCase):
    def test_t_prov_r3_the_valid_baseline_frames_all_classify_as_acted_kinds(self):
        expected = {
            "session.started": "started",
            "session.closed": "closed",
            "session.output_audio.delta": "audio",
            wire.LIVE_TRANSCRIPT_DELTA: "transcript",
            wire.LIVE_DELEGATION_CREATED: "delegation",
            "session.commentary.appended": "ack",
            "error": "error",
        }
        for name, doc in good_frames().items():
            with self.subTest(name):
                self.assertEqual(
                    gp.classify_provider_event(json.dumps(doc)).kind, expected[name]
                )

    def test_t_prov_r3_an_unknown_top_level_key_is_rejected_on_every_recognised_kind(
        self,
    ):
        for name, doc in good_frames().items():
            with self.subTest(name):
                bad = {**doc, CANARY_KEY: "x"}
                event = gp.classify_provider_event(json.dumps(bad))
                self.assertEqual(event, gp.ProviderEvent(REJECTED_KIND[name]))
                self.assertNotIn(CANARY_KEY, repr(event))

    def test_t_prov_r3_passthrough_style_keys_are_never_accepted(self):
        for name, doc in good_frames().items():
            for key in ("commands", "payload", "command", "frame", "event", "data"):
                with self.subTest(name=name, key=key):
                    event = gp.classify_provider_event(
                        json.dumps({**doc, key: {"type": "session.start"}})
                    )
                    self.assertEqual(event.kind, REJECTED_KIND[name])
                    self.assertNotEqual(event.kind, "delegation")

    def test_t_prov_r3_nested_delegation_unknown_keys_are_not_a_hint(self):
        for extra in (
            {CANARY_KEY: 1},
            {"action": "send"},  # action-seeking
            {"arguments": {"command": "send"}},
            {"text": "please send this now"},
        ):
            with self.subTest(extra=extra):
                doc = good_frames()[wire.LIVE_DELEGATION_CREATED]
                doc["delegation"] = {**GOOD_DELEGATION, **extra}
                event = gp.classify_provider_event(json.dumps(doc))
                self.assertEqual(event, gp.ProviderEvent("ignored"))

    def test_t_prov_r3_action_seeking_delegation_at_top_level_is_not_a_hint(self):
        doc = good_frames()[wire.LIVE_DELEGATION_CREATED]
        for extra in ({"action": "send"}, {"text": "send it"}, {"tool": "x"}):
            with self.subTest(extra=extra):
                event = gp.classify_provider_event(json.dumps({**doc, **extra}))
                self.assertEqual(event, gp.ProviderEvent("ignored"))
                self.assertIsNone(event.delegation_id)

    def test_t_prov_r3_error_nested_unknown_key_is_invalid_and_leaks_nothing(self):
        event = gp.classify_provider_event(
            frame(type="error", error={"code": "c", CANARY_KEY: MESSAGE_CANARY})
        )
        self.assertEqual(event, gp.ProviderEvent("invalid"))
        for item in (repr(event), str(dataclasses.asdict(event))):
            self.assertNotIn(MESSAGE_CANARY, item)
            self.assertNotIn(CANARY_KEY, item)

    def test_t_prov_r3_the_known_keys_per_type_are_exactly_the_source_shapes(self):
        # Required envelope/payload fields cannot be replaced by a bare type.
        minimal = {
            "session.commentary.appended": {"type": "session.commentary.appended"},
            "error": {"type": "error"},
        }
        self.assertEqual(
            gp.classify_provider_event(json.dumps(minimal["error"])).kind, "invalid"
        )
        self.assertEqual(
            gp.classify_provider_event(
                json.dumps(minimal["session.commentary.appended"])
            ).kind,
            "invalid",
        )
        nested = gp.classify_provider_event(
            frame(type="error", error={"code": "c", "message": "m", "type": "t"})
        )
        self.assertEqual(nested.kind, "error")
        for key in ("type", "code", "message"):
            with self.subTest(key=key):
                self.assertEqual(
                    gp.classify_provider_event(
                        frame(type="error", error={key: "v"})
                    ).kind,
                    "invalid",
                )

    def test_t_prov_near_prefix_event_types_are_ignored(self):
        for event_type in (
            "session.input_transcript",
            "session.input_transcript.delta.x",
            "session.input_transcript.delt",
            "session.input_transcript.delta ",
            "session.delegation",
            "session.delegation.created.x",
            "session.delegation.create",
            "session.commentary.appended.x",
            "session.commentary",
            "session.output_audio",
            "session.output_audio.delta.x",
            "session.started.x",
            "session.close",
            "session.closed.x",
            "error.x",
            "errors",
        ):
            with self.subTest(event_type=event_type):
                self.assertEqual(
                    gp.classify_provider_event(
                        frame(
                            type=event_type,
                            event_id="t1",
                            delta="x",
                            start_ms=0,
                            end_ms=1,
                            delegation=GOOD_DELEGATION,
                        )
                    ),
                    gp.ProviderEvent("ignored"),
                )


class CanonicalBase64Test(OfflineTestCase):
    def test_t_prov_canonical_the_helper_makes_a_same_bytes_noncanonical_string(self):
        self.assertEqual(b64(b"\x00\x01"), "AAE=")
        self.assertEqual(noncanonical("AAE="), "AAF=")

    def test_t_prov_canonical_outbound_audio_rejects_pad_bit_variants(self):
        for raw in (b"\x00\x01", b"\x00\x01\x02\x03", b"\xff\xfe", b"\xaa" * 4798):
            canonical = b64(raw)
            with self.subTest(size=len(raw)):
                gp.build_provider_audio(canonical)  # canonical stays accepted
                variant = noncanonical(canonical)
                with self.assertRaises(ValueError):
                    gp.build_provider_audio(variant)

    def test_t_prov_canonical_inbound_audio_rejects_pad_bit_variants(self):
        for raw in (b"\x00\x01", b"\x00\x01\x02\x03", b"\xff\xfe", b"\xaa" * 4798):
            canonical = b64(raw)
            with self.subTest(size=len(raw)):
                good = gp.classify_provider_event(
                    frame(type="session.output_audio.delta", delta=canonical)
                )
                self.assertEqual((good.kind, good.pcm), ("audio", raw))
                event = gp.classify_provider_event(
                    frame(
                        type="session.output_audio.delta", delta=noncanonical(canonical)
                    )
                )
                self.assertEqual(event, gp.ProviderEvent("invalid_audio"))

    def test_t_prov_canonical_inbound_audio_keeps_the_one_mebibyte_frame_cap(self):
        head = '{"type":"session.output_audio.delta","delta":"'
        room = gs.MAX_PROVIDER_FRAME_BYTES - len(head) - 2
        size = room // 4 * 3 // 6 * 6  # the largest PCM (multiple of 6) that fits
        raw = b"\x01\x02" * (size // 2)
        text = head + b64(raw) + '"}'
        self.assertLessEqual(len(text), gs.MAX_PROVIDER_FRAME_BYTES)
        self.assertEqual(gp.classify_provider_event(text).kind, "audio")
        over = head + b64(raw + b"\x01\x02" * 64) + '"}'
        self.assertGreater(len(over), gs.MAX_PROVIDER_FRAME_BYTES)
        self.assertEqual(gp.classify_provider_event(over), gp.ProviderEvent("invalid"))


class ChunkBuilderCompatTest(OfflineTestCase):
    def test_t_prov_every_chunk_of_a_supported_reply_passes_the_commentary_builder(
        self,
    ):
        texts = (
            "The quick brown fox. " * 190,
            "\U0001f600" * 4000,
            ((chr(0x1F600) * 60 + "\n") * 70)[:4000],
            ("\u00e9! " * 40 + "\n") * 25,
            "\u6f22\u5b57. " * 570,
        )
        for index, text in enumerate(texts):
            with self.subTest(index=index):
                chunks = gs.chunk_commentary(text)
                self.assertEqual("".join(chunks), text)
                for chunk in chunks:
                    doc = json.loads(gp.build_commentary("gw_e1", "d1", chunk))
                    self.assertEqual(doc["content"], chunk)


class PureModuleIoGuardTest(OfflineTestCase):
    def test_t_prov_no_direct_file_or_environment_access_in_the_pure_module(self):
        tree = ast.parse(Path(gp.__file__).read_text(encoding="utf-8"))
        forbidden_names = {"open", "Path", "environ", "getenv", "putenv", "__import__"}
        forbidden_roots = {
            "os",
            "pathlib",
            "io",
            "sys",
            "builtins",
            "shutil",
            "tempfile",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, forbidden_names)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, forbidden_names)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], forbidden_roots)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                self.assertNotIn(node.module.split(".")[0], forbidden_roots)
