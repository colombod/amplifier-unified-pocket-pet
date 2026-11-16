"""Journal: private, bounded, atomic, single-process JSON records."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import uuid
from pathlib import Path
from unittest import mock

from pet_bridge import journal as journal_module
from pet_bridge.journal import MAX_RECORD_BYTES, Journal, JournalError

from .fakes import OfflineTestCase, raised

CMD = "cmd-00000000-0000-4000-8000-000000000001.json"
CMD2 = "cmd-00000000-0000-4000-8000-000000000002.json"


class JournalCase(OfflineTestCase):
    def open(self, **kwargs) -> Journal:
        journal = Journal(self.root, **kwargs)
        self.addCleanup(journal.close)
        return journal

    def setUp(self) -> None:
        super().setUp()
        self.root = self.journal_root()

    def assertCode(self, code: str, call, *args, **kwargs) -> JournalError:
        with self.assertRaises(JournalError) as ctx:
            call(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception


class RoundTripTest(JournalCase):
    def test_put_get_names_delete(self):
        j = self.open()
        self.assertIsNone(j.get(CMD))
        j.put(CMD, {"state": "PREPARED", "n": 1})
        j.put("binding.json", {"epoch": 0})
        self.assertEqual(j.get(CMD), {"state": "PREPARED", "n": 1})
        self.assertEqual(j.names(), ["binding.json", CMD])
        self.assertEqual(j.names("cmd-"), [CMD])
        j.delete(CMD)
        j.delete(CMD)  # deleting a missing record is a no-op
        self.assertIsNone(j.get(CMD))

    def test_on_disk_layout_is_private_schema_1(self):
        j = self.open()
        j.put(CMD, {"a": "é"})
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        path = self.root / CMD
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, {"schema": 1, "record": {"a": "é"}})
        self.assertEqual(stat.S_IMODE((self.root / ".lock").stat().st_mode), 0o600)

    def test_survives_reopen(self):
        j = Journal(self.root)
        j.put(CMD, {"k": 1})
        j.close()
        j2 = self.open()
        self.assertEqual(j2.get(CMD), {"k": 1})

    def test_context_manager_closes_and_closed_rejects(self):
        with Journal(self.root) as j:
            j.put(CMD, {"k": 1})
        self.assertCode("journal_closed", j.get, CMD)
        self.assertCode("journal_closed", j.put, CMD, {})
        j.close()  # idempotent
        self.open()  # the lock was released


class RootSafetyTest(JournalCase):
    def test_relative_root_rejected(self):
        self.assertCode("journal_root_invalid", Journal, Path("relative/dir"))

    def test_parent_must_already_exist(self):
        self.assertCode(
            "journal_root_invalid", Journal, self.root.parent / "missing" / "j"
        )
        self.assertFalse((self.root.parent / "missing").exists())

    def test_git_directory_or_file_ancestor_rejected(self):
        (self.root.parent / ".git").mkdir()
        self.assertCode("journal_root_invalid", Journal, self.root)
        (self.root.parent / ".git").rmdir()
        (self.root.parent / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
        self.assertCode("journal_root_invalid", Journal, self.root)
        self.assertFalse(self.root.exists())

    def test_symlink_root_rejected(self):
        target = self.root.parent / "real"
        target.mkdir(mode=0o700)
        self.root.symlink_to(target)
        self.assertCode("journal_root_invalid", Journal, self.root)

    def test_symlink_ancestor_rejected(self):
        real = self.root.parent / "real"
        real.mkdir(mode=0o700)
        link = self.root.parent / "link"
        link.symlink_to(real)
        self.assertCode("journal_root_invalid", Journal, link / "j")

    def test_permissive_existing_root_rejected(self):
        self.root.mkdir(mode=0o700)
        os.chmod(self.root, 0o755)
        self.assertCode("journal_root_invalid", Journal, self.root)

    def test_foreign_owner_rejected(self):
        self.root.mkdir(mode=0o700)
        with mock.patch("pet_bridge.journal.os.geteuid", return_value=os.geteuid() + 1):
            self.assertCode("journal_root_invalid", Journal, self.root)

    def test_root_that_is_a_file_rejected(self):
        self.root.write_text("x", encoding="utf-8")
        self.assertCode("journal_root_invalid", Journal, self.root)

    def test_invalid_max_records_rejected(self):
        for bad in (0, -1, True, "3", 10**6):
            self.assertCode(
                "journal_config_invalid", Journal, self.root, max_records=bad
            )


class LockTest(JournalCase):
    def test_second_open_is_refused_until_close(self):
        first = Journal(self.root)
        self.assertCode("journal_locked", Journal, self.root)
        first.close()
        self.open()

    def test_symlinked_lock_path_rejected(self):
        self.root.mkdir(mode=0o700)
        decoy = self.root.parent / "decoy"
        decoy.write_text("", encoding="utf-8")
        (self.root / ".lock").symlink_to(decoy)
        self.assertCode("journal_root_invalid", Journal, self.root)

    def test_permissive_lock_file_rejected(self):
        self.root.mkdir(mode=0o700)
        lock = self.root / ".lock"
        lock.write_text("", encoding="utf-8")
        os.chmod(lock, 0o666)
        self.assertCode("journal_root_invalid", Journal, self.root)


class NameAndRecordValidationTest(JournalCase):
    def test_only_allowed_names(self):
        j = self.open()
        for bad in (
            "",
            "x.json",
            "../binding.json",
            "binding.json.bak",
            "cmd-x.json",
            "cmd-00000000-0000-1000-8000-000000000001.json",  # not uuid4
            "cmd-00000000-0000-4000-8000-00000000000A.json",  # not canonical lower
            "cmd-00000000-0000-4000-8000-000000000001.json/",
            ".lock",
            5,
            None,
        ):
            self.assertCode("journal_name_invalid", j.put, bad, {})
            self.assertCode("journal_name_invalid", j.get, bad)
            self.assertCode("journal_name_invalid", j.delete, bad)

    def test_rejects_bad_records_without_changing_old_value(self):
        j = self.open()
        j.put(CMD, {"v": 1})
        for bad in (
            [1],
            "s",
            None,
            {"x": float("nan")},
            {"x": float("inf")},
            {"x": {1, 2}},
            {"x": "\ud800"},
            {1: "int key"},
        ):
            self.assertCode("journal_record_invalid", j.put, CMD, bad)
        self.assertEqual(j.get(CMD), {"v": 1})

    def test_oversize_record_rejected_and_boundary_accepted(self):
        j = self.open()
        self.assertCode(
            "journal_record_too_large", j.put, CMD, {"x": "a" * MAX_RECORD_BYTES}
        )
        overhead = len(
            json.dumps({"schema": 1, "record": {"x": ""}}, separators=(",", ":"))
        )
        j.put(CMD, {"x": "a" * (MAX_RECORD_BYTES - overhead)})
        self.assertEqual((self.root / CMD).stat().st_size, MAX_RECORD_BYTES)

    def test_error_text_never_carries_payload(self):
        j = self.open()
        marker = "SECRET-MARKER-xyz"
        err = self.assertCode(
            "journal_record_too_large", j.put, CMD, {"x": marker * 10000}
        )
        self.assertNotIn(marker, str(err))
        self.assertNotIn(marker, repr(err))
        err = self.assertCode(
            "journal_record_invalid", j.put, CMD, {"x": marker, "y": float("nan")}
        )
        self.assertNotIn(marker, str(err) + repr(err))


class StoredRecordIntegrityTest(JournalCase):
    def setUp(self) -> None:
        super().setUp()
        self.j = self.open()

    def write_raw(self, data: bytes, mode: int = 0o600) -> Path:
        path = self.root / CMD
        path.write_bytes(data)
        os.chmod(path, mode)
        return path

    def test_permissive_record_rejected(self):
        self.write_raw(b'{"schema":1,"record":{}}', 0o644)
        self.assertCode("journal_unsafe_record", self.j.get, CMD)

    def test_symlink_record_rejected(self):
        target = self.root.parent / "elsewhere.json"
        target.write_bytes(b'{"schema":1,"record":{}}')
        os.chmod(target, 0o600)
        (self.root / CMD).symlink_to(target)
        self.assertCode("journal_unsafe_record", self.j.get, CMD)

    def test_hardlinked_record_rejected(self):
        path = self.write_raw(b'{"schema":1,"record":{}}')
        os.link(path, self.root.parent / "alias.json")
        self.assertCode("journal_unsafe_record", self.j.get, CMD)

    def test_non_regular_record_rejected(self):
        os.mkfifo(self.root / CMD, 0o600)
        self.assertCode("journal_unsafe_record", self.j.get, CMD)

    def test_corrupt_variants_rejected(self):
        for data in (
            b"",
            b"not json",
            b"\xff\xfe",
            b"[]",
            b'{"schema":1}',
            b'{"schema":2,"record":{}}',
            b'{"schema":true,"record":{}}',
            b'{"schema":1,"record":[]}',
            b'{"schema":1,"record":{},"extra":1}',
            b'{"schema":1,"record":{"a":NaN}}',
            b'{"schema":1,"schema":1,"record":{}}',
        ):
            self.write_raw(data)
            self.assertCode("journal_record_corrupt", self.j.get, CMD)

    def test_oversize_file_on_disk_rejected(self):
        self.write_raw(b" " * (MAX_RECORD_BYTES + 1))
        self.assertCode("journal_record_too_large", self.j.get, CMD)


class AtomicityTest(JournalCase):
    def setUp(self) -> None:
        super().setUp()
        self.j = self.open()
        self.j.put(CMD, {"v": "old"})

    def leftovers(self) -> list[str]:
        return [n for n in os.listdir(self.root) if n.startswith(".tmp-")]

    def test_failure_before_rename_keeps_old_value(self):
        with mock.patch("pet_bridge.journal.os.fsync", side_effect=OSError("disk")):
            self.assertCode("journal_io_error", self.j.put, CMD, {"v": "new"})
        self.assertEqual(self.j.get(CMD), {"v": "old"})
        self.assertEqual(self.leftovers(), [])

    def test_rename_failure_keeps_old_value_and_removes_own_temp(self):
        with mock.patch("pet_bridge.journal.os.replace", side_effect=OSError("rename")):
            self.assertCode("journal_io_error", self.j.put, CMD, {"v": "new"})
        self.assertEqual(self.j.get(CMD), {"v": "old"})
        self.assertEqual(self.leftovers(), [])

    def test_write_failure_keeps_old_value(self):
        with mock.patch("pet_bridge.journal.os.write", side_effect=OSError("full")):
            self.assertCode("journal_io_error", self.j.put, CMD, {"v": "new"})
        self.assertEqual(self.j.get(CMD), {"v": "old"})
        self.assertEqual(self.leftovers(), [])

    def test_failure_after_rename_reports_unknown_state_not_rollback(self):
        real_fsync = os.fsync
        calls = []

        def flaky(fd):
            calls.append(fd)
            if len(calls) == 2:  # the directory fsync that follows the rename
                raise OSError("dirsync")
            return real_fsync(fd)

        with mock.patch("pet_bridge.journal.os.fsync", side_effect=flaky):
            self.assertCode("journal_persist_unknown", self.j.put, CMD, {"v": "new"})
        # The rename happened: the new value is visible, and nothing was rolled back.
        self.assertEqual(self.j.get(CMD), {"v": "new"})
        self.assertEqual(self.leftovers(), [])

    def test_delete_directory_sync_failure_reports_unknown_state(self):
        with mock.patch("pet_bridge.journal.os.fsync", side_effect=OSError("dirsync")):
            self.assertCode("journal_persist_unknown", self.j.delete, CMD)
        self.assertIsNone(self.j.get(CMD))  # the unlink happened; durability is unknown

    def test_short_writes_are_completed(self):
        real_write = os.write

        def one_byte(fd, data):
            return real_write(fd, bytes(data[:1]))

        with mock.patch("pet_bridge.journal.os.write", side_effect=one_byte):
            self.j.put(CMD, {"v": "x" * 200})
        self.assertEqual(self.j.get(CMD), {"v": "x" * 200})

    def test_zero_progress_write_is_an_error(self):
        with mock.patch("pet_bridge.journal.os.write", return_value=0):
            self.assertCode("journal_io_error", self.j.put, CMD, {"v": "new"})
        self.assertEqual(self.j.get(CMD), {"v": "old"})

    def test_foreign_temp_files_are_never_removed(self):
        foreign = self.root / ".tmp-99999-deadbeef"
        foreign.write_text("someone else", encoding="utf-8")
        self.j.put(CMD2, {"v": 1})
        self.j.delete(CMD2)
        self.j.close()
        self.assertTrue(foreign.exists())


class BoundTest(JournalCase):
    def test_full_journal_refuses_new_names_but_allows_overwrite_and_delete(self):
        j = self.open(max_records=2)
        j.put(CMD, {"a": 1})
        j.put(CMD2, {"a": 2})
        third = "cmd-00000000-0000-4000-8000-000000000003.json"
        self.assertCode("journal_full", j.put, third, {})
        j.put(CMD, {"a": 3})
        j.delete(CMD2)
        j.put(third, {})
        self.assertEqual(j.count(), 2)

    def test_unrecognised_files_do_not_count_or_list(self):
        j = self.open(max_records=1)
        (self.root / "stray.txt").write_text("x", encoding="utf-8")
        (self.root / ".tmp-1-aa").write_text("x", encoding="utf-8")
        self.assertEqual(j.names(), [])
        j.put(CMD, {})
        self.assertEqual(j.count(), 1)


class ListingFailureTest(JournalCase):
    """R9: a failing directory listing is a fixed JournalError, never a raw OSError."""

    def test_listdir_oserror_becomes_a_fixed_journal_error_without_path_or_payload(
        self,
    ):
        j = self.open()
        j.put(CMD, {"v": 1})
        marker = "/private/path-marker-9137"
        calls = {
            "names": lambda: j.names(),
            "names_prefix": lambda: j.names("cmd-"),
            "count": lambda: j.count(),
            "put_new": lambda: j.put(CMD2, {"v": 2}),
        }
        for label, call in calls.items():
            with mock.patch(
                "pet_bridge.journal.os.listdir", side_effect=OSError(13, marker)
            ):
                err = raised(call)
            self.assertIsInstance(err, JournalError, label)
            assert err is not None
            self.assertEqual(getattr(err, "code", None), "journal_io_error", label)
            text = str(err) + repr(err) + repr(getattr(err, "args", ()))
            self.assertNotIn(marker, text, label)
            self.assertNotIn("Permission", text, label)
            self.assertIsNone(err.__cause__, label)
            self.assertTrue(err.__suppress_context__, label)
        self.assertEqual(j.get(CMD), {"v": 1})  # nothing was disturbed
        self.assertIsNone(j.get(CMD2))


def tmp_name(pid: int = 4242, ident: uuid.UUID | None = None) -> str:
    """An exact generated temp name: ``.tmp-<pid>-<uuid4 hex>``."""
    return f".tmp-{pid}-{(ident or uuid.uuid4()).hex}"


class StaleTempCleanupTest(JournalCase):
    """Startup removes ONLY exact generated temps, after the lock, never following links."""

    CANARY = b"owned-format-canary-contents"

    def setUp(self) -> None:
        super().setUp()
        self.root.mkdir(mode=0o700)
        os.chmod(self.root, 0o700)
        self.outside = self.root.parent / "outside"
        self.outside.mkdir()

    def plant(self, name: str | None = None, mode: int = 0o600, data=None) -> Path:
        path = self.root / (name or tmp_name())
        path.write_bytes(self.CANARY if data is None else data)
        os.chmod(path, mode)
        return path

    def listing(self) -> list[str]:
        return sorted(os.listdir(self.root))

    def stale_cap(self) -> int:
        cap = getattr(journal_module, "MAX_STALE_TEMPS", None)
        self.assertIsInstance(cap, int)
        return cap if isinstance(cap, int) else 64

    def assertFault(self, code: str) -> JournalError:
        with self.assertRaises(JournalError) as ctx:
            Journal(self.root)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(str(ctx.exception), code)
        return ctx.exception

    # -- what is removed -------------------------------------------------------
    def test_owned_format_canaries_are_removed_and_contents_never_read(self):
        planted = [self.plant(tmp_name(pid)) for pid in (1, 4242, 2_147_483_647)]
        opened: list[str] = []
        real_open = os.open

        def spy_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        with (
            mock.patch("pet_bridge.journal.os.open", side_effect=spy_open),
            mock.patch(
                "pet_bridge.journal.os.read",
                side_effect=AssertionError("temp contents must never be read"),
            ),
        ):
            Journal(self.root).close()
        for path in planted:
            self.assertFalse(os.path.lexists(path), path.name)
        self.assertEqual(self.listing(), [".lock"])
        self.assertEqual([n for n in opened if n.startswith(".tmp-")], [])

    def test_boundary_pid_and_variant_names_are_exact_matches(self):
        names = [
            ".tmp-9-" + "0" * 12 + "4000" + variant + "000" + "0" * 12
            for variant in ("8", "9", "a", "b")
        ]
        for name in names:
            self.assertEqual(len(name), len(".tmp-9-") + 32, name)
            self.plant(name)
        Journal(self.root).close()
        self.assertEqual(self.listing(), [".lock"])

    def test_cleanup_happens_while_holding_the_lock_and_removes_before_use(self):
        self.plant()
        seen: dict[str, object] = {}
        real_unlink = os.unlink

        def spy_unlink(path, *args, **kwargs):
            probe = os.open(".lock", os.O_RDWR, dir_fd=kwargs.get("dir_fd"))
            try:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    seen["lock_held"] = False
                except OSError:
                    seen["lock_held"] = True
            finally:
                os.close(probe)
            return real_unlink(path, *args, **kwargs)

        with mock.patch("pet_bridge.journal.os.unlink", side_effect=spy_unlink):
            Journal(self.root).close()
        self.assertEqual(seen, {"lock_held": True})

    def test_a_locked_second_instance_cannot_clean_up(self):
        first = self.open()
        canary = self.plant()
        with self.assertRaises(JournalError) as ctx:
            Journal(self.root)
        self.assertEqual(ctx.exception.code, "journal_locked")
        self.assertTrue(canary.exists())
        self.assertEqual(canary.read_bytes(), self.CANARY)
        first.close()

    def test_a_live_instances_own_in_flight_temp_is_not_a_startup_concern(self):
        first = self.open()
        first.put(CMD, {"v": 1})
        # A second open is refused before any cleanup could run.
        self.assertCode("journal_locked", Journal, self.root)
        self.assertEqual(first.get(CMD), {"v": 1})

    # -- what is never touched -------------------------------------------------
    def test_near_miss_unknown_record_and_lock_names_are_untouched(self):
        ident = uuid.uuid4()
        good = ident.hex
        near = [
            f".tmp-0-{good}",
            f".tmp-007-{good}",
            f".tmp--5-{good}",
            f".tmp-+5-{good}",
            f".tmp-5-{good.upper()}",
            f".tmp-5-{good[:-1]}",
            f".tmp-5-{good}0",
            f".tmp-5-{good}.json",
            f".tmp-5-{ident}",
            f".tmp-5-{good[:12]}1{good[13:]}",
            f".tmp-5-{good[:16]}c{good[17:]}",
            f".tmp-5-{good[:12]}4{good[13:16]}7{good[17:]}",
            f".tmp-12345678901-{good}",
            f".tmp-5-{good}\n",
            f"tmp-5-{good}",
            f".tmp-5_{good}",
            f".TMP-5-{good}",
            ".tmp-99999-deadbeef",
            "unknown.txt",
            ".ruff_cache",
        ]
        for name in near:
            self.plant(name, mode=0o644)
        Journal(self.root).close()
        self.assertEqual(self.listing(), sorted([*near, ".lock"]))
        for name in near:
            self.assertEqual((self.root / name).read_bytes(), self.CANARY)

    def test_record_files_and_lock_are_unchanged(self):
        j = Journal(self.root)
        j.put("binding.json", {"epoch": 3})
        j.put(CMD, {"state": "ACCEPTED"})
        j.close()
        before = {n: (self.root / n).read_bytes() for n in self.listing()}
        self.plant()
        Journal(self.root).close()
        after = {n: (self.root / n).read_bytes() for n in self.listing()}
        self.assertEqual(after, before)

    def test_a_symlink_match_is_a_fault_and_the_outside_target_is_intact(self):
        target = self.outside / "secret.txt"
        target.write_bytes(b"outside")
        os.chmod(target, 0o600)
        link = self.root / tmp_name()
        link.symlink_to(target)
        self.assertFault("journal_temp_unsafe")
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b"outside")
        self.assertEqual(os.listdir(self.outside), ["secret.txt"])

    def test_a_dangling_or_directory_symlink_match_is_also_a_fault(self):
        (self.root / tmp_name()).symlink_to(self.outside / "missing")
        self.assertFault("journal_temp_unsafe")
        self.assertEqual(os.listdir(self.outside), [])

    def make_hard_linked(self) -> Path:
        path = self.plant()
        os.link(path, self.outside / "alias")
        return path

    def make_directory(self) -> Path:
        path = self.root / tmp_name()
        path.mkdir(mode=0o700)
        return path

    def make_fifo(self) -> Path:
        path = self.root / tmp_name()
        os.mkfifo(path, 0o600)
        return path

    def clear_unsafe(self, bad: Path) -> None:
        if bad.is_dir() and not bad.is_symlink():
            bad.rmdir()
        else:
            bad.unlink()
        for leftover in os.listdir(self.outside):
            (self.outside / leftover).unlink()

    def test_unsafe_matches_fail_closed_and_are_preserved(self):
        makers = {
            "mode_0644": lambda: self.plant(mode=0o644),
            "mode_0640": lambda: self.plant(mode=0o640),
            "mode_0700": lambda: self.plant(mode=0o700),
            "hard_linked": self.make_hard_linked,
            "directory": self.make_directory,
            "fifo": self.make_fifo,
            "oversize": lambda: self.plant(data=b"x" * (MAX_RECORD_BYTES + 1)),
        }
        for case, make in makers.items():
            with self.subTest(case=case):
                bad = make()
                healthy = self.plant()  # must survive: prevalidation precedes deletion
                self.assertFault("journal_temp_unsafe")
                self.assertTrue(os.path.lexists(bad))
                self.assertTrue(healthy.exists())
                # The failed open released the lock: removing the culprit unblocks.
                self.clear_unsafe(bad)
                Journal(self.root).close()
                self.assertEqual(self.listing(), [".lock"])

    def test_the_size_boundary_is_accepted(self):
        path = self.plant(data=b"x" * MAX_RECORD_BYTES)
        Journal(self.root).close()
        self.assertFalse(path.exists())

    def test_a_foreign_owner_match_is_a_fault_and_preserved(self):
        path = self.plant()
        healthy = self.plant()
        real_stat = os.stat

        def foreign(name, *args, **kwargs):
            result = real_stat(name, *args, **kwargs)
            if isinstance(name, str) and name.startswith(".tmp-"):
                fields = list(result)
                fields[4] = result.st_uid + 1
                return os.stat_result(fields)
            return result

        with mock.patch("pet_bridge.journal.os.stat", side_effect=foreign):
            self.assertFault("journal_temp_unsafe")
        self.assertTrue(path.exists())
        self.assertTrue(healthy.exists())

    # -- bounds and failure codes ------------------------------------------------
    def test_the_count_cap_is_checked_before_any_deletion(self):
        cap = self.stale_cap()
        planted = [self.plant(tmp_name(i + 1)) for i in range(cap + 1)]
        self.assertFault("journal_temp_overflow")
        for path in planted:
            self.assertTrue(path.exists())

    def test_exactly_the_cap_is_cleaned(self):
        planted = [self.plant(tmp_name(i + 1)) for i in range(self.stale_cap())]
        Journal(self.root).close()
        for path in planted:
            self.assertFalse(path.exists())

    def test_unlink_failure_is_a_fixed_fault_without_names(self):
        path = self.plant()
        with mock.patch(
            "pet_bridge.journal.os.unlink",
            side_effect=OSError(errno.EPERM, "denied " + path.name),
        ):
            err = self.assertFault("journal_temp_cleanup_failed")
        self.assertNotIn(path.name, str(err) + repr(err) + repr(err.args))
        self.assertTrue(path.exists())
        # The lock was released, so a later start can retry.
        Journal(self.root).close()
        self.assertFalse(path.exists())

    def test_directory_sync_failure_is_a_fixed_fault_not_a_silent_success(self):
        path = self.plant()
        with mock.patch(
            "pet_bridge.journal.os.fsync",
            side_effect=OSError(errno.EIO, "io " + path.name),
        ):
            err = self.assertFault("journal_temp_cleanup_failed")
        self.assertNotIn(path.name, str(err) + repr(err) + repr(err.args))
        Journal(self.root).close()

    def test_a_replaced_entry_is_revalidated_before_unlink(self):
        path = self.plant()
        real_stat = os.stat
        calls = {"n": 0}

        def replaced(name, *args, **kwargs):
            result = real_stat(name, *args, **kwargs)
            if isinstance(name, str) and name.startswith(".tmp-"):
                calls["n"] += 1
                if calls["n"] >= 2:
                    fields = list(result)
                    fields[1] = result.st_ino + 1
                    return os.stat_result(fields)
            return result

        with (
            mock.patch("pet_bridge.journal.os.stat", side_effect=replaced),
            mock.patch("pet_bridge.journal.os.unlink") as unlink,
        ):
            self.assertFault("journal_temp_unsafe")
        self.assertFalse(unlink.called)
        self.assertTrue(path.exists())

    def test_new_error_codes_are_registered_and_fixed(self):
        for code in (
            "journal_temp_unsafe",
            "journal_temp_overflow",
            "journal_temp_cleanup_failed",
        ):
            self.assertIn(code, journal_module.ERROR_CODES)
            self.assertEqual(JournalError(code).code, code)
