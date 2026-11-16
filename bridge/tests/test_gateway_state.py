"""Offline tests for gateway_state: strict JSON, device frames, builders (U1, part 1).

Pure functions are tested directly; no mocks stand in for them.  The nested
``session.start`` fixture is the exact shape of ``PET_LIVE_START`` in
``apps/unified-pet/main/assistant_config.h:9-20`` (model gpt-live-1, store false,
developer greeting input, instructions, 24 kHz PCM, voice marin, Responses delegation).
These tests are offline evidence only, never a live or hardware result.
"""
# pyright: reportArgumentType=false, reportCallIssue=false, reportOptionalMemberAccess=false
# (tests deliberately pass invalid types and read Optionals right after asserting them)

from __future__ import annotations

import ast
import base64
import copy
import dataclasses
import hashlib
import itertools
import json
import random
from pathlib import Path
from typing import Any
from unittest import mock

from pet_bridge import gateway_state as gs
from pet_bridge.gateway_state import DeviceFrame, GatewayProtocolError

from .fakes import OfflineTestCase

UUID4 = "123e4567-e89b-42d3-a456-426614174000"
UUID1 = "123e4567-e89b-12d3-a456-426614174000"
SHA = hashlib.sha256(b"sealed text").hexdigest()
GREETING = (
    "Greet the caller now in English. Say hello, introduce yourself as Tachikoma, "
    "and ask how you can help. Then pause and listen."
)
# Source-backed fixture: apps/unified-pet/main/assistant_config.h:9-20 (PET_LIVE_START).
ASSISTANT_START_DOC: dict[str, Any] = {
    "type": "session.start",
    "event_id": "pet_start",
    "session": {
        "model": "gpt-live-1",
        "store": False,
        "input": [
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": GREETING}],
            }
        ],
        "instructions": (
            "You are Tachikoma, a warm, curious pocket assistant. "
            "Keep spoken answers brief and clear. Delegate reasoning to the backend. "
            "You are in an isolated voice test and cannot access Amplifier Unified, "
            "workspaces, notifications or external actions. Never claim to have done so."
        ),
        "audio": {
            "format": {"type": "audio/pcm", "rate": 24000},
            "output": {"voice": "marin"},
        },
        "delegation": {
            "type": "responses",
            "responses": {
                "model": "gpt-6-luna",
                "instructions": (
                    "Help with concise explanations, planning and reasoning. "
                    "No Unified connector or external tools are available. "
                    "Be explicit about that limitation."
                ),
            },
        },
    },
}
ASSISTANT_START = json.dumps(ASSISTANT_START_DOC, separators=(",", ":"))
# assistant_config.h PET_GREETING_EVENT: legacy greeting, delegation_id JSON null.
ASSISTANT_GREETING = (
    '{"type":"session.commentary.append","event_id":"pet_greeting",'
    '"delegation_id":null,"content":"' + GREETING + '"}'
)


def parse(text: Any) -> DeviceFrame:
    return gs.parse_device_frame(text, model="gpt-live-1", voice="marin")


def code_of(case: OfflineTestCase, text: Any) -> str:
    with case.assertRaises(GatewayProtocolError) as ctx:
        parse(text)
    return ctx.exception.code


def mutated_start(mutator) -> str:
    doc = copy.deepcopy(ASSISTANT_START_DOC)
    mutator(doc)
    return json.dumps(doc, separators=(",", ":"))


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def audio_frame(value: Any) -> str:
    return json.dumps({"type": "session.input_audio.append", "audio": value})


# ---------------------------------------------------------------------------------
class DocstringAndConstantsTest(OfflineTestCase):
    def test_t_state_01_docstring_lists_every_frame_type(self):
        doc = gs.__doc__ or ""
        for frame_type in (
            "session.start",
            "session.commentary.append",
            "session.input_audio.append",
            "pet.turn.begin",
            "pet.turn.capture_stop",
            "pet.turn.send_confirm",
            "pet.stop",
            "session.close",
            "session.started",
            "session.closed",
            "session.output_audio.delta",
            "pet.turn.began",
            "pet.turn.preview",
            "pet.turn.preview_invalidated",
            "pet.turn.send_result",
            "pet.reply.started",
            "pet.reply.done",
            "pet.error",
        ):
            with self.subTest(frame_type=frame_type):
                self.assertIn(frame_type, doc)

    def test_t_state_01_constants_equal_the_plan_values(self):
        expected = {
            "ROUTE": "/v1/pet/live",
            "MAX_DEVICE_FRAME_BYTES": 98304,
            "MAX_PROVIDER_FRAME_BYTES": 1048576,
            "MAX_DEVICE_DEPTH": 16,
            "MAX_PROVIDER_DEPTH": 32,
            "MAX_AUDIO_APPEND_BYTES": 4800,
            "AUDIO_CHUNK_BYTES": 4800,
            "AUDIO_BYTES_PER_S": 48000,
            "AUDIO_QUEUE_CAP": 1048576,
            "CAPTURE_QUEUE_CAP": 96000,
            "SESSION_CAP_S": 60.0,
            "SESSION_WORK_S": 58.0,
            "START_DEADLINE_S": 25.0,
            "CLOSE_DEADLINE_S": 15.0,
            "AWAIT_START_S": 10.0,
            "SETTLE_AFTER_STOP_S": 1.2,
            "SETTLE_QUIET_S": 1.5,
            "SETTLE_MAX_S": 6.0,
            "POLL_INTERVAL_S": 1.0,
            "POLL_MAX_S": 60.0,
            "ACK_WAIT_S": 10.0,
            "INBOX_CAP": 256,
            "MAX_TRANSCRIPT_CHARS": 4000,
            "MAX_SEGMENTS": 128,
            "COMMENTARY_CHUNK_BYTES": 480,
            "MAX_COMMENTARY_CHUNKS": 64,
            "PREVIEW_MAX_CHARS": 4000,
            "PREVIEW_MAX_BYTES": 16000,
            "PREVIEW_REVISION_MAX": 2147483647,
            "SAFE_LABEL_MAX": 40,
            "MAX_PET_TOKENS": 16,
            "AUTH_FAIL_LIMIT": 5,
            "AUTH_WINDOW_S": 60.0,
            "AUTH_REGISTRY_CAP": 256,
            "DEFAULT_MODEL": "gpt-live-1",
            "DEFAULT_VOICE": "marin",
        }
        for name, value in expected.items():
            with self.subTest(name=name):
                actual = getattr(gs, name)
                self.assertEqual(actual, value)
                self.assertIs(type(actual), type(value))

    def test_t_state_pin_error_codes_are_exactly_the_twelve_pinned(self):
        self.assertEqual(
            gs.PET_ERROR_CODES,
            frozenset(
                {
                    "busy",
                    "unknown_command",
                    "model_mismatch",
                    "no_speech",
                    "unstable_transcript",
                    "expired",
                    "no_delegation",
                    "reply_unavailable",
                    "deadline",
                    "overflow",
                    "close_unconfirmed",
                    "invalid_preview",
                }
            ),
        )
        self.assertEqual(len(gs.PET_ERROR_CODES), 12)

    def test_t_state_pin_state_sets_are_exact_frozensets(self):
        self.assertEqual(
            gs.SEND_RESULT_STATES, frozenset({"accepted", "rejected", "uncertain"})
        )
        self.assertEqual(
            gs.REPLY_DONE_STATES, frozenset({"completed", "unavailable", "stopped"})
        )
        self.assertEqual(
            gs.INVALIDATED_REASONS, frozenset({"late_transcript", "stopped", "expired"})
        )
        for value in (
            gs.PET_ERROR_CODES,
            gs.SEND_RESULT_STATES,
            gs.REPLY_DONE_STATES,
            gs.INVALIDATED_REASONS,
        ):
            self.assertIsInstance(value, frozenset)

    def test_every_public_name_is_exported(self):
        for name in gs.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(gs, name))
        self.assertEqual(len(gs.__all__), len(set(gs.__all__)))


# ---------------------------------------------------------------------------------
class StrictJsonTest(OfflineTestCase):
    def loads(self, text, *, max_bytes=gs.MAX_DEVICE_FRAME_BYTES, max_depth=16):
        return gs.strict_loads(text, max_bytes=max_bytes, max_depth=max_depth)

    def assertInvalid(self, text, **kw):
        with self.assertRaises(ValueError) as ctx:
            self.loads(text, **kw)
        self.assertEqual(str(ctx.exception), "invalid_json")

    def test_t_state_json_valid_values_round_trip_and_keep_bool_vs_int(self):
        value = self.loads('{"a":true,"b":1,"c":[null,1.5,"x"],"d":{}}')
        self.assertEqual(value, {"a": True, "b": 1, "c": [None, 1.5, "x"], "d": {}})
        self.assertIs(value["a"], True)
        self.assertIs(type(value["b"]), int)
        self.assertEqual(self.loads(b'{"k":"\xc3\xa9"}'), {"k": "\u00e9"})
        self.assertEqual(self.loads(r'"\ud83d\ude00"'), "\U0001f600")

    def test_t_state_json_duplicate_key_is_rejected_at_any_depth(self):
        for text in (
            '{"a":1,"a":2}',
            '{"a":{"b":1,"b":1}}',
            '[{"x":1,"x":1}]',
            '{"type":"pet.stop","type":"session.close"}',
        ):
            with self.subTest(text=text):
                self.assertInvalid(text)

    def test_t_state_json_nan_infinity_and_overflowing_floats_are_rejected(self):
        for text in (
            "NaN",
            "Infinity",
            "-Infinity",
            '{"a":NaN}',
            "[Infinity]",
            "1e999",
            "-1e999",
            '{"a":1e400}',
        ):
            with self.subTest(text=text):
                self.assertInvalid(text)

    def test_t_state_json_depth_limit_counts_containers_not_string_brackets(self):
        expected = []
        for _ in range(15):
            expected = [expected]  # 16 nested lists, innermost empty
        self.assertEqual(self.loads("[" * 16 + "]" * 16), expected)
        self.assertInvalid("[" * 17 + "]" * 17)
        self.assertInvalid('{"a":' * 17 + "1" + "}" * 17)
        self.assertEqual(self.loads('{"a":"[[[[[[[[[[[[[[[[[[["}'), {"a": "[" * 19})
        deep32 = "[" * 32 + "]" * 32
        expected32 = []
        for _ in range(31):
            expected32 = [expected32]
        self.assertEqual(self.loads(deep32, max_bytes=1024, max_depth=32), expected32)
        self.assertInvalid("[" * 33 + "]" * 33, max_bytes=1024, max_depth=32)

    def test_t_state_json_pathologically_deep_input_is_invalid_not_a_crash(self):
        self.assertInvalid("[" * 90000 + "]" * 90000, max_bytes=1048576)

    def test_t_state_json_size_limit_is_in_utf8_bytes(self):
        head, tail = '{"a":"', '"}'
        room = gs.MAX_DEVICE_FRAME_BYTES - len(head) - len(tail)
        self.assertEqual(len(self.loads(head + "x" * room + tail)["a"]), room)
        self.assertInvalid(head + "x" * (room + 1) + tail)
        two_byte = room // 2
        self.assertEqual(
            len(self.loads(head + "\u00e9" * two_byte + tail)["a"]), two_byte
        )
        # Fewer characters than the limit, more bytes than the limit.
        self.assertInvalid(head + "\u00e9" * (two_byte + 1) + tail)
        self.assertInvalid(b'{"a":"' + b"x" * room + b'1"}')

    def test_t_state_json_bad_utf8_and_lone_surrogates_are_rejected(self):
        for text in (
            b'{"a":"\xff"}',
            b'{"a":"\xc0\xaf"}',
            b'{"a":"\xed\xa0\x80"}',
            '{"a":"\ud800"}',
            r'{"a":"\ud800"}',
            r'{"a":"\udc00"}',
            r'{"\ud800":1}',
            r'["ok",{"deep":["\ud800"]}]',
            b"\xef\xbb\xbf{}",
            "\ufeff{}",
        ):
            with self.subTest(text=text):
                self.assertInvalid(text)

    def test_t_state_json_garbage_empty_and_trailing_data_are_rejected(self):
        for text in (
            "",
            " ",
            "{",
            "{} x",
            "{}{}",
            "'a'",
            "{a:1}",
            "[1,]",
            b"",
            b"\x00",
        ):
            with self.subTest(text=text):
                self.assertInvalid(text)

    def test_t_state_json_non_text_input_is_invalid(self):
        for value in (None, 1, [], {}, object()):
            with self.subTest(value=value):
                self.assertInvalid(value)

    def test_t_state_json_huge_integer_literal_is_invalid(self):
        self.assertInvalid("1" * 5000, max_bytes=8192)


