"""Wire: exact command request, strict receipt parsing, GPT-Live event parsing."""
# pyright: reportArgumentType=false, reportOptionalMemberAccess=false, reportOptionalSubscript=false
# (tests deliberately pass invalid types and read Optionals right after asserting them)

from __future__ import annotations

import json
import uuid

from pet_bridge.wire import (
    MAX_COMMENTARY_BYTES,
    MAX_TEXT_CHARS,
    Receipt,
    WireError,
    parse_live_event,
    parse_send_receipt,
    send_command_request,
)

from .fakes import SESSION, OfflineTestCase, raised

CMD = "00000000-0000-4000-8000-000000000001"


def body(**fields) -> bytes:
    base = {"accepted": True, "delivery": "accepted", "sessionId": SESSION}
    base.update(fields)
    return json.dumps({k: v for k, v in base.items() if v is not ...}).encode()


class RequestShapeTest(OfflineTestCase):
    def test_exact_command_request(self):
        method, path, raw = send_command_request(
            SESSION, CMD, "héllo wörld", expected_revision=None
        )
        self.assertEqual(method, "POST")
        self.assertEqual(path, f"/api/sessions/{SESSION}/commands")
        self.assertEqual(
            json.loads(raw),
            {
                "id": CMD,
                "action": "conversation.send",
                "args": {
                    "text": "héllo wörld",
                    "via": "chat",
                    "preserveDraft": True,
                    "attachmentIds": [],
                },
            },
        )
        self.assertIsInstance(raw, bytes)
        raw.decode("utf-8")

    def test_expected_revision_is_top_level_never_in_args(self):
        _m, _p, raw = send_command_request(SESSION, CMD, "hi", expected_revision=42)
        parsed = json.loads(raw)
        self.assertEqual(parsed["expectedRevision"], 42)
        self.assertNotIn("expectedRevision", parsed["args"])
        _m, _p, raw = send_command_request(SESSION, CMD, "hi", expected_revision=0)
        self.assertEqual(json.loads(raw)["expectedRevision"], 0)

    def test_absent_revision_adds_no_key(self):
        _m, _p, raw = send_command_request(SESSION, CMD, "hi", expected_revision=None)
        self.assertNotIn("expectedRevision", json.loads(raw))

    def test_text_is_sent_verbatim_with_no_provenance_prefix(self):
        _m, _p, raw = send_command_request(SESSION, CMD, "  spaced  ")
        self.assertEqual(json.loads(raw)["args"]["text"], "  spaced  ")

    def test_session_id_is_quoted_and_bounded(self):
        for bad in ("", "a/b", "a b", "../x", "x" * 129, "ünï", "a?b", "a#b", None, 5):
            with self.assertRaises(WireError) as ctx:
                send_command_request(bad, CMD, "hi")
            self.assertEqual(ctx.exception.code, "invalid_session_id")
        _m, path, _b = send_command_request("a" * 128, CMD, "hi")
        self.assertEqual(path, f"/api/sessions/{'a' * 128}/commands")

    def test_command_id_must_be_canonical_uuid4(self):
        for bad in (
            "",
            "x",
            str(uuid.uuid1()),
            "0000000a-0000-4000-8000-00000000000b".upper(),
            "{" + CMD + "}",
            CMD.replace("-", ""),
            None,
            7,
        ):
            with self.assertRaises(WireError) as ctx:
                send_command_request(SESSION, bad, "hi")
            self.assertEqual(ctx.exception.code, "invalid_command_id")

    def test_text_limits_reject_never_truncate(self):
        send_command_request(SESSION, CMD, "a" * MAX_TEXT_CHARS)
        with self.assertRaises(WireError) as ctx:
            send_command_request(SESSION, CMD, "a" * (MAX_TEXT_CHARS + 1))
        self.assertEqual(ctx.exception.code, "text_too_long")
        for bad in ("", "   \n\t", "bad\x00nul", "\ud800", None, b"bytes", 5):
            with self.assertRaises(WireError) as ctx:
                send_command_request(SESSION, CMD, bad)
            self.assertEqual(ctx.exception.code, "invalid_text")

    def test_revision_must_be_non_negative_int(self):
        for bad in (-1, True, 1.5, "7"):
            with self.assertRaises(WireError) as ctx:
                send_command_request(SESSION, CMD, "hi", expected_revision=bad)
            self.assertEqual(ctx.exception.code, "invalid_revision")

    def test_error_never_echoes_text(self):
        secret = "TOPSECRET\x00"
        with self.assertRaises(WireError) as ctx:
            send_command_request(SESSION, CMD, secret)
        self.assertNotIn("TOPSECRET", str(ctx.exception) + repr(ctx.exception))


