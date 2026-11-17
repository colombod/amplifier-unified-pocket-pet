"""Offline tests for the private credential store, pairing and attach.

Every network exchange is a scripted ``FakeClient``; the socket guard is active.
Temporary stores live outside any Git checkout and never use the real HOME.
These tests are source-derived offline evidence, not a live Unified qualification.
"""

from __future__ import annotations

import os
import stat
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from pet_bridge import auth
from pet_bridge.auth import (
    Attachment,
    AuthError,
    Credential,
    CredentialStore,
    default_root,
)

from .authfakes import (
    CLIENT_ID,
    CRED_REF,
    DEVICE_ID,
    HOST_IDENTITY,
    INSTANCE,
    INSTANCE_2,
    ORIGIN_URL,
    TOKEN,
    VERIFIER,
    AuthCase,
    raised_any,
    FakeClient,
    attach_ok,
    json_response,
)

CANARY = "CANARY-SECRET-5d2b"


def cred(**changes: Any) -> Credential:
    values: dict[str, Any] = {
        "ref": CRED_REF,
        "origin": ORIGIN_URL,
        "device_id": DEVICE_ID,
        "token": TOKEN,
    }
    values.update(changes)
    return Credential(**values)


def attachment(**changes: Any) -> Attachment:
    values: dict[str, Any] = {
        "credential_ref": CRED_REF,
        "origin": ORIGIN_URL,
        "client_id": CLIENT_ID,
        "host_identity": HOST_IDENTITY,
        "host_instance_id": INSTANCE,
    }
    values.update(changes)
    return Attachment(**values)


class DefaultRootTest(AuthCase):
    def test_xdg_absolute_wins_then_home_config(self):
        self.assertEqual(
            default_root({"XDG_CONFIG_HOME": "/x/cfg", "HOME": "/h"}),
            Path("/x/cfg/amplifier-unified-pet-bridge"),
        )
        for xdg in ("rel/cfg", "", None):
            env = {"HOME": "/home/u"}
            if xdg is not None:
                env["XDG_CONFIG_HOME"] = xdg
            self.assertEqual(
                default_root(env), Path("/home/u/.config/amplifier-unified-pet-bridge")
            )

    def test_no_usable_base_fails_closed(self):
        for env in ({}, {"HOME": "rel"}, {"HOME": ""}, {"XDG_CONFIG_HOME": "rel"}):
            with self.subTest(env=env):
                exc = raised_any(default_root, env)
                self.assertIsInstance(exc, AuthError)
                self.assertEqual(exc.code, "auth_root_invalid")

    def test_does_not_read_the_process_environment(self):
        with mock.patch.dict(os.environ, {"HOME": "/should/not/be/used"}):
            self.assertIsInstance(raised_any(default_root, {}), AuthError)


class StoreOpenTest(AuthCase):
    def test_creates_a_private_directory_and_lock(self):
        store = self.open_store()
        mode = stat.S_IMODE(os.lstat(self.root).st_mode)
        self.assertEqual(mode, 0o700)
        lock = self.root / ".lock"
        self.assertEqual(stat.S_IMODE(os.lstat(lock).st_mode), 0o600)
        self.assertEqual(store.names(), [])

    def test_parent_must_exist_and_is_not_created(self):
        self.root = self.base / "missing" / "auth"
        exc = raised_any(CredentialStore, self.root)
        self.assertEqual(exc.code, "auth_root_invalid")
        self.assertFalse((self.base / "missing").exists())

    def test_rejects_unsafe_roots(self):
        outside = self.base / "real"
        outside.mkdir(mode=0o700)
        link = self.base / "link"
        link.symlink_to(outside)
        (self.base / "gitrepo").mkdir()
        (self.base / "gitrepo" / ".git").mkdir()
        loose = self.base / "loose"
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        regular = self.base / "regular"
        regular.write_text("x")
        cases = {
            "symlink root": link,
            "symlink ancestor": link / "auth",
            "git ancestor": self.base / "gitrepo" / "auth",
            "group-readable dir": loose,
            "regular file": regular,
            "relative": Path("relative/auth"),
            "dotdot": self.base / "a" / ".." / "auth",
        }
        for name, root in cases.items():
            with self.subTest(name):
                exc = raised_any(CredentialStore, root)
                self.assertIsInstance(exc, AuthError)
                self.assertEqual(exc.code, "auth_root_invalid")

    def test_second_open_of_the_same_root_is_refused_until_closed(self):
        first = self.open_store()
        exc = raised_any(CredentialStore, self.root)
        self.assertEqual(exc.code, "auth_locked")
        first.close()
        second = CredentialStore(self.root)
        second.close()

    def test_bad_lock_file_is_refused(self):
        self.open_store().close()
        os.chmod(self.root / ".lock", 0o644)
        self.assertEqual(
            raised_any(CredentialStore, self.root).code, "auth_root_invalid"
        )

    def test_context_manager_releases_the_lock(self):
        with CredentialStore(self.root) as store:
            store.save_credential(cred())
        with CredentialStore(self.root) as again:
            self.assertEqual(again.credential_refs(), [CRED_REF])

    def test_closed_store_refuses_use(self):
        store = self.open_store()
        store.close()
        self.assertEqual(raised_any(store.names).code, "auth_closed")

    def test_max_records_is_validated(self):
        for bad in (0, 65, True, "3", None):
            with self.subTest(bad=bad):
                self.assertEqual(
                    raised_any(CredentialStore, self.root, max_records=bad).code,
                    "auth_config_invalid",
                )


