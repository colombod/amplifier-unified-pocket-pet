#!/usr/bin/env python3
"""Explicitly obtain the public, already-pinned offline component cache.

No registry solver is used. The registry checksum bytes and every extracted file
must reconstruct the existing complete-tree pins before the cache is published.
This is preparation only; the firmware build never invokes this script.
"""
import argparse
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import urllib.parse
import urllib.request
import zipfile

APP = Path(__file__).resolve().parents[1]
LOCK_SHA = "ee9197346a7f843fc9b0e913280b9d15d8a3dec7497eb85239124433179ab2b1"
PINS_SHA = "3616d451349f84de0ac624ae80ce56943720901a3b95295f76b30676e292ea44"
HOST = "components-file.espressif.com"
MAX_META = 4 * 1024 * 1024
MAX_ZIP = 128 * 1024 * 1024
MAX_TOTAL = 512 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024
HEX = re.compile(r"^[0-9a-f]{64}$")
NAME = re.compile(r"^[a-z0-9_]+/[a-z0-9_]+$")
VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)*(?:~[0-9]+)?$")


class PreparationError(Exception):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreparationError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def load_json(data):
    return json.loads(data, object_pairs_hook=no_duplicates)


def inputs(app=APP):
    lock = (app / "dependencies.lock").read_bytes()
    pins_bytes = (app / "scripts/offline-cache-pins.json").read_bytes()
    if digest(lock) != LOCK_SHA or digest(pins_bytes) != PINS_SHA:
        raise PreparationError("Lock or complete-tree pins have changed")
    pins = load_json(pins_bytes)
    text = lock.decode("utf-8")
    blocks = re.findall(r"^  ([\w-]+/[\w-]+):\n(.*?)(?=^  \S|^direct_dependencies:)",
                        text, re.M | re.S)
    packages = {}
    for full_name, block in blocks:
        version = re.findall(r"^    version: (\S+)$", block, re.M)
        component_hash = re.findall(r"^    component_hash: (\S+)$", block, re.M)
        if (not NAME.fullmatch(full_name) or len(version) != 1 or
                not VERSION.fullmatch(version[0]) or len(component_hash) != 1 or
                not HEX.fullmatch(component_hash[0])):
            raise PreparationError(f"Unexpected locked component: {full_name}")
        short_name = full_name.split("/")[1]
        if short_name in packages:
            raise PreparationError(f"Duplicate component name: {short_name}")
        packages[short_name] = (full_name, version[0], component_hash[0])
    if set(packages) != set(pins):
        raise PreparationError("Lock and component pin sets differ")
    return packages, pins


def safe_path(path):
    if (not isinstance(path, str) or not path or path.startswith("/") or
            "\\" in path or "\x00" in path or ":" in path or
            any(part in ("", ".", "..") for part in path.split("/"))):
        raise PreparationError(f"Unsafe archive path: {path!r}")
    return path


def rows_hash(rows):
    return digest("".join(path + "\0" + value + "\n"
                          for path, value in sorted(rows.items())).encode("utf-8"))


def validate_manifest(raw, component_hash, pin):
    manifest = load_json(raw)
    if manifest.get("version") != "1.0" or manifest.get("algorithm") != "sha256":
        raise PreparationError("Unexpected checksum format")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != pin["files"] - 2:
        raise PreparationError("Checksum file count differs from pin")
    expected = {}
    total = 0
    for record in files:
        if not isinstance(record, dict):
            raise PreparationError("Invalid checksum record")
        path = safe_path(record.get("path"))
        size, sha = record.get("size"), record.get("hash")
        if (path in expected or path in ("CHECKSUMS.json", ".component_hash") or
                type(size) is not int or size < 0 or size > MAX_FILE or
                not isinstance(sha, str) or not HEX.fullmatch(sha)):
            raise PreparationError(f"Invalid checksum record: {path}")
        expected[path] = (size, sha)
        total += size
    if total > MAX_TOTAL:
        raise PreparationError("Component is too large")
    rows = {path: value[1] for path, value in expected.items()}
    rows["CHECKSUMS.json"] = digest(raw)
    rows[".component_hash"] = digest(component_hash.encode("ascii"))
    if rows_hash(rows) != pin["tree_sha256"]:
        raise PreparationError("Registry checksum metadata differs from complete-tree pin")
    return expected


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != HOST or
            parsed.query or parsed.fragment):
        raise PreparationError("Unexpected download origin")