class ReceiptAcceptedTest(OfflineTestCase):
    def parse(self, status, raw, **kw) -> Receipt:
        return parse_send_receipt(status, raw, session_id=SESSION, **kw)

    def test_known_success_without_invented_host_instance_id(self):
        r = self.parse(200, body())
        self.assertEqual(r.outcome, "accepted")
        self.assertNotIn("host_instance_id", r.metadata)
        self.assertNotIn("state_revision", r.metadata)

    def test_success_with_optional_fields_validated_and_reported(self):
        r = self.parse(
            202,
            body(hostInstanceId="host-1", stateRevision=7, inputId=CMD),
            command_id=CMD,
        )
        self.assertEqual(r.outcome, "accepted")
        self.assertEqual(r.metadata["host_instance_id"], "host-1")
        self.assertEqual(r.metadata["state_revision"], 7)
        self.assertEqual(r.metadata["http_status"], 202)

    def test_present_but_malformed_optional_fields_are_uncertain(self):
        for extra in (
            {"hostInstanceId": 5},
            {"hostInstanceId": "bad id"},
            {"stateRevision": "7"},
            {"stateRevision": True},
            {"stateRevision": -1},
        ):
            self.assertEqual(self.parse(200, body(**extra)).outcome, "uncertain", extra)

    def test_foreign_receipts_are_uncertain(self):
        self.assertEqual(
            self.parse(200, body(sessionId="other")).reason, "foreign_session"
        )
        self.assertEqual(self.parse(200, body(sessionId=...)).outcome, "uncertain")
        self.assertEqual(
            self.parse(200, body(inputId="x" * 5), command_id=CMD).reason,
            "foreign_command",
        )

    def test_http_200_alone_is_not_success(self):
        for raw in (
            b"",
            b"{}",
            b"[]",
            b"null",
            b"not json",
            b'{"accepted":true}',
            b'{"accepted":"true","delivery":"accepted","sessionId":"sess-abc_123"}',
            b'{"accepted":1,"delivery":"accepted","sessionId":"sess-abc_123"}',
        ):
            r = self.parse(200, raw)
            self.assertEqual(r.outcome, "uncertain", raw)

    def test_known_non_accepted_deliveries_are_not_accepted(self):
        for delivery in ("sending", "unknown", "held", "queued_for_update"):
            r = self.parse(200, body(delivery=delivery))
            self.assertEqual(r.outcome, "uncertain", delivery)
            self.assertEqual(r.reason, f"delivery_{delivery}")
        self.assertEqual(
            self.parse(200, body(delivery="failed")).outcome, "uncertain"
        )  # contradictory
        self.assertEqual(
            self.parse(200, body(delivery="surprise")).reason, "unknown_delivery"
        )
        self.assertEqual(self.parse(200, body(delivery=...)).outcome, "uncertain")

    def test_http_200_accepted_false(self):
        r = self.parse(200, body(accepted=False, delivery="failed"))
        self.assertEqual(r.outcome, "rejected")
        for delivery in ("sending", "unknown", "accepted", "held", ...):
            r = self.parse(200, body(accepted=False, delivery=delivery))
            self.assertEqual(r.outcome, "uncertain", delivery)

    def test_strict_scalar_types(self):
        self.assertEqual(self.parse(200, body(accepted=1)).outcome, "uncertain")
        self.assertEqual(
            self.parse(True, body()).outcome, "uncertain"
        )  # bool is not a status
        self.assertEqual(self.parse("200", body()).outcome, "uncertain")
        self.assertEqual(self.parse(200, body(sessionId=["x"])).outcome, "uncertain")

    def test_nan_and_duplicate_keys_are_malformed(self):
        self.assertEqual(
            self.parse(
                200,
                b'{"accepted":true,"delivery":"accepted","sessionId":"sess-abc_123","stateRevision":NaN}',
            ).outcome,
            "uncertain",
        )
        dup = b'{"accepted":true,"accepted":true,"delivery":"accepted","sessionId":"sess-abc_123"}'
        self.assertEqual(self.parse(200, dup).outcome, "uncertain")

    def test_body_bound(self):
        padded = body(note="x" * 17000)
        self.assertEqual(self.parse(200, padded).reason, "receipt_too_large")
        self.assertEqual(self.parse(200, "str body").outcome, "uncertain")