# ---------------------------------------------------------------------------------
class SessionStartFrameTest(OfflineTestCase):
    def test_t_state_start_exact_assistant_config_fixture_parses_as_start(self):
        frame = parse(ASSISTANT_START)
        self.assertEqual(frame, DeviceFrame(kind="start", event_id="pet_start"))

    def test_t_state_start_ignores_input_instructions_and_delegation_without_retaining_them(
        self,
    ):
        names = {f.name for f in dataclasses.fields(DeviceFrame)}
        self.assertEqual(
            names,
            {
                "kind",
                "event_id",
                "turn_id",
                "text_sha256",
                "preview_revision",
                "b64",
                "pcm_len",
            },
        )
        frame = parse(ASSISTANT_START)
        for fragment in ("Tachikoma", "gpt-6-luna", "developer", "responses"):
            self.assertNotIn(fragment, repr(frame))
        for key in ("input", "instructions", "delegation"):
            with self.subTest(removed=key):
                self.assertEqual(
                    parse(mutated_start(lambda d, k=key: d["session"].pop(k))).kind,
                    "start",
                )
        self.assertEqual(
            parse(
                mutated_start(
                    lambda d: d["session"].update(
                        input=[{}] * 16, instructions="", delegation={"x": [1]}
                    )
                )
            ).kind,
            "start",
        )
        minimal = mutated_start(
            lambda d: [
                d["session"].pop(k) for k in ("input", "instructions", "delegation")
            ]
        )
        self.assertEqual(parse(minimal).kind, "start")

    def test_t_state_start_shape_violations_are_unknown_command(self):
        cases = {
            "unknown session key": lambda d: d["session"].update(tools=[]),
            "unknown top key": lambda d: d.update(extra=1),
            "missing event_id": lambda d: d.pop("event_id"),
            "missing session": lambda d: d.pop("session"),
            "session not dict": lambda d: d.update(session=[]),
            "store true": lambda d: d["session"].update(store=True),
            "store int zero": lambda d: d["session"].update(store=0),
            "store string": lambda d: d["session"].update(store="false"),
            "store null": lambda d: d["session"].update(store=None),
            "store missing": lambda d: d["session"].pop("store"),
            "model missing": lambda d: d["session"].pop("model"),
            "model not str": lambda d: d["session"].update(model=1),
            "audio missing": lambda d: d["session"].pop("audio"),
            "audio not dict": lambda d: d["session"].update(audio="x"),
            "audio extra": lambda d: d["session"]["audio"].update(extra=1),
            "audio format missing": lambda d: d["session"]["audio"].pop("format"),
            "audio output missing": lambda d: d["session"]["audio"].pop("output"),
            "format extra": lambda d: d["session"]["audio"]["format"].update(extra=1),
            "format type": lambda d: d["session"]["audio"]["format"].update(
                type="audio/pcm16"
            ),
            "rate string": lambda d: d["session"]["audio"]["format"].update(
                rate="24000"
            ),
            "rate true": lambda d: d["session"]["audio"]["format"].update(rate=True),
            "rate 16000": lambda d: d["session"]["audio"]["format"].update(rate=16000),
            "rate float": lambda d: d["session"]["audio"]["format"].update(
                rate=24000.0
            ),
            "rate missing": lambda d: d["session"]["audio"]["format"].pop("rate"),
            "output extra": lambda d: d["session"]["audio"]["output"].update(extra=1),
            "voice missing": lambda d: d["session"]["audio"]["output"].pop("voice"),
            "voice not str": lambda d: d["session"]["audio"]["output"].update(voice=5),
            "input not list": lambda d: d["session"].update(input="x"),
            "input 17 items": lambda d: d["session"].update(input=[{}] * 17),
            "input item not dict": lambda d: d["session"].update(input=["x"]),
            "instructions not str": lambda d: d["session"].update(instructions=[]),
            "instructions 16385": lambda d: d["session"].update(
                instructions="x" * 16385
            ),
            "delegation not dict": lambda d: d["session"].update(delegation="x"),
        }
        for label, mutator in cases.items():
            with self.subTest(label):
                self.assertEqual(
                    code_of(self, mutated_start(mutator)), "unknown_command"
                )

    def test_t_state_start_bounds_accept_the_edges(self):
        text = mutated_start(
            lambda d: d["session"].update(instructions="x" * 16384, input=[{}] * 16)
        )
        self.assertEqual(parse(text).kind, "start")

    def test_t_state_start_bad_event_ids_are_unknown_command(self):
        for value in (
            "",
            "-x",
            ".x",
            "a b",
            "ok\n",
            "x" * 65,
            "\u00e9",
            5,
            None,
            ["a"],
        ):
            with self.subTest(value=value):
                text = mutated_start(lambda d, v=value: d.update(event_id=v))
                self.assertEqual(code_of(self, text), "unknown_command")
        for value in ("a", "pet_start", "A.b:c-d_e", "x" * 64):
            with self.subTest(good=value):
                text = mutated_start(lambda d, v=value: d.update(event_id=v))
                self.assertEqual(parse(text).event_id, value)

    def test_t_state_start_model_or_voice_mismatch_is_model_mismatch(self):
        wrong_model = mutated_start(lambda d: d["session"].update(model="gpt-live-2"))
        self.assertEqual(code_of(self, wrong_model), "model_mismatch")
        wrong_voice = mutated_start(
            lambda d: d["session"]["audio"]["output"].update(voice="alloy")
        )
        self.assertEqual(code_of(self, wrong_voice), "model_mismatch")
        with self.assertRaises(GatewayProtocolError) as ctx:
            gs.parse_device_frame(ASSISTANT_START, model="gpt-live-2", voice="marin")
        self.assertEqual(ctx.exception.code, "model_mismatch")
        self.assertEqual(
            gs.parse_device_frame(
                ASSISTANT_START, model="gpt-live-1", voice="marin"
            ).kind,
            "start",
        )

    def test_t_state_start_shape_errors_win_over_mismatch(self):
        text = mutated_start(
            lambda d: (
                d["session"].update(model="gpt-live-2"),
                d["session"].update(store=True),
            )
        )
        self.assertEqual(code_of(self, text), "unknown_command")

    def test_t_state_start_error_text_is_the_fixed_code_only(self):
        with self.assertRaises(GatewayProtocolError) as ctx:
            parse(mutated_start(lambda d: d["session"].update(store=True)))
        self.assertEqual(str(ctx.exception), "unknown_command")
        self.assertNotIn("Tachikoma", repr(ctx.exception))


