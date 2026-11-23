"""Host-only app update/rollback tests. Real esptool image parser; RAM flash, no serial."""
from contextlib import redirect_stdout
import hashlib
import io
from pathlib import Path
import struct
import subprocess
import os
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from esptool.bin_image import ESP32S3FirmwareImage, ImageSegment

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_app as u


def app_image(path, version, *, name=b"unified_pet", revisions=(0, 0, 0)):
    desc = bytearray(256)
    struct.pack_into("<I", desc, 0, 0xabcd5432)
    desc[16:16 + len(version)] = version
    desc[48:48 + len(name)] = name
    image = ESP32S3FirmwareImage()
    image.chip_id = 9
    image.min_rev, image.min_rev_full, image.max_rev_full = revisions
    image.entrypoint = 0x40370000
    image.segments = [ImageSegment(0x3c000020, bytes(desc))]
    image.save(str(path))
    return path.read_bytes()


def layout():
    rows = [(1, 2, 0x9000, 0x6000, b"nvs"), (1, 1, 0xf000, 0x1000, b"phy_init"),
            (0, 0, u.APP, u.CAP, b"factory")]
    entries = b"".join(struct.pack("<HBBII16sI", 0x50aa, *row, 0) for row in rows)
    return (entries + b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(entries).digest()).ljust(u.TABLE_BYTES, b"\xff")


class Flash:
    IS_STUB = True
    FLASH_SECTOR_SIZE = u.SECTOR
    FLASH_WRITE_SIZE = 1024

    def __init__(self, old, fault=None):
        self.fault = fault
        self.prefix = bytearray(b"\xa5" * u.APP)
        self.prefix[u.TABLE_OFFSET:u.TABLE_OFFSET + u.TABLE_BYTES] = layout()
        self.partition = bytearray(old.ljust(u.CAP, b"\xff"))
        self.tail = bytearray(b"\x36" * (u.FLASH_BYTES - u.APP - u.CAP))
        self.resets = self.begins = 0
        self.blocks = []

    def get_chip_revision(self):
        return 101

    def get_major_chip_version(self):
        return 1

    def read_flash(self, offset, size):
        if offset == u.TABLE_OFFSET and size == u.TABLE_BYTES:
            return bytes(u.TABLE_BYTES) if self.fault == "table" else layout()
        assert offset == u.APP and size <= u.CAP
        data = bytes(self.partition[:size])
        if self.fault == "readback" and self.begins:
            return data[:-1] + bytes([data[-1] ^ 1])
        return data

    def flash_md5sum(self, offset, size):
        if (offset, size) == (0, u.APP):
            return hashlib.md5(self.prefix).hexdigest()
        if (offset, size) == (u.APP, u.CAP):
            return hashlib.md5(self.partition).hexdigest()
        assert offset >= u.APP and offset + size == u.FLASH_BYTES
        digest = hashlib.md5(memoryview(self.partition)[offset - u.APP:])
        digest.update(self.tail)
        return digest.hexdigest()

    def flash_defl_begin(self, size, compressed_size, offset):
        from esptool import loader
        assert offset == u.APP and 0 < size <= u.CAP and loader.WRITE_BLOCK_ATTEMPTS == 1
        self.begins += 1
        assert self.begins == 1
        assert (self.attempt / "write-intent").exists()
        assert (self.attempt / "before-partition.bin").read_bytes() == self.partition
        self.count = size
        end = (size + u.SECTOR - 1) // u.SECTOR * u.SECTOR
        self.partition[:end] = b"\xff" * end
        return (compressed_size + self.FLASH_WRITE_SIZE - 1) // self.FLASH_WRITE_SIZE

    def flash_defl_block(self, block, seq, timeout):
        assert seq == len(self.blocks)
        self.blocks.append(block)
        if self.fault == "ack":
            raise OSError("simulated loss after erase")

    def flash_defl_finish(self, reboot, timeout):
        assert reboot is False
        new = zlib.decompress(b"".join(self.blocks))
        assert len(new) == self.count
        self.partition[:len(new)] = new
        if self.fault == "prefix":
            self.prefix[0x9000] ^= 1
        if self.fault == "suffix":
            self.tail[-1] ^= 1

    def hard_reset(self):
        assert (self.attempt / "verified").exists()
        assert (self.attempt / "boot-intent").exists()
        self.resets += 1
        if self.fault == "reset-ack":
            raise OSError("reset outcome unconfirmed")


class UpdateTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.old = app_image(self.root / "old.bin", b"old")
        self.new = app_image(self.root / "new.bin", b"new")
        from esptool import loader
        self.addCleanup(setattr, loader, "WRITE_BLOCK_ATTEMPTS", loader.WRITE_BLOCK_ATTEMPTS)

    def transfer(self, flash, new, expect, name):
        flash.attempt = u.reserve(self.root / name)
        with redirect_stdout(io.StringIO()):
            u.update(flash, new, u.sha(new), u.sha(expect), flash.attempt, "fixture-board", reset=flash.hard_reset)

    def test_update_and_separately_authorized_reverse_update(self):
        flash = Flash(self.old)
        before = bytes(flash.prefix)
        self.transfer(flash, self.new, self.old, "update")
        self.assertEqual(flash.read_flash(u.APP, len(self.new)), self.new)
        self.assertEqual((flash.begins, flash.resets, flash.prefix), (1, 1, before))
        old_path = self.root / "update/previous-app.bin"
        restored, version = u.read_image(old_path, u.sha(self.old))
        self.assertEqual((version, old_path.stat().st_mode & 0o777), ("old", 0o600))
        flash.begins = 0
        flash.blocks.clear()
        self.transfer(flash, restored, self.new, "rollback")
        self.assertEqual((flash.read_flash(u.APP, len(self.old)), flash.prefix), (self.old, before))

    def test_no_write_on_bad_layout_prior_or_unavailable_backup(self):
        for name in ("table", "prior", "disk"):
            with self.subTest(name=name):
                flash = Flash(self.old, fault=name)
                flash.attempt = u.reserve(self.root / name)
                if name == "disk":
                    (flash.attempt / "before-partition.bin").touch()
                with self.assertRaises((u.Refused, FileExistsError)), redirect_stdout(io.StringIO()):
                    u.update(flash, self.new, u.sha(self.new), "0" * 64 if name == "prior" else u.sha(self.old),
                             flash.attempt, "fixture-board", reset=flash.hard_reset)
                self.assertEqual((flash.begins, flash.resets), (0, 0))
                self.assertFalse((flash.attempt / "write-intent").exists())

    def test_no_replay_boot_or_false_success_after_uncertainty(self):
        for name in ("ack", "readback", "prefix", "suffix"):
            with self.subTest(name=name):
                flash = Flash(self.old, fault=name)
                with self.assertRaises((OSError, u.Refused)):
                    self.transfer(flash, self.new, self.old, name)
                self.assertEqual((flash.begins, flash.resets), (1, 0))
                self.assertFalse((flash.attempt / "verified").exists())
                self.assertFalse((flash.attempt / "boot-requested").exists())
                self.assertTrue((flash.attempt / "write-intent").exists())
                with self.assertRaises(FileExistsError):
                    u.reserve(flash.attempt)

    def test_lost_reset_response_is_not_a_second_reset_or_false_boot_receipt(self):
        flash = Flash(self.old, fault="reset-ack")
        with self.assertRaises(OSError):
            self.transfer(flash, self.new, self.old, "reset")
        self.assertEqual((flash.begins, flash.resets), (1, 1))
        self.assertTrue((flash.attempt / "boot-intent").exists())
        self.assertFalse((flash.attempt / "boot-requested").exists())

    def test_image_revision_checked_before_write(self):
        flash = Flash(self.old)
        for i, fields in enumerate([(2, 0, 0), (2, 0, 65535), (0, 102, 200), (0, 1, 100)]):
            new = app_image(self.root / "rev.bin", b"rev", revisions=fields)
            with self.assertRaisesRegex(u.Refused, "revision"):
                self.transfer(flash, new, self.old, f"refused-{i}")
            self.assertFalse((flash.attempt / "write-intent").exists())
        for fields in [(1, 0, 0), (1, 0, 65535), (0, 101, 101), (0, 1, 65535)]:
            u.check_revision(flash, app_image(self.root / "rev.bin", b"rev", revisions=fields))
        self.assertEqual((flash.begins, flash.resets), (0, 0))

    def test_serial_handle_cannot_be_reopened_even_after_first_open_fails(self):
        class Underlying:
            attempts = 0
            def open(self):
                self.attempts += 1
                raise OSError("disappeared")
        class Wire(u.OneOpen, Underlying):
            pass
        wire = Wire()
        with self.assertRaises(OSError):
            wire.open()
        with self.assertRaises(u.Refused):
            wire.open()
        self.assertEqual(wire.attempts, 1)

    def test_ambient_config_refused_before_serial_or_backup(self):
        cfg = self.root / "custom.cfg"
        cfg.write_text("[esptool]\nreset_delay=0.5\n")
        backup = self.root / "never-created"
        result = subprocess.run([sys.executable, "-I", "-B", u.__file__, "--image", str(self.root / "new.bin"),
                                 "--sha256", u.sha(self.new), "--apply", "--port", "/no/such/device",
                                 "--board-id", "00:00:00:00:00:00", "--expect-installed", u.sha(self.old),
                                 "--save-to", str(backup)], env={**os.environ, "ESPTOOL_CFGFILE": str(cfg)},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Custom esptool configuration", result.stderr)
        self.assertFalse(backup.exists())

    def test_default_mode_never_opens_port_or_reserves_backup(self):
        args = ["update_app", "--image", str(self.root / "new.bin"), "--sha256", u.sha(self.new),
                "--port", "/not/a/device", "--save-to", str(self.root / "unused")]
        with patch.object(sys, "argv", args), patch.object(u, "connect") as connect, \
                patch.object(u, "reserve") as reserve, redirect_stdout(io.StringIO()):
            self.assertEqual(u.main(), 0)
        connect.assert_not_called()
        reserve.assert_not_called()

    @unittest.skipUnless(sys.platform == "linux", "Linux port selection")
    def test_wrong_rom_after_usb_match_never_uploads_stub(self):
        # The only file descriptors opened are a new pseudoterminal pair.
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        port = Path(os.ttyname(slave))
        board = "00:11:22:33:44:55"
        usb = SimpleNamespace(device=str(port), vid=0x303a, pid=0x1001, serial_number=board)
        def detect(wire, *, connect_attempts):
            self.assertEqual((wire.timeout, wire.exclusive, connect_attempts), (10, True, 1))
            return SimpleNamespace(CHIP_NAME="ESP32-S3", read_mac=lambda: bytes.fromhex("00AA22334455"))
        with patch("serial.tools.list_ports.comports", return_value=[usb]), \
                patch("esptool.cmds.detect_chip", side_effect=detect), \
                self.assertRaisesRegex(u.Refused, "ROM identity mismatch"), u.connect(port, board):
            self.fail("No flash stub may be yielded for a different board.")

    def test_reject_wrong_project_corruption_and_trailing_bytes(self):
        wrong = app_image(self.root / "wrong.bin", b"old", name=b"esp32_pet")
        for bad in (wrong, self.new + b"\xff", self.new[:-1] + bytes([self.new[-1] ^ 1])):
            with self.assertRaises(u.Refused):
                u.image(bad, u.sha(bad))
        with self.assertRaises(u.Refused):
            u.read_image(self.root / "new.bin", u.sha(self.old))


if __name__ == "__main__":
    unittest.main()