"""Real terminal behavior with disposable HOME and no credential provisioning."""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import tempfile
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
MARKER = "LOCAL_WIZARD_PTY_FAKE_KEY_ONLY"
CHILD = r'''
import fcntl, importlib.util, json, os, stat, sys, termios
from pathlib import Path
from unittest.mock import patch
if sys.stdin.isatty():
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
root = Path(os.environ["FIXTURE_REPOSITORY"])
sys.path.insert(0, str(root / "scripts"))
import multidot_setup as setup
import multidot_wizard as wizard
spec = importlib.util.spec_from_file_location("fixture_operator_entry", root / "scripts/multidot.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
environment = setup.default_environment(tools_root=Path.home() / "unused-fixture-tools")
environment["upstream_python"] = str(Path(sys.prefix) / "bin/python")
if os.environ.get("FIXTURE_MODE") == "fail_save_sync":
    original_save, original_fsync = wizard._save, wizard.os.fsync
    def fsync_fixture(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("LOCAL_WIZARD_PTY_FAKE_KEY_ONLY")
        return original_fsync(fd)
    def save_fixture(*args, **kwargs):
        with patch.object(wizard.os, "fsync", side_effect=fsync_fixture):
            return original_save(*args, **kwargs)
    wizard._save = save_fixture
def prepare(dots, environment):
    Path(os.environ["FIXTURE_RECORD"]).write_text(json.dumps({"dot_count": len(dots), "fake_only": True}))
    if os.environ.get("FIXTURE_MODE") == "fail":
        raise setup.SetupError("fixture_preparation_failed")
    return {"ok": True, "configured_dots": len(dots), "fixture_preparation_only": True}
code = 2
try:
    with patch.object(entry, "verify_environment"), patch.object(entry, "default_environment", return_value=environment), patch.object(entry, "configure", side_effect=prepare):
        code = entry.main(sys.argv[1:])
finally:
    restored = bool(termios.tcgetattr(0)[3] & termios.ECHO) if sys.stdin.isatty() else "not_tty"
    print("PTY_ECHO_RESTORED=" + str(restored), flush=True)
raise SystemExit(code)
'''


class WizardTerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multidot-wizard-pty-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.config = self.home / ".multidot/config.json"
        self.record = self.root / "mock-preparation.json"
        self.env = {"PATH": os.defpath, "LANG": "C.UTF-8", "HOME": str(self.home),
                    "FIXTURE_REPOSITORY": str(ROOT), "FIXTURE_RECORD": str(self.record)}
        self.output = ""
        self.cursor = 0

    def start(self, args=None, mode=None):
        master, slave = os.openpty()
        env = dict(self.env)
        if mode:
            env["FIXTURE_MODE"] = mode
        self.process = subprocess.Popen([sys.executable, "-c", CHILD, *(args or ["setup"])],
                                        stdin=slave, stdout=slave, stderr=slave,
                                        env=env, start_new_session=True)
        os.close(slave)
        self.master = master
        self.addCleanup(self.close_session)

    def close_session(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        os.close(self.master)

    def read(self, timeout=0.1):
        if select.select([self.master], [], [], timeout)[0]:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return False
            self.output += chunk.decode("utf-8", "replace")
            return bool(chunk)
        return True

    def until(self, prompt):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            position = self.output.find(prompt, self.cursor)
            if position >= 0:
                self.cursor = position + len(prompt)
                return
            if not self.read():
                break
        self.fail("Expected fixture terminal prompt was not observed")

    def send(self, text):
        os.write(self.master, text.encode())

    def finish(self, code=0):
        deadline = time.monotonic() + 10
        while self.process.poll() is None and time.monotonic() < deadline:
            self.read()
        self.assertEqual(self.process.wait(timeout=1), code)
        while self.read(0.05):
            if not select.select([self.master], [], [], 0)[0]:
                break
        self.assertNotIn(MARKER, self.output)
        self.assertIn("PTY_ECHO_RESTORED=True", self.output)

    def enter_dot(self, number=1):
        self.until("Dot name: ")
        self.send("Fixture reviewer " + str(number) + "\n")
        self.until("Tunnel ID: ")
        self.send(f"tunnel_{number:032x}\n")
        self.until("Runtime API key (masked): ")
        self.assertFalse(termios.tcgetattr(self.master)[3] & termios.ECHO)
        self.send(MARKER + "\n")
        self.until("Add another dot? [y/N]: ")
        self.assertIn("*" * len(MARKER), self.output)
        self.assertTrue(termios.tcgetattr(self.master)[3] & termios.ECHO)

    def assert_no_config_or_preparation(self):
        self.assertFalse(self.config.exists())
        self.assertFalse(self.record.exists())
        if self.config.parent.exists():
            self.assertFalse(list(self.config.parent.glob("*.pending")))

    def test_actual_tty_masks_keys_and_saves_two_profiles_after_one_confirmation(self):
        self.start()
        self.enter_dot(1)
        self.send("y\n")
        self.enter_dot(2)
        self.send("n\n")
        self.until("Save and prepare local setup? [y/N]: ")
        self.assertFalse(self.config.exists())
        self.assertFalse(self.record.exists())
        self.send("y\n")
        self.finish()
        self.assertEqual(json.loads(self.record.read_text())["dot_count"], 2)
        data = json.loads(self.config.read_text())
        self.assertEqual(len(data["dots"]), 2)
        self.assertTrue(all(row["runtime_api_key"] == MARKER for row in data["dots"]))
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config.parent.stat().st_mode), 0o700)

    def test_declining_final_confirmation_saves_nothing(self):
        self.start()
        self.enter_dot()
        self.send("n\n")
        self.until("Save and prepare local setup? [y/N]: ")
        self.send("n\n")
        self.finish()
        self.assert_no_config_or_preparation()

    def test_ctrl_c_during_masked_input_restores_echo_and_leaves_no_config(self):
        self.start()
        self.until("Dot name: ")
        self.send("Fixture\n")
        self.until("Tunnel ID: ")
        self.send("tunnel_" + "1" * 32 + "\n")
        self.until("Runtime API key (masked): ")
        self.assertFalse(termios.tcgetattr(self.master)[3] & termios.ECHO)
        self.send(MARKER)
        self.send("\x03")
        self.finish()
        self.assert_no_config_or_preparation()

    def test_eof_during_masked_input_restores_echo_and_leaves_no_config(self):
        self.start()
        self.until("Dot name: ")
        self.send("Fixture\n")
        self.until("Tunnel ID: ")
        self.send("tunnel_" + "1" * 32 + "\n")
        self.until("Runtime API key (masked): ")
        self.send("\x04")
        self.finish()
        self.assert_no_config_or_preparation()

    def test_preparation_error_preserves_complete_approved_config_without_values_in_output(self):
        self.start(mode="fail")
        self.enter_dot()
        self.send("n\n")
        self.until("Save and prepare local setup? [y/N]: ")
        self.send("y\n")
        self.finish(code=2)
        self.assertEqual(json.loads(self.config.read_text())["dots"][0]["runtime_api_key"], MARKER)
        self.assertIn('"wizard_config_saved": true', self.output)
        self.assertFalse(list(self.config.parent.glob("*.pending")))

    def test_save_sync_error_reports_saved_config_without_starting_preparation(self):
        self.start(mode="fail_save_sync")
        self.enter_dot()
        self.send("n\n")
        self.until("Save and prepare local setup? [y/N]: ")
        self.send("y\n")
        self.finish(code=2)
        self.assertEqual(json.loads(self.config.read_text())["dots"][0]["runtime_api_key"], MARKER)
        self.assertIn('"wizard_config_saved": true', self.output)
        self.assertIn("config_saved_sync_unconfirmed", self.output)
        self.assertFalse(self.record.exists())
        self.assertFalse(list(self.config.parent.glob("*.pending")))

    def test_no_tty_never_falls_back_to_reading_or_echoing_a_piped_key(self):
        result = subprocess.run([sys.executable, "-c", CHILD, "setup"], input=MARKER,
                                capture_output=True, text=True, env=self.env, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn("interactive_terminal_required", result.stderr)
        self.assertNotIn(MARKER, result.stdout + result.stderr)
        self.assert_no_config_or_preparation()

    def test_non_interactive_default_file_remains_supported_without_tty(self):
        self.config.parent.mkdir(mode=0o700)
        self.config.write_text(json.dumps({"schema_version": 1, "dots": [
            {"name": "Fixture", "tunnel_id": "tunnel_" + "1" * 32, "runtime_api_key": MARKER}]}))
        self.config.chmod(0o600)
        result = subprocess.run([sys.executable, "-c", CHILD, "setup", "--non-interactive"],
                                capture_output=True, text=True, env=self.env, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.record.read_text())["dot_count"], 1)
        self.assertNotIn(MARKER, result.stdout + result.stderr)
        self.assertNotIn("Dot name:", result.stdout + result.stderr)

    def test_explicit_config_preserves_legacy_non_interactive_behavior(self):
        path = self.root / "existing.private.json"
        path.write_text(json.dumps({"schema_version": 1, "dots": [
            {"name": "Fixture", "tunnel_id": "tunnel_" + "1" * 32, "runtime_api_key": MARKER}]}))
        path.chmod(0o600)
        result = subprocess.run([sys.executable, "-c", CHILD, "setup", "--config", str(path)],
                                capture_output=True, text=True, env=self.env, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(MARKER, result.stdout + result.stderr)
        self.assertNotIn("Dot name:", result.stdout + result.stderr)
        self.assertFalse(self.config.exists())


if __name__ == "__main__":
    unittest.main()
