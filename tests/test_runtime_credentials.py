"""Private-config regression tests with synthetic markers only; no network."""
from contextlib import redirect_stderr, redirect_stdout
import fcntl
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
import runtime_credentials as credentials
import runtime_supervisor as supervisor

MARKER = "LOCAL_CONFIG_TEST_ONLY_9c08b417"


class RuntimeCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multidot-config-test-")
        self.root = Path(self.temp.name)
        self.private = self.root / "credentials.private.json"
        self.runtime_path = self.root / "runtime.local.json"
        self.config = json.loads((ROOT / "config/runtime.example.json").read_text())
        self.config["state_root"] = str(self.root / "state")
        self.runtime_path.write_text(json.dumps(self.config))
        self.values = {"schema_version": 1, **{field: MARKER + "_" + field for field in credentials.FIELDS}}
        self.original_umask = os.umask(0o077)

    def tearDown(self):
        os.umask(self.original_umask)
        self.temp.cleanup()

    def save(self, values=None, raw=None):
        self.private.write_text(raw if raw is not None else json.dumps(self.values if values is None else values))
        self.private.chmod(0o600)

    def call(self, command, *extra):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = credentials.main([command, "--private-config", str(self.private), "--runtime-config", str(self.runtime_path), *extra])
        rendered = out.getvalue() + err.getvalue()
        self.assertNotIn(MARKER, rendered)
        return code, json.loads(rendered)

    def test_example_contains_only_blank_values(self):
        example = json.loads((ROOT / "config/credentials.example.json").read_text())
        self.assertEqual(example, credentials.EMPTY)

    def test_init_is_blank_private_and_refuses_overwrite(self):
        code, result = self.call("init")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.private.read_text()), credentials.EMPTY)
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o600)
        self.save()
        original = self.private.read_bytes()
        self.assertEqual(self.call("init")[0], 2)
        self.assertEqual(self.private.read_bytes(), original)

    def test_install_writes_exact_file_formats_without_network_or_start(self):
        self.save()
        with patch("socket.socket", side_effect=AssertionError("No network allowed")), patch("subprocess.Popen", side_effect=AssertionError("No runtime allowed")):
            code, result = self.call("install")
        self.assertEqual(code, 0)
        self.assertFalse(result["network_contacted"])
        self.assertFalse(result["runtime_started"])
        secret_dir = self.root / "state/secrets"
        self.assertEqual(stat.S_IMODE(secret_dir.stat().st_mode), 0o700)
        for field, (name, _) in credentials.FIELDS.items():
            value = ("Bearer " if field == "worker_b_token" else "") + self.values[field]
            self.assertEqual((secret_dir / name).read_text(), value + "\n")
            self.assertEqual(stat.S_IMODE((secret_dir / name).stat().st_mode), 0o600)
        supervisor.render(self.config)
        rendered = (self.root / "state/config/tunnel-b.yaml").read_text()
        self.assertNotIn(MARKER, rendered)
        config = json.loads(rendered)
        self.assertEqual(config["control_plane"]["api_key"], "file:" + str(secret_dir / "runtime-api-key"))

    def test_missing_required_values_reports_names_and_writes_nothing(self):
        values = dict(credentials.EMPTY, runtime_api_key=MARKER)
        self.save(values)
        code, result = self.call("install")
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "missing_required_values")
        self.assertEqual(result["fields"], ["producer_token", "worker_b_token"])
        self.assertEqual(list((self.root / "state/secrets").iterdir()), [])

    def test_existing_values_are_not_read_or_overwritten(self):
        self.save()
        self.assertEqual(self.call("install")[0], 0)
        path = self.root / "state/secrets/runtime-api-key"
        original = path.read_bytes()
        self.assertEqual(self.call("install")[1]["error"], "existing_values_not_replaced")
        self.assertEqual(path.read_bytes(), original)
        self.save(credentials.EMPTY)
        read_bytes = Path.read_bytes
        def guard(p):
            if p.parent.name == "secrets":
                raise AssertionError("Existing secret destination must not be read")
            return read_bytes(p)
        with patch.object(Path, "read_bytes", guard):
            code, result = self.call("install")
        self.assertEqual(code, 0)
        self.assertEqual(result["installed_fields"], [])
        self.assertEqual(result["reused_fields"], sorted(credentials.FIELDS))

    def test_values_for_disabled_components_are_not_installed(self):
        self.config["components"] = ["dot2api"]
        self.runtime_path.write_text(json.dumps(self.config))
        self.save()
        code, result = self.call("install")
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "values_not_needed_by_components")

    def test_private_json_parse_errors_never_echo_input(self):
        cases = ['{"runtime_api_key":"' + MARKER, MARKER, '{"' + MARKER + '": 1}',
                 '{"schema_version":1,"schema_version":1,"runtime_api_key":"' + MARKER + '"}']
        for raw in cases:
            with self.subTest(raw_kind=len(raw)):
                self.save(raw=raw)
                self.assertEqual(self.call("install")[0], 2)

    def test_control_characters_and_wrong_types_are_rejected_by_field_name(self):
        for value in (MARKER + "\n", MARKER + " ", {"value": MARKER}, "X" * 8192):
            with self.subTest(value_type=type(value).__name__):
                values = dict(self.values, runtime_api_key=value)
                self.save(values)
                code, result = self.call("install")
                self.assertEqual(code, 2)
                self.assertEqual(result["fields"], ["runtime_api_key"])

    def test_final_file_length_matches_existing_runtime_reader(self):
        self.values.update(runtime_api_key="X" * 8191, producer_token="Y" * 8191, worker_b_token="Z" * 8184)
        self.save()
        self.assertEqual(self.call("install")[0], 0)
        for field, (name, _) in credentials.FIELDS.items():
            path = self.root / "state/secrets" / name
            self.assertEqual(path.stat().st_size, 8192)
            expected = ("Bearer " if field == "worker_b_token" else "") + self.values[field]
            self.assertEqual(supervisor.secret(path), expected)

    def test_worker_token_one_byte_over_storage_limit_is_rejected(self):
        self.save(dict(self.values, worker_b_token="X" * 8185))
        code, result = self.call("install")
        self.assertEqual(code, 2)
        self.assertEqual(result["fields"], ["worker_b_token"])
        self.assertFalse((self.root / "state/secrets").exists())

    def test_only_complete_files_are_published_without_replacement(self):
        self.save()
        link = os.link
        published = []
        def inspect(source, destination, **kwargs):
            directory = kwargs["src_dir_fd"]
            with self.assertRaises(FileNotFoundError):
                os.stat(destination, dir_fd=directory, follow_symlinks=False)
            fd = os.open(source, os.O_RDONLY, dir_fd=directory)
            with os.fdopen(fd, "r") as stream:
                value = stream.read()
            field = next(field for field, (name, _) in credentials.FIELDS.items() if name == destination)
            self.assertEqual(value, ("Bearer " if field == "worker_b_token" else "") + self.values[field] + "\n")
            published.append(field)
            return link(source, destination, **kwargs)
        with patch.object(os, "link", inspect):
            self.assertEqual(self.call("install")[0], 0)
        self.assertEqual(sorted(published), sorted(credentials.FIELDS))
        self.assertFalse(list((self.root / "state/secrets").glob(".pending-*")))

    def test_write_sync_failure_leaves_no_final_or_temporary_values(self):
        self.save()
        with patch.object(os, "fsync", side_effect=OSError(MARKER)):
            code, result = self.call("install")
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "private_config_operation_failed")
        self.assertEqual(list((self.root / "state/secrets").iterdir()), [])

    def test_partial_or_oversized_existing_entries_are_rejected_without_reading(self):
        self.save(credentials.EMPTY)
        secret_dir = self.root / "state/secrets"
        secret_dir.mkdir(parents=True, mode=0o700)
        destination = secret_dir / "runtime-api-key"
        for size in (0, 8193):
            with self.subTest(size=size):
                destination.write_bytes(b"X" * size)
                destination.chmod(0o600)
                code, result = self.call("install")
                self.assertEqual(code, 2)
                self.assertEqual(result["error"], "invalid_existing_file_size")
                self.assertEqual(result["fields"], ["runtime_api_key"])

    def test_invalid_runtime_config_never_echoes_input(self):
        self.save()
        self.runtime_path.write_text('{"invalid": "' + MARKER)
        code, result = self.call("install")
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "invalid_runtime_config")

    def test_argument_errors_are_redacted(self):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit):
            credentials.main(["install", "--key", MARKER])
        self.assertNotIn(MARKER, out.getvalue() + err.getvalue())

    def test_unsafe_private_permissions_are_refused_without_chmod(self):
        self.save()
        self.private.chmod(0o644)
        self.assertEqual(self.call("install")[1]["error"], "private_config_permissions_required")
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o644)

    def test_symlink_private_file_and_parent_are_rejected(self):
        actual = self.root / "actual.private.json"
        actual.write_text(json.dumps(self.values))
        actual.chmod(0o600)
        self.private.symlink_to(actual)
        self.assertEqual(self.call("install")[0], 2)
        self.private.unlink()
        real_parent = self.root / "real"
        real_parent.mkdir()
        fake_parent = self.root / "alias"
        fake_parent.symlink_to(real_parent, target_is_directory=True)
        self.private = fake_parent / "credentials.private.json"
        self.assertEqual(self.call("init")[0], 2)

    def test_symlink_secret_destination_is_not_followed(self):
        self.save()
        secret_dir = self.root / "state/secrets"
        secret_dir.mkdir(parents=True, mode=0o700)
        target = self.root / "unrelated"
        target.write_text(MARKER)
        (secret_dir / "runtime-api-key").symlink_to(target)
        self.assertEqual(self.call("install")[0], 2)
        self.assertEqual(target.read_text(), MARKER)

    def test_symlink_state_ancestor_is_refused(self):
        self.save()
        target = self.root / "elsewhere"
        target.mkdir()
        (self.root / "state").symlink_to(target, target_is_directory=True)
        self.assertEqual(self.call("install")[0], 2)
        self.assertEqual(list(target.iterdir()), [])

    def test_running_supervisor_lock_refuses_install(self):
        self.save()
        run = self.root / "state/run"
        run.mkdir(parents=True, mode=0o700)
        with open(run / "supervisor.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, result = self.call("install")
        self.assertEqual(code, 2)
        self.assertEqual(result["error"], "stop_runtime_before_install")

    def test_supervisor_check_and_status_do_not_display_markers(self):
        self.save()
        self.assertEqual(self.call("install")[0], 0)
        for action in ("check", "status"):
            out, err = StringIO(), StringIO()
            def fail_with_sensitive_message(_config):
                raise ValueError(MARKER)
            with patch.object(sys, "argv", ["runtime_supervisor.py", "--config", str(self.runtime_path), action]), \
                    patch.object(supervisor, "preflight", fail_with_sensitive_message), \
                    redirect_stdout(out), redirect_stderr(err):
                code = supervisor.main()
            self.assertNotIn(MARKER, out.getvalue() + err.getvalue())
            self.assertEqual(code, 2 if action == "check" else 0)

    def test_gitignore_covers_private_configs_backups_and_generated_secrets(self):
        ignored = ["config/credentials.private.json", "config/credentials.private.json.bak", "config/credentials.private.json~",
                   "config/.credentials.private.json.swp", "config/#credentials.private.json#", "config/runtime.local.json",
                   "config/runtime.local.json.bak", "config/runtime.local.json~", "config/.runtime.local.json.swp",
                   "var/runtime/secrets/runtime-api-key", "secrets/worker-b-authorization", "secrets/producer-token"]
        result = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"], input="\n".join(ignored) + "\n",
                                cwd=ROOT, text=True, capture_output=True, check=True)
        self.assertEqual(set(result.stdout.splitlines()), set(ignored))
        result = subprocess.run(["git", "check-ignore", "--no-index", "config/credentials.example.json"], cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