class GreetingFrameTest(OfflineTestCase):
    def test_t_state_greeting_legacy_fixture_is_greeting_without_content(self):
        frame = parse(ASSISTANT_GREETING)
        self.assertEqual(frame, DeviceFrame(kind="greeting", event_id="pet_greeting"))
        self.assertNotIn("Tachikoma", repr(frame))

    def test_t_state_greeting_malformed_variants_are_unknown_command(self):
        base = json.loads(ASSISTANT_GREETING)

        def variant(**changes):
            doc = {**base, **changes}
            return json.dumps(doc)

        def without(key):
            doc = dict(base)
            del doc[key]
            return json.dumps(doc)

        cases = {
            "delegation_id string": variant(delegation_id="x"),
            "extra key": variant(extra=1),
            "empty content": variant(content=""),
            "content 4001": variant(content="x" * 4001),
            "content not str": variant(content=5),
            "bad event id": variant(event_id="bad id"),
            "missing delegation_id": without("delegation_id"),
            "missing content": without("content"),
            "missing event_id": without("event_id"),
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.assertEqual(code_of(self, text), "unknown_command")
        self.assertEqual(parse(variant(content="x" * 4000)).kind, "greeting")


class AudioFrameTest(OfflineTestCase):
    def test_t_state_audio_accepts_the_edges_and_preserves_the_original_string(self):
        for size in (2, 4, 960, 4798, 4800):
            with self.subTest(size=size):
                raw = bytes(range(256)) * 19
                encoded = b64(raw[:size])
                frame = parse(audio_frame(encoded))
                self.assertEqual(
                    frame, DeviceFrame(kind="audio", b64=encoded, pcm_len=size)
                )
                self.assertEqual(frame.b64, encoded)

    def test_t_state_audio_rejects_size_parity_and_alphabet_violations(self):
        good = b64(b"\x00\x01" * 10)
        cases = {
            "4802 bytes": b64(b"\x00" * 4802),
            "4801 bytes": b64(b"\x00" * 4801),
            "odd 3 bytes": b64(b"\x00" * 3),
            "odd 4799": b64(b"\x00" * 4799),
            "1 byte": b64(b"\x00"),
            "empty": "",
            "not base64": "!!!!",
            "urlsafe plus": "-_8=",
            "whitespace tail": good + "\n",
            "whitespace lead": " " + good,
            "inner space": good[:4] + " " + good[4:],
            "missing padding": b64(b"\x00\x01\x02")[:-1],
            "non ascii": "\u00e9\u00e9\u00e9\u00e9",
            "pad then data": "QQ==QQ==",
        }
        for label, value in cases.items():
            with self.subTest(label):
                self.assertEqual(code_of(self, audio_frame(value)), "unknown_command")

    def test_t_state_audio_rejects_wrong_value_types_and_shapes(self):
        for value in (None, 5, True, [], {}, b"QQ=="):
            with self.subTest(value=value):
                text = '{"type":"session.input_audio.append","audio":%s}' % (
                    json.dumps(value) if not isinstance(value, bytes) else "[1]"
                )
                self.assertEqual(code_of(self, text), "unknown_command")
        good = b64(b"\x00\x01")
        for text in (
            json.dumps({"type": "session.input_audio.append"}),
            json.dumps({"type": "session.input_audio.append", "audio": good, "x": 1}),
            json.dumps(
                {"type": "session.input_audio.append", "audio": good, "event_id": "a"}
            ),
        ):
            with self.subTest(text=text):
                self.assertEqual(code_of(self, text), "unknown_command")

    def test_t_state_audio_repr_hides_the_payload(self):
        raw = b"AUDIOCANARY" * 4 + b"ab"
        frame = parse(audio_frame(b64(raw)))
        self.assertNotIn(b64(raw), repr(frame))
        self.assertEqual(frame.pcm_len, len(raw))


class PetCommandFrameTest(OfflineTestCase):
    def test_t_state_pet_begin_stop_close_are_exact_single_key_frames(self):
        for frame_type, kind in (
            ("pet.turn.begin", "begin"),
            ("pet.stop", "stop"),
            ("session.close", "close"),
        ):
            with self.subTest(frame_type=frame_type):
                self.assertEqual(
                    parse(json.dumps({"type": frame_type})), DeviceFrame(kind=kind)
                )
                self.assertEqual(
                    code_of(self, json.dumps({"type": frame_type, "x": 1})),
                    "unknown_command",
                )
                self.assertEqual(
                    code_of(self, json.dumps({"type": frame_type, "turn_id": UUID4})),
                    "unknown_command",
                )

    def test_t_state_pet_capture_stop_requires_a_canonical_lowercase_uuid4(self):
        frame = parse(json.dumps({"type": "pet.turn.capture_stop", "turn_id": UUID4}))
        self.assertEqual(frame, DeviceFrame(kind="capture_stop", turn_id=UUID4))
        bad = (
            UUID1,
            UUID4.upper(),
            UUID4.replace("-", ""),
            "{" + UUID4 + "}",
            "urn:uuid:" + UUID4,
            " " + UUID4,
            UUID4 + "\n",
            "",
            5,
            None,
            [UUID4],
        )
        for value in bad:
            with self.subTest(value=value):
                text = json.dumps({"type": "pet.turn.capture_stop", "turn_id": value})
                self.assertEqual(code_of(self, text), "unknown_command")
        for text in (
            json.dumps({"type": "pet.turn.capture_stop"}),
            json.dumps({"type": "pet.turn.capture_stop", "turn_id": UUID4, "x": 1}),
        ):
            with self.subTest(text=text):
                self.assertEqual(code_of(self, text), "unknown_command")

    def confirm(self, **changes):
        doc = {
            "type": "pet.turn.send_confirm",
            "turn_id": UUID4,
            "text_sha256": SHA,
            "preview_revision": 1,
        }
        doc.update(changes)
        return json.dumps(doc)

    def test_t_state_pet_send_confirm_exact_fields_and_revision_range(self):
        for revision in (1, 2, 2147483647):
            with self.subTest(revision=revision):
                self.assertEqual(
                    parse(self.confirm(preview_revision=revision)),
                    DeviceFrame(
                        kind="send_confirm",
                        turn_id=UUID4,
                        text_sha256=SHA,
                        preview_revision=revision,
                    ),
                )
        for value in (0, -1, 2147483648, True, False, "1", 1.0, 1.5, None, [1]):
            with self.subTest(revision=value):
                self.assertEqual(
                    code_of(self, self.confirm(preview_revision=value)),
                    "unknown_command",
                )

    def test_t_state_pet_send_confirm_digest_must_be_64_lowercase_hex(self):
        for value in (
            SHA.upper(),
            SHA[:-1],
            SHA + "0",
            "g" * 64,
            SHA + "\n",
            " " + SHA,
            "",
            5,
            None,
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    code_of(self, self.confirm(text_sha256=value)), "unknown_command"
                )

    def test_t_state_pet_send_confirm_rejects_missing_extra_and_bad_turn_ids(self):
        for key in ("turn_id", "text_sha256", "preview_revision"):
            doc = json.loads(self.confirm())
            del doc[key]
            with self.subTest(missing=key):
                self.assertEqual(code_of(self, json.dumps(doc)), "unknown_command")
        self.assertEqual(code_of(self, self.confirm(extra=1)), "unknown_command")
        self.assertEqual(code_of(self, self.confirm(turn_id=UUID1)), "unknown_command")
        self.assertEqual(
            code_of(self, self.confirm(turn_id=UUID4.upper())), "unknown_command"
        )


class MalformedFrameTest(OfflineTestCase):
    def test_t_state_unknown_and_outbound_types_are_unknown_command(self):
        for frame_type in (
            "pet.turn.cancel",
            "pet.turn.began",
            "pet.turn.preview",
            "pet.reply.done",
            "pet.error",
            "session.started",
            "session.closed",
            "session.output_audio.delta",
            "session.delegation.created",
            "session.input_transcript.delta",
            "session.start ",
            "SESSION.CLOSE",
            "",
        ):
            with self.subTest(frame_type=frame_type):
                self.assertEqual(
                    code_of(self, json.dumps({"type": frame_type})), "unknown_command"
                )

    def test_t_state_non_object_or_untyped_json_is_unknown_command(self):
        for text in (
            "[]",
            "1",
            "null",
            '"s"',
            "true",
            "{}",
            '{"type":1}',
            '{"type":null}',
            '{"type":["pet.stop"]}',
            '{"TYPE":"pet.stop"}',
        ):
            with self.subTest(text=text):
                self.assertEqual(code_of(self, text), "unknown_command")

    def test_t_state_undecodable_oversize_and_non_text_input_is_unknown_command(self):
        head, tail = '{"type":"pet.stop","pad":"', '"}'
        pad = gs.MAX_DEVICE_FRAME_BYTES - len(head) - len(tail)
        # At the limit the frame is read, then refused for its extra key; one byte more is
        # refused as oversize.  Both are the same fixed code.
        self.assertEqual(code_of(self, head + "x" * pad + tail), "unknown_command")
        self.assertEqual(
            code_of(self, head + "x" * (pad + 1) + tail), "unknown_command"
        )
        for value in (
            "{",
            "",
            '{"type":"pet.stop","type":"pet.stop"}',
            '{"type":"pet.stop","n":NaN}',
            "[" * 17 + "]" * 17,
            b'{"type":"pet.stop"}',
            None,
            5,
            b"\xff",
        ):
            with self.subTest(value=value):
                self.assertEqual(code_of(self, value), "unknown_command")

    def test_t_state_depth_17_frame_is_refused_even_inside_an_ignored_field(self):
        text = mutated_start(
            lambda d: d["session"].update(
                delegation={"a": json.loads("[" * 14 + "]" * 14)}
            )
        )
        # Depth: root 1 + session 2 + delegation 3 + a(list) 4 .. 17 is over the cap of 16.
        self.assertEqual(code_of(self, text), "unknown_command")

    def test_t_state_valid_frames_do_not_mutate_between_calls(self):
        first = parse(ASSISTANT_START)
        second = parse(ASSISTANT_START)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)


