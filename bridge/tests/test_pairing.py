"""Offline tests for pairing (start/poll/abandon) and native attach.

A scripted ``FakeClient`` stands in for the HTTPS client; nothing touches a socket.
Wire shapes are taken from reading installed Unified 0.80.1 and are NOT a live
qualification.
"""

from __future__ import annotations

import inspect
import json
import os
import stat
import unittest
from typing import Any
from unittest import mock

from pet_bridge import auth
from pet_bridge.auth import (
    Attachment,
    AuthError,
    Credential,
    PairPrompt,
    PollResult,
    attach,
    pair_abandon,
    pair_poll,
    pair_start,
)
from pet_bridge.transport import Origin, Response, TransportError

from .authfakes import (
    CLIENT_ID,
    CRED_REF,
    DEVICE_ID,
    HOST_IDENTITY,
    INSTANCE,
    INSTANCE_2,
    ORIGIN_URL,
    PAIRING_ID,
    TOKEN,
    VERIFIER,
    AuthCase,
    FakeClient,
    approved,
    attach_ok,
    challenge_of,
    json_response,
    pair_ok,
    raised_any,
    rand,
)
from .fakes import FakeClock


class PairCase(AuthCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = FakeClock(5000.0)
        self.store = self.open_store()

    def start(self, client: FakeClient, **kwargs: Any) -> PairPrompt:
        return pair_start(
            self.store,
            client,
            kwargs.pop("origin", ORIGIN_URL),
            kwargs.pop("name", "pet bridge"),
            now=self.clock,
            rand=rand,
            **kwargs,
        )

    def started(self) -> FakeClient:
        client = FakeClient(json_response(pair_ok()))
        self.start(client)
        return client

    def poll(self, client: FakeClient) -> PollResult:
        return pair_poll(self.store, client, self.clock)


class PairStartTest(PairCase):
    def test_success_persists_before_the_single_post_and_returns_only_url_and_code(
        self,
    ):
        seen: list[Any] = []
        client = FakeClient(json_response(pair_ok()))

        def before(call: dict[str, Any]) -> None:
            attempt = self.store.attempt()
            seen.append(attempt.state if attempt else None)
            seen.append(attempt.verifier if attempt else None)

        client.before = before
        prompt = self.start(client)
        self.assertEqual(seen, ["ATTEMPTED", VERIFIER])  # durable BEFORE the POST
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["route"].path, "/api/devices/pair")
        self.assertEqual(call["route"].method, "POST")
        self.assertEqual(call["origin"], Origin.parse(ORIGIN_URL))
        self.assertNotIn("Authorization", call["headers"])
        self.assertEqual(
            json.loads(call["body"]),
            {"name": "pet bridge", "challenge": challenge_of(VERIFIER)},
        )
        self.assertEqual(
            prompt,
            PairPrompt(
                ORIGIN_URL + "/devices/pair?id=" + PAIRING_ID, "ABCD-2345", 5600.0
            ),
        )
        for text in (repr(prompt), str(prompt)):
            self.assertNotIn(VERIFIER, text)
        stored = self.store.attempt()
        assert stored is not None
        self.assertEqual(
            (
                stored.state,
                stored.pairing_id,
                stored.code,
                stored.expires_at,
                stored.interval,
                stored.next_poll_at,
            ),
            ("STARTED", PAIRING_ID, "ABCD-2345", 5600.0, 5, 5005.0),
        )

    def test_prepared_is_written_before_attempted(self):
        states: list[str] = []
        real = auth.CredentialStore.save_attempt

        def spy(store, attempt):
            states.append(attempt.state)
            return real(store, attempt)

        with mock.patch.object(auth.CredentialStore, "save_attempt", spy):
            self.start(FakeClient(json_response(pair_ok())))
        self.assertEqual(states, ["PREPARED", "ATTEMPTED", "STARTED"])

    def test_the_proof_exists_only_in_the_private_attempt_file(self):
        self.start(FakeClient(json_response(pair_ok())))
        for name in os.listdir(self.root):
            data = (self.root / name).read_bytes()
            self.assertEqual(VERIFIER.encode() in data, name == "attempt.json", name)
            self.assertEqual(stat.S_IMODE(os.lstat(self.root / name).st_mode), 0o600)

    def test_name_is_stripped_and_validated(self):
        client = FakeClient(json_response(pair_ok()))
        self.start(client, name="  my pet  ")
        self.assertEqual(json.loads(client.calls[0]["body"])["name"], "my pet")
        for name in ("", "   ", "x" * 81, "a\nb", "a\x00b", "a\x7fb", 5, None):
            with self.subTest(name=name):
                self.store.delete_attempt()
                client = FakeClient()
                exc = raised_any(self.start, client, name=name)
                self.assertEqual(exc.code, "auth_name_invalid")
                self.assertEqual(client.calls, [])

    def test_origin_must_be_a_strict_https_origin(self):
        for origin in (
            "http://x.example",
            "https://x.example/path",
            "https://u@x.example",
            "",
            None,
        ):
            with self.subTest(origin=origin):
                client = FakeClient()
                exc = raised_any(self.start, client, origin=origin)
                self.assertEqual(exc.code, "auth_origin_invalid")
                self.assertEqual(client.calls, [])
        self.assertIsNone(self.store.attempt())

    def test_invalid_start_responses_leave_an_unknown_start_that_blocks_restart(self):
        bad = {
            "short id": pair_ok(
                id="a" * 42, verificationPath="/devices/pair?id=" + "a" * 42
            ),
            "long id": pair_ok(
                id="a" * 44, verificationPath="/devices/pair?id=" + "a" * 44
            ),
            "id chars": pair_ok(
                id="a" * 42 + "+", verificationPath="/devices/pair?id=" + "a" * 42 + "+"
            ),
            "code lowercase": pair_ok(code="abcd-2345"),
            "code alphabet": pair_ok(code="ABCO-2345"),
            "code shape": pair_ok(code="ABCD2345"),
            "code too long": pair_ok(code="ABCD-23456"),
            "path other id": pair_ok(verificationPath="/devices/pair?id=" + "Z" * 43),
            "path absolute": pair_ok(
                verificationPath="https://evil.example/devices/pair?id=" + PAIRING_ID
            ),
            "path scheme-relative": pair_ok(
                verificationPath="//evil.example/devices/pair?id=" + PAIRING_ID
            ),
            "path extra query": pair_ok(
                verificationPath="/devices/pair?id=" + PAIRING_ID + "&x=1"
            ),
            "path missing": pair_ok(verificationPath=...),
            "expires bool": pair_ok(expiresIn=True),
            "expires zero": pair_ok(expiresIn=0),
            "expires 601": pair_ok(expiresIn=601),
            "expires str": pair_ok(expiresIn="600"),
            "expires float": pair_ok(expiresIn=600.0),
            "interval bool": pair_ok(interval=True),
            "interval zero": pair_ok(interval=0),
            "interval 61": pair_ok(interval=61),
            "interval str": pair_ok(interval="5"),
            "extra key": pair_ok(extra=1),
            "missing key": pair_ok(code=...),
            "not an object": [1, 2],
        }
        for name, doc in bad.items():
            with self.subTest(name):
                self.store.delete_attempt()
                client = FakeClient(json_response(doc))
                exc = raised_any(self.start, client)
                self.assertEqual(exc.code, "auth_start_unknown")
                attempt = self.store.attempt()
                assert attempt is not None
                self.assertEqual(attempt.state, "START_UNKNOWN")
                again = FakeClient()
                self.assertEqual(
                    raised_any(self.start, again).code, "auth_start_unknown"
                )
                self.assertEqual(again.calls, [])  # no second POST

    def test_non_json_and_status_201_are_unknown_starts(self):
        for response in (
            Response(200, b"not json"),
            Response(200, b""),
            json_response(pair_ok(), 201),
        ):
            with self.subTest(status=response.status, body=response.body[:8]):
                self.store.delete_attempt()
                self.assertEqual(
                    raised_any(self.start, FakeClient(response)).code,
                    "auth_start_unknown",
                )

    def test_transport_failures_are_unknown_unless_provably_unsent(self):
        unsent = TransportError("busy", maybe_sent=False)
        sent = TransportError("timeout", maybe_sent=True)
        for exc, state in (
            (sent, "START_UNKNOWN"),
            (RuntimeError("x"), "START_UNKNOWN"),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.store.delete_attempt()
                self.assertEqual(
                    raised_any(self.start, FakeClient(exc)).code, "auth_start_unknown"
                )
                attempt = self.store.attempt()
                assert attempt is not None
                self.assertEqual(attempt.state, state)
        self.store.delete_attempt()
        self.assertEqual(
            raised_any(self.start, FakeClient(unsent)).code, "auth_request_failed"
        )
        self.assertIsNone(
            self.store.attempt()
        )  # nothing was sent: a fresh start is allowed
        self.start(FakeClient(json_response(pair_ok())))

    def test_cancellation_marks_the_start_unknown_and_propagates(self):
        with self.assertRaises(KeyboardInterrupt):
            self.start(FakeClient(KeyboardInterrupt()))
        attempt = self.store.attempt()
        assert attempt is not None
        self.assertEqual(attempt.state, "START_UNKNOWN")

    def test_client_errors_are_a_known_rejection_and_allow_a_fresh_start(self):
        for status in (400, 429):
            with self.subTest(status=status):
                exc = raised_any(
                    self.start, FakeClient(Response(status, b"plain text"))
                )
                self.assertEqual(
                    (exc.code, exc.status), ("auth_start_rejected", status)
                )
                self.assertIsNone(self.store.attempt())
        for status in (500, 502, 503):
            with self.subTest(status=status):
                self.store.delete_attempt()
                exc = raised_any(self.start, FakeClient(Response(status, b"")))
                self.assertEqual(exc.code, "auth_start_unknown")

    def test_crash_after_attempted_is_recovered_as_unknown_without_any_post(self):
        self.store.save_attempt(
            auth.Attempt(
                state="ATTEMPTED", origin=ORIGIN_URL, name="pet", verifier=VERIFIER
            )
        )
        self.store.close()
        store = auth.CredentialStore(self.root)
        self.addCleanup(store.close)
        client = FakeClient()
        for call in (
            lambda: pair_start(
                store, client, ORIGIN_URL, "pet", now=self.clock, rand=rand
            ),
            lambda: pair_poll(store, client, self.clock),
        ):
            self.assertEqual(raised_any(call).code, "auth_start_unknown")
        self.assertEqual(client.calls, [])
        self.assertTrue(pair_abandon(store))
        pair_start(
            store,
            FakeClient(json_response(pair_ok())),
            ORIGIN_URL,
            "pet",
            now=self.clock,
            rand=rand,
        )

    def test_crash_after_prepared_is_safe_to_replace(self):
        self.store.save_attempt(
            auth.Attempt(
                state="PREPARED", origin=ORIGIN_URL, name="pet", verifier="P" * 43
            )
        )
        client = FakeClient(json_response(pair_ok()))
        self.start(client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            json.loads(client.calls[0]["body"])["challenge"], challenge_of(VERIFIER)
        )

    def test_a_started_attempt_blocks_a_second_start(self):
        self.started()
        client = FakeClient()
        self.assertEqual(raised_any(self.start, client).code, "auth_attempt_exists")
        self.assertEqual(client.calls, [])

    def test_failing_to_persist_the_started_record_does_not_show_a_prompt(self):
        real = auth.CredentialStore.save_attempt

        def flaky(store, attempt):
            if attempt.state == "STARTED":
                raise AuthError("auth_io_error")
            return real(store, attempt)

        with mock.patch.object(auth.CredentialStore, "save_attempt", flaky):
            exc = raised_any(self.start, FakeClient(json_response(pair_ok())))
        self.assertEqual(exc.code, "auth_io_error")
        attempt = self.store.attempt()
        assert attempt is not None
        self.assertEqual(attempt.state, "ATTEMPTED")  # recovers as an unknown start

    def test_persist_failure_before_the_post_sends_nothing(self):
        with mock.patch.object(os, "replace", side_effect=OSError(5, "x")):
            client = FakeClient()
            exc = raised_any(self.start, client)
        self.assertEqual(exc.code, "auth_io_error")
        self.assertEqual(client.calls, [])

    def test_no_error_carries_the_proof_or_a_cause(self):
        for item in (
            RuntimeError(VERIFIER),
            TransportError("timeout", maybe_sent=True),
        ):
            self.store.delete_attempt()
            exc = raised_any(self.start, FakeClient(item))
            self.assertNotIn(VERIFIER, str(exc) + repr(exc) + repr(exc.args))
            self.assertIsNone(exc.__cause__)
            self.assertTrue(exc.__suppress_context__)

    def test_abandon_is_local_only_and_never_touches_credentials(self):
        self.store.save_credential(Credential(CRED_REF, ORIGIN_URL, DEVICE_ID, TOKEN))
        self.assertFalse(pair_abandon(self.store))
        self.started()
        self.assertTrue(pair_abandon(self.store))
        self.assertIsNone(self.store.attempt())
        self.assertEqual(self.store.credential_refs(), [CRED_REF])


class PairPollTest(PairCase):
    def setUp(self) -> None:
        super().setUp()
        self.started()

    def test_no_attempt(self):
        self.store.delete_attempt()
        client = FakeClient()
        self.assertEqual(raised_any(self.poll, client).code, "auth_no_attempt")
        self.assertEqual(client.calls, [])

    def test_too_early_makes_no_request(self):
        client = FakeClient()
        result = self.poll(client)
        self.assertEqual((result.status, result.retry_after), ("too_early", 5.0))
        self.assertEqual(client.calls, [])
        self.clock.advance(4.9)
        self.assertEqual(self.poll(client).status, "too_early")
        self.assertEqual(client.calls, [])

    def test_pending_posts_id_and_proof_once_and_persists_the_next_slot_first(self):
        self.clock.advance(5)
        seen: list[float | None] = []
        client = FakeClient(json_response({"status": "pending"}))
        client.before = lambda call: seen.append(self.store.attempt().next_poll_at)  # type: ignore[union-attr]
        result = self.poll(client)
        self.assertEqual((result.status, result.retry_after), ("pending", 5.0))
        self.assertEqual(seen, [5010.0])  # persisted BEFORE the POST
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["route"].path, "/api/devices/pair/token")
        self.assertEqual(
            json.loads(call["body"]), {"id": PAIRING_ID, "verifier": VERIFIER}
        )
        self.assertNotIn("Authorization", call["headers"])
        self.assertIsNotNone(self.store.attempt())
        self.assertEqual(self.poll(FakeClient()).status, "too_early")

    def test_poll_after_expiry_deletes_the_attempt_without_a_request(self):
        self.clock.advance(600)
        client = FakeClient()
        self.assertEqual(self.poll(client).status, "expired")
        self.assertEqual(client.calls, [])
        self.assertIsNone(self.store.attempt())

    def test_terminal_statuses_delete_the_attempt(self):
        for body, expected in (
            (json_response({"status": "denied"}), "denied"),
            (json_response({"status": "revoked"}), "revoked"),
            (Response(410, b"gone"), "expired"),
            (Response(403, b"Invalid device proof."), "invalid_proof"),
        ):
            with self.subTest(expected=expected):
                self.store.delete_attempt()
                self.started()
                self.clock.advance(5)
                self.assertEqual(self.poll(FakeClient(body)).status, expected)
                self.assertIsNone(self.store.attempt())

    def test_rate_limited_poll_keeps_the_attempt(self):
        self.clock.advance(5)
        self.assertEqual(
            self.poll(FakeClient(Response(429, b"slow"))).status, "too_early"
        )
        self.assertIsNotNone(self.store.attempt())

    def test_unexpected_status_and_bad_bodies_keep_the_attempt(self):
        cases = [
            (Response(500, b""), "auth_http_status"),
            (Response(404, b""), "auth_http_status"),
            (Response(204, b""), "auth_http_status"),
            (json_response({"status": "weird"}), "auth_response_invalid"),
            (json_response({"status": "pending", "extra": 1}), "auth_response_invalid"),
            (json_response({}), "auth_response_invalid"),
            (Response(200, b"nope"), "auth_response_invalid"),
            (json_response({"status": True}), "auth_response_invalid"),
        ]
        for response, code in cases:
            with self.subTest(
                code=code, status=response.status, body=response.body[:8]
            ):
                self.clock.advance(5)
                self.assertEqual(raised_any(self.poll, FakeClient(response)).code, code)
                self.assertIsNotNone(self.store.attempt())

    def test_transport_failure_is_a_fixed_error_and_keeps_the_attempt(self):
        self.clock.advance(5)
        exc = raised_any(
            self.poll, FakeClient(TransportError("timeout", maybe_sent=True))
        )
        self.assertEqual(exc.code, "auth_request_failed")
        self.assertNotIn(VERIFIER, repr(exc) + str(exc))
        self.assertIsNotNone(self.store.attempt())

    def test_approved_persists_the_credential_before_deleting_the_proof(self):
        self.clock.advance(5)
        order: list[str] = []
        real_delete = auth.CredentialStore.delete_attempt

        def spy(store):
            order.append(
                "credential" if store.credential(CRED_REF) else "NO-CREDENTIAL"
            )
            order.append("attempt-present" if store.attempt() else "attempt-gone")
            return real_delete(store)

        with mock.patch.object(auth.CredentialStore, "delete_attempt", spy):
            result = self.poll(FakeClient(json_response(approved())))
        self.assertEqual(order, ["credential", "attempt-present"])
        self.assertEqual(result, PollResult("approved", None, CRED_REF))
        self.assertIsNone(self.store.attempt())
        self.assertEqual(
            self.store.credential(CRED_REF),
            Credential(CRED_REF, ORIGIN_URL, DEVICE_ID, TOKEN),
        )
        path = self.root / f"credential-{CRED_REF}.json"
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        self.assertNotIn(TOKEN, repr(result) + str(result))

    def test_malformed_approved_responses_store_nothing_and_keep_the_proof(self):
        other = "cd" * 16
        bad = {
            "amt token": approved(token="amt_" + DEVICE_ID + "." + "T" * 64),
            "amd token": approved(token="amd_" + DEVICE_ID + "." + "T" * 64),
            "short secret": approved(token="amc_" + DEVICE_ID + "." + "T" * 63),
            "device mismatch": approved(deviceID=other),
            "upper device": approved(deviceID=DEVICE_ID.upper()),
            "scope device": approved(scope="device-v1"),
            "scope missing": approved(scope=...),
            "token missing": approved(token=...),
            "extra grants": approved(grants=["x"]),
            "token int": approved(token=5),
        }
        for name, doc in bad.items():
            with self.subTest(name):
                self.clock.advance(5)
                exc = raised_any(self.poll, FakeClient(json_response(doc)))
                self.assertEqual(exc.code, "auth_response_invalid")
                self.assertNotIn("T" * 20, str(exc) + repr(exc))
                self.assertEqual(self.store.credential_refs(), [])
                self.assertIsNotNone(self.store.attempt())

    def test_credential_write_failure_keeps_the_proof_and_does_not_delete_it(self):
        self.clock.advance(5)
        with mock.patch.object(
            auth.CredentialStore,
            "save_credential",
            side_effect=AuthError("auth_io_error"),
        ):
            exc = raised_any(self.poll, FakeClient(json_response(approved())))
        self.assertEqual(exc.code, "auth_io_error")
        self.assertIsNotNone(self.store.attempt())

    def test_failed_proof_delete_blocks_a_fresh_pair_and_a_repeat_poll_finishes_safely(
        self,
    ):
        self.clock.advance(5)
        with mock.patch.object(
            auth.CredentialStore,
            "delete_attempt",
            side_effect=AuthError("auth_io_error"),
        ):
            exc = raised_any(self.poll, FakeClient(json_response(approved())))
        self.assertEqual(exc.code, "auth_io_error")
        self.assertEqual(
            self.store.credential_refs(), [CRED_REF]
        )  # credential is durable
        blocked = FakeClient()
        self.assertEqual(raised_any(self.start, blocked).code, "auth_attempt_exists")
        self.assertEqual(blocked.calls, [])
        self.clock.advance(5)
        repeat = self.poll(FakeClient(json_response(approved())))
        self.assertEqual(repeat, PollResult("approved", None, CRED_REF))
        self.assertIsNone(self.store.attempt())

    def test_repeated_approval_with_a_different_token_never_overwrites(self):
        self.store.save_credential(Credential(CRED_REF, ORIGIN_URL, DEVICE_ID, TOKEN))
        self.clock.advance(5)
        other = "amc_" + DEVICE_ID + "." + "U" * 64
        exc = raised_any(self.poll, FakeClient(json_response(approved(token=other))))
        self.assertEqual(exc.code, "auth_credential_conflict")
        credential = self.store.credential(CRED_REF)
        assert credential is not None
        self.assertEqual(credential.token, TOKEN)

    def test_now_may_be_a_callable_or_default_to_wall_clock(self):
        self.clock.advance(5)
        self.assertEqual(
            pair_poll(
                self.store, FakeClient(json_response({"status": "pending"})), self.clock
            ).status,
            "pending",
        )
        with mock.patch.object(auth.time, "time", return_value=5000.0 + 10_000):
            self.assertEqual(pair_poll(self.store, FakeClient()).status, "expired")


