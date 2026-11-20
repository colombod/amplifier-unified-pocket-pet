"""Offline tests for companion session listing, idle preflight and detail windows.

The server is a scripted simulation derived from reading installed Unified 0.80.1
(``companion.py``, ``browser_detail.page``).  Not a live wire qualification.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from pet_bridge import companion, reply
from pet_bridge.auth import Attachment
from pet_bridge.companion import (
    CompanionError,
    detail_window,
    list_all_work,
    list_sessions,
    preflight,
    sanitize_title,
)
from pet_bridge.transport import Origin, Response, TransportError

from .authfakes import (
    CLIENT_ID,
    CRED_REF,
    HOST_IDENTITY,
    INSTANCE,
    INSTANCE_2,
    ORIGIN_URL,
    TOKEN,
    FakeClient,
    json_response,
    raised_any,
)
from .fakes import OfflineTestCase, assistant_row, user_row

SID = "sess-abc_123"
ATT = Attachment(CRED_REF, ORIGIN_URL, CLIENT_ID, HOST_IDENTITY, INSTANCE, token=TOKEN)


def srow(
    session_id: Any, status: Any = "idle", title: Any = "A title", **activity: Any
) -> dict:
    flags = {"execution": False, "preparing": False, "loading": False}
    flags.update(activity)
    return {
        "id": session_id,
        "title": title,
        "status": status,
        "activity": flags,
        "pinned": False,
        "role": "root",
        "agentCreated": False,
    }


def lpage(rows: list, nxt: Any = None, **changes: Any) -> Response:
    doc: dict[str, Any] = {
        "revision": 3,
        "items": rows,
        "nextCursor": nxt,
        "hostIdentity": HOST_IDENTITY,
        "hostInstanceId": INSTANCE,
    }
    doc.update(changes)
    return json_response({k: v for k, v in doc.items() if v is not ...})


def cpage(rows: list, nxt: Any = None, **changes: Any) -> Response:
    return lpage(rows, nxt, **{"revision": "opaque-sessions-v1", **changes})


OBSERVATION_TARGET = (
    "/api/sessions?view=observation&scope=roots&activity=all&pinned=all&limit=200"
)


class AllWorkCatalogTest(OfflineTestCase):
    def test_listing_paths_never_request_removed_companion_or_bootstrap(self):
        for listing in (list_sessions, list_all_work):
            with self.subTest(listing=listing.__name__):
                client = FakeClient(cpage([srow("s1")], "c1"), cpage([srow("s2")]))
                result = listing(client, ATT)
                self.assertTrue(result.complete)
                self.assertEqual([c["route"].target for c in client.calls],
                                 [OBSERVATION_TARGET, OBSERVATION_TARGET + "&cursor=c1"])
                self.assertTrue(all(c["route"].method == "GET" for c in client.calls))

    def test_opt_in_includes_dormant_unpinned_roots_without_a_send_revision(self):
        client = FakeClient(
            cpage([srow("dormant-unpinned"), srow("busy", "working", execution=True)]),
        )
        listing = list_all_work(client, ATT, monotonic=lambda: 12.5)
        self.assertEqual([r.id for r in listing.sessions], ["dormant-unpinned", "busy"])
        self.assertFalse(listing.sessions[0].pinned)
        self.assertEqual((listing.sessions[0].role, listing.sessions[0].agent_created),
                         ("root", False))
        self.assertEqual(listing.observation_revision, "opaque-sessions-v1")
        self.assertFalse(hasattr(listing, "revision"))  # never the core's global send CAS
        self.assertTrue(listing.complete)
        self.assertEqual((listing.pages, listing.scope, listing.fetched_monotonic),
                         (1, "roots", 12.5))
        self.assertEqual((listing.host_identity, listing.host_instance_id),
                         (HOST_IDENTITY, INSTANCE))
        self.assertEqual([c["route"].path for c in client.calls],
                         ["/api/sessions"])
        for call in client.calls:
            self.assertEqual(call["route"].method, "GET")
            self.assertEqual(call["body"], b"")
            self.assertEqual(call["origin"], Origin.parse(ORIGIN_URL))
            self.assertEqual(call["headers"]["Authorization"], "Bearer " + TOKEN)
            self.assertEqual(call["headers"]["X-Amplifier-Client"], CLIENT_ID)

    def test_explicit_roots_delegates_and_all_scope(self):
        for scope, roles in (
            ("roots", ["root"]), ("delegates", ["delegate"]),
            ("all", ["root", "delegate"]),
        ):
            with self.subTest(scope=scope):
                client = FakeClient(
                    cpage([{**srow(f"s{i}"), "role": role} for i, role in enumerate(roles)]),
                )
                listing = list_all_work(client, ATT, scope=scope)
                self.assertEqual([r.role for r in listing.sessions], roles)
                self.assertEqual(dict(client.calls[-1]["route"].query)["scope"], scope)
        for scope, role in (("roots", "delegate"), ("delegates", "root"), ("all", "internal")):
            with self.subTest(scope=scope, role=role):
                client = FakeClient(cpage([{**srow("s1"), "role": role}]))
                self.assertEqual(raised_any(list_all_work, client, ATT, scope=scope).code,
                                 "companion_response_invalid")

    def test_empty_continuations_preserve_opaque_int_or_string_revision(self):
        for revision in (0, 42, "42", INSTANCE + ":sessions:" + "f" * 64):
            with self.subTest(revision=revision):
                client = FakeClient(
                    cpage([], "cursor+1/opaque=", revision=revision),
                    cpage([srow("s1")], "c2", revision=revision),
                    cpage([], "c3", revision=revision),
                    cpage([srow("s2")], revision=revision),
                )
                listing = list_all_work(client, ATT, limit=2)
                self.assertEqual([r.id for r in listing.sessions], ["s1", "s2"])
                self.assertTrue(listing.complete)
                self.assertEqual(listing.pages, 4)
                self.assertIs(type(listing.observation_revision), type(revision))
                self.assertEqual(listing.observation_revision, revision)
                routes = [c["route"] for c in client.calls]
                self.assertEqual([dict(r.query).get("cursor") for r in routes],
                                 [None, "cursor+1/opaque=", "c2", "c3"])
                for route in routes:
                    self.assertEqual(dict(route.query)["limit"], "2")
                    self.assertEqual(dict(route.query)["activity"], "all")
                    self.assertEqual(dict(route.query)["pinned"], "all")

    def test_page_cap_is_explicitly_incomplete_even_for_empty_pages(self):
        for empty in (True, False):
            with self.subTest(empty=empty):
                client = FakeClient(*[
                    cpage([] if empty else [srow(f"s{i}")], f"c{i}") for i in range(8)
                ])
                listing = list_all_work(client, ATT)
                self.assertFalse(listing.complete)
                self.assertEqual((listing.pages, len(client.calls)), (5, 5))
                self.assertEqual(len(listing.sessions), 0 if empty else 5)

    def test_missing_or_different_identity_on_any_page_fails_closed(self):
        for listing in (list_sessions, list_all_work):
            for field, values in (
                ("hostIdentity", (None, "6" * 64, ...)),
                ("hostInstanceId", (None, INSTANCE_2, ...)),
            ):
                for value in values:
                    for continuation in (False, True):
                        with self.subTest(listing=listing.__name__, field=field,
                                          value=value, continuation=continuation):
                            before = [cpage([srow("s1")], "c1")] if continuation else []
                            client = FakeClient(*before, cpage([], **{field: value}))
                            self.assertEqual(raised_any(listing, client, ATT).code,
                                             "companion_host_mismatch")
                            self.assertEqual(len(client.calls), len(before) + 1)

    def test_bad_scope_or_limit_fails_before_request(self):
        for kwargs in ({"scope": "all&scope=roots"}, {"scope": None},
                       {"scope": []}, {"limit": True}, {"limit": 201}, {"limit": 0}):
            with self.subTest(kwargs=kwargs):
                client = FakeClient()
                self.assertEqual(raised_any(list_all_work, client, ATT, **kwargs).code,
                                 "companion_request_invalid")
                self.assertEqual(client.calls, [])

    def test_changed_or_type_changed_snapshot_discards_partial_catalog(self):
        for first, second in ((1, 2), (1, "1"), ("1", 1), ("one", "two")):
            with self.subTest(first=first, second=second):
                client = FakeClient(
                    cpage([srow("s1")], "c1", revision=first),
                    cpage([srow("s2")], revision=second),
                )
                self.assertEqual(raised_any(list_all_work, client, ATT).code,
                                 "companion_snapshot_changed")
                self.assertEqual(len(client.calls), 2)

    def test_duplicate_ids_and_cursor_cycles_fail_closed(self):
        cases = (
            ([cpage([srow("s1"), srow("s1")])], "companion_response_invalid"),
            ([cpage([srow("s1")], "c1"), cpage([srow("s1")])], "companion_response_invalid"),
            ([cpage([], "c1"), cpage([], "c1")], "companion_cursor_cycle"),
            ([cpage([], "c1"), cpage([], "c2"), cpage([], "c1")], "companion_cursor_cycle"),
        )
        for pages, code in cases:
            with self.subTest(code=code, pages=len(pages)):
                client = FakeClient(*pages)
                self.assertEqual(raised_any(list_all_work, client, ATT).code, code)
                self.assertEqual(len(client.calls), len(pages))

    def test_malformed_page_fields_fail_closed(self):
        changes = [
            {"items": x} for x in (None, {}, [srow(f"s{i}") for i in range(201)])
        ] + [
            {"revision": x} for x in (None, True, -1, 1.5, "", "x" * 201, "bad rev", ...)
        ] + [
            {"nextCursor": x} for x in (True, 1, [], "", "bad cursor", "%" * 1024, ...)
        ]
        for change in changes:
            with self.subTest(change=list(change)):
                client = FakeClient(cpage([], **change))
                self.assertEqual(raised_any(list_all_work, client, ATT).code,
                                 "companion_response_invalid")
                self.assertEqual(len(client.calls), 1)
        client = FakeClient(cpage([srow("s1"), srow("s2")]))
        self.assertEqual(raised_any(list_all_work, client, ATT, limit=1).code,
                         "companion_response_invalid")

    def test_malformed_or_oversized_rows_fail_closed(self):
        changes = (
            {"id": "bad/id"}, {"id": None}, {"title": 12}, {"title": "x" * 201},
            {"status": "x" * 201}, {"activity": {}}, {"activity": {"execution": 1}},
            {"pinned": ...}, {"pinned": 0}, {"role": ...}, {"role": []},
            {"role": "internal"}, {"agentCreated": None}, {"agentCreated": "false"},
        )
        for change in changes:
            with self.subTest(change=list(change)):
                raw = {k: v for k, v in {**srow("s1"), **change}.items() if v is not ...}
                client = FakeClient(cpage([raw]))
                self.assertEqual(raised_any(list_all_work, client, ATT).code,
                                 "companion_response_invalid")
        for raw in (None, [], "not-row"):
            client = FakeClient(cpage([raw]))
            self.assertEqual(raised_any(list_all_work, client, ATT).code,
                             "companion_response_invalid")

    def test_additive_fields_ignored_and_missing_creation_evidence_is_unknown(self):
        row = srow("s1", title="bad\u202e\x1b\n" + "x" * 180)
        del row["agentCreated"]
        row.update(workspace="/not/a/supported/fact", future={"anything": True})
        client = FakeClient(
            cpage([row], future={"test": 1}),
        )
        listing = list_all_work(client, ATT)
        self.assertIsNone(listing.sessions[0].agent_created)
        self.assertFalse(hasattr(listing.sessions[0], "workspace"))
        self.assertEqual(len(listing.sessions[0].title), 80)
        self.assertNotIn("\x1b", listing.sessions[0].title)
        self.assertNotIn("\u202e", listing.sessions[0].title)

    def test_all_http_failures_stop_with_no_retry_or_partial_result(self):
        for status, code in (
            (400, "companion_invalid_cursor"), (401, "companion_unauthorized"),
            (403, "companion_unauthorized"),  # reference companion-v1 scope denies catalog
            (404, "companion_observation_unsupported"), (409, "companion_snapshot_changed"),
            (503, "companion_observation_busy"), (500, "companion_http_status"),
        ):
            for stage in ("first", "continuation"):
                with self.subTest(status=status, stage=stage):
                    before = []
                    if stage == "continuation":
                        before.append(cpage([srow("partial")], "c1"))
                    client = FakeClient(*before, Response(status, b"SECRET-ERROR"), cpage([]))
                    exc = raised_any(list_all_work, client, ATT)
                    self.assertIsInstance(exc, CompanionError)
                    self.assertEqual((exc.code, exc.status), (code, status))
                    self.assertEqual(len(client.calls), len(before) + 1)
                    self.assertNotIn("SECRET-ERROR", repr(exc))

    def test_transport_invalid_json_and_token_failures_are_fixed(self):
        for result, code in (
            (TransportError("timeout"), "companion_request_failed"),
            (Response(200, b"not JSON"), "companion_response_invalid"),
            (Response(200, b'{"items":[],"revision":1,"revision":2,"nextCursor":null}'),
             "companion_response_invalid"),
        ):
            client = FakeClient(result)
            exc = raised_any(list_all_work, client, ATT)
            self.assertEqual(exc.code, code)
            self.assertIsNone(exc.__cause__)
            self.assertEqual(len(client.calls), 1)
        bare = Attachment(CRED_REF, ORIGIN_URL, CLIENT_ID, HOST_IDENTITY, INSTANCE)
        client = FakeClient()
        self.assertEqual(raised_any(list_all_work, client, bare).code, "companion_unauthorized")
        self.assertEqual(client.calls, [])


class FixedClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class ListTest(OfflineTestCase):
    def test_one_page_exact_rows_revision_and_complete_flag(self):
        client = FakeClient(
            lpage(
                [
                    srow("s1", title="First"),
                    srow("s2", status="working", execution=True),
                ]
            )
        )
        listing = list_sessions(client, ATT, monotonic=FixedClock(50.0))
        self.assertEqual([r.id for r in listing.sessions], ["s1", "s2"])
        self.assertEqual((listing.revision, listing.observation_revision), (0, 3))
        self.assertTrue(listing.complete)
        self.assertEqual(listing.pages, 1)
        self.assertEqual(listing.fetched_monotonic, 50.0)
        self.assertEqual(
            (listing.host_identity, listing.host_instance_id), (HOST_IDENTITY, INSTANCE)
        )
        first = listing.sessions[0]
        self.assertEqual(
            (
                first.title,
                first.status,
                first.execution,
                first.preparing,
                first.loading,
            ),
            ("First", "idle", False, False, False),
        )
        self.assertTrue(listing.sessions[1].execution)
        (call,) = client.calls
        self.assertEqual(
            (call["route"].method, call["route"].target), ("GET", OBSERVATION_TARGET)
        )
        self.assertEqual(call["origin"], Origin.parse(ORIGIN_URL))
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(call["headers"]["X-Amplifier-Client"], CLIENT_ID)
        self.assertEqual(call["body"], b"")

    def test_pages_are_followed_with_the_server_cursor_and_merged_in_order(self):
        client = FakeClient(
            lpage([srow("s1")], "cur-1"),
            lpage([srow("s2")], "cur-2"),
            lpage([srow("s3")]),
        )
        listing = list_sessions(client, ATT)
        self.assertEqual([r.id for r in listing.sessions], ["s1", "s2", "s3"])
        self.assertTrue(listing.complete)
        self.assertEqual(listing.pages, 3)
        self.assertEqual(
            [c["route"].target for c in client.calls],
            [
                OBSERVATION_TARGET,
                OBSERVATION_TARGET + "&cursor=cur-1",
                OBSERVATION_TARGET + "&cursor=cur-2",
            ],
        )

    def test_the_page_cap_is_explicit_and_incomplete_never_complete(self):
        pages = [lpage([srow(f"s{i}")], f"cur-{i}") for i in range(10)]
        client = FakeClient(*pages)
        listing = list_sessions(client, ATT)
        self.assertEqual(len(client.calls), 5)
        self.assertEqual(listing.pages, 5)
        self.assertFalse(listing.complete)
        self.assertEqual(len(listing.sessions), 5)

    def test_cursor_cycle_fails_closed(self):
        client = FakeClient(lpage([srow("s1")], "cur-1"), lpage([srow("s2")], "cur-1"))
        self.assertEqual(
            raised_any(list_sessions, client, ATT).code, "companion_cursor_cycle"
        )
        client = FakeClient(
            lpage([srow("s1")], "cur-1"),
            lpage([srow("s2")], "cur-2"),
            lpage([srow("s3")], "cur-1"),
        )
        self.assertEqual(
            raised_any(list_sessions, client, ATT).code, "companion_cursor_cycle"
        )

    def test_snapshot_changes_between_pages_fail_closed(self):
        for change in ({"revision": 11}, {"revision": "opaque-new-snapshot"}):
            with self.subTest(change=change):
                client = FakeClient(
                    lpage([srow("s1")], "c1"), lpage([srow("s2")], **change)
                )
                self.assertEqual(
                    raised_any(list_sessions, client, ATT).code,
                    "companion_snapshot_changed",
                )

    def test_opaque_observation_revision_is_preserved_across_pages(self):
        # Current installed client_observations.revision_for/directory_key emits
        # instance:activity:generation-or-digest. It is NOT the send CAS revision.
        for revision in (INSTANCE + ":activity:12", INSTANCE + ":activity:" + "a" * 64):
            with self.subTest(revision=revision):
                listing = list_sessions(
                    FakeClient(
                        lpage([srow("s1")], "c1", revision=revision),
                        lpage([srow("s2")], revision=revision),
                    ),
                    ATT,
                )
                self.assertTrue(listing.complete)
                self.assertEqual(listing.observation_revision, revision)
                self.assertIsInstance(listing.observation_revision, str)
                self.assertEqual(listing.revision, 0)
                self.assertIs(type(listing.revision), int)

    def test_observation_revision_change_or_type_change_fails_closed(self):
        for first, second in (("opaque-v1", "opaque-v2"), (3, "3"), ("3", 3)):
            with self.subTest(first=first, second=second):
                with self.assertRaises(CompanionError) as caught:
                    list_sessions(
                        FakeClient(
                            lpage([srow("s1")], "c1", revision=first),
                            lpage([srow("s2")], revision=second),
                        ),
                        ATT,
                    )
                self.assertEqual(caught.exception.code, "companion_snapshot_changed")

    def test_opaque_observation_revision_invalid_values_fail_closed(self):
        for value in (True, -1, 1.5, None, {}, [], "", "a" * 201,
                      "a b", "a\nb", "\x00", "révision"):
            with self.subTest(value=value):
                with self.assertRaises(CompanionError) as caught:
                    list_sessions(FakeClient(lpage([], revision=value)), ATT)
                self.assertEqual(caught.exception.code, "companion_response_invalid")

    def test_host_identity_or_instance_mismatch_fails_closed(self):
        for change in (
            {"hostIdentity": "6" * 64},
            {"hostInstanceId": INSTANCE_2},
            {"hostIdentity": ...},
            {"hostInstanceId": ...},
        ):
            with self.subTest(change=change):
                code = raised_any(
                    list_sessions, FakeClient(lpage([srow("s1")], **change)), ATT
                ).code
                self.assertEqual(code, "companion_host_mismatch")
        client = FakeClient(
            lpage([srow("s1")], "c1"), lpage([srow("s2")], hostInstanceId=INSTANCE_2)
        )
        self.assertEqual(
            raised_any(list_sessions, client, ATT).code, "companion_host_mismatch"
        )

    def test_malformed_pages_fail_closed(self):
        bad = {
            "items not list": {"items": {}},
            "items missing": {"items": ...},
            "row not dict": {"items": ["x"]},
            "row no id": {
                "items": [{k: v for k, v in srow("s1").items() if k != "id"}]
            },
            "row id invalid": {"items": [srow("bad id")]},
            "row id type": {"items": [srow(5)]},
            "status type": {"items": [{**srow("s1"), "status": 5}]},
            "status missing": {
                "items": [{k: v for k, v in srow("s1").items() if k != "status"}]
            },
            "activity missing": {
                "items": [{k: v for k, v in srow("s1").items() if k != "activity"}]
            },
            "activity flag int": {"items": [srow("s1", execution=0)]},
            "activity flag str": {"items": [srow("s1", loading="false")]},
            "activity flag missing": {
                "items": [
                    {**srow("s1"), "activity": {"execution": False, "preparing": False}}
                ]
            },
            "title type": {"items": [{**srow("s1"), "title": 5}]},
            "title oversized": {"items": [srow("s1", title="x" * 201)]},
            "status oversized": {"items": [srow("s1", status="x" * 201)]},
            "pinned missing": {"items": [{k: v for k, v in srow("s1").items() if k != "pinned"}]},
            "role scope mismatch": {"items": [{**srow("s1"), "role": "delegate"}]},
            "duplicate id": {"items": [srow("s1"), srow("s1")]},
            "page oversized": {"items": [srow(f"s{i}") for i in range(201)]},
            "revision bool": {"revision": True},
            "revision empty": {"revision": ""},
            "revision negative": {"revision": -1},
            "revision missing": {"revision": ...},
            "observation revision": {"revision": 1.5},
            "cursor missing": {"nextCursor": ...},
            "cursor type": {"nextCursor": 5},
            "cursor invalid": {"nextCursor": "a b"},
            "cursor empty": {"nextCursor": ""},
        }
        for name, change in bad.items():
            with self.subTest(name):
                exc = raised_any(
                    list_sessions, FakeClient(lpage([srow("s1")], **change)), ATT
                )
                self.assertIsInstance(exc, CompanionError)
                self.assertEqual(exc.code, "companion_response_invalid")
        self.assertEqual(
            raised_any(list_sessions, FakeClient(Response(200, b"nope")), ATT).code,
            "companion_response_invalid",
        )

    def test_duplicate_ids_across_pages_fail_closed(self):
        client = FakeClient(lpage([srow("s1")], "c1"), lpage([srow("s1")]))
        self.assertEqual(
            raised_any(list_sessions, client, ATT).code, "companion_response_invalid"
        )

    def test_http_failures_are_fixed_codes_with_one_bounded_get_and_no_loop(self):
        cases = {
            400: "companion_invalid_cursor",
            401: "companion_unauthorized",
            403: "companion_unauthorized",
            409: "companion_snapshot_changed",
            503: "companion_observation_busy",
            500: "companion_http_status",
            404: "companion_observation_unsupported",
        }
        for status, code in cases.items():
            for continuation in (False, True):
                with self.subTest(status=status, continuation=continuation):
                    before = [lpage([srow("partial")], "c1")] if continuation else []
                    client = FakeClient(*before,
                    Response(
                        status,
                        b'{"error":"secret detail","code":"observation_pending"}',
                    )
                    )
                    exc = raised_any(list_sessions, client, ATT)
                    self.assertEqual((exc.code, exc.status), (code, status))
                    self.assertEqual(len(client.calls), len(before) + 1)
                    self.assertNotIn("secret detail", str(exc) + repr(exc))

    def test_transport_failure_is_fixed_and_chainless(self):
        exc = raised_any(
            list_sessions, FakeClient(TransportError("timeout", maybe_sent=True)), ATT
        )
        self.assertEqual(exc.code, "companion_request_failed")
        self.assertIsNone(exc.__cause__)

    def test_attachment_without_a_token_cannot_authenticate(self):
        bare = Attachment(CRED_REF, ORIGIN_URL, CLIENT_ID, HOST_IDENTITY, INSTANCE)
        client = FakeClient()
        self.assertEqual(
            raised_any(list_sessions, client, bare).code, "companion_unauthorized"
        )
        self.assertEqual(client.calls, [])

    def test_attention_items_and_extra_fields_do_not_matter(self):
        listing = list_sessions(
            FakeClient(lpage([srow("s1")], attentionItems=[{"anything": 1}], extra="x")), ATT
        )
        self.assertEqual(len(listing.sessions), 1)


class TitleTest(OfflineTestCase):
    def test_titles_are_sanitised_for_display(self):
        cases = {
            "plain": "plain",
            "  spaced \t  out\n title  ": "spaced out title",
            "red\x1b[31mtext\x1b[0m": "red[31mtext[0m",
            "bidi \u202eevil\u202c": "bidi evil",
            "zero\u200bwidth": "zerowidth",
            "nul\x00byte\x7f": "nulbyte",
            "": "",
            "\x1b\x07": "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(sanitize_title(raw), expected)

    def test_long_titles_are_cut_to_80_characters(self):
        cut = sanitize_title("x" * 500)
        self.assertEqual(len(cut), 80)
        self.assertTrue(cut.endswith("\u2026"))
        self.assertEqual(sanitize_title("y" * 80), "y" * 80)

    def test_listing_exposes_only_the_sanitised_title(self):
        listing = list_sessions(
            FakeClient(lpage([srow("s1", title="a\u202eb\x1b" + "z" * 190)])), ATT
        )
        title = listing.sessions[0].title
        self.assertLessEqual(len(title), 80)
        self.assertNotIn("\u202e", title)
        self.assertNotIn("\x1b", title)


class PreflightTest(OfflineTestCase):
    def listing(self, rows, nxt=None, now=100.0):
        return list_sessions(
            FakeClient(lpage(rows, nxt)), ATT, monotonic=FixedClock(now)
        )

    def test_only_a_fresh_exact_idle_row_with_all_flags_false_is_idle(self):
        listing = self.listing([srow("s1"), srow("s2", status="working")])
        result = preflight(listing, "s1", monotonic=FixedClock(105.0))
        self.assertEqual(
            (result.session_id, result.idle, result.revision, result.reason),
            ("s1", True, 0, "idle"),
        )

    def test_hostile_status_text_is_retained_raw_and_is_never_idle(self):
        for status in ("\x1b[31midle", "IDLE", "idle ", "idle\n", "x" * 200):
            with self.subTest(status=status[:10]):
                listing = self.listing([srow("s1", status=status)])
                self.assertEqual(listing.sessions[0].status, status)  # not sanitized
                result = preflight(listing, "s1", monotonic=FixedClock(100.0))
                self.assertFalse(result.idle)
                self.assertEqual(result.reason, "status_not_idle")

    def test_everything_else_is_never_idle(self):
        for status in (
            "working",
            "running",
            "stopping",
            "error",
            "stopped",
            "interrupted",
            "unknown",
            "IDLE",
            "idle ",
            "",
            "ready",
            "completed",
        ):
            with self.subTest(status=status):
                result = preflight(
                    self.listing([srow("s1", status=status)]),
                    "s1",
                    monotonic=FixedClock(100.0),
                )
                self.assertFalse(result.idle)
                self.assertEqual(result.reason, "status_not_idle")
        for flag in ("execution", "preparing", "loading"):
            with self.subTest(flag=flag):
                result = preflight(
                    self.listing([srow("s1", **{flag: True})]),
                    "s1",
                    monotonic=FixedClock(100.0),
                )
                self.assertFalse(result.idle)
                self.assertEqual(result.reason, "activity")

    def test_unlisted_missing_or_invalid_selection_is_never_idle(self):
        pages = [lpage([srow(f"s{i}")], f"c{i}") for i in range(1, 6)]
        listing = list_sessions(FakeClient(*pages), ATT, monotonic=FixedClock(100.0))
        self.assertFalse(listing.complete)  # an incomplete listing still lists s1..s5
        for selected in ("s9", "", None, 5, "bad id"):
            with self.subTest(selected=selected):
                result = preflight(listing, selected, monotonic=FixedClock(100.0))
                self.assertFalse(result.idle)
                self.assertEqual(result.reason, "unlisted")
        self.assertFalse(
            preflight(self.listing([]), "s1", monotonic=FixedClock(100.0)).idle
        )

    def test_a_stale_list_is_not_fresh(self):
        listing = self.listing([srow("s1")], now=100.0)
        self.assertTrue(
            preflight(listing, "s1", monotonic=FixedClock(110.0), max_age=10.0).idle
        )
        stale = preflight(listing, "s1", monotonic=FixedClock(110.5), max_age=10.0)
        self.assertEqual((stale.idle, stale.reason), (False, "stale_list"))
        backwards = preflight(listing, "s1", monotonic=FixedClock(99.0), max_age=10.0)
        self.assertEqual((backwards.idle, backwards.reason), (False, "stale_list"))

    def test_preflight_does_not_claim_an_atomic_guarantee(self):
        self.assertIn("not atomic", (companion.__doc__ or "").lower())
        result = preflight(
            self.listing([srow("s1")]), "s1", monotonic=FixedClock(100.0)
        )
        self.assertNotIn("title", repr(result))


class DetailServer:
    """Simulates ``browser_detail.page`` for part=messages (newest tail, ``before`` = id)."""

    def __init__(
        self,
        rows: list[dict],
        limit: int = 60,
        user_base: int = 0,
        source_base: int = 0,
    ) -> None:
        self.rows, self.limit = rows, limit
        self.user_base, self.source_base = user_base, source_base
        self.requests: list[dict[str, str]] = []

    def __call__(self, call: dict[str, Any]) -> Response:
        query = dict(call["route"].query)
        self.requests.append(query)
        before = query.get("before")
        end = len(self.rows)
        if before is not None:
            end = next(i for i, r in enumerate(self.rows) if r["id"] == before)
        start = max(0, end - self.limit)
        items = self.rows[start:end]
        users = sum(r.get("role") == "user" for r in self.rows[:start])
        return json_response(
            {
                "items": items,
                "offset": start,
                "total": len(self.rows),
                "before": items[0]["id"] if start and items else None,
                "userOffset": self.user_base + users,
                "sourceOffset": self.source_base + start,
            }
        )

    def client(self, count: int = 10) -> FakeClient:
        return FakeClient(*[self] * count)


def history(n: int) -> list[dict]:
    roles = ["user", "assistant", "tool", "system"]
    return [
        {"id": f"m{i:03d}", "role": roles[i % 4], "text": f"t{i}", "extra": {"i": i}}
        for i in range(n)
    ]


def total(doc):
    doc["total"] += 1


def gap(doc):
    doc["offset"] -= 1
    doc["items"] = doc["items"][1:]
    doc["before"] = doc["items"][0]["id"]


def overlap(doc):
    doc["items"] = doc["items"] + [{"id": "m999", "role": "user"}]


def before_none(doc):
    doc["before"] = None


def before_wrong(doc):
    doc["before"] = "other"


def empty_items(doc):
    doc["items"], doc["offset"] = [], 1


def source_shift(doc):
    doc["sourceOffset"] += 1


def user_up(doc):
    doc["userOffset"] += 99


def dup_id(doc):
    doc["items"][0]["id"] = "m119"
    doc["before"] = "m119"


class DetailWindowTest(OfflineTestCase):
    def test_a_short_history_is_one_complete_page(self):
        server = DetailServer(history(5))
        window = detail_window(server.client(), ATT, SID)
        self.assertEqual(server.requests, [{"sessionId": SID, "part": "messages"}])
        self.assertEqual(
            [r["id"] for r in window.items], ["m000", "m001", "m002", "m003", "m004"]
        )
        self.assertEqual((window.offset, window.total, window.pages), (0, 5, 1))
        self.assertTrue(window.complete)
        self.assertTrue(window.coverage_complete)
        page = window.as_page()
        self.assertEqual(
            set(page),
            {"items", "offset", "total", "before", "userOffset", "sourceOffset"},
        )
        self.assertEqual((page["offset"], page["total"], page["before"]), (0, 5, None))
        self.assertEqual(window.last_id, "m004")

    def test_pages_follow_the_string_message_id_and_assemble_oldest_first(self):
        rows = history(130)
        server = DetailServer(rows, limit=60)
        client = server.client()
        window = detail_window(client, ATT, SID)
        self.assertEqual(
            server.requests,
            [
                {"sessionId": SID, "part": "messages"},
                {"sessionId": SID, "part": "messages", "before": "m070"},
                {"sessionId": SID, "part": "messages", "before": "m010"},
            ],
        )
        self.assertEqual(window.items, tuple(rows))
        self.assertEqual((window.offset, window.total, window.pages), (0, 130, 3))
        self.assertTrue(window.coverage_complete)
        for call in client.calls:
            self.assertEqual(call["headers"]["X-Amplifier-Client"], CLIENT_ID)
            self.assertEqual(call["route"].max_response, 1024 * 1024)

    def test_native_offsets_are_carried_from_the_oldest_page_without_inventing_any(
        self,
    ):
        rows = history(70)
        window = detail_window(
            DetailServer(rows, limit=60, user_base=40, source_base=500).client(),
            ATT,
            SID,
        )
        page = window.as_page()
        self.assertEqual((page["offset"], page["total"]), (0, 70))
        self.assertEqual(page["userOffset"], 40)
        self.assertEqual(page["sourceOffset"], 500)

    def test_the_four_page_bound_reports_an_incomplete_window_not_a_forged_one(self):
        rows = history(300)
        server = DetailServer(rows, limit=60)
        window = detail_window(server.client(), ATT, SID)
        self.assertEqual(len(server.requests), 4)
        self.assertEqual(window.pages, 4)
        self.assertEqual((window.offset, window.total), (60, 300))
        self.assertFalse(window.complete)
        self.assertFalse(window.coverage_complete)
        self.assertEqual(len(window.items), 240)
        self.assertEqual(window.items[0]["id"], "m060")
        page = window.as_page()
        self.assertEqual(
            (page["offset"], page["total"]), (60, 300)
        )  # never rewritten to 0

    def test_foreign_rows_are_retained_byte_for_byte(self):
        rows = history(10)
        rows[3] = {
            "id": "m003",
            "role": "weird",
            "payload": {"x": [1, 2]},
            "textDetail": {"truncated": True},
        }
        rows[4] = {
            "id": "m004",
            "role": "user",
            "nativeInputAmbiguous": True,
            "text": "x",
        }
        window = detail_window(DetailServer(rows).client(), ATT, SID)
        self.assertEqual(list(window.items), rows)

    def test_a_complete_window_feeds_the_core_reply_evaluation(self):
        rows = [
            user_row("11111111-1111-4111-8111-111111111111", "gen-1"),
            assistant_row("gen-1", "the answer"),
        ]
        window = detail_window(DetailServer(rows).client(), ATT, SID)
        decision = reply.evaluate_reply(
            window.as_page(),
            command_id="11111111-1111-4111-8111-111111111111",
            session_id=SID,
            coverage_complete=window.coverage_complete,
        )
        self.assertEqual((decision.status, decision.text), ("speak", "the answer"))

    def test_an_incomplete_window_can_never_be_spoken(self):
        rows = history(300)
        rows[-2] = user_row("11111111-1111-4111-8111-111111111111", "gen-1") | {
            "id": "mu"
        }
        rows[-1] = assistant_row("gen-1", "answer") | {"id": "ma"}
        window = detail_window(DetailServer(rows).client(), ATT, SID)
        decision = reply.evaluate_reply(
            window.as_page(),
            command_id="11111111-1111-4111-8111-111111111111",
            session_id=SID,
            coverage_complete=window.coverage_complete,
        )
        self.assertNotEqual(decision.status, "speak")

    def test_baseline_signature(self):
        rows = history(8)
        found = detail_window(
            DetailServer(rows).client(), ATT, SID, baseline_last_id="m004"
        )
        self.assertTrue(found.baseline_found)
        self.assertEqual(
            [r["id"] for r in found.since_baseline], ["m005", "m006", "m007"]
        )
        missing = detail_window(
            DetailServer(rows).client(), ATT, SID, baseline_last_id="zzz"
        )
        self.assertFalse(missing.baseline_found)
        self.assertEqual(missing.since_baseline, ())
        none = detail_window(DetailServer(rows).client(), ATT, SID)
        self.assertIsNone(none.baseline_found)
        newest = detail_window(
            DetailServer(rows).client(), ATT, SID, baseline_last_id="m007"
        )
        self.assertEqual((newest.baseline_found, newest.since_baseline), (True, ()))
        self.assertEqual(
            raised_any(
                detail_window,
                DetailServer(rows).client(),
                ATT,
                SID,
                baseline_last_id="bad id",
            ).code,
            "companion_request_invalid",
        )

    def test_bad_session_ids_send_nothing(self):
        for bad in ("", "a/b", "..", "a%2Fb", "a b", None, 5):
            with self.subTest(bad=bad):
                client = FakeClient()
                self.assertEqual(
                    raised_any(detail_window, client, ATT, bad).code,
                    "companion_request_invalid",
                )
                self.assertEqual(client.calls, [])

    def _tampered(self, rows, limit, mutate):
        server = DetailServer(rows, limit=limit)

        def second(call: dict[str, Any]) -> Response:
            response = server(call)
            if "before" in call["route"].__dict__.get("_q", {}) or dict(
                call["route"].query
            ).get("before"):
                doc = json.loads(response.body)
                mutate(doc)
                return json_response(doc)
            return response

        return FakeClient(second, second, second, second)

    def test_inconsistent_pages_fail_closed(self):
        cases = {
            "total changed": (total, "companion_snapshot_changed"),
            "gap": (gap, "companion_response_invalid"),
            "overlap": (overlap, "companion_response_invalid"),
            "before none": (before_none, "companion_response_invalid"),
            "before wrong": (before_wrong, "companion_response_invalid"),
            "empty items": (empty_items, "companion_response_invalid"),
            "source offset": (source_shift, "companion_response_invalid"),
            "user offset": (user_up, "companion_response_invalid"),
            "duplicate id": (dup_id, "companion_response_invalid"),
        }
        for name, (mutate, code) in cases.items():
            with self.subTest(name):
                client = self._tampered(history(130), 60, mutate)
                exc = raised_any(detail_window, client, ATT, SID)
                self.assertEqual(exc.code, code)

    def test_malformed_single_pages_fail_closed(self):
        good = json.loads(
            DetailServer(history(5))({"route": type("R", (), {"query": ()})()}).body
        )
        cases = {
            "items not list": {"items": {}},
            "row not dict": {"items": ["x"]},
            "row id missing": {"items": [{"role": "user"}]},
            "row id empty": {"items": [{"id": "", "role": "user"}]},
            "row id int": {"items": [{"id": 5}]},
            "duplicate in page": {"items": [{"id": "a"}, {"id": "a"}], "total": 2},
            "offset bool": {"offset": True},
            "offset negative": {"offset": -1},
            "offset float": {"offset": 0.0},
            "total bool": {"total": True},
            "total str": {"total": "5"},
            "total short": {"total": 4},
            "before type": {"before": 5},
            "before on first complete page": {"before": "m000"},
            "user offset missing": {"userOffset": ...},
            "source offset bool": {"sourceOffset": True},
            "source offset negative": {"sourceOffset": -1},
        }
        for name, change in cases.items():
            with self.subTest(name):
                doc = {**good, **change}
                doc = {k: v for k, v in doc.items() if v is not ...}
                exc = raised_any(
                    detail_window, FakeClient(json_response(doc)), ATT, SID
                )
                self.assertEqual(exc.code, "companion_response_invalid")
        self.assertEqual(
            raised_any(detail_window, FakeClient(Response(200, b"[]")), ATT, SID).code,
            "companion_response_invalid",
        )

    def test_http_failures_are_fixed_codes_without_retry(self):
        cases = {
            400: "companion_bad_request",
            401: "companion_unauthorized",
            403: "companion_unauthorized",
            404: "companion_not_found",
            409: "companion_snapshot_changed",
            503: "companion_observation_busy",
            500: "companion_http_status",
        }
        for status, code in cases.items():
            with self.subTest(status=status):
                client = FakeClient(
                    Response(status, b'{"error":"This history changed."}')
                )
                exc = raised_any(detail_window, client, ATT, SID)
                self.assertEqual((exc.code, exc.status), (code, status))
                self.assertEqual(len(client.calls), 1)

    def test_later_page_failure_loses_the_whole_window(self):
        server = DetailServer(history(130))
        client = FakeClient(server, Response(409, b"x"))
        self.assertEqual(
            raised_any(detail_window, client, ATT, SID).code,
            "companion_snapshot_changed",
        )

    def test_transport_failure_is_fixed(self):
        exc = raised_any(
            detail_window,
            FakeClient(TransportError("timeout", maybe_sent=True)),
            ATT,
            SID,
        )
        self.assertEqual(exc.code, "companion_request_failed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