# ---------------------------------------------------------------------------------
class OutboundBuilderTest(OfflineTestCase):
    def test_t_state_out_session_started_and_closed_are_exact(self):
        self.assertEqual(gs.build_session_started(), '{"type":"session.started"}')
        self.assertEqual(gs.build_session_closed(), '{"type":"session.closed"}')

    def test_t_state_out_audio_uses_the_delta_key_with_padded_standard_base64(self):
        pcm = bytes([1, 2, 3, 4, 250, 251])
        self.assertEqual(
            gs.build_audio_delta(pcm),
            '{"type":"session.output_audio.delta","delta":"AQIDBPr7"}',
        )
        two = gs.build_audio_delta(b"\xfb\xff")
        self.assertEqual(two, '{"type":"session.output_audio.delta","delta":"+/8="}')
        frame = json.loads(gs.build_audio_delta(b"\x00\x01" * 2400))
        self.assertEqual(set(frame), {"type", "delta"})
        self.assertNotIn("audio", frame)
        self.assertEqual(
            base64.b64decode(frame["delta"], validate=True), b"\x00\x01" * 2400
        )

    def test_t_state_out_audio_rejects_bad_sizes_and_types(self):
        for value in (
            b"",
            b"\x00",
            b"\x00" * 3,
            b"\x00" * 4802,
            b"\x00" * 4801,
            "ab",
            None,
            bytearray(b"\x00\x01"),
            5,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gs.build_audio_delta(value)

    def test_t_state_out_turn_began_is_exact_and_validates_every_field(self):
        self.assertEqual(
            gs.build_turn_began(UUID4, "Build bot", "deadbeef"),
            f'{{"type":"pet.turn.began","turn_id":"{UUID4}","safe_session_label":"Build bot",'
            '"session_id_short":"deadbeef"}',
        )
        gs.build_turn_began(UUID4, "x" * 40, "00000000")
        for args in (
            ("not-a-uuid", "label", "deadbeef"),
            (UUID1, "label", "deadbeef"),
            (UUID4.upper(), "label", "deadbeef"),
            (UUID4, "", "deadbeef"),
            (UUID4, "x" * 41, "deadbeef"),
            (UUID4, "line\nbreak", "deadbeef"),
            (UUID4, "two  spaces", "deadbeef"),
            (UUID4, "ctl\x00", "deadbeef"),
            (UUID4, "label", "DEADBEEF"),
            (UUID4, "label", "deadbee"),
            (UUID4, "label", "deadbeef0"),
            (UUID4, "label", "deadbeeg"),
            (UUID4, 5, "deadbeef"),
            (UUID4, "label", None),
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                gs.build_turn_began(*args)

    def test_t_state_out_preview_is_exact_with_raw_utf8_and_escaped_controls(self):
        text = 'h\u00e9llo "q"\n'
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.assertEqual(
            gs.build_preview(UUID4, 3, text, sha),
            f'{{"type":"pet.turn.preview","turn_id":"{UUID4}","preview_revision":3,'
            f'"text":"h\u00e9llo \\"q\\"\\n","text_sha256":"{sha}"}}',
        )

    def test_t_state_out_preview_hash_must_be_the_exact_sealed_digest(self):
        text = "sealed text"
        good = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.assertIn(good, gs.build_preview(UUID4, 1, text, good))
        for sha in (
            good.upper(),
            good[:-1],
            good + "0",
            hashlib.sha256(b"other").hexdigest(),
            "0" * 64,
            "",
            None,
        ):
            with self.subTest(sha=sha), self.assertRaises(ValueError):
                gs.build_preview(UUID4, 1, text, sha)

    def test_t_state_out_size_preview_edges_are_4000_chars_and_16000_bytes(self):
        text = "\U0001f600" * 4000
        self.assertEqual(len(text.encode("utf-8")), 16000)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        frame = gs.build_preview(UUID4, 1, text, sha)
        self.assertLess(len(frame.encode("utf-8")), gs.MAX_DEVICE_FRAME_BYTES)
        self.assertEqual(json.loads(frame)["text"], text)
        over = text + "x"
        with self.assertRaises(ValueError):
            gs.build_preview(
                UUID4, 1, over, hashlib.sha256(over.encode("utf-8")).hexdigest()
            )
        ascii_ok = "a" * 4000
        gs.build_preview(
            UUID4, 1, ascii_ok, hashlib.sha256(ascii_ok.encode()).hexdigest()
        )

    def test_t_state_out_size_preview_rejects_bad_revision_text_and_surrogates(self):
        text = "x"
        sha = hashlib.sha256(b"x").hexdigest()
        for revision in (0, -1, 2147483648, True, False, "1", 1.0, None):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                gs.build_preview(UUID4, revision, text, sha)
        gs.build_preview(UUID4, 2147483647, text, sha)
        for bad_text in (5, None, b"x", "\ud800"):
            with self.subTest(text=bad_text), self.assertRaises(ValueError):
                gs.build_preview(UUID4, 1, bad_text, sha)
        with self.assertRaises(ValueError):
            gs.build_preview("nope", 1, text, sha)

    def test_t_state_out_size_every_builder_asserts_the_frame_cap(self):
        text = "x" * 100
        sha = hashlib.sha256(text.encode()).hexdigest()
        with mock.patch.object(gs, "MAX_DEVICE_FRAME_BYTES", 20):
            with self.assertRaises(ValueError):
                gs.build_preview(UUID4, 1, text, sha)
            with self.assertRaises(ValueError):
                gs.build_audio_delta(b"\x00\x01" * 100)
            with self.assertRaises(ValueError):
                gs.build_turn_began(UUID4, "label", "deadbeef")

    def test_t_state_out_preview_invalidated_send_result_and_reply_done_are_exact(self):
        for reason in sorted(gs.INVALIDATED_REASONS):
            self.assertEqual(
                gs.build_preview_invalidated(UUID4, reason),
                f'{{"type":"pet.turn.preview_invalidated","turn_id":"{UUID4}","reason":"{reason}"}}',
            )
        for state in sorted(gs.SEND_RESULT_STATES):
            self.assertEqual(
                gs.build_send_result(UUID4, state),
                f'{{"type":"pet.turn.send_result","turn_id":"{UUID4}","state":"{state}"}}',
            )
        for state in sorted(gs.REPLY_DONE_STATES):
            self.assertEqual(
                gs.build_reply_done(UUID4, state),
                f'{{"type":"pet.reply.done","turn_id":"{UUID4}","state":"{state}"}}',
            )

    def test_t_state_out_enum_arguments_outside_the_pinned_sets_raise_value_error(self):
        builders = (
            (
                gs.build_preview_invalidated,
                ("late", "invalidated", "", None, "LATE_INPUT"),
            ),
            (gs.build_send_result, ("sent", "failed", "", None, "Accepted")),
            (gs.build_reply_done, ("done", "failed", "", None, "Completed")),
        )
        valid_member = {
            gs.build_preview_invalidated: "late_transcript",
            gs.build_send_result: "accepted",
            gs.build_reply_done: "stopped",
        }
        for builder, values in builders:
            for value in values:
                with (
                    self.subTest(builder=builder.__name__, value=value),
                    self.assertRaises(ValueError),
                ):
                    builder(UUID4, value)
            with (
                self.subTest(builder=builder.__name__, turn="bad"),
                self.assertRaises(ValueError),
            ):
                builder("bad", valid_member[builder])
        with self.assertRaises(ValueError):
            gs.build_reply_started("bad")
        with self.assertRaises(ValueError):
            gs.build_reply_started(UUID1)

    def test_t_state_out_reply_started_carries_the_rendering_label_constant(self):
        self.assertEqual(
            gs.build_reply_started(UUID4),
            f'{{"type":"pet.reply.started","turn_id":"{UUID4}",'
            '"rendering":"spoken_rendering_not_verbatim"}',
        )
        frame = json.loads(gs.build_reply_started(UUID4))
        self.assertEqual(frame["rendering"], "spoken_rendering_not_verbatim")
        self.assertEqual(set(frame), {"type", "turn_id", "rendering"})

    def test_t_state_out_pet_error_emits_only_the_twelve_pinned_codes(self):
        for code in sorted(gs.PET_ERROR_CODES):
            with self.subTest(code=code):
                self.assertEqual(
                    gs.build_pet_error(code), f'{{"type":"pet.error","code":"{code}"}}'
                )
        for code in (
            "provider_error",
            "close",
            "internal",
            "",
            None,
            "BUSY",
            "busy ",
            5,
        ):
            with self.subTest(code=code), self.assertRaises(ValueError):
                gs.build_pet_error(code)

    def test_t_state_out_frames_are_compact_text_with_no_nan_or_ascii_escaping(self):
        frame = gs.build_pet_error("busy")
        self.assertNotIn(" ", frame)
        self.assertIsInstance(frame, str)


class EventIdAndAudioHelperTest(OfflineTestCase):
    def test_t_state_event_id_format_is_exact_with_no_trailing_newline(self):
        for value in ("a", "pet_start", "gw_" + "0" * 32, "A.b:c-d_e", "x" * 64):
            with self.subTest(value=value):
                self.assertTrue(gs.is_event_id(value))
        for value in (
            "",
            "-a",
            "_a",
            ".a",
            "a b",
            "a\n",
            "\na",
            "x" * 65,
            "\u00e9",
            5,
            None,
            b"a",
        ):
            with self.subTest(value=value):
                self.assertFalse(gs.is_event_id(value))

    def test_t_state_audio_b64_length_helper_matches_frame_rules(self):
        self.assertEqual(gs.audio_b64_len(b64(b"\x00\x01")), 2)
        self.assertEqual(gs.audio_b64_len(b64(b"\x00" * 4800)), 4800)
        for value in (
            b64(b"\x00" * 4802),
            b64(b"\x00"),
            b64(b"\x00" * 3),
            "",
            "!!!!",
            None,
            5,
        ):
            with self.subTest(value=value):
                self.assertIsNone(gs.audio_b64_len(value))


class ModelVoiceNameTest(OfflineTestCase):
    def test_t_state_model_and_voice_names_use_the_config_formats(self):
        for value in ("gpt-live-1", "a", "a.b-c", "x" * 64, "0a"):
            with self.subTest(model=value):
                self.assertTrue(gs.valid_model(value))
        for value in ("GPT", "", "-a", "a b", "x" * 65, "a\n", None, 5, b"a"):
            with self.subTest(bad_model=value):
                self.assertFalse(gs.valid_model(value))
        for value in ("marin", "a", "a_b-c", "x" * 32):
            with self.subTest(voice=value):
                self.assertTrue(gs.valid_voice(value))
        for value in ("Marin", "", "1a", "a b", "x" * 33, "a\n", None, 5, b"a"):
            with self.subTest(bad_voice=value):
                self.assertFalse(gs.valid_voice(value))


class SessionLabelHelpersTest(OfflineTestCase):
    def test_t_state_short8_is_the_last_eight_lowercase_hex_as_is(self):
        self.assertEqual(gs.valid_short8("sess-0123abcd"), "0123abcd")
        self.assertEqual(gs.valid_short8("deadbeef"), "deadbeef")
        self.assertEqual(gs.valid_short8("x" * 100 + "00000000"), "00000000")
        for value in (
            "sess-0123ABCD",
            "DEADBEEF",
            "sess-deadBEEF",
            "sess-xyz12345",
            "abc",
            "",
            "sess-0123abc",
            "sess-0123abcdg",
            "sess-0123abc\n",
            None,
            5,
            b"deadbeef",
        ):
            with self.subTest(value=value):
                self.assertIsNone(gs.valid_short8(value))

    def test_t_state_safe_label_is_the_sanitized_display_form_of_one_to_forty_chars(
        self,
    ):
        for value in ("Build bot", "a", "x" * 40, "caf\u00e9 \u6f22\u5b57"):
            with self.subTest(value=value):
                self.assertEqual(gs.safe_label(value), value)
        for value in (
            "",
            "x" * 41,
            "a  b",
            " a",
            "a ",
            "a\nb",
            "a\tb",
            "a\x00b",
            "a\u200bb",
            "a\ud800b",
            None,
            5,
            b"abc",
        ):
            with self.subTest(value=value):
                self.assertIsNone(gs.safe_label(value))


# ---------------------------------------------------------------------------------
class TranscriptStagerTest(OfflineTestCase):
    def test_t_state_stager_out_of_order_deltas_are_sorted_by_start_end_event_id(self):
        stager = gs.TranscriptStager()
        self.assertEqual(stager.add("e3", "c", 20, 30), "added")
        self.assertEqual(stager.add("e1", "a", 0, 10), "added")
        self.assertEqual(stager.add("e2", "b", 10, 20), "added")
        self.assertEqual(stager.ordered(), (("e1", "a"), ("e2", "b"), ("e3", "c")))
        self.assertEqual(stager.text(), "abc")
        self.assertEqual((stager.count, stager.chars), (3, 3))

    def test_t_state_stager_ties_break_on_end_then_event_id(self):
        stager = gs.TranscriptStager()
        stager.add("b", "2", 5, 5)
        stager.add("a", "1", 5, 5)
        stager.add("c", "0", 5, 6)
        self.assertEqual(stager.ordered(), (("a", "1"), ("b", "2"), ("c", "0")))

    def test_t_state_stager_identical_duplicate_is_ignored_and_not_counted(self):
        stager = gs.TranscriptStager()
        self.assertEqual(stager.add("e1", "hello", 0, 10), "added")
        self.assertEqual(stager.add("e1", "hello", 0, 10), "duplicate")
        self.assertEqual((stager.count, stager.chars, stager.text()), (1, 5, "hello"))

    def test_t_state_stager_same_id_with_different_text_or_time_is_a_conflict(self):
        for other in (
            ("e1", "HELLO", 0, 10),
            ("e1", "hello", 0, 11),
            ("e1", "hello", 1, 10),
        ):
            with self.subTest(other=other):
                stager = gs.TranscriptStager()
                stager.add("e1", "hello", 0, 10)
                with self.assertRaises(GatewayProtocolError) as ctx:
                    stager.add(*other)
                self.assertEqual(ctx.exception.code, "conflict")
                self.assertEqual(stager.ordered(), (("e1", "hello"),))

    def test_t_state_stager_overlapping_intervals_are_rejected(self):
        for start, end in ((5, 15), (0, 10), (2, 4), (-0, 1), (9, 30), (0, 30)):
            with self.subTest(start=start, end=end):
                stager = gs.TranscriptStager()
                stager.add("e1", "a", 0, 10)
                with self.assertRaises(GatewayProtocolError) as ctx:
                    stager.add("e2", "b", start, end)
                self.assertEqual(ctx.exception.code, "overlap")
                self.assertEqual(stager.count, 1)

    def test_t_state_stager_adjacent_and_zero_length_segments_never_overlap(self):
        stager = gs.TranscriptStager()
        stager.add("e1", "a", 0, 10)
        self.assertEqual(stager.add("e2", "b", 10, 20), "added")  # adjacent
        self.assertEqual(stager.add("z1", "", 5, 5), "added")  # zero length inside e1
        self.assertEqual(stager.add("z2", "", 5, 5), "added")  # two at the same instant
        self.assertEqual(stager.add("z3", "", 0, 0), "added")
        self.assertEqual(stager.add("e0", "x", -0, 0), "added")
        self.assertEqual(stager.count, 6)

    def test_t_state_stager_segment_cap_is_128(self):
        stager = gs.TranscriptStager()
        for index in range(128):
            stager.add(f"e{index}", "a", index, index + 1)
        self.assertEqual(stager.count, 128)
        with self.assertRaises(GatewayProtocolError) as ctx:
            stager.add("e128", "a", 128, 129)
        self.assertEqual(ctx.exception.code, "too_many")
        self.assertEqual(stager.count, 128)
        # A replay of an accepted segment stays harmless at the cap.
        self.assertEqual(stager.add("e5", "a", 5, 6), "duplicate")

    def test_t_state_stager_char_cap_is_4000_including_across_segments(self):
        stager = gs.TranscriptStager()
        self.assertEqual(stager.add("a", "x" * 4000, 0, 10), "added")
        self.assertEqual(stager.chars, 4000)
        with self.assertRaises(GatewayProtocolError) as ctx:
            stager.add("b", "y", 10, 11)
        self.assertEqual(ctx.exception.code, "too_long")
        single = gs.TranscriptStager()
        with self.assertRaises(GatewayProtocolError) as ctx:
            single.add("a", "x" * 4001, 0, 10)
        self.assertEqual(ctx.exception.code, "too_long")
        split = gs.TranscriptStager()
        split.add("a", "x" * 2000, 0, 10)
        with self.assertRaises(GatewayProtocolError) as ctx:
            split.add("b", "y" * 2001, 10, 20)
        self.assertEqual(ctx.exception.code, "too_long")
        self.assertEqual((split.count, split.chars), (1, 2000))

    def test_t_state_stager_invalid_arguments_are_rejected_without_state_change(self):
        bad = (
            ("bad id", "a", 0, 1),
            ("", "a", 0, 1),
            (None, "a", 0, 1),
            ("e", 5, 0, 1),
            ("e", None, 0, 1),
            ("e", "a", -1, 1),
            ("e", "a", 0, -1),
            ("e", "a", 5, 4),
            ("e", "a", True, 1),
            ("e", "a", 0, True),
            ("e", "a", 0.0, 1),
            ("e", "a", "0", 1),
            ("e", "a\ud800", 0, 1),
        )
        stager = gs.TranscriptStager()
        for args in bad:
            with self.subTest(args=args):
                with self.assertRaises(GatewayProtocolError) as ctx:
                    stager.add(*args)
                self.assertEqual(ctx.exception.code, "invalid")
        self.assertEqual((stager.count, stager.chars, stager.ordered()), (0, 0, ()))

    def test_t_state_stager_repr_shows_counts_only(self):
        stager = gs.TranscriptStager()
        stager.add("secret-event-id", "SECRETWORDS", 0, 10)
        shown = repr(stager) + str(stager)
        self.assertNotIn("SECRETWORDS", shown)
        self.assertNotIn("secret-event-id", shown)
        self.assertIn("1", shown)


class CommentaryChunkTest(OfflineTestCase):
    def assertLossless(self, text, chunks):
        self.assertEqual("".join(chunks), text)
        for chunk in chunks:
            self.assertTrue(chunk)
            self.assertLessEqual(len(chunk.encode("utf-8")), 480)

    def test_t_state_chunk_short_text_is_one_chunk(self):
        self.assertEqual(gs.chunk_commentary("hello"), ("hello",))
        text = "a" * 480
        self.assertEqual(gs.chunk_commentary(text), (text,))

    def test_t_state_chunk_lossless_for_ascii_emoji_cjk_and_combining(self):
        samples = {
            "ascii": "The quick brown fox. " * 80,
            "emoji": "\U0001f600" * 130,
            "cjk": "\u6f22" * 161,
            "combining": "e\u0301" * 400,
            "two byte": "a" + "\u00e9" * 300,
            "mixed": ("Caf\u00e9 \U0001f600 \u6f22\u5b57! " * 60),
            "long word": "q" * 1000,
        }
        for label, text in samples.items():
            with self.subTest(label):
                chunks = gs.chunk_commentary(text)
                self.assertLossless(text, chunks)

    def test_t_state_chunk_never_splits_a_code_point(self):
        text = "\U0001f600" * 130
        chunks = gs.chunk_commentary(text)
        self.assertEqual([len(c) for c in chunks], [120, 10])
        for chunk in chunks:
            chunk.encode("utf-8")  # raises on a split surrogate pair
        cjk = gs.chunk_commentary("\u6f22" * 161)
        self.assertEqual([len(c) for c in cjk], [160, 1])
        two = gs.chunk_commentary("a" + "\u00e9" * 300)
        self.assertEqual(len(two[0].encode("utf-8")), 479)

    def test_t_state_chunk_prefers_a_sentence_boundary(self):
        first = "x" * 300 + ". "
        second = "y" * 300 + ". "
        self.assertEqual(gs.chunk_commentary(first + second), (first, second))
        for terminator in ("! ", "? "):
            with self.subTest(terminator=terminator):
                a, b = "x" * 300 + terminator, "y" * 300
                self.assertEqual(gs.chunk_commentary(a + b), (a, b))
        newline_a, newline_b = "x" * 300 + "\n", "y" * 300
        self.assertEqual(
            gs.chunk_commentary(newline_a + newline_b), (newline_a, newline_b)
        )

    def test_t_state_chunk_sentence_beats_a_later_whitespace(self):
        text = "x" * 200 + ". " + "y" * 100 + " " + "z" * 300
        chunks = gs.chunk_commentary(text)
        self.assertEqual(chunks[0], "x" * 200 + ". ")
        self.assertLossless(text, chunks)

    def test_t_state_chunk_falls_back_to_whitespace_then_a_hard_cut(self):
        spaced = ("w" * 300 + " ", "z" * 300)
        self.assertEqual(gs.chunk_commentary("".join(spaced)), spaced)
        hard = gs.chunk_commentary("q" * 1000)
        self.assertEqual([len(c) for c in hard], [480, 480, 40])

    def test_t_state_chunk_whitespace_only_text_stays_lossless(self):
        text = " " * 1000
        self.assertLossless(text, gs.chunk_commentary(text))

    def test_t_state_chunk_four_thousand_four_byte_chars_fit_in_64_chunks(self):
        text = "\U0001f600" * 4000
        chunks = gs.chunk_commentary(text)
        self.assertLessEqual(len(chunks), 64)
        self.assertEqual(len(chunks), 34)
        self.assertLossless(text, chunks)

    def test_t_state_chunk_count_bound_is_64(self):
        exact = "a" * (64 * 480)
        self.assertEqual(len(gs.chunk_commentary(exact)), 64)
        with self.assertRaises(ValueError):
            gs.chunk_commentary(exact + "a")

    def test_t_state_chunk_rejects_empty_non_text_and_surrogates(self):
        for value in ("", None, 5, b"abc", "a\ud800b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gs.chunk_commentary(value)

    def test_t_state_chunk_property_lossless_over_pseudo_random_text(self):
        pool = [
            "a",
            "b",
            " ",
            " ",
            ". ",
            "! ",
            "? ",
            "\n",
            "\u00e9",
            "\u6f22",
            "\U0001f600",
            "e\u0301",
            "word",
        ]
        rng = random.Random(20261111)
        for round_no in range(300):
            text = "".join(rng.choice(pool) for _ in range(rng.randint(1, 900)))
            with self.subTest(round=round_no):
                chunks = gs.chunk_commentary(text)
                self.assertLossless(text, chunks)
                self.assertLessEqual(len(chunks), 64)


class AudioPacingMathTest(OfflineTestCase):
    def test_t_state_split_pcm_chunks_are_even_and_at_most_4800(self):
        pcm = bytes(range(256)) * 38  # 9728 bytes
        chunks = gs.split_pcm(pcm)
        self.assertEqual([len(c) for c in chunks], [4800, 4800, 128])
        self.assertEqual(b"".join(chunks), pcm)
        self.assertEqual(gs.split_pcm(b"\x00" * 9600), (b"\x00" * 4800, b"\x00" * 4800))
        self.assertEqual(gs.split_pcm(b"\x00\x01"), (b"\x00\x01",))
        self.assertEqual(gs.split_pcm(b""), ())
        self.assertEqual([len(c) for c in gs.split_pcm(b"\x00" * 10, 4)], [4, 4, 2])

    def test_t_state_split_pcm_rejects_odd_lengths_and_bad_sizes(self):
        for args in (
            (b"\x00" * 3,),
            (b"\x00",),
            (b"\x00" * 4, 3),
            (b"\x00" * 4, 0),
            (b"\x00" * 4, -2),
            (b"\x00" * 4, True),
            ("ab",),
            (None,),
            (bytearray(b"\x00\x01"),),
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                gs.split_pcm(*args)

    def test_t_state_pace_4800_bytes_is_exactly_one_tenth_of_a_second(self):
        send_at, nxt = gs.next_send_time(None, 0.0, 4800)
        self.assertEqual((send_at, nxt), (0.0, 0.1))
        self.assertEqual(4800 / gs.AUDIO_BYTES_PER_S, 0.1)
        send_at2, nxt2 = gs.next_send_time(nxt, 0.0, 4800)
        self.assertEqual(send_at2, 0.1)
        self.assertAlmostEqual(nxt2, 0.2, places=12)
        send_at3, nxt3 = gs.next_send_time(nxt2, 0.0, 4800)
        self.assertAlmostEqual(send_at3, 0.2, places=12)
        self.assertAlmostEqual(nxt3, 0.3, places=12)

    def test_t_state_pace_idle_resets_the_schedule_to_now(self):
        self.assertEqual(gs.next_send_time(1.0, 5.0, 4800), (5.0, 5.0 + 0.1))
        self.assertEqual(gs.next_send_time(5.0, 5.0, 2400), (5.0, 5.05))
        send_at, nxt = gs.next_send_time(7.0, 5.0, 96000)
        self.assertEqual(send_at, 7.0)
        self.assertEqual(nxt, 9.0)

    def test_t_state_pace_rejects_non_positive_or_non_int_sizes(self):
        for nbytes in (0, -1, True, 4800.0, None, "4800"):
            with self.subTest(nbytes=nbytes), self.assertRaises(ValueError):
                gs.next_send_time(None, 0.0, nbytes)


# ---------------------------------------------------------------------------------
HASH_A = hashlib.sha256(b"token-a").hexdigest()
HASH_B = hashlib.sha256(b"token-b").hexdigest()
CRED = "device-" + "0123456789abcdef" * 2


def good_config(drop=(), **changes) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "version": 1,
        "state_root": "/var/pet/state",
        "credential_ref": CRED,
        "session_id": "sess-0123abcd",
        "session_label": "Build bot",
        "bind_host": "192.168.1.10",
        "port": 8443,
        "host_header": "192.168.1.10:8443",
        "tls_cert_file": "/etc/pet/cert.pem",
        "tls_key_file": "/etc/pet/key.pem",
        "pet_token_sha256": [HASH_A],
        "provider_key_env": "PET_PROVIDER_KEY",
    }
    doc.update(changes)
    for key in drop:
        doc.pop(key, None)
    return doc


def config_code(case: OfflineTestCase, doc: Any) -> str:
    with case.assertRaises(GatewayProtocolError) as ctx:
        gs.parse_config(doc)
    return ctx.exception.code


class ConfigAcceptTest(OfflineTestCase):
    def test_explicit_existing_auth_root_is_optional_private_and_not_in_repr(self):
        cfg = gs.parse_config(good_config(auth_root="/private/existing/auth"))
        self.assertEqual(cfg.auth_root, "/private/existing/auth")
        self.assertNotIn("/private/existing/auth", repr(cfg))
        self.assertIsNone(gs.parse_config(good_config()).auth_root)
        for bad in (None, "", "relative", "/a/../b", "/a/./b", "/a\nb", 1):
            with self.subTest(bad=bad):
                self.assertEqual(config_code(self, good_config(auth_root=bad)), "config_invalid")

    def test_t_state_cfg_a_good_document_parses_with_defaults(self):
        cfg = gs.parse_config(good_config())
        self.assertEqual(cfg.state_root, "/var/pet/state")
        self.assertEqual(cfg.credential_ref, CRED)
        self.assertEqual(cfg.session_id, "sess-0123abcd")
        self.assertEqual(cfg.session_id_short, "0123abcd")
        self.assertEqual(cfg.session_label, "Build bot")
        self.assertEqual((cfg.bind_host, cfg.port), ("192.168.1.10", 8443))
        self.assertEqual(cfg.host_header, "192.168.1.10:8443")
        self.assertEqual(
            (cfg.tls_cert_file, cfg.tls_key_file),
            ("/etc/pet/cert.pem", "/etc/pet/key.pem"),
        )
        self.assertEqual(cfg.pet_token_sha256, (HASH_A,))
        self.assertIsInstance(cfg.pet_token_sha256, tuple)
        self.assertEqual((cfg.model, cfg.voice), ("gpt-live-1", "marin"))
        self.assertEqual(
            (cfg.provider_key_env, cfg.provider_key_file), ("PET_PROVIDER_KEY", None)
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cfg.port = 1  # type: ignore[misc]

    def test_t_state_cfg_provider_key_file_form_and_explicit_model_voice(self):
        doc = good_config(
            drop=("provider_key_env",),
            provider_key_file="/etc/pet/provider.key",
            model="gpt-live-2",
            voice="alloy",
            pet_token_sha256=[HASH_A, HASH_B],
        )
        cfg = gs.parse_config(doc)
        self.assertEqual(
            (cfg.provider_key_file, cfg.provider_key_env),
            ("/etc/pet/provider.key", None),
        )
        self.assertEqual((cfg.model, cfg.voice), ("gpt-live-2", "alloy"))
        self.assertEqual(cfg.pet_token_sha256, (HASH_A, HASH_B))

    def test_t_state_cfg_private_ranges_and_cgnat_are_accepted_for_bind_and_host(self):
        for ip in (
            "10.0.0.5",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.254",
            "192.168.1.5",
            "192.168.255.255",
            "100.64.0.1",
            "100.127.255.255",
        ):
            with self.subTest(ip=ip):
                cfg = gs.parse_config(good_config(bind_host=ip, host_header=ip))
                self.assertEqual((cfg.bind_host, cfg.host_header), (ip, ip))
                cfg = gs.parse_config(
                    good_config(bind_host=ip, host_header=f"{ip}:65535")
                )
                self.assertEqual(cfg.host_header, f"{ip}:65535")

    def test_t_state_cfg_port_and_hash_edges_are_accepted(self):
        for port in (1024, 65535):
            self.assertEqual(gs.parse_config(good_config(port=port)).port, port)
        hashes = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(16)]
        self.assertEqual(
            len(gs.parse_config(good_config(pet_token_sha256=hashes)).pet_token_sha256),
            16,
        )
        self.assertEqual(
            gs.parse_config(good_config(host_header="192.168.1.10:1")).host_header,
            "192.168.1.10:1",
        )

    def test_t_state_cfg_short8_is_the_last_eight_hex_as_is(self):
        self.assertEqual(
            gs.parse_config(good_config(session_id="x-deadbeef")).session_id_short,
            "deadbeef",
        )
        self.assertEqual(
            gs.parse_config(good_config(session_id="00000000")).session_id_short,
            "00000000",
        )

    def test_t_state_cfg_repr_hides_secrets_ids_and_paths_of_secrets(self):
        cfg = gs.parse_config(good_config(provider_key_env="SECRET_ENV_NAME"))
        shown = repr(cfg) + str(cfg)
        for hidden in (HASH_A, "sess-0123abcd", CRED, "SECRET_ENV_NAME"):
            self.assertNotIn(hidden, shown)
        self.assertIn("0123abcd", shown)  # only the short id is displayable
        file_cfg = gs.parse_config(
            good_config(
                drop=("provider_key_env",), provider_key_file="/secret/provider.key"
            )
        )
        self.assertNotIn("/secret/provider.key", repr(file_cfg))


class ConfigRejectTest(OfflineTestCase):
    def test_t_state_cfg_non_dict_documents_are_config_invalid(self):
        for doc in (None, [], "x", 1, good_config().items()):
            with self.subTest(doc=type(doc).__name__):
                self.assertEqual(config_code(self, doc), "config_invalid")

    def test_t_state_cfg_every_required_key_is_required(self):
        for key in good_config():
            if key == "provider_key_env":
                continue
            with self.subTest(missing=key):
                self.assertEqual(
                    config_code(self, good_config(drop=(key,))), "config_invalid"
                )

    def test_t_state_cfg_unknown_keys_are_rejected(self):
        for extra in (
            {"extra": 1},
            {"Version": 1},
            {"provider_key": "x"},
            {"debug": True},
        ):
            with self.subTest(extra=extra):
                self.assertEqual(
                    config_code(self, good_config(**extra)), "config_invalid"
                )

    def test_t_state_cfg_exactly_one_provider_key_form(self):
        self.assertEqual(
            config_code(self, good_config(drop=("provider_key_env",))), "config_invalid"
        )
        both = good_config(provider_key_file="/etc/pet/provider.key")
        self.assertEqual(config_code(self, both), "config_invalid")
        for bad in ("relative.key", "", "/etc/../pet.key", "/a/./b", 5, None):
            doc = good_config(drop=("provider_key_env",), provider_key_file=bad)
            with self.subTest(file=bad):
                self.assertEqual(config_code(self, doc), "config_invalid")
        for bad_env in ("lower", "1ABC", "", "A" * 65, "A-B", "A B", None, 5):
            with self.subTest(env=bad_env):
                self.assertEqual(
                    config_code(self, good_config(provider_key_env=bad_env)),
                    "config_invalid",
                )

    def test_t_state_cfg_version_is_the_int_one(self):
        for value in (0, 2, True, 1.0, "1", None):
            with self.subTest(value=value):
                self.assertEqual(
                    config_code(self, good_config(version=value)), "config_invalid"
                )

    def test_t_state_cfg_paths_must_be_absolute_and_clean(self):
        for key in ("state_root", "tls_cert_file", "tls_key_file"):
            for bad in (
                "relative/path",
                "",
                "~/x",
                "/a/../b",
                "/a/./b",
                "/a\x00b",
                "/a\nb",
                5,
                None,
                ["/a"],
            ):
                with self.subTest(key=key, value=bad):
                    self.assertEqual(
                        config_code(self, good_config(**{key: bad})), "config_invalid"
                    )

    def test_t_state_cfg_bind_host_must_be_an_exact_private_or_cgnat_ipv4_literal(self):
        for ip in (
            "0.0.0.0",
            "127.0.0.1",
            "8.8.8.8",
            "11.0.0.1",
            "172.15.255.255",
            "172.32.0.1",
            "100.63.255.255",
            "100.128.0.1",
            "192.169.0.1",
            "169.254.1.1",
            "224.0.0.1",
            "255.255.255.255",
            "::1",
            "fe80::1",
            "10.0.0",
            "10.0.0.256",
            "010.0.0.1",
            "10.0.0.01",
            "10.0.0.1 ",
            " 10.0.0.1",
            "10.0.0.1/8",
            "localhost",
            "pet.example",
            "",
            None,
            5,
            ["10.0.0.1"],
        ):
            with self.subTest(ip=ip):
                doc = good_config(bind_host=ip, host_header="192.168.1.10")
                self.assertEqual(config_code(self, doc), "config_invalid")

    def test_t_state_cfg_port_must_be_an_int_in_1024_to_65535(self):
        for port in (0, 1023, 65536, -1, True, False, 8443.0, "8443", None):
            with self.subTest(port=port):
                self.assertEqual(
                    config_code(self, good_config(port=port)), "config_invalid"
                )

    def test_t_state_cfg_host_header_is_an_exact_ip_or_ip_colon_port(self):
        for header in (
            "pet.example",
            "pet.example:8443",
            "192.168.1.10:",
            ":8443",
            "192.168.1.10:08443",
            "192.168.1.10:0",
            "192.168.1.10:65536",
            "192.168.1.10:8443:1",
            "192.168.1.10:+443",
            "192.168.1.10:443 ",
            " 192.168.1.10",
            "192.168.1.10 ",
            "127.0.0.1:8443",
            "8.8.8.8",
            "010.0.0.1",
            "[::1]:8443",
            "https://192.168.1.10",
            "",
            None,
            5,
        ):
            with self.subTest(header=header):
                self.assertEqual(
                    config_code(self, good_config(host_header=header)), "config_invalid"
                )

    def test_t_state_cfg_token_hash_list_format_count_and_uniqueness(self):
        sixteen = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(17)]
        for value in (
            [],
            sixteen,
            [HASH_A, HASH_A],
            [HASH_A.upper()],
            [HASH_A[:-1]],
            [HASH_A + "0"],
            ["g" * 64],
            [HASH_A + "\n"],
            [5],
            [None],
            HASH_A,
            (HASH_A,),
            {HASH_A},
            None,
        ):
            with self.subTest(value=repr(value)[:40]):
                self.assertEqual(
                    config_code(self, good_config(pet_token_sha256=value)),
                    "config_invalid",
                )

    def test_t_state_cfg_credential_ref_session_id_and_label_formats(self):
        for ref in (
            "device-" + "0" * 31,
            "device-" + "0" * 33,
            "device-" + "A" * 32,
            "dev-" + "0" * 32,
            "",
            None,
            5,
        ):
            with self.subTest(ref=ref):
                self.assertEqual(
                    config_code(self, good_config(credential_ref=ref)), "config_invalid"
                )
        for session_id in ("", " x", "x y", "-abc", "a/b", "x" * 129, None, 5):
            with self.subTest(session_id=session_id):
                self.assertEqual(
                    config_code(self, good_config(session_id=session_id)),
                    "config_invalid",
                )
        for label in (
            "",
            "x" * 41,
            "a\nb",
            "a\x00b",
            "a  b",
            " a",
            "a\u200bb",
            None,
            5,
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    config_code(self, good_config(session_label=label)),
                    "config_invalid",
                )

    def test_t_state_cfg_model_and_voice_formats(self):
        for model in ("GPT", "", "-a", "a b", "a" * 65, None, 5):
            with self.subTest(model=model):
                self.assertEqual(
                    config_code(self, good_config(model=model)), "config_invalid"
                )
        for voice in ("Marin", "", "1a", "a b", "a" * 33, None, 5):
            with self.subTest(voice=voice):
                self.assertEqual(
                    config_code(self, good_config(voice=voice)), "config_invalid"
                )

    def test_t_state_cfg_unsupported_short8_is_core_binding_required_after_other_checks(
        self,
    ):
        for session_id in (
            "sess-0123ABCD",
            "DEADBEEF",
            "sess-deadBEEF",
            "sess-xyz12345",
            "abc",
            "sess-0123abc",
        ):
            with self.subTest(session_id=session_id):
                self.assertEqual(
                    config_code(self, good_config(session_id=session_id)),
                    "core_binding_required",
                )
        # Another defect wins: the document is refused as config_invalid first.
        doc = good_config(session_id="DEADBEEF", port=1)
        self.assertEqual(config_code(self, doc), "config_invalid")

    def test_t_state_cfg_errors_carry_only_the_fixed_code(self):
        with self.assertRaises(GatewayProtocolError) as ctx:
            gs.parse_config(
                good_config(provider_key_env="SECRET_CANARY_VALUE_x", port=1)
            )
        self.assertEqual(str(ctx.exception), "config_invalid")
        self.assertNotIn("SECRET_CANARY", repr(ctx.exception))


# =================================================================================
# U1 corrections v2.  Labels: "pin" tests describe behaviour that already held and
# may be green on arrival; the others were written RED first (see the receipt).
# =================================================================================
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def noncanonical(canonical: str) -> str:
    """The same decoded bytes under a different, non-zero pad-bit last character."""
    stripped = canonical.rstrip("=")
    flipped = ALPHABET[ALPHABET.index(stripped[-1]) ^ 1]
    variant = stripped[:-1] + flipped + canonical[len(stripped) :]
    assert variant != canonical
    assert base64.b64decode(variant, validate=True) == base64.b64decode(canonical)
    return variant


def dumps(doc: Any) -> str:
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False)


class CanonicalDeviceAudioTest(OfflineTestCase):
    # Sizes with pad bits: 2 -> "=", 4 -> "==", 4796 -> "=", 4798 -> "==".
    SIZES = (2, 4, 4796, 4798)

    def test_t_state_canon_a_pad_bit_variant_of_valid_audio_is_unknown_command(self):
        self.assertEqual(b64(b"\x00\x01"), "AAE=")
        self.assertEqual(noncanonical("AAE="), "AAF=")
        for size in self.SIZES:
            canonical = b64((bytes(range(256)) * 19)[:size])
            with self.subTest(size=size):
                self.assertEqual(parse(audio_frame(canonical)).pcm_len, size)
                variant = noncanonical(canonical)
                self.assertEqual(code_of(self, audio_frame(variant)), "unknown_command")
                self.assertIsNone(gs.audio_b64_len(variant))
                self.assertEqual(gs.audio_b64_len(canonical), size)

    def test_t_state_canon_unpadded_edge_of_4800_bytes_stays_accepted(self):
        encoded = b64((bytes(range(256)) * 19)[:4800])
        self.assertNotIn("=", encoded)
        self.assertEqual(parse(audio_frame(encoded)).pcm_len, 4800)

    def test_t_state_canon_outbound_audio_delta_is_always_canonical(self):
        for size in self.SIZES:
            pcm = (bytes(range(256)) * 19)[:size]
            doc = json.loads(gs.build_audio_delta(pcm))
            self.assertEqual(doc["delta"], b64(pcm))


def start_with_input_pad(count: int) -> str:
    doc = {
        "type": "session.start",
        "event_id": "pet_start",
        "session": {
            "model": "gpt-live-1",
            "store": False,
            "input": [{"p": "x" * count}],
            "audio": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "output": {"voice": "marin"},
            },
        },
    }
    return dumps(doc)


