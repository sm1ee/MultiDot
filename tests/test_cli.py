from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from multidot.cli import main
from multidot.controller import Controller
from multidot.mock import MockNative
from multidot.dot2api_adapter import TransportUncertain, UpstreamRejected


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "hub.sqlite"
        self.config = self.root / "controller.json"
        self.config.write_text(json.dumps({"mode": "native", "base_url": "http://127.0.0.1:9", "producer": "hub-producer", "token_env": "MULTIDOT_TEST_ONLY_TOKEN", "projects": ["demo"]}))
        self.common = ["--db", str(self.db), "--config", str(self.config)]

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args):
        output, error = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(self.common + list(args))
        return code, json.loads(output.getvalue()) if output.getvalue() else None, error.getvalue()

    def test_offline_create_and_cancel(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.call("bootstrap", "--project", "demo", "--goal", "Test")[0], 0)
            self.assertEqual(self.call("snapshot", "--file", "examples/snapshot.json")[0], 0)
            code, result, error = self.call("submit", "--file", "examples/job.json")
            self.assertEqual(code, 0, error)
            code, result, error = self.call("request-cancel", result["job_id"], "--reason", "User request")
            self.assertEqual(code, 0, error)
            self.assertEqual(result["state"], "CANCELLED")

    def test_recovery_flags_are_exclusive(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as context:
            main(self.common + ["recover", "--dry-run", "--apply"])
        self.assertEqual(context.exception.code, 2)

    def test_start_single_instance_and_owned_graceful_stop(self):
        self.call("bootstrap", "--project", "demo", "--goal", "Test")
        command = [sys.executable, "-m", "multidot"] + self.common + ["start", "--interval", "1"]
        env = dict(os.environ, MULTIDOT_TEST_ONLY_TOKEN="MOCK-only-token-no-server")
        first = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not Path(str(self.db.resolve()) + ".lock.pid").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            second = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(second.returncode, 2, second.stdout + second.stderr)
            self.assertIn("already running", second.stderr)
            first.send_signal(signal.SIGTERM)
            stdout, stderr = first.communicate(timeout=4)
            self.assertEqual(first.returncode, 0, stderr)
            self.assertTrue(json.loads(stdout)["stopped"])
        finally:
            if first.poll() is None:
                first.terminate()
                first.communicate(timeout=4)

    def test_observation_retry_is_bounded(self):
        adapter = MockNative()
        controller = Controller(self.db, adapter, ["demo"])
        try:
            controller.bootstrap("demo", "Test")
            controller.add_snapshot(json.loads(Path("examples/snapshot.json").read_text()))
            controller.submit_job(json.loads(Path("examples/job.json").read_text()))
            controller.tick()
            calls = []
            def unavailable(task):
                calls.append(task)
                raise TransportUncertain()
            adapter.get = unavailable
            for _ in range(8):
                controller.db.execute("UPDATE attempts SET next_observe=0")
                controller.reconcile()
            self.assertEqual(len(calls), 10)  # five tries for each of two mock tasks
            self.assertEqual(controller.db.execute("SELECT min(observation_halted) FROM attempts").fetchone()[0], 1)
        finally:
            controller.close()

    def test_auth_rejection_stops_reads_immediately(self):
        adapter = MockNative()
        controller = Controller(self.db, adapter, ["demo"])
        try:
            controller.bootstrap("demo", "Test")
            controller.add_snapshot(json.loads(Path("examples/snapshot.json").read_text()))
            controller.submit_job(json.loads(Path("examples/job.json").read_text()))
            controller.tick()
            calls = []
            def rejected(task):
                calls.append(task)
                raise UpstreamRejected(429)
            adapter.get = rejected
            for _ in range(3):
                controller.reconcile()
            self.assertEqual(len(calls), 2)
        finally:
            controller.close()


if __name__ == "__main__":
    unittest.main()
