"""Host pixel/atlas/export checks; not a rendered UI or physical display test."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

from PIL import Image

APP = Path(__file__).resolve().parents[1]
REPO = APP.parents[1]
SOURCE = REPO / "assets/source/amplifier-assistant.webp"
COUNTS = (6, 8, 8, 4, 5, 8, 6, 6, 6, 8, 8)
NAMES = ("idle", "right", "left", "wave", "jump", "error", "wait", "think", "review", "look_a", "look_b")
FIRST = (0, 6, 14, 22, 26, 31, 39, 45, 51, 57, 65)
FRAME_BYTES = 192 * 208 * 2
spec = importlib.util.spec_from_file_location("pet_export", APP / "scripts/export_pet.py")
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class Artwork(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.temp.name) / "export"
        cls.report = exporter.export(SOURCE, cls.out)
        cls.data = (cls.out / "amplifier-assistant.rgb565").read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_supplied_atlas_and_provenance_are_exact(self):
        raw = SOURCE.read_bytes()
        self.assertEqual(len(raw), 2660392)
        self.assertEqual(hashlib.sha1(b"blob 2660392\0" + raw).hexdigest(),
                         "d1175e32dde375192c1b46a92b0c2470bed7e6d1")
        digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual(digest, "fee48c1c5169014c6a3911a9cdd143acb4463c1630d3fa107619485d373cf272")
        provenance = json.loads((REPO / "assets/source/amplifier-assistant.provenance.json").read_text())
        self.assertEqual(provenance, {
            "path": "assets/source/amplifier-assistant.webp",
            "source_path": "pets/amplifier/spritesheet.webp",
            "source_revision": "84c24087417c95547d823b549280dada7db8890d",
            "git_blob": "d1175e32dde375192c1b46a92b0c2470bed7e6d1",
            "sha256": digest,
            "owner_supplied_for_project": True,
        })
        self.assertEqual(self.report["source_sha256"], digest)
        with Image.open(SOURCE) as sheet:
            self.assertEqual((sheet.format, sheet.mode, sheet.size), ("WEBP", "RGBA", (1536, 2288)))

    def test_all_73_required_cells_match_composited_little_endian_rgb565(self):
        self.assertEqual(self.report["states"], [
            {"name": name, "first": first, "frames": count}
            for name, first, count in zip(NAMES, FIRST, COUNTS)])
        self.assertEqual((self.report["width"], self.report["height"], self.report["frames"]),
                         (192, 208, 73))
        self.assertEqual(self.report["frame_bytes"], FRAME_BYTES)
        self.assertEqual(len(self.data), 73 * FRAME_BYTES)
        self.assertEqual(self.report["asset_bytes"], len(self.data))
        with Image.open(SOURCE) as sheet:
            for row, count in enumerate(COUNTS):
                for col in range(count):
                    with self.subTest(row=row, col=col):
                        cell = sheet.crop((192*col, 208*row, 192*(col+1), 208*(row+1)))
                        self.assertIsNotNone(cell.getchannel("A").getbbox())
                        rgb = Image.alpha_composite(Image.new("RGBA", cell.size, (15, 23, 42, 255)), cell)
                        # Independent byte-by-byte quantization, including transparent edges.
                        expected = bytearray()
                        for r, g, b, _ in rgb.getdata():
                            pixel = (r // 8) * 2048 + (g // 4) * 32 + b // 8
                            expected.extend((pixel & 255, pixel >> 8))
                        index = FIRST[row] + col
                        actual = self.data[index*FRAME_BYTES:(index+1)*FRAME_BYTES]
                        self.assertEqual(actual, expected)
                        self.assertGreater(len(set(struct.iter_unpack("<H", actual))), 1)

    def test_readme_thumbnail_is_the_native_first_exported_frame(self):
        with Image.open(REPO / "assets/amplifier-assistant-preview.png") as image:
            self.assertEqual((image.format, image.mode, image.size, image.n_frames),
                             ("PNG", "RGB", (192, 208), 1))
            expected = bytearray()
            for (pixel,) in struct.iter_unpack("<H", self.data[:FRAME_BYTES]):
                r, g, b = pixel >> 11, (pixel >> 5) & 63, pixel & 31
                expected.extend(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))
            self.assertEqual(image.tobytes(), expected)
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn('src="assets/amplifier-assistant-preview.png" width="192" height="208"', readme)

    def test_blank_required_cells_in_each_row_fail_before_output(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            for row, count in enumerate(COUNTS):
                with self.subTest(row=row):
                    with Image.open(SOURCE) as original:
                        sheet = original.copy()
                    # Include the final required cell; padding columns need not contain art.
                    sheet.paste((0, 0, 0, 0), (192*(count-1), 208*row, 192*count, 208*(row+1)))
                    source = folder / f"blank-{row}.webp"
                    sheet.save(source, lossless=True)
                    out = folder / f"out-{row}"
                    with self.assertRaisesRegex(ValueError, f"Blank required cell: .*row {row}, column {count-1}"):
                        exporter.export(source, out)
                    self.assertFalse(out.exists())

    def test_opaque_background_only_cell_also_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            with Image.open(SOURCE) as original:
                sheet = original.copy()
            sheet.paste((15, 23, 42, 255), (0, 0, 192, 208))
            source = folder / "background.webp"
            sheet.save(source, lossless=True)
            with self.assertRaisesRegex(ValueError, "Blank required cell"):
                exporter.export(source, folder / "out")
            self.assertFalse((folder / "out").exists())

    def test_wrong_format_mode_or_dimensions_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            for name, mode, size in (("rgb.webp", "RGB", (1536, 2288)),
                                     ("small.webp", "RGBA", (192, 208)),
                                     ("wrong.png", "RGBA", (1536, 2288))):
                with self.subTest(name=name):
                    source = folder / name
                    Image.new(mode, size, (255, 0, 0, 128) if mode == "RGBA" else "red").save(source)
                    with self.assertRaisesRegex(ValueError, "RGBA WebP v2 atlas"):
                        exporter.export(source, folder / "out")
                    self.assertFalse((folder / "out").exists())


if __name__ == "__main__":
    unittest.main()