def json_depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((json_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((json_depth(v) for v in value), default=0)
    return 0


def start_with_nested_input(levels: int) -> str:
    nested: Any = []
    for _ in range(levels - 1):
        nested = [nested]
    doc = json.loads(start_with_input_pad(0))
    doc["session"]["input"] = [{"a": nested}]
    return dumps(doc)


class StartFrameBoundaryTest(OfflineTestCase):
    def test_t_state_mf1_valid_start_padded_by_ignored_input_has_an_exact_byte_cap(
        self,
    ):
        base = len(start_with_input_pad(0).encode("utf-8"))
        at_cap = start_with_input_pad(gs.MAX_DEVICE_FRAME_BYTES - base)
        over = start_with_input_pad(gs.MAX_DEVICE_FRAME_BYTES - base + 1)
        self.assertEqual(len(at_cap.encode("utf-8")), 98304)
        self.assertEqual(len(over.encode("utf-8")), 98305)
        self.assertEqual(parse(at_cap).kind, "start")
        self.assertEqual(code_of(self, over), "unknown_command")
        # The same frame shape one byte smaller is still accepted: size is the cause.
        self.assertEqual(parse(start_with_input_pad(10)).kind, "start")

    def test_t_state_mf1_depth_16_is_accepted_and_17_is_refused_through_the_parser(
        self,
    ):
        sixteen, seventeen = start_with_nested_input(12), start_with_nested_input(13)
        self.assertEqual(json_depth(json.loads(sixteen)), 16)
        self.assertEqual(json_depth(json.loads(seventeen)), 17)
        self.assertEqual(parse(sixteen).kind, "start")
        self.assertEqual(code_of(self, seventeen), "unknown_command")


def preview_for(text: str) -> str:
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return gs.build_preview(UUID4, 1, text, sha)


def greeting_with(content: str) -> str:
    return dumps({**json.loads(ASSISTANT_GREETING), "content": content})


def start_with_instructions(text: str) -> str:
    doc = json.loads(start_with_input_pad(0))
    doc["session"]["instructions"] = text
    return dumps(doc)


class CodePointLimitTest(OfflineTestCase):
    ASCII, TWO_BYTE, FOUR_BYTE = "x", "\u00e9", "\U0001f600"

    def test_t_state_mf2_preview_cap_is_4000_code_points_ascii(self):
        self.assertEqual(
            json.loads(preview_for(self.ASCII * 4000))["text"], self.ASCII * 4000
        )
        with self.assertRaises(ValueError):
            preview_for(self.ASCII * 4001)  # 4001 bytes: the code point cap, not bytes

    def test_t_state_mf2_preview_cap_is_4000_code_points_multibyte(self):
        for char in (self.TWO_BYTE, self.FOUR_BYTE):
            with self.subTest(char=char):
                preview_for(char * 4000)  # 8000 / 16000 bytes
                with self.assertRaises(ValueError):
                    preview_for(char * 4001)

    def test_t_state_mf2_stager_cap_is_4000_code_points_across_segments_ascii(self):
        self._stager_cap(self.ASCII)

    def test_t_state_mf2_stager_cap_is_4000_code_points_across_segments_multibyte(
        self,
    ):
        for char in (self.TWO_BYTE, self.FOUR_BYTE):
            with self.subTest(char=char):
                self._stager_cap(char)

    def _stager_cap(self, char: str) -> None:
        stager = gs.TranscriptStager()
        for index in range(100):  # 100 segments of 40 code points == 4000, under 128
            self.assertEqual(
                stager.add(f"e{index}", char * 40, index * 10, index * 10 + 10), "added"
            )
        self.assertEqual((stager.count, stager.chars), (100, 4000))
        self.assertEqual(len(stager.text()), 4000)
        with self.assertRaises(GatewayProtocolError) as ctx:
            stager.add("late", char, 5000, 5001)
        self.assertEqual(ctx.exception.code, "too_long")
        self.assertEqual((stager.count, stager.chars), (100, 4000))
        single = gs.TranscriptStager()
        self.assertEqual(single.add("a", char * 4000, 0, 1), "added")
        other = gs.TranscriptStager()
        with self.assertRaises(GatewayProtocolError) as ctx:
            other.add("a", char * 4001, 0, 1)
        self.assertEqual(ctx.exception.code, "too_long")

    def test_t_state_mf2_greeting_cap_is_4000_code_points(self):
        for char in (self.ASCII, self.TWO_BYTE, self.FOUR_BYTE):
            with self.subTest(char=char):
                self.assertEqual(parse(greeting_with(char * 4000)).kind, "greeting")
                self.assertEqual(
                    code_of(self, greeting_with(char * 4001)), "unknown_command"
                )

    def test_t_state_mf2_start_instructions_cap_is_16384_code_points(self):
        for char in (self.ASCII, self.TWO_BYTE, self.FOUR_BYTE):
            with self.subTest(char=char):
                self.assertEqual(
                    parse(start_with_instructions(char * 16384)).kind, "start"
                )
                self.assertEqual(
                    code_of(self, start_with_instructions(char * 16385)),
                    "unknown_command",
                )

    def test_t_state_mf2_config_path_cap_is_4096_code_points(self):
        # The plan names no unit; the implementation counts code points (pinned).
        for key in ("state_root", "tls_cert_file", "tls_key_file"):
            for fill in (self.ASCII, self.TWO_BYTE):
                with self.subTest(key=key, fill=fill):
                    at_cap = "/" + fill * 4095
                    self.assertEqual(len(at_cap), 4096)
                    self.assertEqual(
                        getattr(gs.parse_config(good_config(**{key: at_cap})), key),
                        at_cap,
                    )
                    self.assertEqual(
                        config_code(self, good_config(**{key: at_cap + fill})),
                        "config_invalid",
                    )


class StagerOverlapMatrixTest(OfflineTestCase):
    def assertOverlap(self, existing, start, end):
        stager = gs.TranscriptStager()
        for index, (s, e) in enumerate(existing):
            stager.add(f"e{index}", "a", s, e)
        before = (stager.count, stager.chars, stager.ordered())
        with self.assertRaises(GatewayProtocolError) as ctx:
            stager.add("new", "b", start, end)
        self.assertEqual(ctx.exception.code, "overlap")
        self.assertEqual((stager.count, stager.chars, stager.ordered()), before)

    def assertAdded(self, existing, start, end):
        stager = gs.TranscriptStager()
        for index, (s, e) in enumerate(existing):
            stager.add(f"e{index}", "a", s, e)
        self.assertEqual(stager.add("new", "b", start, end), "added")
        self.assertEqual(stager.count, len(existing) + 1)

    def test_t_state_mf3_left_overlap_against_a_nonzero_existing_segment(self):
        for start, end in ((5, 15), (5, 11), (0, 30), (9, 11)):
            with self.subTest(start=start, end=end):
                self.assertOverlap([(10, 20)], start, end)

    def test_t_state_mf3_overlap_with_only_a_non_first_segment(self):
        existing = [(0, 10), (20, 30), (40, 50)]
        for start, end in ((25, 35), (15, 25), (45, 60), (29, 41)):
            with self.subTest(start=start, end=end):
                self.assertOverlap(existing, start, end)
        for start, end in ((10, 20), (12, 18), (30, 40), (50, 60)):
            with self.subTest(free=(start, end)):
                self.assertAdded(existing, start, end)

    def test_t_state_mf3_overlap_spanning_several_segments(self):
        self.assertOverlap([(0, 10), (20, 30)], 5, 25)
        self.assertOverlap([(0, 10), (20, 30)], 0, 30)

    def test_t_state_mf3_pin_zero_length_segment_then_a_positive_span_over_it(self):
        # Pinned plan semantics: a zero-length segment never overlaps anything,
        # in either arrival order.
        for point in (0, 5, 10):
            with self.subTest(order="zero first", point=point):
                self.assertAdded([(point, point)], 0, 10)
            with self.subTest(order="span first", point=point):
                self.assertAdded([(0, 10)], point, point)

    def test_t_state_mf3_pin_zero_length_among_several_and_adjacent_edges(self):
        self.assertAdded([(5, 5), (20, 20)], 0, 30)
        self.assertAdded([(0, 30)], 5, 5)
        self.assertAdded([(10, 20)], 20, 30)  # adjacent right
        self.assertAdded([(10, 20)], 0, 10)  # adjacent left

    def test_t_state_mf3_duplicate_and_conflict_semantics_are_unchanged(self):
        stager = gs.TranscriptStager()
        stager.add("e1", "a", 0, 10)
        self.assertEqual(stager.add("e1", "a", 0, 10), "duplicate")
        with self.assertRaises(GatewayProtocolError) as ctx:
            stager.add("e1", "b", 0, 10)
        self.assertEqual(ctx.exception.code, "conflict")


# Independent reference of the plan algorithm (7.1) plus the MF4 repair rule.
_ENDS = (". ", "! ", "? ", "\n")


def ref_window(rest: str) -> str:
    size = end = 0
    for char in rest:
        width = len(char.encode("utf-8"))
        if size + width > 480:
            break
        size += width
        end += 1
    return rest[:end]


def ref_cut(window: str) -> int:
    found = [window.rfind(end) + len(end) for end in _ENDS if end in window]
    if found:
        return max(found)
    for index in range(len(window) - 1, -1, -1):
        if window[index].isspace():
            return index + 1
    return len(window)


def ref_split(text: str, prefer: bool) -> list[str]:
    chunks, rest = [], text
    while rest:
        window = ref_window(rest)
        cut = len(window) if len(window) == len(rest) or not prefer else ref_cut(window)
        chunks.append(rest[:cut])
        rest = rest[cut:]
    return chunks


def ref_expected(text: str) -> list[str] | None:
    preferred = ref_split(text, True)
    if len(preferred) <= 64:
        return preferred
    maximal = ref_split(text, False)
    return maximal if len(maximal) <= 64 else None


class ChunkerRepairTest(OfflineTestCase):
    REGRESSION = ((chr(0x1F600) * 60 + "\n") * 70)[:4000]

    def assertChunks(self, text: str, chunks: tuple[str, ...]) -> None:
        self.assertEqual("".join(chunks), text)  # lossless: no ellipsis, no drop
        self.assertLessEqual(len(chunks), 64)
        for chunk in chunks:
            self.assertTrue(chunk)
            self.assertLessEqual(len(chunk.encode("utf-8")), 480)
            chunk.encode("utf-8")  # no split surrogate

    def test_t_state_mf4_regression_newline_heavy_emoji_reply_is_chunked(self):
        text = self.REGRESSION
        self.assertEqual((len(text), len(text.encode("utf-8"))), (4000, 15805))
        self.assertChunks(text, gs.chunk_commentary(text))

    def test_t_state_mf4_over_64_preferred_falls_back_to_maximal_cuts(self):
        text = self.REGRESSION
        self.assertGreater(len(ref_split(text, True)), 64)
        chunks = gs.chunk_commentary(text)
        self.assertEqual(list(chunks), ref_split(text, False))
        for left, right in itertools.pairwise(chunks):
            # maximal: the next code point would not have fit
            self.assertGreater(len((left + right[0]).encode("utf-8")), 480)

    def test_t_state_mf4_preferred_cuts_are_kept_when_within_64(self):
        text = "x" * 100 + ". " + "y" * 100 + "! " + "z" * 100 + "\n" + "w" * 400
        self.assertLessEqual(len(ref_split(text, True)), 64)
        chunks = gs.chunk_commentary(text)
        self.assertEqual(list(chunks), ref_split(text, True))
        # The first window holds 480 characters: the last boundary in it is the newline.
        self.assertEqual(chunks[0], text[:305])
        self.assertTrue(chunks[0].endswith("z\n"))

    def test_t_state_mf4_last_boundary_oracle_for_mixed_terminators(self):
        # '. ', '! ' and a newline at 120/240/360; the LAST one in the window wins.
        marks = {". ": 120, "! ": 240, "\n": 360}
        for order in itertools.permutations(marks):
            positions = sorted(marks.values())
            body = ["x"] * 700
            for end, pos in zip(order, positions, strict=True):
                body[pos : pos + len(end)] = list(end)
            text = "".join(body)
            expected_first = positions[-1] + len(order[-1])
            with self.subTest(order=order):
                chunks = gs.chunk_commentary(text)
                self.assertEqual(len(chunks[0]), expected_first)
                self.assertEqual(list(chunks), ref_expected(text))

    def test_t_state_mf4_a_terminator_split_by_the_window_edge_is_not_a_boundary(self):
        # '.' is the last character of the 480-byte window and ' ' falls outside it.
        text = "a" * 100 + "! " + "b" * 377 + ". " + "c" * 100
        self.assertEqual(text[479:481], ". ")
        chunks = gs.chunk_commentary(text)
        self.assertEqual(chunks[0], "a" * 100 + "! ")
        self.assertEqual(list(chunks), ref_expected(text))

    def test_t_state_mf4_property_deterministic_corpus_matches_the_reference(self):
        pool = [
            "a",
            " ",
            ". ",
            "! ",
            "? ",
            "\n",
            "\n",
            "\u00e9",
            "\u6f22",
            "\U0001f600",
        ]
        rng = random.Random(20261112)
        for round_no in range(250):
            weights = rng.choice((1, 3, 10))
            text = "".join(
                rng.choice(pool) for _ in range(rng.randint(1, 4000 // weights))
            )[:4000]
            expected = ref_expected(text)
            with self.subTest(round=round_no):
                self.assertIsNotNone(expected)  # every <=4000 cp / 16000 B text fits
                self.assertEqual(list(gs.chunk_commentary(text)), expected)
                self.assertChunks(text, gs.chunk_commentary(text))

    def test_t_state_mf4_sweep_newline_heavy_multibyte_4000_code_point_replies(self):
        for fill in ("\U0001f600", "\u6f22", "\u00e9", "a"):
            for run in range(1, 130):
                text = ((fill * run + "\n") * 4000)[:4000]
                with self.subTest(fill=fill, run=run):
                    self.assertChunks(text, gs.chunk_commentary(text))

    def test_t_state_mf4_more_than_64_chunks_even_when_maximal_is_still_an_error(self):
        with self.assertRaises(ValueError):
            gs.chunk_commentary("\U0001f600" * 7681)  # 30724 bytes, 65 maximal chunks
        gs.chunk_commentary("\U0001f600" * 7680)  # exactly 64 maximal chunks


class PureStateModuleIoGuardTest(OfflineTestCase):
    def test_t_state_no_direct_file_or_environment_access_in_the_pure_module(self):
        tree = ast.parse(Path(gs.__file__).read_text(encoding="utf-8"))
        forbidden = {"open", "Path", "environ", "getenv", "putenv", "__import__"}
        roots = {"pathlib", "io", "sys", "builtins", "shutil", "tempfile", "subprocess"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, forbidden)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, forbidden)
                if isinstance(node.value, ast.Name) and node.value.id == "os":
                    self.assertEqual(node.attr, "path")  # os.path only
                if (
                    isinstance(node.value, ast.Attribute)
                    and isinstance(node.value.value, ast.Name)
                    and node.value.value.id == "os"
                ):
                    self.assertEqual(node.attr, "isabs")  # pure string test only
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], roots)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                self.assertNotIn(node.module.split(".")[0], roots)