def fetch(url, limit):
    check_url(url)
    # Avoid loading credentials or proxy configuration from the user's home.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), SameHostRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "unified-pet-build-preparation/1"})
    with opener.open(request, timeout=60) as response:
        check_url(response.geturl())
        data = response.read(limit + 1)
    if len(data) > limit:
        raise PreparationError("Download exceeds size limit")
    return data


def registry_urls(raw, full_name, version, component_hash):
    metadata = load_json(raw)
    namespace, component = full_name.split("/")
    if metadata.get("name") != component or metadata.get("namespace") != namespace:
        raise PreparationError("Registry component identity differs from lock")
    versions = metadata.get("versions")
    if not isinstance(versions, list):
        raise PreparationError("Invalid registry component metadata")
    matching = [item for item in versions
                if isinstance(item, dict) and item.get("version") == version]
    if len(matching) != 1 or matching[0].get("component_hash") != component_hash:
        raise PreparationError("Registry version or component hash differs from lock")
    entry = matching[0]
    if entry.get("yanked_at"):
        raise PreparationError("Locked registry component is yanked")
    prefix = f"https://{HOST}/components/{namespace}/{component}/{version}/"
    checksum, archive = entry.get("checksums"), entry.get("url")
    if not isinstance(checksum, str) or not isinstance(archive, str):
        raise PreparationError("Missing registry artifact URL")
    checksum = urllib.parse.urljoin(f"https://{HOST}/", checksum)
    archive = urllib.parse.urljoin(f"https://{HOST}/", archive)
    if (checksum != prefix + "CHECKSUMS.json" or not archive.startswith(prefix) or
            "/" in archive[len(prefix):] or not archive.endswith(".zip")):
        raise PreparationError("Unexpected registry artifact path")
    check_url(checksum)
    check_url(archive)
    return checksum, archive


