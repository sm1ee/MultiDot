"""Web-mode command routing with fake metadata and no listener/provisioning."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import multidot_setup as setup
import multidot_wizard as wizard

spec = importlib.util.spec_from_file_location("web_cli_fixture", ROOT / "scripts/multidot.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class WebCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multidot-web-cli-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.scope = ExitStack()
        self.addCleanup(self.scope.close)
        self.scope.enter_context(patch.dict(os.environ, {"HOME": str(self.home)}))
        self.environment = setup.default_environment(tools_root=self.home / "unused-tools")
        self.environment["upstream_python"] = str(Path(sys.prefix) / "bin/python")
        self.scope.enter_context(patch.object(entry, "default_environment", return_value=self.environment))
        self.verify = self.scope.enter_context(patch.object(entry, "verify_environment"))
        self.prepare = self.scope.enter_context(patch.object(entry, "configure", return_value={"ok": True}))
        self.load = self.scope.enter_context(patch.object(entry, "load_dots", return_value=[]))
        self.tty = self.scope.enter_context(patch.object(wizard, "require_tty"))
        self.collect = Mock(return_value={"ok": True, "saved": True, "requested_setup": True})
        module = types.ModuleType("multidot_web")
        module.collect_and_save = self.collect
        self.scope.enter_context(patch.dict(sys.modules, {"multidot_web": module}))
        self.out, self.err = StringIO(), StringIO()

    def call(self, *args):
        with redirect_stdout(self.out), redirect_stderr(self.err):
            return entry.main(list(args))

    def test_web_mode_does_not_require_tty_and_prepares_only_after_approval(self):
        self.assertEqual(self.call("setup", "--web"), 0)
        self.tty.assert_not_called()
        self.verify.assert_called_once_with(self.environment)
        self.collect.assert_called_once_with(setup.default_config(), state_root=self.environment["state_root"])
        self.load.assert_called_once_with(setup.default_config())
        self.prepare.assert_called_once_with([], self.environment)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_web_explicit_config_preserves_requested_path(self):
        self.assertEqual(self.call("setup", "--web", "--config", "chosen.private.json"), 0)
        self.collect.assert_called_once_with(Path("chosen.private.json"), state_root=self.environment["state_root"])
        self.load.assert_called_once_with(Path("chosen.private.json"))

    def test_web_cancellation_does_not_read_config_or_prepare(self):
        self.collect.return_value = {"ok": True, "saved": False, "requested_setup": False, "cancelled": True}
        self.assertEqual(self.call("setup", "--web"), 0)
        self.prepare.assert_not_called()
        self.load.assert_not_called()
        self.assertTrue(json.loads(self.out.getvalue())["cancelled"])

    def test_web_environment_failure_happens_before_form_is_opened(self):
        self.verify.side_effect = setup.SetupError("fixture_missing_toolchain")
        self.assertEqual(self.call("setup", "--web"), 2)
        self.collect.assert_not_called()
        self.prepare.assert_not_called()

    def test_web_publish_error_preserves_saved_metadata(self):
        failure = setup.SetupError("config_saved_sync_unconfirmed")
        failure.config_saved = True
        self.collect.side_effect = failure
        self.assertEqual(self.call("setup", "--web"), 2)
        self.assertTrue(json.loads(self.err.getvalue())["wizard_config_saved"])
        self.prepare.assert_not_called()

    def test_web_preparation_error_has_no_exception_payload(self):
        marker = "WEB_CLI_FAKE_SECRET_MARKER_ONLY"
        self.prepare.side_effect = RuntimeError(marker)
        self.assertEqual(self.call("setup", "--web"), 2)
        self.assertNotIn(marker, self.out.getvalue() + self.err.getvalue())
        self.assertTrue(json.loads(self.err.getvalue())["wizard_config_saved"])

    def test_web_flag_is_setup_only(self):
        for command in ("init", "run", "status", "stop"):
            with self.subTest(command=command):
                self.assertEqual(self.call(command, "--web"), 2)
        self.collect.assert_not_called()
        self.prepare.assert_not_called()

    def test_web_and_terminal_modes_are_mutually_exclusive(self):
        for option in ("--interactive", "--non-interactive"):
            with self.subTest(option=option), self.assertRaises(SystemExit) as caught:
                self.call("setup", "--web", option)
            self.assertEqual(caught.exception.code, 2)
        self.collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
