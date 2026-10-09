"""Tests for the manual fake-only gate without running a browser or setup."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
from io import StringIO
import json
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("manual_fake_fixture", ROOT / "tests/manual_web_setup_fixture.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class ManualFixtureTests(unittest.TestCase):
    def data(self, count=1):
        return {"schema_version": 1, "dots": [
            {"name": "Fixture dot " + str(index), "tunnel_id": "tunnel_" + format(index, "032x"),
             "runtime_api_key": fixture.MARKER} for index in range(1, count + 1)]}

    def test_only_exact_synthetic_values_are_accepted(self):
        fixture.validate_fixture(json.dumps(self.data(10)).encode())
        for field in ("name", "tunnel_id", "runtime_api_key"):
            data = self.data()
            data["dots"][0][field] = "UNACCEPTED_SYNTHETIC_VALUE_ONLY"
            with self.subTest(field=field), self.assertRaises(fixture.setup.SetupError) as caught:
                fixture.validate_fixture(json.dumps(data).encode())
            self.assertEqual(str(caught.exception), "fixture_values_only")
            self.assertNotIn("UNACCEPTED", str(caught.exception))

    def test_invalid_json_errors_are_fixed_and_value_free(self):
        with self.assertRaises(fixture.setup.SetupError) as caught:
            fixture.validate_fixture(b"UNACCEPTED_SYNTHETIC_VALUE_ONLY")
        self.assertEqual(caught.exception.code, "fixture_values_only")

    def test_manual_entry_uses_disposable_home_and_skips_provisioning(self):
        observed = []
        def collect(path, state_root):
            home = Path(os.environ["HOME"])
            observed.append(home)
            self.assertEqual(path, home / ".multidot/config.json")
            self.assertEqual(state_root, home / "unused-state")
            with fixture.wizard.private_destination(path) as publish:
                result = {"saved": False}
                publish(json.dumps(self.data()).encode(), result)
                self.assertTrue(result["saved"])
            return {"saved": True, "requested_setup": True, "configured_dots": 1}
        output = StringIO()
        with patch.object(fixture.web, "collect_and_save", side_effect=collect), \
                patch.object(fixture.setup, "configure") as configure, \
                patch.object(fixture.setup, "provision_stage") as provision, \
                redirect_stdout(output):
            self.assertEqual(fixture.main(), 0)
        configure.assert_not_called()
        provision.assert_not_called()
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].exists())
        self.assertIn('"save_passed": true', output.getvalue())

    def test_nonfixture_key_never_reaches_atomic_publisher(self):
        observed = []
        def collect(path, state_root):
            observed.append(path)
            with fixture.wizard.private_destination(path) as publish:
                data = self.data()
                data["dots"][0]["runtime_api_key"] = "UNACCEPTED_SYNTHETIC_VALUE_ONLY"
                publish(json.dumps(data).encode(), {"saved": False})
        out, err = StringIO(), StringIO()
        with patch.object(fixture.web, "collect_and_save", side_effect=collect), \
                patch.object(fixture.wizard, "_save") as save, \
                redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(fixture.main(), 2)
        save.assert_not_called()
        self.assertNotIn("UNACCEPTED_SYNTHETIC_VALUE_ONLY", out.getvalue() + err.getvalue())
        self.assertFalse(observed[0].exists())

    def test_manual_page_labels_fake_gate_and_has_capture_phase_submit_guard(self):
        server = object.__new__(fixture.FixtureServer)
        with patch.object(fixture.web._Server, "page", return_value=
                          b"<h1>MultiDot local setup</h1></body>"):
            server.nonce = "synthetic-nonce"
            page = server.page().decode()
        self.assertIn("FAKE-ONLY CHECK", page)
        self.assertIn("Never enter a real key", page)
        self.assertIn("event.stopImmediatePropagation()", page)
        self.assertIn("}, true)", page)
        self.assertIn("name.readOnly = true", page)
        self.assertIn("tunnel.readOnly = true", page)


if __name__ == "__main__":
    unittest.main()