class ReceiptFailureTest(OfflineTestCase):
    def parse(self, status, raw, **kw) -> Receipt:
        return parse_send_receipt(status, raw, session_id=SESSION, **kw)

    def test_well_formed_explicit_not_accepted_409_is_rejection(self):
        r = self.parse(
            409, json.dumps({"accepted": False, "error": "stale revision"}).encode()
        )
        self.assertEqual((r.outcome, r.reason), ("rejected", "http_409"))

    def test_malformed_error_bodies_are_uncertain(self):
        """Nothing provable from an unparseable/non-object body: never 'rejected'."""
        oversize = b'{"accepted":false,"pad":"' + b"x" * 17000 + b'"}'
        bodies = {
            "empty": b"",
            "plaintext": b"Conflict",
            "html": b"<html>",
            "truncated_json": b'{"accepted":false',
            "oversize": oversize,
            "nan": b'{"accepted":false,"stateRevision":NaN}',
            "infinity": b'{"accepted":false,"x":-Infinity}',
            "duplicate_keys": b'{"accepted":false,"accepted":false}',
            "invalid_utf8": b'{"accepted":false,"error":"\xff\xfe"}',
            "array": b'[{"accepted":false}]',
            "null": b"null",
            "string": b'"accepted:false"',
            "number": b"409",
        }
        for status in (400, 401, 403, 404, 409, 413, 422, 429):
            for name, raw in bodies.items():
                with self.subTest(status=status, body=name):
                    r = self.parse(status, raw)
                    self.assertEqual(
                        (r.outcome, r.reason, r.metadata),
                        ("uncertain", "malformed_receipt", {"http_status": status}),
                    )
        self.assertEqual(self.parse(409, "str body").outcome, "uncertain")
        self.assertEqual(self.parse(409, None).outcome, "uncertain")

    def test_incongruent_types_on_error_status_are_uncertain(self):
        cases = {
            "accepted_string": {"accepted": "false"},
            "accepted_int": {"accepted": 0},
            "delivery_int": {"accepted": False, "delivery": 5},
            "delivery_list": {"accepted": False, "delivery": ["failed"]},
            "delivery_null_nested": {"accepted": False, "receipt": {"delivery": {}}},
            "receipt_string": {"accepted": False, "receipt": "failed"},
            "receipt_array": {"accepted": False, "receipt": []},
            "session_list": {"accepted": False, "sessionId": [SESSION]},
            "input_int": {"accepted": False, "inputId": 7},
            "host_int": {"accepted": False, "hostInstanceId": 1},
            "revision_bool": {"accepted": False, "stateRevision": True},
            "conflicting": {
                "accepted": False,
                "delivery": "failed",
                "receipt": {"delivery": "held"},
            },
            "type_conflict": {"accepted": False, "receipt": {"accepted": 0}},
        }
        for status in (400, 409, 422):
            for name, payload in cases.items():
                with self.subTest(status=status, case=name):
                    r = self.parse(status, json.dumps(payload).encode(), command_id=CMD)
                    self.assertEqual(r.outcome, "uncertain")
                    self.assertNotEqual(r.reason, f"http_{status}")

    def test_incongruent_types_on_200_stay_uncertain(self):
        for patch in (
            {"accepted": "true"},
            {"delivery": 1},
            {"delivery": ["accepted"]},
            {"sessionId": 5},
            {"receipt": "accepted"},
            {"stateRevision": "3"},
            {"hostInstanceId": []},
        ):
            with self.subTest(patch=patch):
                self.assertEqual(self.parse(200, body(**patch)).outcome, "uncertain")

    def test_other_definite_client_errors_are_rejected(self):
        for status in (400, 401, 403, 404, 413, 422, 429):
            self.assertEqual(self.parse(status, b"{}").outcome, "rejected", status)
            explicit = json.dumps({"accepted": False}).encode()
            self.assertEqual(self.parse(status, explicit).outcome, "rejected", status)

    def test_ambiguous_statuses_are_uncertain(self):
        for status in (301, 408, 418, 500, 502, 503):
            self.assertEqual(self.parse(status, b"{}").outcome, "uncertain", status)

    def test_504_runtime_pending_is_uncertain(self):
        raw = json.dumps({"error": "runtime busy", "code": "runtime_pending"}).encode()
        r = self.parse(504, raw)
        self.assertEqual((r.outcome, r.reason), ("uncertain", "runtime_pending"))
        self.assertEqual(self.parse(504, b"").reason, "gateway_timeout")
        both = json.dumps({"accepted": True, "code": "runtime_pending"}).encode()
        self.assertEqual(self.parse(504, both).outcome, "uncertain")

    def test_error_with_nested_receipt_in_flight_is_uncertain(self):
        for delivery in ("accepted", "sending", "unknown", "queued_for_update"):
            raw = json.dumps(
                {
                    "accepted": False,
                    "error": "boom",
                    "receipt": {"delivery": delivery, "sessionId": SESSION},
                }
            ).encode()
            self.assertEqual(self.parse(409, raw).outcome, "uncertain", delivery)
        raw = json.dumps(
            {"accepted": False, "receipt": {"delivery": "failed"}}
        ).encode()
        self.assertEqual(self.parse(409, raw).outcome, "rejected", "failed")

    def test_error_with_foreign_nested_session_is_uncertain(self):
        raw = json.dumps(
            {"accepted": False, "receipt": {"delivery": "failed", "sessionId": "other"}}
        ).encode()
        self.assertEqual(self.parse(400, raw).reason, "foreign_session")

    def test_metadata_never_carries_response_or_error_text(self):
        marker = "SECRET-REPLY-TEXT"
        raw = json.dumps(
            {"accepted": False, "error": marker, "result": {"message": marker}}
        ).encode()
        r = self.parse(409, raw)
        dump = repr(r) + str(r.metadata) + r.reason
        self.assertNotIn(marker, dump)