def attach_credential(store: auth.CredentialStore) -> None:
    store.save_credential(Credential(CRED_REF, ORIGIN_URL, DEVICE_ID, TOKEN))


class AttachTest(AuthCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        attach_credential(self.store)

    def do_attach(self, client: FakeClient, **kwargs: Any) -> Attachment:
        return attach(self.store, client, CRED_REF, rand=rand, **kwargs)

    def test_client_id_is_persisted_before_the_single_exact_post(self):
        seen: list[Any] = []
        client = FakeClient(json_response(attach_ok()))
        client.before = lambda call: seen.append(self.store.client_binding())
        result = self.do_attach(client)
        binding = seen[0]
        self.assertEqual(
            (binding.client_id, binding.credential_ref), (CLIENT_ID, CRED_REF)
        )
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(
            (call["route"].method, call["route"].path), ("POST", "/api/clients/attach")
        )
        self.assertEqual(call["origin"], Origin.parse(ORIGIN_URL))
        self.assertEqual(
            json.loads(call["body"]),
            {
                "clientId": CLIENT_ID,
                "kind": "native",
                "protocolVersion": 1,
                "includeState": False,
            },
        )
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertNotIn("X-Amplifier-Client", call["headers"])
        self.assertEqual(
            result,
            Attachment(CRED_REF, ORIGIN_URL, CLIENT_ID, HOST_IDENTITY, INSTANCE),
        )
        self.assertEqual(result.token, TOKEN)
        self.assertNotIn(TOKEN, repr(result))
        self.assertEqual(self.store.attachment_record(CRED_REF), result)

    def test_host_identity_is_never_sent_only_pinned_from_the_response(self):
        client = FakeClient(json_response(attach_ok()))
        self.do_attach(client)
        self.assertNotIn("hostIdentity", client.calls[0]["body"].decode())

    def test_invalid_responses_store_no_attachment(self):
        bad = {
            "client mismatch": attach_ok(clientId="D" * 32),
            "protocol 2": attach_ok(protocolVersion=2),
            "protocol bool": attach_ok(protocolVersion=True),
            "protocol str": attach_ok(protocolVersion="1"),
            "protocol missing": attach_ok(protocolVersion=...),
            "instance not uuid": attach_ok(hostInstanceId="x"),
            "instance v1": attach_ok(
                hostInstanceId="12345678-1234-1234-8234-123456789abc"
            ),
            "instance upper": attach_ok(hostInstanceId=INSTANCE.upper()),
            "identity short": attach_ok(hostIdentity="5" * 63),
            "identity upper": attach_ok(hostIdentity="A" * 64),
            "identity missing": attach_ok(hostIdentity=...),
            "not an object": [],
        }
        for name, doc in bad.items():
            with self.subTest(name):
                exc = raised_any(self.do_attach, FakeClient(json_response(doc)))
                self.assertEqual(exc.code, "auth_response_invalid")
                self.assertIsNone(self.store.attachment_record(CRED_REF))
        self.assertEqual(
            raised_any(self.do_attach, FakeClient(Response(200, b"nope"))).code,
            "auth_response_invalid",
        )

    def test_unauthorized_fails_closed_and_never_starts_a_pairing(self):
        for status in (401, 403):
            client = FakeClient(Response(status, b"denied"))
            exc = raised_any(self.do_attach, client)
            self.assertEqual((exc.code, exc.status), ("auth_unauthorized", status))
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(client.calls[0]["route"].path, "/api/clients/attach")
        self.assertIsNone(self.store.attempt())
        exc = raised_any(self.do_attach, FakeClient(Response(500, b"")))
        self.assertEqual((exc.code, exc.status), ("auth_http_status", 500))

    def test_transport_failure_is_fixed_and_keeps_the_client_id(self):
        exc = raised_any(
            self.do_attach, FakeClient(TransportError("timeout", maybe_sent=True))
        )
        self.assertEqual(exc.code, "auth_request_failed")
        self.assertNotIn(TOKEN, repr(exc) + str(exc))
        self.assertEqual(self.store.client_binding().client_id, CLIENT_ID)  # type: ignore[union-attr]
        self.assertIsNone(self.store.attachment_record(CRED_REF))

    def test_missing_credential(self):
        client = FakeClient()
        exc = raised_any(attach, self.store, client, "device-" + "ef" * 16, rand=rand)
        self.assertEqual(exc.code, "auth_no_credential")
        self.assertEqual(client.calls, [])

    def test_the_client_id_is_reused_and_a_second_credential_cannot_take_it(self):
        self.do_attach(FakeClient(json_response(attach_ok())))
        again = FakeClient(json_response(attach_ok()))
        with mock.patch.object(
            auth.secrets, "token_urlsafe", side_effect=AssertionError("new id")
        ):
            result = attach(self.store, again, CRED_REF)
        self.assertEqual(json.loads(again.calls[0]["body"])["clientId"], CLIENT_ID)
        self.assertEqual(result.client_id, CLIENT_ID)
        other_id = "cd" * 16
        other_token = "amc_" + other_id + "." + "S" * 64
        self.store.save_credential(
            Credential("device-" + other_id, ORIGIN_URL, other_id, other_token)
        )
        client = FakeClient()
        exc = raised_any(attach, self.store, client, "device-" + other_id, rand=rand)
        self.assertEqual(exc.code, "auth_client_bound")
        self.assertEqual(client.calls, [])

    def test_changed_host_identity_fails_and_is_never_rebindable(self):
        self.do_attach(FakeClient(json_response(attach_ok())))
        before = self.store.attachment_record(CRED_REF)
        exc = raised_any(
            self.do_attach,
            FakeClient(json_response(attach_ok(hostIdentity="6" * 64))),
        )
        self.assertEqual(exc.code, "auth_host_mismatch")
        self.assertEqual(self.store.attachment_record(CRED_REF), before)

    def test_changed_instance_always_fails_and_changes_nothing(self):
        self.do_attach(FakeClient(json_response(attach_ok())))
        before = self.store.attachment_record(CRED_REF)
        binding = self.store.client_binding()
        for _ in range(2):  # no state is ever learned from a rejected instance
            exc = raised_any(
                self.do_attach,
                FakeClient(json_response(attach_ok(hostInstanceId=INSTANCE_2))),
            )
            self.assertEqual(exc.code, "auth_instance_changed")
            self.assertEqual(self.store.attachment_record(CRED_REF), before)
            self.assertEqual(self.store.client_binding(), binding)

    def test_attach_has_no_instance_override_parameter(self):
        names = list(inspect.signature(attach).parameters)
        self.assertEqual(names, ["store", "client", "ref", "rand"])
        self.assertNotIn("accept_instance_change", names)
        self.assertFalse(hasattr(auth, "_pinned_host"))
        client = FakeClient(json_response(attach_ok(hostInstanceId=INSTANCE_2)))
        with self.assertRaises(TypeError):
            attach(
                self.store,
                client,
                CRED_REF,
                accept_instance_change=True,  # pyright: ignore[reportCallIssue]
            )
        self.assertEqual(client.calls, [])

    def test_same_instance_reattach_is_idempotent(self):
        first = self.do_attach(FakeClient(json_response(attach_ok())))
        second = self.do_attach(FakeClient(json_response(attach_ok())))
        self.assertEqual(first, second)

    def test_load_attachment_helper_joins_the_token(self):
        self.do_attach(FakeClient(json_response(attach_ok())))
        loaded = auth.load_attachment(self.store, CRED_REF)
        assert loaded is not None
        self.assertEqual(loaded.token, TOKEN)
        self.assertIsNone(auth.load_attachment(self.store, "device-" + "ef" * 16))

    def test_no_key_comes_from_the_environment(self):
        with mock.patch.dict(
            os.environ, {"AMPLIFIER_TOKEN": "amc_" + "e" * 32 + "." + "E" * 64}
        ):
            client = FakeClient(json_response(attach_ok()))
            self.do_attach(client)
        self.assertEqual(client.calls[0]["headers"]["Authorization"], "Bearer " + TOKEN)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