def extract(raw_zip, expected, folder):
    seen = set()
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
        for entry in archive.infolist():
            is_dir = entry.is_dir()
            path = safe_path(entry.filename[:-1] if is_dir else entry.filename)
            if path in seen:
                raise PreparationError(f"Duplicate archive entry: {path}")
            seen.add(path)
            kind = stat.S_IFMT(entry.external_attr >> 16)
            if kind not in (0, stat.S_IFDIR if is_dir else stat.S_IFREG):
                raise PreparationError(f"Non-regular archive entry: {path}")
            if is_dir:
                if entry.file_size != 0 or not any(p.startswith(path + "/") for p in expected):
                    raise PreparationError(f"Unexpected archive directory: {path}")
                continue
            if path not in expected or entry.file_size != expected[path][0]:
                raise PreparationError(f"Unexpected archive file or size: {path}")
            target = folder.joinpath(*path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            sha = hashlib.sha256()
            written = 0
            with archive.open(entry) as source, target.open("xb") as out:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > expected[path][0]:
                        raise PreparationError(f"File exceeds expected size: {path}")
                    sha.update(chunk)
                    out.write(chunk)
            if written != expected[path][0] or sha.hexdigest() != expected[path][1]:
                raise PreparationError(f"File checksum mismatch: {path}")
    if set(expected) != {x for x in seen if x in expected}:
        raise PreparationError("Archive is missing a pinned file")


def verify_component(folder, component_hash, pin):
    rows = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise PreparationError("Symlink in prepared cache")
        if path.is_file():
            rows[path.relative_to(folder).as_posix()] = digest(path.read_bytes())
        elif not path.is_dir():
            raise PreparationError("Non-regular entry in prepared cache")
    if len(rows) != pin["files"] or rows_hash(rows) != pin["tree_sha256"]:
        raise PreparationError(f"Prepared component differs from pin: {folder.name}")
    if (folder / ".component_hash").read_bytes() != component_hash.encode("ascii"):
        raise PreparationError(f"Component hash differs from lock: {folder.name}")


def secure_parent(destination):
    if not destination.is_absolute() or destination.name in ("", ".", ".."):
        raise PreparationError("Use an absolute destination in an existing private directory")
    parts = destination.parts
    if any(part in (".", "..") for part in parts):
        raise PreparationError("Destination must not contain dot segments")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in parts[1:-1]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise PreparationError("Destination parent must be owned by you and not group/other writable")
        return fd
    except BaseException:
        os.close(fd)
        raise


def publish(parent_fd, stage_name, dest_name):
    # RENAME_NOREPLACE makes the final handoff atomic even if the destination
    # appeared after the initial check. Fail closed on unsupported systems.
    if sys.platform != "linux":
        raise PreparationError("Atomic no-replace publication currently requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise PreparationError("renameat2 is unavailable; cache was not published")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent_fd, os.fsencode(stage_name), parent_fd, os.fsencode(dest_name), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), dest_name)


def same_directory(fd, path_fd):
    left, right = os.fstat(fd), os.fstat(path_fd)
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def prepare(destination, downloader=fetch):
    packages, pins = inputs()
    if sys.platform != "linux":
        raise PreparationError("Preparation currently requires Linux")
    parent_fd = secure_parent(destination)
    stage_name = ".pet-components-" + secrets.token_hex(12)
    display_stage = destination.parent / stage_name
    created = False
    stage_fd = None
    try:
        try:
            os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise PreparationError("Destination already exists; nothing was overwritten")
        os.mkdir(stage_name, 0o700, dir_fd=parent_fd)
        created = True
        stage_fd = os.open(stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                           dir_fd=parent_fd)
        # Every write and verification resolves under the pinned staging inode.
        stage = Path("/proc/self/fd") / str(stage_fd)
        for name, (full_name, version, component_hash) in sorted(packages.items()):
            namespace, component = full_name.split("/")
            meta = downloader(f"https://{HOST}/components/{namespace}/{component}.json", MAX_META)
            checksum_url, archive_url = registry_urls(meta, full_name, version, component_hash)
            raw_manifest = downloader(checksum_url, MAX_META)
            expected = validate_manifest(raw_manifest, component_hash, pins[name])
            raw_zip = downloader(archive_url, MAX_ZIP)
            folder = stage / name
            folder.mkdir()
            extract(raw_zip, expected, folder)
            (folder / "CHECKSUMS.json").write_bytes(raw_manifest)
            (folder / ".component_hash").write_bytes(component_hash.encode("ascii"))
            verify_component(folder, component_hash, pins[name])
            print(f"Verified {full_name} {version}: {pins[name]['files']} files; "
                  f"archive sha256 {digest(raw_zip)}", flush=True)
        if {p.name for p in stage.iterdir()} != set(pins):
            raise PreparationError("Prepared component set differs from lock")
        current_parent = secure_parent(destination)
        try:
            if not same_directory(parent_fd, current_parent):
                raise PreparationError("Destination parent changed during preparation")
            current_stage = os.open(stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=parent_fd)
            try:
                if not same_directory(stage_fd, current_stage):
                    raise PreparationError("Staging directory changed during preparation")
            finally:
                os.close(current_stage)
        finally:
            os.close(current_parent)
        publish(parent_fd, stage_name, destination.name)
        created = False
        print(f"Prepared verified offline cache: {destination}", flush=True)
    finally:
        if stage_fd is not None:
            os.close(stage_fd)
        os.close(parent_fd)
        if created:
            print(f"Preparation did not complete; partial stage retained under the original parent "
                  f"(intended path {display_stage}; parent may have moved)",
                  file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", required=True, metavar="ABSENT_ABSOLUTE_PATH",
                        help="opt in to public downloads and publish a new cache")
    args = parser.parse_args()
    try:
        prepare(Path(args.prepare))
    except (PreparationError, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(f"Preparation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())