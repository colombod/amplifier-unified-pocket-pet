"""Synthetic, offline security and atomicity checks for public cache preparation."""
import contextlib
import builtins
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_components.py"
spec = importlib.util.spec_from_file_location("prepare_components", SCRIPT)
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)


def zip_bytes(entries):
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(output, "w") as archive:
            for name, data in entries:
                archive.writestr(name, data)
    return output.getvalue()


def change_zip_header(raw, local_offset, central_offset, value):
    data = bytearray(raw)
    local = data.index(b"PK\x03\x04")
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<H", data, local + local_offset, value)
    struct.pack_into("<H", data, central + central_offset, value)
    return bytes(data)


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.parent = Path(self.temp.name)
        self.dest = self.parent / "cache"
        self.body = b"public component content\n"
        self.hash = "d" * 64
        self.meta = json.dumps({
            "version": "1.0", "algorithm": "sha256", "files": [{
                "path": "nested/file.txt", "size": len(self.body),
                "hash": prep.digest(self.body),
            }],
        }, separators=(",", ":")).encode()
        self.pin = {
            "files": 3,
            "tree_sha256": prep.rows_hash({
                "nested/file.txt": prep.digest(self.body),
                "CHECKSUMS.json": prep.digest(self.meta),
                ".component_hash": prep.digest(self.hash.encode()),
            }),
        }
        self.packages = {"fixture": ("public/fixture", "1.0.0", self.hash)}
        self.pins = {"fixture": self.pin}
        self.metadata = json.dumps({
            "name": "fixture", "namespace": "public", "versions": [{
                "version": "1.0.0", "component_hash": self.hash,
                "url": "components/public/fixture/1.0.0/published.zip",
                "checksums": "components/public/fixture/1.0.0/CHECKSUMS.json",
            }],
        }).encode()
        self.zipped = zip_bytes([("nested/file.txt", self.body)])

    def tearDown(self):
        self.temp.cleanup()

    def download(self, url, limit):
        if url.endswith("/fixture.json"):
            return self.metadata
        return self.meta if url.endswith("CHECKSUMS.json") else self.zipped

    def run_prepare(self, downloader=None):
        with mock.patch.object(prep, "inputs", return_value=(self.packages, self.pins)):
            prep.prepare(self.dest, downloader or self.download)

    def run_cli(self, downloader):
        real_prepare = prep.prepare
        stderr = io.StringIO()
        stdout = io.StringIO()
        with (mock.patch.object(prep, "inputs", return_value=(self.packages, self.pins)),
              mock.patch.object(prep, "prepare", side_effect=lambda path: real_prepare(path, downloader)),
              mock.patch.object(sys, "argv", ["prepare_components.py", "--prepare", str(self.dest)]),
              contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout)):
            result = prep.main()
        if result:
            self.assertNotIn("Prepared verified offline cache:", stdout.getvalue())
        return result, stderr.getvalue()

    def test_real_lock_and_pins_agree(self):
        packages, pins = prep.inputs()
        self.assertEqual(len(packages), 13)
        self.assertEqual(set(packages), set(pins))
        self.assertEqual(sum(p["files"] for p in pins.values()), 4523)

    def test_tools_image_uses_pinned_multiplatform_release(self):
        dockerfile = SCRIPT.parents[3] / "Dockerfile"
        self.assertEqual(
            dockerfile.read_text(encoding="utf-8").splitlines()[0],
            "FROM espressif/idf:v5.5.4@sha256:"
            "b9f2d6ea1c19e0c9f7959bdb74a9e3c775642f9d0f3b841937c5fa3363db892b",
        )

    def test_success_and_full_actual_tree_verification(self):
        downloader = mock.Mock(side_effect=self.download)
        self.run_prepare(downloader)
        self.assertEqual([call.args[0] for call in downloader.call_args_list], [
            "https://components-file.espressif.com/components/public/fixture.json",
            "https://components-file.espressif.com/components/public/fixture/1.0.0/CHECKSUMS.json",
            "https://components-file.espressif.com/components/public/fixture/1.0.0/published.zip",
        ])
        folder = self.dest / "fixture"
        self.assertEqual((folder / "nested/file.txt").read_bytes(), self.body)
        self.assertEqual((folder / ".component_hash").read_bytes(), self.hash.encode())
        prep.verify_component(folder, self.hash, self.pin)
        (folder / "nested/file.txt").write_bytes(b"tampered")
        with self.assertRaises(prep.PreparationError):
            prep.verify_component(folder, self.hash, self.pin)

    def test_existing_destination_is_never_touched_or_downloaded(self):
        self.dest.mkdir()
        marker = self.dest / "keep"
        marker.write_bytes(b"keep")
        downloader = mock.Mock()
        with self.assertRaises(prep.PreparationError):
            self.run_prepare(downloader)
        downloader.assert_not_called()
        self.assertEqual(marker.read_bytes(), b"keep")

    def test_symlink_destination_and_parent_rejected(self):
        elsewhere = self.parent / "elsewhere"
        elsewhere.mkdir()
        self.dest.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(prep.PreparationError):
            self.run_prepare()
        self.assertEqual(list(elsewhere.iterdir()), [])
        link = self.parent / "link"
        link.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(OSError):
            prep.secure_parent(link / "cache")

    def test_group_writable_parent_rejected(self):
        self.parent.chmod(0o770)
        with self.assertRaises(prep.PreparationError):
            self.run_prepare()

    def test_corrupt_metadata_and_archive_do_not_publish(self):
        for fetch in (
            lambda url, limit: self.metadata if url.endswith("/fixture.json") else (
                self.meta + b" " if url.endswith("CHECKSUMS.json") else self.zipped),
            lambda url, limit: self.metadata if url.endswith("/fixture.json") else (
                self.meta if url.endswith("CHECKSUMS.json")
                else zip_bytes([("nested/file.txt", b"x" * len(self.body))])),
            lambda url, limit: self.metadata if url.endswith("/fixture.json") else (
                self.meta if url.endswith("CHECKSUMS.json") else b"not a zip"),
        ):
            with self.subTest(fetch=fetch), self.assertRaises((prep.PreparationError, zipfile.BadZipFile)):
                self.run_prepare(fetch)
            self.assertFalse(self.dest.exists())

    def test_interrupted_download_does_not_publish(self):
        def fail(url, limit):
            raise OSError("interrupted")
        with self.assertRaises(OSError):
            self.run_prepare(fail)
        self.assertFalse(self.dest.exists())

    def test_cli_reports_bad_registry_and_checksum_json_without_publishing(self):
        for location in ("registry", "checksum"):
            for raw in (b"null", b"[]"):
                with self.subTest(location=location, raw=raw):
                    self.dest = self.parent / (location + str(len(list(self.parent.iterdir()))))
                    def download(url, limit):
                        if url.endswith("/fixture.json"):
                            return raw if location == "registry" else self.metadata
                        return raw if location == "checksum" else self.zipped
                    result, stderr = self.run_cli(download)
                    self.assertEqual(result, 1)
                    self.assertIn("Preparation failed:", stderr)
                    self.assertIn("partial stage retained", stderr)
                    self.assertNotIn("Traceback", stderr)
                    self.assertFalse(self.dest.exists())

    def test_cli_reports_encrypted_and_unsupported_archives_without_publishing(self):
        compressed = io.BytesIO()
        with zipfile.ZipFile(compressed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("nested/file.txt", self.body)
        corrupt = bytearray(compressed.getvalue())
        name_len, extra_len = struct.unpack_from("<HH", corrupt, 26)
        corrupt[30 + name_len + extra_len] ^= 0xff
        cases = (
            ("encrypted", change_zip_header(self.zipped, 6, 8, 1)),
            ("unsupported", change_zip_header(self.zipped, 8, 10, 99)),
            ("unsupported-version", change_zip_header(self.zipped, 4, 6, 99)),
            ("truncated", self.zipped[:-10]),
            ("corrupt-deflate", bytes(corrupt)),
        )
        for label, archive in cases:
            with self.subTest(label=label):
                self.dest = self.parent / label
                def download(url, limit):
                    if url.endswith("/fixture.json"):
                        return self.metadata
                    return self.meta if url.endswith("CHECKSUMS.json") else archive
                result, stderr = self.run_cli(download)
                self.assertEqual(result, 1)
                self.assertIn("Preparation failed:", stderr)
                self.assertIn("partial stage retained", stderr)
                self.assertNotIn("Traceback", stderr)
                self.assertFalse(self.dest.exists())

    def test_cli_reports_incomplete_http_response_without_publishing(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://components-file.espressif.com/components/public/fixture.json"
        response.length = None
        response.read.side_effect = http.client.IncompleteRead(b"partial", 1)
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(prep.urllib.request, "build_opener", return_value=opener):
            result, stderr = self.run_cli(prep.fetch)
        self.assertEqual(result, 1)
        self.assertIn("Preparation failed:", stderr)
        self.assertIn("partial stage retained", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertFalse(self.dest.exists())

    def test_cli_rejects_silent_short_read_with_valid_registry_prefix(self):
        wire = (b"HTTP/1.1 200 OK\r\nContent-Length: " +
                str(len(self.metadata) + 10).encode() + b"\r\n\r\n" + self.metadata)
        sock = mock.Mock()
        sock.makefile.return_value = io.BytesIO(wire)
        response = http.client.HTTPResponse(sock)
        response.begin()
        response.geturl = lambda: "https://components-file.espressif.com/components/public/fixture.json"
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(prep.urllib.request, "build_opener", return_value=opener):
            result, stderr = self.run_cli(prep.fetch)
        self.assertEqual(result, 1)
        self.assertIn("Preparation failed:", stderr)
        self.assertIn("partial stage retained", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(opener.open.call_count, 1)
        self.assertFalse(self.dest.exists())

    def test_optional_codecs_may_be_missing_at_import(self):
        original_import = builtins.__import__
        for codec in ("zlib", "lzma"):
            with self.subTest(codec=codec):
                def without_codec(name, *args, **kwargs):
                    if name == codec:
                        raise ImportError("synthetic unavailable codec")
                    return original_import(name, *args, **kwargs)
                spec = importlib.util.spec_from_file_location("prepare_without_codec", SCRIPT)
                module = importlib.util.module_from_spec(spec)
                with mock.patch.object(builtins, "__import__", side_effect=without_codec):
                    spec.loader.exec_module(module)
                folder = self.parent / ("without-" + codec)
                folder.mkdir()
                module.extract(self.zipped,
                               {"nested/file.txt": (len(self.body), module.digest(self.body))},
                               folder)
                self.assertEqual((folder / "nested/file.txt").read_bytes(), self.body)

    def test_unexpected_programming_error_is_not_masked(self):
        with (mock.patch.object(prep, "load_json", side_effect=RuntimeError("bug")),
              self.assertRaisesRegex(RuntimeError, "bug")):
            prep.registry_urls(self.metadata, "public/fixture", "1.0.0", self.hash)

    def test_racing_destination_is_not_overwritten(self):
        real_publish = prep.publish

        def race(fd, stage, dest):
            os.mkdir(dest, dir_fd=fd)
            real_publish(fd, stage, dest)

        with mock.patch.object(prep, "publish", side_effect=race), self.assertRaises(FileExistsError):
            self.run_prepare()
        self.assertTrue(self.dest.is_dir())
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_renamed_parent_cannot_redirect_stage_or_publish(self):
        owner = self.parent / "owner"
        owner.mkdir(mode=0o700)
        self.dest = owner / "cache"
        moved = self.parent / "moved"
        replacement = self.parent / "replacement"
        replacement.mkdir()
        switched = False

        def download(url, limit):
            nonlocal switched
            if not switched:
                owner.rename(moved)
                owner.symlink_to(replacement, target_is_directory=True)
                switched = True
            return self.download(url, limit)

        with self.assertRaises(OSError):
            self.run_prepare(download)
        self.assertEqual(list(replacement.iterdir()), [])
        self.assertFalse((moved / "cache").exists())
        stages = list(moved.glob(".pet-components-*"))
        self.assertEqual(len(stages), 1)
        prep.verify_component(stages[0] / "fixture", self.hash, self.pin)

    def test_registry_metadata_identity_version_and_urls(self):
        prep.registry_urls(self.metadata, "public/fixture", "1.0.0", self.hash)
        for field, value in (("component_hash", "0" * 64), ("version", "1.0.1"),
                             ("url", "https://evil.example/file.zip"),
                             ("checksums", "https://components-file.espressif.com/wrong"),
                             ("yanked_at", "2026-01-01")):
            data = json.loads(self.metadata)
            data["versions"][0][field] = value
            with self.subTest(field=field), self.assertRaises(prep.PreparationError):
                prep.registry_urls(json.dumps(data).encode(), "public/fixture", "1.0.0", self.hash)

    def test_manifest_rejects_duplicate_keys_paths_and_invalid_sizes(self):
        with self.assertRaises(prep.PreparationError):
            prep.load_json(b'{"files":[],"files":[]}')
        for bad in ("../escape", "/abs", "a//b", "a/./b", "a\\b", "C:escape"):
            with self.subTest(bad=bad), self.assertRaises(prep.PreparationError):
                prep.safe_path(bad)
        invalid = json.loads(self.meta)
        invalid["files"][0]["size"] = True
        with self.assertRaises(prep.PreparationError):
            prep.validate_manifest(json.dumps(invalid).encode(), self.hash, self.pin)

    def test_zip_rejects_traversal_duplicates_extra_and_missing(self):
        expected = {"nested/file.txt": (len(self.body), prep.digest(self.body))}
        cases = [
            [("../escape", b"x")],
            [("nested/file.txt", self.body), ("nested/file.txt", self.body)],
            [("nested/file.txt", self.body), ("extra", b"x")],
            [],
        ]
        for i, entries in enumerate(cases):
            folder = self.parent / f"test{i}"
            folder.mkdir()
            with self.subTest(i=i), self.assertRaises(prep.PreparationError):
                prep.extract(zip_bytes(entries), expected, folder)
        self.assertFalse((self.parent / "escape").exists())

    def test_zip_rejects_symlink_and_special_file(self):
        for index, mode in enumerate((stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600)):
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as archive:
                entry = zipfile.ZipInfo("nested/file.txt")
                entry.create_system = 3
                entry.external_attr = mode << 16
                archive.writestr(entry, self.body)
            folder = self.parent / str(index)
            folder.mkdir()
            with self.assertRaises(prep.PreparationError):
                prep.extract(data.getvalue(), {"nested/file.txt": (len(self.body), prep.digest(self.body))}, folder)

    def test_origin_validation_rejects_http_credentials_and_redirects(self):
        prep.check_url("https://components-file.espressif.com/components/a")
        for url in ("http://components-file.espressif.com/a",
                    "https://evil.example/a",
                    "https://components-file.espressif.com@evil.example/a",
                    "https://components-file.espressif.com/a?token=x"):
            with self.subTest(url=url), self.assertRaises(prep.PreparationError):
                prep.check_url(url)
        with self.assertRaises(prep.PreparationError):
            prep.SameHostRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example/a")


if __name__ == "__main__":
    unittest.main()