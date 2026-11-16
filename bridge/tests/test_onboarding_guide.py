"""Execute the guide's token snippet only with disposable synthetic private state."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


GUIDE = Path(__file__).resolve().parents[2] / "docs" / "team-prototype.md"


def token_snippet() -> str:
    guide = GUIDE.read_text(encoding="utf-8")
    section = guide.split("the following Linux command", 1)[1]
    snippet = section.split("   \"$PET_PYTHON\" -B - <<'PY'\n", 1)[1].split("   PY\n", 1)[0]
    return textwrap.dedent(snippet)


class TokenGuideTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_snippet(self, root: str | None = None, prefix: str = ""):
        return subprocess.run(
            [sys.executable, "-B", "-c", prefix + token_snippet()],
            env={"XDG_CONFIG_HOME": str(self.root) if root is None else root},
            capture_output=True, text=True, timeout=10, check=False,
        )

    def test_generated_token_is_private_and_hash_excludes_newline(self) -> None:
        result = self.run_snippet()
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        path = self.root / "pet-token.txt"
        raw = path.read_bytes()
        self.assertRegex(raw, rb"^[A-Za-z0-9_-]{43}\n$")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        token = raw[:-1]
        digest = hashlib.sha256(token).hexdigest()
        self.assertIn(digest, result.stdout)
        self.assertNotIn(token.decode("ascii"), result.stdout + result.stderr)
        self.assertNotIn(hashlib.sha256(raw).hexdigest(), result.stdout)

        again = self.run_snippet()
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual(path.read_bytes(), raw)
        self.assertNotIn(token.decode("ascii"), again.stdout + again.stderr)
        self.assertNotIn(digest, again.stdout + again.stderr)

    def test_invalid_or_unprivate_directory_is_refused(self) -> None:
        for root in ("relative", str(self.root / "missing")):
            with self.subTest(root=root):
                result = self.run_snippet(root)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
        os.chmod(self.root, 0o755)
        result = self.run_snippet()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "pet-token.txt").exists())

    def test_symlink_directory_and_existing_token_are_not_followed(self) -> None:
        link = self.root.parent / (self.root.name + "-link")
        link.symlink_to(self.root, target_is_directory=True)
        self.addCleanup(link.unlink)
        self.assertNotEqual(self.run_snippet(str(link)).returncode, 0)

        victim = self.root / "other"
        victim.write_bytes(b"synthetic-existing-data")
        (self.root / "pet-token.txt").symlink_to(victim)
        result = self.run_snippet()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(victim.read_bytes(), b"synthetic-existing-data")
        self.assertEqual(result.stdout, "")

    def test_intermediate_symlink_is_not_followed(self) -> None:
        inside = self.root / "private"
        inside.mkdir(mode=0o700)
        link = self.root.parent / (self.root.name + "-intermediate")
        link.symlink_to(self.root, target_is_directory=True)
        self.addCleanup(link.unlink)
        result = self.run_snippet(str(link / "private"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertFalse((inside / "pet-token.txt").exists())

    def test_posix_and_bash_heredoc_handle_spaces(self) -> None:
        for shell in ("/bin/sh", "/bin/bash"):
            if not Path(shell).exists():
                continue
            folder = self.root / Path(shell).name / "private path"
            folder.mkdir(mode=0o700, parents=True)
            command = '"$PET_PYTHON" -B - <<\'PY\'\n' + token_snippet() + 'PY\n'
            result = subprocess.run(
                [shell, "-c", command],
                env={"PET_PYTHON": sys.executable, "XDG_CONFIG_HOME": str(folder)},
                capture_output=True, text=True, timeout=10, check=False,
            )
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            token = (folder / "pet-token.txt").read_bytes()[:-1]
            self.assertIn(hashlib.sha256(token).hexdigest(), result.stdout)
            self.assertNotIn(token.decode("ascii"), result.stdout)

    def test_fsync_failures_report_no_success_and_preserve_uncertain_file(self) -> None:
        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at):
                folder = self.root / str(fail_at)
                folder.mkdir(mode=0o700)
                prefix = f"""
import os
_real_fsync = os.fsync
_count = 0
def fail_fsync(fd):
    global _count
    _count += 1
    if _count == {fail_at}:
        raise OSError("injected fault")
    return _real_fsync(fd)
os.fsync = fail_fsync
"""
                result = self.run_snippet(str(folder), prefix)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
                token_file = folder / "pet-token.txt"
                raw = token_file.read_bytes()
                self.assertTrue(re.fullmatch(rb"[A-Za-z0-9_-]{43}\n", raw))
                self.assertNotIn(raw[:-1].decode("ascii"), result.stderr)
                again = self.run_snippet(str(folder))
                self.assertNotEqual(again.returncode, 0)
                self.assertEqual(token_file.read_bytes(), raw)


class FrontDoorGuideTest(unittest.TestCase):
    def test_instruction_block_keeps_confirmation_provenance_and_revision_fence(self) -> None:
        guide = GUIDE.read_text(encoding="utf-8")
        block = guide.split("   ```text\n", 1)[1].split("   ```", 1)[0]
        for requirement in (
            "A spoken yes is advisory",
            "never substitutes for Unified's own approval",
            "question actions are scoped to the calling chat",
            "Call coordination.context",
            "humanInput.sourceMessageId and humanInput.sourceInputId",
            "its admitted sourceInputIds must include that sourceInputId",
            "do not claim success or resend",
            "confirmed\n   revision and prompt",
            "if the\n   confirmed snapshot changed, stop and reconfirm",
            "actual user's sourceMessageId",
            "delivered sourceRequestId",
            "coordination.send has no sourceMessageId argument",
            "never invent it",
            "Workspace creation, chat creation and starting work are\n   separate outcomes",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, block)