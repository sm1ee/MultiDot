"""Generic setup MOCK fixtures. No upstream import, real key or network use."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import multidot_setup as setup
import runtime_supervisor as runtime
import importlib.util
entry_spec = importlib.util.spec_from_file_location("multidot_entry", ROOT / "scripts/multidot.py")
entry = importlib.util.module_from_spec(entry_spec)
entry_spec.loader.exec_module(entry)

MARKER = "LOCAL_MULTIDOT_SETUP_TEST_ONLY"


class GenericSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multidot-generic-setup-test-")
        self.root = Path(self.temp.name)
        self.config = self.root / "dots.private.json"
        self.state = self.root / "state"
        self.environment = setup.default_environment(self.state, self.root / "tools")
        self.environment["repository"] = str(ROOT)
        self.umask = os.umask(0o077)
        self.provision_calls = 0

    def tearDown(self):
        os.umask(self.umask)
        self.temp.cleanup()

    def rows(self, count):
        return [{"name": f"\uc5f0\uad6c \ub2f4\ub2f9 {i}", "tunnel_id": f"tunnel_{i + 1:032x}",
                 "runtime_api_key": MARKER, "role": "worker"} for i in range(count)]

    def save(self, rows):
        self.config.write_text(json.dumps({"schema_version": 1, "dots": rows}, ensure_ascii=False))
        self.config.chmod(0o600)

    def fake_provision(self, stage, manifest, dots):
        self.provision_calls += 1
        setup.write_new(stage / "upstream/encryption.key", b"LOCAL_ONLY_KEY\n")
        setup.write_new(stage / "upstream/dot2api.sqlite3", b"LOCAL_ONLY_DATABASE")
        setup.write_new(stage / "secrets/producer-token", (MARKER + "_producer\n").encode())
        for row in manifest["workers"]:
            setup.write_new(stage / "secrets" / row["id"] / "worker-authorization", ("Bearer " + MARKER + row["id"] + "\n").encode())
            setup.write_new(stage / "secrets" / row["id"] / "runtime-api-key", (MARKER + "\n").encode())

    def configure(self, rows):
        with patch.object(setup, "provision_stage", self.fake_provision), \
                patch.object(setup, "choose_ports", lambda count: list(range(30000, 30000 + count))):
            return setup.configure(rows, self.environment, test_only=True, control_plane_url="http://127.0.0.1:9")

    def manifest(self):
        return setup.read_manifest(self.state)

    def test_blank_template_and_private_init(self):
        self.assertEqual(json.loads((ROOT / "config/dots.example.json").read_text()), setup.EMPTY)
        setup.initialize_config(self.config)
        self.assertEqual(json.loads(self.config.read_text()), setup.EMPTY)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            setup.initialize_config(self.config)

    def test_one_two_four_ten_profiles_reach_runtime_and_controller(self):
        for count in (1, 2, 4, 10):
            with self.subTest(count=count):
                self.state = self.root / f"state-{count}"
                self.environment["state_root"] = str(self.state)
                rows = self.rows(count)
                self.save(rows)
                result = self.configure(setup.load_dots(self.config))
                self.assertEqual(result["configured_dots"], count)
                manifest = self.manifest()
                self.assertEqual(len(manifest["workers"]), count)
                config = runtime.load(self.state / "config/runtime.json")
                self.assertEqual(len(runtime.worker_entries(config)), count)
                controller = json.loads((self.state / "config/controller.json").read_text())
                self.assertEqual(len(controller["workers"]), count)
                ids = {row["id"] for row in manifest["workers"]}
                self.assertEqual(ids, {row["id"] for row in controller["workers"]})
                self.assertEqual(len(ids), count)
                self.assertEqual(len(runtime.component_specs(config)), 2 + 2 * count)
                self.assertNotIn(MARKER, (self.state / "config/runtime.json").read_text())
                self.assertNotIn(MARKER, (self.state / "manifest.private.json").read_text())

    def test_display_name_and_order_changes_preserve_identity_scope_and_secrets(self):
        rows = self.rows(4)
        self.configure(rows)
        before = self.manifest()
        hashes = {str(path.relative_to(self.state)): path.read_bytes() for path in (self.state / "secrets").rglob("*") if path.is_file()}
        reordered = [dict(row, name="\u5b8c\u5168\u306b\u5225\u306e\u8868\u793a\u540d " + str(i)) for i, row in enumerate(reversed(rows))]
        self.configure(reordered)
        after = self.manifest()
        self.assertEqual(before["tenant"], after["tenant"])
        self.assertEqual(before["installation_id"], after["installation_id"])
        self.assertEqual({r["tunnel_id"]: r["id"] for r in before["workers"]}, {r["tunnel_id"]: r["id"] for r in after["workers"]})
        self.assertEqual(hashes, {str(path.relative_to(self.state)): path.read_bytes() for path in (self.state / "secrets").rglob("*") if path.is_file()})
        self.assertEqual(self.provision_calls, 1)
        self.assertEqual({r["name"] for r in after["workers"]}, {r["name"] for r in reordered})

    def test_two_installations_get_distinct_tenants_and_ids(self):
        self.configure(self.rows(2))
        first = self.manifest()
        self.state = self.root / "second"
        self.environment["state_root"] = str(self.state)
        self.configure(self.rows(2))
        second = self.manifest()
        self.assertNotEqual(first["tenant"], second["tenant"])
        self.assertTrue({r["id"] for r in first["workers"]}.isdisjoint(r["id"] for r in second["workers"]))

    def test_advanced_synthesis_role_has_no_name_meaning(self):
        rows = self.rows(3)
        rows[0]["name"] = "Summary"
        rows[2]["name"] = "Any name"
        rows[2]["role"] = "synthesis"
        self.configure(rows)
        manifest = self.manifest()
        self.assertEqual([r["role"] for r in manifest["workers"]], ["worker", "worker", "synthesis"])

    def test_role_addition_removal_or_key_rotation_never_reissues(self):
        rows = self.rows(2)
        self.configure(rows)
        before = (self.state / "manifest.private.json").read_bytes()
        variants = [self.rows(3), self.rows(1), [dict(rows[0], role="synthesis"), rows[1]],
                    [dict(rows[0], runtime_api_key=MARKER + "_changed"), rows[1]]]
        for altered in variants:
            with self.subTest(count=len(altered)), self.assertRaises(setup.SetupError):
                self.configure(altered)
        self.assertEqual(before, (self.state / "manifest.private.json").read_bytes())
        self.assertEqual(self.provision_calls, 1)

    def test_blank_key_reuses_existing_identity_but_not_new_setup(self):
        rows = self.rows(1)
        with self.assertRaises(setup.SetupError):
            self.configure([dict(rows[0], runtime_api_key="")])
        self.assertFalse(self.state.exists())
        self.configure(rows)
        self.configure([dict(rows[0], runtime_api_key="")])
        self.assertEqual(self.provision_calls, 1)

    def test_rejected_rename_preserves_valid_metadata(self):
        rows = self.rows(1)
        self.configure(rows)
        before = (self.state / "config/runtime.json").read_bytes()
        self.save([dict(rows[0], name="bad\x7f")])
        with self.assertRaises(setup.SetupError):
            setup.load_dots(self.config)
        self.assertEqual(before, (self.state / "config/runtime.json").read_bytes())

    def test_missing_existing_secret_fails_without_reprovision(self):
        self.configure(self.rows(1))
        (self.state / "secrets/producer-token").unlink()
        with self.assertRaises((setup.SetupError, FileNotFoundError)):
            self.configure(self.rows(1))
        self.assertEqual(self.provision_calls, 1)

    def test_duplicate_tunnel_and_invalid_roles_are_rejected(self):
        for rows in ([self.rows(1)[0]] * 2, [dict(self.rows(1)[0], role="invalid")],
                     [dict(row, role="synthesis") for row in self.rows(2)]):
            self.save(rows)
            with self.assertRaises(setup.SetupError):
                setup.load_dots(self.config)

    def test_no_three_or_four_worker_limit(self):
        self.save(self.rows(25))
        self.assertEqual(len(setup.load_dots(self.config)), 25)

    def test_configuration_errors_do_not_echo_values(self):
        self.config.write_text('{"dots": "' + MARKER)
        self.config.chmod(0o600)
        out, err = StringIO(), StringIO()
        with patch.object(entry, "verify_environment"), \
                patch.object(entry, "default_environment", return_value=dict(self.environment, upstream_python=str(Path(sys.prefix) / "bin/python"))), \
                redirect_stdout(out), redirect_stderr(err):
            code = entry.main(["setup", "--config", str(self.config)])
        self.assertEqual(code, 2)
        self.assertNotIn(MARKER, out.getvalue() + err.getvalue())

    def test_relative_operator_tool_paths_become_absolute(self):
        environment = setup.default_environment(self.state, Path("relative-tools"))
        self.assertTrue(Path(environment["upstream_python"]).is_absolute())

    def test_dotdot_tool_aliases_resolve_to_same_prefix(self):
        first = setup.default_environment(self.state, self.root / "tools")
        second = setup.default_environment(self.state, self.root / "alias/../tools")
        self.assertEqual(first["upstream_python"], second["upstream_python"])

    def test_generated_bounds_fail_before_provisioning(self):
        with patch.object(setup, "MAX_MANIFEST_BYTES", 16), self.assertRaises(setup.SetupError):
            self.configure(self.rows(1))
        self.assertEqual(self.provision_calls, 0)
        self.assertFalse(self.state.exists())

    def test_private_config_and_generated_credentials_are_git_ignored(self):
        paths = ["config/dots.private.json", "config/dots.private.json.bak",
                 "config/dots.local.json", "secrets/worker-test/runtime-api-key",
                 "secrets/worker-test/worker-authorization", "custom-state/upstream/encryption.key",
                 "MultiDot-state/config/runtime.json", "custom-state/manifest.private.json"]
        result = subprocess.run(["git", "check-ignore", "--stdin"], input="\n".join(paths) + "\n",
                                cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(set(result.stdout.splitlines()), set(paths))

    def test_failure_before_publish_does_not_leave_runtime_state(self):
        def fail(stage, manifest, dots):
            self.fake_provision(stage, manifest, dots)
            raise ValueError(MARKER)
        with patch.object(setup, "provision_stage", fail), self.assertRaises(ValueError):
            setup.configure(self.rows(1), self.environment, test_only=True, control_plane_url="http://127.0.0.1:9")
        self.assertFalse(self.state.exists())
        self.assertFalse(list(self.root.glob(".state.setup-*")))

    def test_atomic_publish_refuses_existing_empty_target(self):
        source, target = self.root / "stage", self.root / "target"
        source.mkdir(mode=0o700)
        target.mkdir(mode=0o700)
        parent = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(OSError):
                setup.publish_directory(parent, source.name, target.name)
        finally:
            os.close(parent)
        self.assertTrue(source.is_dir())
        self.assertTrue(target.is_dir())

    def test_config_symlink_directory_is_not_followed_on_rename(self):
        rows = self.rows(1)
        self.configure(rows)
        directory = self.state / "config"
        moved = self.state / "old-config"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(OSError):
            self.configure([dict(rows[0], name="Renamed")])
        self.assertEqual(self.provision_calls, 1)


if __name__ == "__main__":
    unittest.main()
