"""Update only the existing unified_pet app on an identified Waveshare AMOLED 1.8 V2.

Default: validate candidate bytes offline. --apply requires exclusive use, a
matched board and installed app, and a new private backup directory. Never
install a bootloader/table, erase all flash, restore NVS, retry a write, or
automatically roll back. This is NOT a first-install tool.
"""
import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import zlib

TABLE_OFFSET, TABLE_BYTES = 0x8000, 3072
TABLE_SHA = "5a349f86ef3dc2e5421cfc191e2f70030b67eff9eb81fad7b1d0219e57657302"
APP, CAP, SECTOR = 0x10000, 0xe00000, 4096
FLASH_BYTES = 0x1000000


class Refused(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def image(raw, expected, *, from_partition=False):
    from esptool.bin_image import LoadFirmwareImage
    require(256 <= len(raw) <= CAP and raw[0] == 0xe9 and raw[23] == 1 and
            int.from_bytes(raw[12:14], "little") == 9 and raw[32:36] == bytes.fromhex("3254cdab") and
            raw[80:112].rstrip(b"\0") == b"unified_pet", "Not a supported ESP32-S3 unified_pet app.")
    if not from_partition:
        require(sha(raw) == expected, "Candidate SHA-256 mismatch.")
    parsed = LoadFirmwareImage("esp32s3", io.BytesIO(raw))
    length = parsed.data_length + 32
    require(parsed.chip_id == 9 and parsed.checksum == parsed.calculate_checksum() and
            len(parsed.stored_digest) == 32 and parsed.stored_digest == parsed.calc_digest and
            length <= len(raw) and (from_partition or length == len(raw)), "Invalid ESP app image.")
    app = raw[:length]
    require(sha(app) == expected, "Installed app does not match the expected prior SHA-256.")
    version = app[48:80].rstrip(b"\0")
    require(re.fullmatch(rb"[A-Za-z0-9._+-]{1,31}", version), "Invalid firmware version.")
    return app, version.decode("ascii")


def read_image(path, expected):
    path = path.absolute()
    require(path.resolve(strict=True) == path, "Use an image path without symlinks.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        require(stat.S_ISREG(info.st_mode) and 256 <= info.st_size <= CAP, "Invalid app file.")
        return image(f.read(CAP + 1), expected)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    sync_dir(path.parent)


def reserve(path):
    path = path.absolute()
    parent = path.parent
    require(parent.resolve(strict=True) == parent and parent.is_dir(), "Use a direct private backup parent.")
    st = parent.stat()
    require(st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) == 0o700,
            "Backup parent must already exist, owned by you, mode 0700.")
    require(not any((p / ".git").exists() for p in (parent, *parent.parents)),
            "Keep private backups outside Git.")
    path.mkdir(mode=0o700)  # single-use; never overwrite another attempt
    sync_dir(parent)
    return path


def check_tool():
    import esptool
    import serial
    from esptool import config, loader
    require(esptool.__version__ == "4.11.0" and serial.VERSION == "3.5",
            "Use esptool 4.11.0 and pyserial 3.5.")
    # esptool reads configuration on import; checking only the version misses
    # custom reset sequences and timeouts. Refuse, never rewrite user config.
    _, path = config.load_config_file()
    require(path is None and not loader.cfg and not any(
        k in os.environ for k in ("ESPTOOL_CFGFILE", "ESPTOOL_ENV_FPGA")),
        "Custom esptool configuration is not supported here.")


def check_revision(esp, raw):
    from esptool.bin_image import LoadFirmwareImage
    parsed = LoadFirmwareImage("esp32s3", io.BytesIO(raw))
    # Match esptool 4.11's ESP32-S3 rules for full vs legacy revision fields.
    lo, hi = parsed.min_rev_full, parsed.max_rev_full
    if hi == 0 or (hi == 65535 and lo == 0 and parsed.min_rev != 0):
        ok = esp.get_major_chip_version() >= parsed.min_rev
    else:
        ok = lo <= esp.get_chip_revision() <= hi
    require(ok, "Image does not support this board revision; no write.")


class OneOpen:
    """If esptool loses the checked file descriptor, never reopen the pathname."""
    opened_once = False

    def open(self):
        require(not self.opened_once, "Serial disconnected; reopening a possibly different board is refused.")
        self.opened_once = True
        return super().open()


def update(esp, app, expected, prior_sha, directory, board, *, reset):
    """One write in the fixed factory app region; no exception handler retries or resets."""
    from esptool import loader
    require(sha(app) == expected and 0 < len(app) <= CAP and len(app) % 4 == 0,
            "Invalid write plan.")
    require(expected != prior_sha, "Candidate is already the expected installed app.")
    require(esp.IS_STUB and esp.FLASH_SECTOR_SIZE == SECTOR, "Unexpected flash geometry.")
    check_revision(esp, app)
    table = esp.read_flash(TABLE_OFFSET, TABLE_BYTES)
    require(sha(table) == TABLE_SHA, "Board partition table differs; no write.")
    end = APP + ((len(app) + SECTOR - 1) // SECTOR) * SECTOR
    def untouched():
        sums = (esp.flash_md5sum(0, APP), esp.flash_md5sum(end, FLASH_BYTES - end))
        require(all(isinstance(s, str) and re.fullmatch("[a-f0-9]{32}", s) for s in sums),
                "Cannot verify protected flash ranges.")
        return sums
    before = untouched()
    print("Reading current app partition before any write.", flush=True)
    previous = esp.read_flash(APP, CAP)
    require(len(previous) == CAP, "Incomplete app-partition backup.")
    prior, version = image(previous, prior_sha, from_partition=True)
    save(directory / "before-partition.bin", previous)
    save(directory / "previous-app.bin", prior)  # downgrade after a verified update; not torn-image recovery
    require((directory / "before-partition.bin").read_bytes() == previous and
            (directory / "previous-app.bin").read_bytes() == prior, "Backup readback failed.")
    require(esp.flash_md5sum(APP, CAP) == hashlib.md5(previous).hexdigest()
            and untouched() == before, "Flash changed during backup.")
    packed = zlib.compress(app, 9)
    block = esp.FLASH_WRITE_SIZE
    require(block > 0 and zlib.decompress(packed) == app, "Cannot prepare compressed write.")
    save(directory / "backup.json", json.dumps({
        "board": board, "prior_version": version, "prior_app_sha256": prior_sha,
        "prior_partition_sha256": sha(previous), "new_app_sha256": expected, "offset": APP,
        "erase_end_exclusive": end,
    }, indent=2).encode() + b"\n")
    save(directory / "write-intent", b"Inspect outcome before any other attempt.\n")
    loader.WRITE_BLOCK_ATTEMPTS = 1
    count = esp.flash_defl_begin(len(app), len(packed), APP)
    require(count == (len(packed) + block - 1) // block, "Unexpected block count; stop.")
    # Stub ACKs receipt, then writes while receiving the next block. As in
    # esptool, the timeout covers the *previous* block's expanded size.
    dec = zlib.decompressobj()
    timeout = loader.DEFAULT_TIMEOUT
    for seq, offset in enumerate(range(0, len(packed), block)):
        piece = packed[offset:offset + block]
        expanded_size = len(dec.decompress(piece))
        esp.flash_defl_block(piece, seq, timeout=timeout)
        timeout = loader.timeout_per_mb(loader.ERASE_WRITE_TIMEOUT_PER_MB, expanded_size)
    esp.flash_defl_finish(reboot=False, timeout=timeout)
    require(esp.read_flash(APP, len(app)) == app, "App readback mismatch; do not retry.")
    require(untouched() == before, "Protected flash changed; do not boot or retry.")
    save(directory / "verified", b"Exact app readback and unchanged prefix and suffix before boot.\n")
    save(directory / "boot-intent", b"Reset may have occurred; inspect, do not repeat.\n")
    reset()
    save(directory / "boot-requested", b"Reset requested; running UI and voice not verified.\n")


@contextmanager
def connect(port, board):
    import fcntl
    import termios
    import serial
    from serial.tools import list_ports
    from esptool.cmds import detect_chip, detect_flash_size
    from esptool.reset import HardReset
    class Port(OneOpen, serial.Serial):
        pass
    require(port.is_absolute() and port.resolve(strict=True).is_relative_to("/dev"),
            "Choose a local serial device explicitly.")
    st = port.stat()
    require(stat.S_ISCHR(st.st_mode), "Not a serial device.")
    matches = [p for p in list_ports.comports() if os.path.samefile(p.device, port)]
    require(len(matches) == 1 and (matches[0].vid, matches[0].pid) == (0x303a, 0x1001)
            and (matches[0].serial_number or "").lower() == board,
            "USB board identity mismatch; port not opened.")
    wire = Port(port=None, baudrate=115200, timeout=10, write_timeout=10, exclusive=True)
    wire.dtr = wire.rts = False
    wire.port = str(port)
    with wire:
        require(os.path.samestat(os.fstat(wire.fileno()), st), "Serial device changed.")
        fcntl.ioctl(wire.fileno(), termios.TIOCEXCL)
        chip = detect_chip(wire, connect_attempts=1)
        require(chip.CHIP_NAME == "ESP32-S3" and bytes(chip.read_mac()).hex(":") == board,
                "ROM identity mismatch; no write.")
        info = chip.get_security_info()
        flags = info["parsed_flags"]
        require(not flags["SECURE_BOOT_EN"] and not flags["SECURE_DOWNLOAD_ENABLE"]
                and info["flash_crypt_cnt"].bit_count() % 2 == 0 and not chip.stub_is_disabled,
                "Unsupported security mode; no write.")
        esp = chip.run_stub()
        require(esp.IS_STUB and detect_flash_size(esp) == "16MB", "Not the supported 16 MiB flash.")
        esp.flash_set_parameters(FLASH_BYTES)
        esp.change_baud(460800)
        # Direct one-shot reset on the same open handle, not the strategy
        # wrapper which can reopen, retry, or suppress a failed ioctl.
        yield esp, HardReset(wire).reset


def main():
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("--image", type=Path, required=True)
    p.add_argument("--sha256", required=True, help="trusted SHA-256 of the exact candidate app")
    p.add_argument("--apply", action="store_true", help="perform one app-only update (resets board)")
    p.add_argument("--port", type=Path)
    p.add_argument("--board-id", help="expected USB serial / station MAC, colon separated")
    p.add_argument("--expect-installed", help="trusted SHA-256 of current executable app image")
    p.add_argument("--save-to", type=Path, help="absent backup directory under an existing private parent")
    args = p.parse_args()
    directory = None
    try:
        check_tool()
        require(re.fullmatch("[0-9a-f]{64}", args.sha256), "Invalid candidate hash.")
        app, version = read_image(args.image, args.sha256)
        end = APP + ((len(app) + SECTOR - 1) // SECTOR) * SECTOR
        require(end <= APP + CAP, "Write would exceed the app partition.")
        print(f"unified_pet {version}: {len(app)} bytes, app sectors [0x{APP:x},0x{end:x}).")
        if not args.apply:
            print("Offline plan only. No serial access, backup, write or reset.")
            return 0
        require(sys.platform == "linux" and os.geteuid() != 0, "Apply as the normal Linux board owner.")
        require(args.port and args.save_to and re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", args.board_id or "")
                and re.fullmatch("[0-9a-f]{64}", args.expect_installed or "")
                and args.expect_installed != args.sha256,
                "Apply needs a port, board id, different prior hash and new private backup directory.")
        directory = reserve(args.save_to)
        print("Opening the named board resets it. Keep power connected.", flush=True)
        with connect(args.port, args.board_id) as (esp, reset):
            update(esp, app, args.sha256, args.expect_installed, directory, args.board_id, reset=reset)
        print("Installed app verified; settings-area checksum unchanged before boot.")
        return 0
    except ImportError:
        print("A required host-tools dependency is missing.", file=sys.stderr)
    except Refused as exc:
        print(str(exc), file=sys.stderr)
    except Exception as exc:
        print(f"Stopped ({type(exc).__name__}). No automatic retry or restore.", file=sys.stderr)
    if directory is not None and (directory / "write-intent").exists():
        print("Write intent recorded: do not repeat the update or reset. Inspect the private receipts.",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())