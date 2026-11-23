"""Exercise the teammate installer in a disposable home. No running systemd, pet or provider."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from .test_gateway_server import config_doc

INSTALLER = Path(__file__).resolve().parents[1] / "deploy/install_user_service.py"


@unittest.skipIf(sys.platform != "linux" or os.geteuid() == 0, "Linux user-service setup")
class NewUserServiceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "fresh user"
        self.home.mkdir(mode=0o700)
        self.env = {"HOME": str(self.home), "PATH": os.defpath,
                    "XDG_CONFIG_HOME": str(self.home / "config"),
                    "XDG_STATE_HOME": str(self.home / "local state")}
        self.settings = self.home / "private 100%.json"
        self.settings.write_text(json.dumps(config_doc(self.home)), encoding="utf-8")
        self.settings.chmod(0o600)
        self.unit = self.home / "config/systemd/user/pocket-gateway.service"
        self.logs = self.home / "local state/pocket-gateway"

    def invoke(self, *args):
        return subprocess.run([sys.executable, "-I", "-B", str(INSTALLER), *args],
                              env=self.env, cwd=self.home, capture_output=True, text=True,
                              timeout=15)

    def test_preview_install_protect_and_remove(self):
        original = self.settings.read_bytes()
        args = ("--config", str(self.settings))
        self.assertEqual(self.invoke(*args).returncode, 0)
        self.assertEqual(list(self.home.iterdir()), [self.settings])
        written = self.invoke(*args, "--install")
        self.assertEqual(written.returncode, 0, written.stderr)
        text = self.unit.read_text()
        interpreter = os.path.abspath(sys.executable).replace("%", "%%")
        self.assertIn(f'ExecStart="{interpreter}" -B -m pet_bridge serve '
                      f'--config "{str(self.settings).replace("%", "%%")}"', text)
        self.assertEqual(text.count("ExecStart="), 1)
        self.assertNotIn("@", text)
        self.assertNotIn("EnvironmentFile=", text)
        self.assertNotIn("session_label", text + written.stdout + written.stderr)
        self.assertEqual(stat.S_IMODE(self.logs.stat().st_mode), 0o700)
        self.assertEqual(list(self.logs.iterdir()), [])  # not even the event log was opened
        self.assertEqual(self.invoke(*args, "--install").returncode, 1)
        self.assertEqual(self.unit.read_text(), text)
        self.unit.unlink()  # the documented unit removal leaves all private settings in place
        self.assertEqual(self.settings.read_bytes(), original)

    def test_env_file_is_parsed_not_exported(self):
        doc = config_doc(self.home)
        fields = "\n".join(
            f"POCKET_GATEWAY_{key.upper()}={','.join(value) if isinstance(value, list) else value}"
            for key, value in doc.items() if key not in {"version", "provider_key_file"})
        secret = "synthetic-never-in-output"
        self.settings.write_text("POCKET_GATEWAY_CONFIG_VERSION=1\n" + fields
                                 + "\nPOCKET_GATEWAY_PROVIDER_KEY=" + secret + "\n")
        result = self.invoke("--env-file", str(self.settings), "--install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(" --env-file ", self.unit.read_text())
        self.assertNotIn(secret, result.stdout + result.stderr + self.unit.read_text())
        self.assertNotIn(" --config ", self.unit.read_text())

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd verifier not installed")
    def test_systemd_accepts_generated_unit_without_starting_service(self):
        runtime = self.home / "runtime"
        runtime.mkdir(mode=0o700)
        self.env["XDG_RUNTIME_DIR"] = str(runtime)
        result = self.invoke("--config", str(self.settings), "--install")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(["systemd-analyze", "--user", "--generators=no", "--man=no",
                                 "verify", str(self.unit)], env=self.env, cwd=self.home,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.unit.parent / "default.target.wants").exists())
        self.assertEqual(list(self.logs.iterdir()), [])

    def test_unsafe_settings_or_existing_target_never_replaced(self):
        self.settings.chmod(0o644)
        self.assertNotEqual(self.invoke("--config", str(self.settings), "--install").returncode, 0)
        self.assertFalse(self.unit.exists())
        self.settings.chmod(0o600)
        self.unit.parent.mkdir(parents=True)
        self.unit.symlink_to(self.settings)
        original = self.settings.read_bytes()
        self.assertNotEqual(self.invoke("--config", str(self.settings), "--install").returncode, 0)
        self.assertTrue(self.unit.is_symlink())
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertFalse(self.logs.exists())

    def test_existing_log_and_nonliteral_paths_are_refused(self):
        self.logs.mkdir(mode=0o700, parents=True)
        existing = self.logs / "events.jsonl"
        existing.write_text("keep")
        self.assertNotEqual(self.invoke("--config", str(self.settings), "--install").returncode, 0)
        self.assertEqual(existing.read_text(), "keep")
        existing.unlink()
        bad = self.settings.with_name("bad\nunit.json")
        shutil.copy2(self.settings, bad)
        self.assertNotEqual(self.invoke("--config", str(bad), "--install").returncode, 0)
        self.assertFalse(self.unit.exists())