class StoreRecordSafetyTest(AuthCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()
        self.store.save_credential(cred())
        self.path = self.root / f"credential-{CRED_REF}.json"

    def test_written_record_is_a_private_regular_file(self):
        st = os.lstat(self.path)
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual(
            (stat.S_IMODE(st.st_mode), st.st_nlink, st.st_uid), (0o600, 1, os.geteuid())
        )
        self.assertEqual(sorted(os.listdir(self.root)), [".lock", self.path.name])

    def test_unsafe_existing_records_are_refused(self):
        os.chmod(self.path, 0o644)
        self.assertEqual(
            raised_any(self.store.credential, CRED_REF).code, "auth_unsafe_record"
        )
        os.chmod(self.path, 0o600)
        os.link(self.path, self.base / "hardlink")
        self.assertEqual(
            raised_any(self.store.credential, CRED_REF).code, "auth_unsafe_record"
        )
        os.unlink(self.base / "hardlink")
        self.assertEqual(self.store.credential(CRED_REF), cred())

    def test_symlink_and_fifo_records_are_refused_without_hanging(self):
        data = self.path.read_bytes()
        os.unlink(self.path)
        target = self.base / "elsewhere.json"
        target.write_bytes(data)
        os.chmod(target, 0o600)
        self.path.symlink_to(target)
        self.assertEqual(
            raised_any(self.store.credential, CRED_REF).code, "auth_unsafe_record"
        )
        os.unlink(self.path)
        os.mkfifo(self.path, 0o600)
        self.assertEqual(
            raised_any(self.store.credential, CRED_REF).code, "auth_unsafe_record"
        )

    def test_oversized_and_corrupt_records_fail_with_fixed_codes(self):
        cases = {
            "auth_record_too_large": b"x" * (64 * 1024 + 1),
            "auth_record_corrupt": [
                b"",
                b"not json",
                b"[]",
                b'{"schema":1,"kind":"credential"}',
                b'{"schema":1,"schema":1,"kind":"credential"}',
                b'{"schema":NaN}',
                b"\xff\xfe",
                self.path.read_bytes().replace(b'"schema":1', b'"schema":2'),
                self.path.read_bytes() + b" trailing",
                self.path.read_bytes().replace(
                    b'"kind":"credential"', b'"kind":"attempt"'
                ),
            ],
        }
        for code, payloads in cases.items():
            for payload in payloads if isinstance(payloads, list) else [payloads]:
                with self.subTest(code=code, payload=payload[:24]):
                    self.path.write_bytes(payload)
                    os.chmod(self.path, 0o600)
                    exc = raised_any(self.store.credential, CRED_REF)
                    self.assertEqual(exc.code, code)
                    self.assertNotIn(CANARY, str(exc))

    def test_record_that_disagrees_with_its_name_is_refused(self):
        other = "device-" + "cd" * 16
        data = self.path.read_bytes().replace(CRED_REF.encode(), other.encode())
        self.path.write_bytes(data)
        os.chmod(self.path, 0o600)
        self.assertEqual(
            raised_any(self.store.credential, CRED_REF).code, "auth_record_corrupt"
        )

    def test_only_allow_listed_names_are_accepted(self):
        for name in (
            "evil.json",
            "../x.json",
            "attempt.txt",
            "credential-device-XYZ.json",
            "credential-device-" + "A" * 32 + ".json",
            "attachment-device-short.json",
            "client-id.json.bak",
            "",
            ".lock",
            "sub/attempt.json",
            "attempt.json\n",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    raised_any(self.store._write, name, {"x": 1}).code,
                    "auth_record_invalid",
                )
                self.assertEqual(
                    raised_any(self.store._read, name).code, "auth_record_invalid"
                )

    def test_unexpected_files_are_ignored_by_listing_but_never_read(self):
        (self.root / "stray.txt").write_text(CANARY)
        self.assertEqual(self.store.credential_refs(), [CRED_REF])

    def test_record_count_is_bounded(self):
        self.store.close()
        small = CredentialStore(self.root, max_records=2)
        self.addCleanup(small.close)
        small.save_attachment(attachment())
        exc = raised_any(small.save_client_id, CLIENT_ID, CRED_REF)
        self.assertEqual(exc.code, "auth_full")
        # replacing an existing record never needs room
        small.save_attachment(
            attachment(host_instance_id="12345678-1234-4234-8234-123456789abd")
        )

    def test_stale_generated_temps_are_removed_after_the_lock_and_near_misses_kept(
        self,
    ):
        self.store.close()
        stale = self.root / (".tmp-123-" + "a" * 32)
        near = self.root / (".tmp-abc-" + "a" * 32)
        for p in (stale, near):
            p.write_text("old secret")
            os.chmod(p, 0o600)
        store = CredentialStore(self.root)
        self.addCleanup(store.close)
        self.assertFalse(stale.exists())
        self.assertTrue(near.exists())

    def test_unsafe_stale_temp_fails_the_open_and_is_preserved(self):
        self.store.close()
        stale = self.root / (".tmp-123-" + "b" * 32)
        stale.write_text("x")
        os.chmod(stale, 0o644)
        exc = raised_any(CredentialStore, self.root)
        self.assertEqual(exc.code, "auth_root_invalid")
        self.assertTrue(stale.exists())


class ForeignOwnerTest(AuthCase):
    """Each ownership guard is exercised alone by faking ONLY that object's uid.

    No chown, sudo or second user: ``os.fstat``/``os.stat`` report a foreign owner for
    exactly one kind of object while every other attribute stays valid (mode, link
    count, type), so the uid clause is the only thing that can reject it.
    """

    @staticmethod
    def foreign(st: os.stat_result) -> Any:
        return types.SimpleNamespace(
            st_mode=st.st_mode,
            st_uid=st.st_uid + 1,
            st_nlink=st.st_nlink,
            st_size=st.st_size,
        )

    def fstat_foreign_for(self, predicate: Any) -> Any:
        real = os.fstat

        def fake(fd: int) -> Any:
            st = real(fd)
            return self.foreign(st) if predicate(st) else st

        return mock.patch.object(auth.os, "fstat", fake)

    def test_a_foreign_owned_directory_is_refused_before_the_lock_is_touched(self):
        self.root.parent.mkdir(exist_ok=True)
        self.root.mkdir(mode=0o700)
        with self.fstat_foreign_for(lambda st: stat.S_ISDIR(st.st_mode)):
            exc = raised_any(CredentialStore, self.root)
        self.assertIsInstance(exc, AuthError)
        self.assertEqual(getattr(exc, "code", None), "auth_root_invalid")
        self.assertFalse((self.root / ".lock").exists())  # the directory guard fired

    def test_a_foreign_owned_lock_file_is_refused(self):
        self.open_store().close()
        with self.fstat_foreign_for(lambda st: stat.S_ISREG(st.st_mode)):
            exc = raised_any(CredentialStore, self.root)
        self.assertIsInstance(exc, AuthError)
        self.assertEqual(getattr(exc, "code", None), "auth_root_invalid")
        CredentialStore(self.root).close()  # sane again once ownership is real

    def test_a_foreign_owned_record_is_never_read(self):
        store = self.open_store()
        store.save_credential(cred())
        with self.fstat_foreign_for(lambda st: stat.S_ISREG(st.st_mode)):
            exc = raised_any(store.credential, CRED_REF)
        self.assertIsInstance(exc, AuthError)
        self.assertEqual(getattr(exc, "code", None), "auth_unsafe_record")
        self.assertEqual(store.credential(CRED_REF), cred())

    def test_a_foreign_owned_stale_temp_is_preserved_and_the_open_fails(self):
        self.open_store().close()
        stale = self.root / (".tmp-123-" + "c" * 32)
        stale.write_bytes(b"someone else's bytes")
        os.chmod(stale, 0o600)
        real = os.stat

        def fake(path: Any, *args: Any, **kwargs: Any) -> Any:
            st = real(path, *args, **kwargs)
            if isinstance(path, str) and path == stale.name:
                return self.foreign(st)
            return st

        with mock.patch.object(auth.os, "stat", fake):
            exc = raised_any(CredentialStore, self.root)
        self.assertIsInstance(exc, AuthError)
        self.assertEqual(getattr(exc, "code", None), "auth_root_invalid")
        self.assertEqual(stale.read_bytes(), b"someone else's bytes")  # not deleted
        CredentialStore(self.root).close()  # real ownership: removed normally
        self.assertFalse(stale.exists())


class DeepSymlinkAncestorTest(AuthCase):
    def test_a_symlink_two_levels_above_the_root_is_refused_and_nothing_is_created(
        self,
    ):
        real = self.base / "real"
        (real / "sub").mkdir(parents=True, mode=0o700)
        link = self.base / "link"
        link.symlink_to(real)
        root = link / "sub" / "auth"
        # the immediate parent (link/sub) is a real directory: only the ancestor
        # walk can catch the link two levels up
        self.assertTrue(stat.S_ISDIR(os.lstat(root.parent).st_mode))
        exc = raised_any(CredentialStore, root)
        self.assertIsInstance(exc, AuthError)
        self.assertEqual(getattr(exc, "code", None), "auth_root_invalid")
        self.assertFalse((real / "sub" / "auth").exists())


class AtomicWriteTest(AuthCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.open_store()

    def test_write_order_is_exclusive_nofollow_temp_fsync_replace_dir_fsync(self):
        events: list[tuple[str, Any]] = []
        real_open, real_fsync, real_replace = os.open, os.fsync, os.replace

        def rec_open(path, flags, *a, **k):
            if str(path).startswith(".tmp-"):
                events.append(("open", flags))
            return real_open(path, flags, *a, **k)

        def rec_fsync(fd):
            events.append(("fsync", stat.S_ISDIR(os.fstat(fd).st_mode)))
            return real_fsync(fd)

        def rec_replace(*a, **k):
            events.append(("replace", None))
            return real_replace(*a, **k)

        with (
            mock.patch.object(os, "open", rec_open),
            mock.patch.object(os, "fsync", rec_fsync),
            mock.patch.object(os, "replace", rec_replace),
        ):
            self.store.save_credential(cred())
        kinds = [e[0] for e in events]
        self.assertEqual(kinds, ["open", "fsync", "replace", "fsync"])
        flags = events[0][1]
        for needed in (os.O_EXCL, os.O_CREAT, os.O_NOFOLLOW, os.O_WRONLY):
            self.assertTrue(flags & needed)
        self.assertEqual([e[1] for e in events if e[0] == "fsync"], [False, True])

    def test_failure_before_the_rename_keeps_the_old_value_and_removes_the_temp(self):
        self.store.save_credential(cred())
        before = (self.root / f"credential-{CRED_REF}.json").read_bytes()
        with mock.patch.object(os, "replace", side_effect=OSError(5, CANARY)):
            exc = raised_any(self.store.save_credential, cred(token=TOKEN[:-1] + "Z"))
        self.assertEqual(exc.code, "auth_io_error")
        self.assertNotIn(CANARY, str(exc) + repr(exc))
        self.assertEqual(
            (self.root / f"credential-{CRED_REF}.json").read_bytes(), before
        )
        self.assertEqual(
            sorted(p for p in os.listdir(self.root) if p.startswith(".tmp-")), []
        )

    def test_failure_writing_or_syncing_the_temp_never_publishes(self):
        for target in ("fsync", "write"):
            with self.subTest(target=target):
                with mock.patch.object(os, target, side_effect=OSError(5, CANARY)):
                    exc = raised_any(self.store.save_credential, cred())
                self.assertEqual(exc.code, "auth_io_error")
                self.assertEqual(self.store.credential_refs(), [])
                self.assertEqual(
                    [p for p in os.listdir(self.root) if p.startswith(".tmp-")], []
                )

    def test_failure_after_the_rename_is_reported_as_persist_unknown_not_a_rollback(
        self,
    ):
        real_fsync = os.fsync
        calls = {"n": 0}

        def flaky(fd):
            calls["n"] += 1
            if calls["n"] == 2:  # the directory sync
                raise OSError(5, CANARY)
            return real_fsync(fd)

        with mock.patch.object(os, "fsync", flaky):
            exc = raised_any(self.store.save_credential, cred())
        self.assertEqual(exc.code, "auth_persist_unknown")
        self.assertNotIn(CANARY, str(exc))
        self.assertEqual(
            self.store.credential(CRED_REF), cred()
        )  # visible, not rolled back

    def test_delete_is_durable_and_reports_directory_sync_failure(self):
        self.store.save_credential(cred())
        real_fsync = os.fsync
        with mock.patch.object(os, "fsync", side_effect=OSError(5, "x")):
            exc = raised_any(self.store._delete, f"credential-{CRED_REF}.json")
        self.assertEqual(exc.code, "auth_persist_unknown")
        self.assertIsNone(self.store.credential(CRED_REF))
        del real_fsync
        self.assertFalse(
            raised_any(self.store._delete, f"credential-{CRED_REF}.json")
        )  # idempotent

    def test_a_non_json_serialisable_or_nonfinite_record_is_refused_before_any_write(
        self,
    ):
        for record in (
            {"x": float("nan")},
            {"x": object()},
            {"x": {1: 2}},
            {"x": float("inf")},
        ):
            with self.subTest(record=str(record)[:20]):
                self.assertEqual(
                    raised_any(self.store._write, "attempt.json", record).code,
                    "auth_record_invalid",
                )
        self.assertEqual(os.listdir(self.root), [".lock"])

    def test_record_larger_than_the_limit_is_refused_on_write(self):
        exc = raised_any(self.store._write, "attempt.json", {"x": "a" * (64 * 1024)})
        self.assertEqual(exc.code, "auth_record_too_large")


class CredentialModelTest(AuthCase):
    def test_round_trip_and_repr_never_shows_the_token(self):
        store = self.open_store()
        c = cred()
        store.save_credential(c)
        loaded = store.credential(CRED_REF)
        self.assertEqual(loaded, c)
        for value in (repr(c), str(c), repr(loaded), repr([c]), repr({"c": c})):
            self.assertNotIn(TOKEN, value)
            self.assertNotIn("T" * 20, value)

    def test_token_shape_is_exact_companion_scope_only(self):
        good = TOKEN
        bad = [
            "amt_" + DEVICE_ID + "." + "T" * 64,
            "amd_" + DEVICE_ID + "." + "T" * 64,
            "amc_" + DEVICE_ID + "." + "T" * 63,
            "amc_" + DEVICE_ID + "." + "T" * 65,
            "amc_" + DEVICE_ID.upper() + "." + "T" * 64,
            "amc_" + DEVICE_ID[:-1] + "." + "T" * 64,
            "amc_" + DEVICE_ID + "." + "T" * 63 + "+",
            "amc_" + DEVICE_ID + "." + "T" * 64 + "\n",
            " " + good,
            good + " ",
            "amc_" + "cd" * 16 + "." + "T" * 64,  # device part differs from device_id
            "",
            5,
        ]
        store = self.open_store()
        for token in bad:
            with self.subTest(token=str(token)[:12] + str(len(str(token)))):
                exc = raised_any(store.save_credential, cred(token=token))
                self.assertEqual(exc.code, "auth_record_invalid")
                self.assertNotIn("T" * 20, str(exc) + repr(exc))
        self.assertEqual(store.credential_refs(), [])

    def test_ref_device_and_origin_are_validated(self):
        store = self.open_store()
        for changes in (
            {"ref": "device-" + "AB" * 16},
            {"ref": "dev-" + DEVICE_ID},
            {"ref": "device-" + "cd" * 16},  # not the device_id
            {"device_id": "zz" * 16},
            {"origin": "http://unified.example.ts.net"},
            {"origin": ORIGIN_URL + "/path"},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    raised_any(store.save_credential, cred(**changes)).code,
                    "auth_record_invalid",
                )

    def test_missing_credential_is_none_and_bad_ref_is_invalid(self):
        store = self.open_store()
        self.assertIsNone(store.credential(CRED_REF))
        for ref in ("x", "device-" + "A" * 32, "../device-" + "a" * 32, None, 5):
            with self.subTest(ref=ref):
                self.assertEqual(
                    raised_any(store.credential, ref).code, "auth_record_invalid"
                )


class AttachmentRecordTest(AuthCase):
    def test_attachment_round_trip_has_no_token_on_disk(self):
        store = self.open_store()
        store.save_credential(cred())
        store.save_attachment(attachment())
        raw = (self.root / f"attachment-{CRED_REF}.json").read_bytes()
        self.assertNotIn(TOKEN.encode(), raw)
        self.assertNotIn(b"token", raw)
        record = store.attachment_record(CRED_REF)
        self.assertEqual(record, attachment())
        joined = store.load_attachment(CRED_REF)
        assert joined is not None
        self.assertEqual(joined.token, TOKEN)
        self.assertNotIn(TOKEN, repr(joined) + str(joined))

    def test_load_attachment_needs_both_records_and_matching_origin(self):
        store = self.open_store()
        self.assertIsNone(store.load_attachment(CRED_REF))
        store.save_attachment(attachment())
        self.assertIsNone(store.load_attachment(CRED_REF))  # no credential
        store.save_credential(cred())
        self.assertIsNotNone(store.load_attachment(CRED_REF))
        store.save_attachment(attachment(origin="https://other.example"))
        self.assertEqual(
            raised_any(store.load_attachment, CRED_REF).code, "auth_record_corrupt"
        )

    def test_attachment_fields_are_strict(self):
        store = self.open_store()
        for changes in (
            {"client_id": "bad id"},
            {"client_id": ""},
            {"client_id": "a" * 101},
            {"host_identity": "5" * 63},
            {"host_identity": "A" * 64},
            {"host_instance_id": "12345678-1234-1234-8234-123456789abc"},
            {"host_instance_id": "12345678-1234-4234-8234-123456789ABC"},
            {"credential_ref": "device-xyz"},
            {"origin": "http://x.example"},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    raised_any(store.save_attachment, attachment(**changes)).code,
                    "auth_record_invalid",
                )

    def test_client_binding_round_trip(self):
        store = self.open_store()
        self.assertIsNone(store.client_binding())
        store.save_client_id(CLIENT_ID, CRED_REF)
        binding = store.client_binding()
        assert binding is not None
        self.assertEqual(
            (binding.client_id, binding.credential_ref), (CLIENT_ID, CRED_REF)
        )
        self.assertEqual(
            raised_any(store.save_client_id, "bad id", CRED_REF).code,
            "auth_record_invalid",
        )


class RefreshInstanceTest(AuthCase):
    def prepared(self):
        store = self.open_store()
        store.save_credential(cred())
        store.save_attachment(attachment())
        store.save_client_id(CLIENT_ID, CRED_REF)
        return store

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.root.iterdir()}

    def test_same_host_refresh_changes_only_instance(self):
        store = self.prepared()
        before = self.snapshot()
        client = FakeClient(json_response(attach_ok(hostInstanceId=INSTANCE_2)))
        result = auth.refresh_instance(
            store, client, CRED_REF, expected_instance_id=INSTANCE
        )
        self.assertEqual(result.host_instance_id, INSTANCE_2)
        self.assertEqual(result.token, TOKEN)
        self.assertEqual(result.host_identity, HOST_IDENTITY)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.bodies()[0]["clientId"], CLIENT_ID)
        after = self.snapshot()
        changed = [name for name in before if before[name] != after[name]]
        self.assertEqual(changed, [f"attachment-{CRED_REF}.json"])
        self.assertNotIn(TOKEN.encode(), after[changed[0]])

    def test_ordinary_attach_still_rejects_new_instance(self):
        store = self.prepared()
        before = self.snapshot()
        client = FakeClient(json_response(attach_ok(hostInstanceId=INSTANCE_2)))
        self.assertEqual(raised_any(auth.attach, store, client, CRED_REF).code,
                         "auth_instance_changed")
        self.assertEqual(self.snapshot(), before)

    def test_different_host_or_client_or_malformed_response_preserves_records(self):
        store = self.prepared()
        before = self.snapshot()
        for doc, code in (
            (attach_ok(hostIdentity="6" * 64), "auth_host_mismatch"),
            (attach_ok(clientId="other-client"), "auth_response_invalid"),
            (attach_ok(hostInstanceId="bad"), "auth_response_invalid"),
            (attach_ok(hostIdentity=...), "auth_response_invalid"),
        ):
            client = FakeClient(json_response(doc))
            self.assertEqual(raised_any(
                auth.refresh_instance, store, client, CRED_REF,
                expected_instance_id=INSTANCE).code, code)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(self.snapshot(), before)

    def test_stale_expected_instance_refuses_before_network(self):
        store = self.prepared()
        client = FakeClient()
        before = self.snapshot()
        for expected in (INSTANCE_2, "bad", None):
            self.assertEqual(raised_any(
                auth.refresh_instance, store, client, CRED_REF,
                expected_instance_id=expected).code, "auth_instance_changed")
        self.assertEqual(client.calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_failed_request_is_not_retried_or_persisted(self):
        store = self.prepared()
        before = self.snapshot()
        for response, code in (
            (OSError(CANARY), "auth_request_failed"),
            (json_response({}, 401), "auth_unauthorized"),
            (json_response({}, 503), "auth_http_status"),
        ):
            client = FakeClient(response)
            error = raised_any(auth.refresh_instance, store, client, CRED_REF,
                               expected_instance_id=INSTANCE)
            self.assertEqual(error.code, code)
            self.assertNotIn(CANARY, str(error))
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(self.snapshot(), before)

    def test_unchanged_instance_does_not_rewrite_attachment(self):
        store = self.prepared()
        client = FakeClient(json_response(attach_ok()))
        with mock.patch.object(store, "save_attachment") as save:
            result = auth.refresh_instance(store, client, CRED_REF,
                                           expected_instance_id=INSTANCE)
        save.assert_not_called()
        self.assertEqual(result.host_instance_id, INSTANCE)

    def test_client_binding_conflict_refuses_before_network(self):
        store = self.prepared()
        store.save_client_id("different-client", CRED_REF)
        before = self.snapshot()
        client = FakeClient()
        self.assertEqual(raised_any(auth.refresh_instance, store, client, CRED_REF,
                         expected_instance_id=INSTANCE).code, "auth_client_bound")
        self.assertEqual(client.calls, [])
        self.assertEqual(self.snapshot(), before)


class AttemptRecordTest(AuthCase):
    def test_attempt_states_and_field_rules(self):
        store = self.open_store()
        base: dict[str, Any] = {
            "origin": ORIGIN_URL,
            "name": "pet",
            "verifier": VERIFIER,
        }
        prepared = auth.Attempt(state="PREPARED", **base)
        store.save_attempt(prepared)
        self.assertEqual(store.attempt(), prepared)
        self.assertNotIn(VERIFIER, repr(prepared) + str(prepared))
        started = auth.Attempt(
            state="STARTED",
            pairing_id="I" * 43,
            code="ABCD-2345",
            expires_at=1000.0,
            interval=5,
            next_poll_at=900.0,
            **base,
        )
        store.save_attempt(started)
        self.assertEqual(store.attempt(), started)
        bad = [
            auth.Attempt(state="WEIRD", **base),
            auth.Attempt(state="STARTED", **base),  # missing started fields
            auth.Attempt(state="PREPARED", pairing_id="I" * 43, **base),
            auth.Attempt(
                state="PREPARED", origin=ORIGIN_URL, name="pet", verifier="short"
            ),
            auth.Attempt(
                state="PREPARED", origin=ORIGIN_URL, name="", verifier=VERIFIER
            ),
            auth.Attempt(
                state="PREPARED", origin="http://x.example", name="p", verifier=VERIFIER
            ),
        ]
        for attempt in bad:
            with self.subTest(state=attempt.state):
                self.assertEqual(
                    raised_any(store.save_attempt, attempt).code, "auth_record_invalid"
                )

    def test_delete_attempt_is_idempotent(self):
        store = self.open_store()
        store.save_attempt(
            auth.Attempt(
                state="PREPARED", origin=ORIGIN_URL, name="p", verifier=VERIFIER
            )
        )
        store.delete_attempt()
        store.delete_attempt()
        self.assertIsNone(store.attempt())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