class LiveEventTest(OfflineTestCase):
    def transcript(self, **kw) -> dict:
        event = {
            "type": "session.input_transcript.delta",
            "event_id": "ev-1",
            "delta": "hel",
            "start_ms": 0,
            "end_ms": 120,
        }
        event.update(kw)
        return event

    def test_exact_transcript_delta_prefix_and_fields(self):
        parsed = parse_live_event(self.transcript())
        self.assertEqual(
            (parsed.kind, parsed.event_id, parsed.text, parsed.start_ms, parsed.end_ms),
            ("transcript_delta", "ev-1", "hel", 0, 120),
        )
        self.assertNotIn("hel", repr(parsed))

    def test_shortened_or_foreign_type_names_are_not_events(self):
        for kind in (
            "input_transcript.delta",
            "commentary.append",
            "delegation.created",
            "session.input_transcript.done",
            "response.output_text.delta",
            "",
            None,
            5,
        ):
            self.assertIsNone(
                parse_live_event({"type": kind, "event_id": "e", "delta": "x"}), kind
            )
        self.assertIsNone(parse_live_event("not a dict"))
        self.assertIsNone(parse_live_event({}))

    def test_transcript_validation(self):
        bad = [
            {"event_id": ""},
            {"event_id": "has space"},
            {"event_id": "x" * 65},
            {"event_id": 5},
            {"delta": 5},
            {"delta": "a" * 4001},
            {"delta": "\ud800"},
            {"start_ms": -1},
            {"start_ms": True},
            {"start_ms": 1.5},
            {"end_ms": "9"},
            {"start_ms": 10, "end_ms": 5},
        ]
        for patch in bad:
            with self.assertRaises(WireError) as ctx:
                parse_live_event(self.transcript(**patch))
            self.assertEqual(ctx.exception.code, "live_event_invalid", patch)
        for missing in ("event_id", "delta", "start_ms", "end_ms"):
            event = self.transcript()
            del event[missing]
            with self.assertRaises(WireError):
                parse_live_event(event)

    def test_delegation_created_nesting_carries_no_text(self):
        event = {
            "type": "session.delegation.created",
            "offset_ms": 5,
            "delegation": {"id": "d-1", "type": "delegation", "target": "client"},
        }
        parsed = parse_live_event(event)
        self.assertEqual(
            (parsed.kind, parsed.delegation_id, parsed.offset_ms, parsed.text),
            ("delegation_hint", "d-1", 5, None),
        )

    def test_delegation_validation(self):
        base = {
            "type": "session.delegation.created",
            "offset_ms": 5,
            "delegation": {"id": "d-1", "type": "delegation", "target": "client"},
        }
        for mutate in (
            lambda e: e["delegation"].update(target="server"),
            lambda e: e["delegation"].update(type="other"),
            lambda e: e["delegation"].update(id="bad id"),
            lambda e: e.update(offset_ms=-1),
            lambda e: e.update(offset_ms=False),
            lambda e: e.update(delegation="x"),
            lambda e: e.pop("delegation"),
        ):
            event = json.loads(json.dumps(base))
            mutate(event)
            with self.assertRaises(WireError):
                parse_live_event(event)

    def test_commentary_exact_prefix_and_bounds(self):
        event = {
            "type": "session.commentary.append",
            "event_id": "c-1",
            "delegation_id": "d-1",
            "content": "Looking now.",
        }
        parsed = parse_live_event(event)
        self.assertEqual(
            (
                parsed.kind,
                parsed.text,
                parsed.delegation_id,
                parsed.truncated,
                parsed.may_paraphrase,
            ),
            ("commentary", "Looking now.", "d-1", False, True),
        )

    def test_commentary_truncated_to_480_utf8_bytes_with_ellipsis(self):
        content = "é" * 400  # 800 bytes
        parsed = parse_live_event(
            {
                "type": "session.commentary.append",
                "event_id": "c",
                "delegation_id": "d",
                "content": content,
            }
        )
        self.assertTrue(parsed.truncated)
        self.assertLessEqual(len(parsed.text.encode("utf-8")), MAX_COMMENTARY_BYTES)
        self.assertTrue(parsed.text.endswith("…"))
        exact = "a" * MAX_COMMENTARY_BYTES
        parsed = parse_live_event(
            {
                "type": "session.commentary.append",
                "event_id": "c",
                "delegation_id": "d",
                "content": exact,
            }
        )
        self.assertFalse(parsed.truncated)
        self.assertEqual(parsed.text, exact)

    def test_commentary_validation(self):
        base = {
            "type": "session.commentary.append",
            "event_id": "c",
            "delegation_id": "d",
            "content": "ok",
        }
        for patch in (
            {"content": 5},
            {"content": ""},
            {"content": "x" * 70000},
            {"delegation_id": "no good"},
            {"event_id": None},
        ):
            with self.assertRaises(WireError):
                parse_live_event({**base, **patch})


REJECT_STATUSES = (400, 401, 403, 404, 409, 413, 422, 429)


class ReceiptHardeningTest(OfflineTestCase):
    """R5: malformed / contradictory / conflicting receipts are uncertain, never a raise."""

    def parse(self, status, obj, **kw) -> Receipt:
        raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        err = raised(parse_send_receipt, status, raw, session_id=SESSION, **kw)
        if err is not None:
            self.fail(f"parse raised {type(err).__name__} for {status} {obj!r}")
        return parse_send_receipt(status, raw, session_id=SESSION, **kw)

    @staticmethod
    def good(**top):
        base = {"accepted": True, "delivery": "accepted", "sessionId": SESSION}
        base.update(top)
        return base

    BAD_DELIVERIES = (["accepted"], {"x": 1}, 1.5, True, False, 0, 7, [], {})

    def test_unhashable_or_wrongly_typed_delivery_is_uncertain_at_top_and_nested(self):
        for bad in self.BAD_DELIVERIES:
            for status in (200, 202, *REJECT_STATUSES):
                top = {"accepted": status < 300, "delivery": bad, "sessionId": SESSION}
                nested = {
                    "accepted": status < 300,
                    "sessionId": SESSION,
                    "receipt": {"delivery": bad},
                }
                both = {
                    "accepted": status < 300,
                    "delivery": "accepted",
                    "sessionId": SESSION,
                    "receipt": {"delivery": bad},
                }
                for obj in (top, nested, both):
                    r = self.parse(status, obj)
                    self.assertEqual(r.outcome, "uncertain", (status, obj))
                    self.assertNotEqual(r.outcome, "accepted")

    def test_non_dict_nested_receipt_is_uncertain(self):
        for nested in ("x", ["accepted"], 5, True):
            for status in (200, 409):
                obj = self.good(receipt=nested)
                if status != 200:
                    obj["accepted"] = False
                self.assertEqual(self.parse(status, obj).outcome, "uncertain", nested)

    def test_non_2xx_with_accepted_true_is_contradictory_uncertain(self):
        for status in REJECT_STATUSES:
            for delivery in ("accepted", "failed", "sending", ...):
                top = self.good(delivery=delivery)
                if delivery is ...:
                    del top["delivery"]
                r = self.parse(status, top)
                self.assertEqual(r.outcome, "uncertain", (status, delivery))
            nested = {"accepted": False, "receipt": {"accepted": True}}
            self.assertEqual(self.parse(status, nested).outcome, "uncertain", status)

    def test_top_vs_nested_conflicts_fail_closed_on_success_and_error(self):
        conflicts = (
            ({"accepted": True}, {"accepted": False}),
            ({"accepted": False}, {"accepted": True}),
            ({"delivery": "accepted"}, {"delivery": "failed"}),
            ({"delivery": "failed"}, {"delivery": "accepted"}),
            ({"delivery": "accepted"}, {"delivery": "held"}),
            ({"sessionId": SESSION}, {"sessionId": "sess-other-9"}),
            ({"hostInstanceId": "host-1"}, {"hostInstanceId": "host-2"}),
            ({"stateRevision": 1}, {"stateRevision": 2}),
            ({"stateRevision": 1}, {"stateRevision": True}),
            ({"stateRevision": 1}, {"stateRevision": 1.0}),
            ({"inputId": CMD}, {"inputId": "x"}),
        )
        failed = {"accepted": False, "delivery": "failed", "sessionId": SESSION}
        for status in (200, 202, 409, 400):
            for top_extra, nested_extra in conflicts:
                base = self.good() if status < 300 else failed
                obj = {**base, **top_extra, "receipt": dict(nested_extra)}
                r = self.parse(status, obj, command_id=CMD)
                self.assertEqual(
                    r.outcome, "uncertain", (status, top_extra, nested_extra)
                )

    def test_agreeing_top_and_nested_values_keep_the_known_success(self):
        obj = self.good(
            hostInstanceId="host-1",
            stateRevision=4,
            receipt={
                "delivery": "accepted",
                "sessionId": SESSION,
                "hostInstanceId": "host-1",
                "stateRevision": 4,
                "accepted": True,
            },
        )
        r = self.parse(200, obj)
        self.assertEqual((r.outcome, r.reason), ("accepted", "accepted"))
        self.assertEqual(r.metadata["host_instance_id"], "host-1")

    def test_malformed_nested_optional_metadata_is_uncertain(self):
        for extra in (
            {"hostInstanceId": 5},
            {"hostInstanceId": "bad id"},
            {"stateRevision": "7"},
            {"stateRevision": False},
        ):
            obj = self.good(receipt=extra)
            self.assertEqual(self.parse(200, obj).outcome, "uncertain", extra)

    def test_in_flight_deliveries_on_an_error_status_are_uncertain_including_held(self):
        for status in REJECT_STATUSES:
            for delivery in (
                "accepted",
                "sending",
                "unknown",
                "held",
                "queued_for_update",
            ):
                top = {"accepted": False, "delivery": delivery}
                nested = {"accepted": False, "receipt": {"delivery": delivery}}
                for obj in (top, nested):
                    r = self.parse(status, obj)
                    self.assertEqual(r.outcome, "uncertain", (status, delivery, obj))
                    self.assertEqual(r.reason, "ambiguous_error_receipt")

    def test_genuine_rejections_stay_rejected_and_known_success_stays_accepted(self):
        for status in REJECT_STATUSES:
            for obj in (
                {"accepted": False, "error": "stale"},
                {"accepted": False, "delivery": "failed"},
                {
                    "accepted": False,
                    "receipt": {"delivery": "failed", "sessionId": SESSION},
                },
                {"error": "nope"},
                {},
            ):
                r = self.parse(status, obj)
                self.assertEqual(r.outcome, "rejected", (status, obj))
                self.assertEqual(r.reason, f"http_{status}")
        r = self.parse(200, self.good())
        self.assertEqual(r.outcome, "accepted")
        self.assertNotIn("host_instance_id", r.metadata)
        self.assertNotIn("state_revision", r.metadata)

    def test_unknown_delivery_string_on_an_error_status_is_not_a_clean_rejection(self):
        r = self.parse(409, {"accepted": False, "delivery": "surprise"})
        self.assertEqual(r.outcome, "uncertain")

    def test_no_error_or_body_text_leaks_from_hostile_receipts(self):
        marker = "SECRET-MARKER-5521"
        hostile = (
            {"accepted": True, "delivery": [marker], "sessionId": SESSION},
            {"accepted": False, "delivery": {marker: marker}, "error": marker},
            {"accepted": True, "delivery": "accepted", "sessionId": marker},
            {
                "accepted": True,
                "delivery": "accepted",
                "sessionId": SESSION,
                "receipt": {"delivery": marker},
            },
            {"accepted": False, "receipt": {"sessionId": marker}, "error": marker},
        )
        for status in (200, 409):
            for obj in hostile:
                r = self.parse(status, obj)
                dump = repr(r) + str(r.metadata) + r.reason + r.outcome
                self.assertNotIn(marker, dump, obj)
