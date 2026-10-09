"""MOCK-only startup regression tests; no native service or real token is used."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from multidot.cli import main
from multidot.controller import Controller
from multidot.dot2api_adapter import TransportUncertain, UpstreamRejected
from multidot.mock import MockNative


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multidot-startup-test-")
        self.root = Path(self.temp.name)
        self.path = self.root / "hub.sqlite"
        self.adapter = MockNative()
        self.controller = Controller(self.path, self.adapter, ["demo"])
        self.controller.bootstrap("demo", "Startup recovery tests")
        snapshot = {"schema_version": "multidot.snapshot.v1", "project_id": "demo",
                    "snapshot_id": "v1", "artifacts": [{"name": "input.md",
                    "media_type": "text/markdown", "content": "MOCK material."}]}
        self.controller.add_snapshot(snapshot)
        spec = {"schema_version": "multidot.job.v1", "project_id": "demo",
                "request_id": "startup-test", "title": "Startup recovery",
                "goal": "Review supplied material", "input_snapshot_id": "v1",
                "policy_id": "provided-materials-only", "steps": [{"step_id": "b",
                "worker": "dot-b", "instructions": "Review", "acceptance_criteria": ["Evidence"],
                "depends_on": []}]}
        self.job = self.controller.submit_job(spec)["job_id"]
        self.attempt = self.controller.schedule()[0]

    def tearDown(self):
        self.controller.close()
        self.temp.cleanup()

    def attempt_row(self):
        return dict(self.controller.db.execute("SELECT * FROM attempts WHERE id=?", (self.attempt,)).fetchone())

    def outbox(self):
        return dict(self.controller.db.execute("SELECT * FROM dispatch_outbox WHERE attempt_id=?", (self.attempt,)).fetchone())

    def start_one_tick(self):
        config = self.root / "controller.json"
        config.write_text(json.dumps({"mode": "native", "base_url": "http://127.0.0.1:9",
                                     "producer": "hub-producer", "token_env": "MULTIDOT_STARTUP_TEST_TOKEN",
                                     "projects": ["demo"]}))
        handlers = {}
        def register(signum, handler):
            handlers[signum] = handler
        def stop_on_sleep(_interval):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        output, error = StringIO(), StringIO()
        with patch.dict(os.environ, {"MULTIDOT_STARTUP_TEST_TOKEN": "MOCK-only-no-server"}), \
                patch("multidot.cli.NativeTaskAdapter", return_value=self.adapter), \
                patch("multidot.cli.signal.signal", side_effect=register), \
                patch("multidot.cli.time.sleep", side_effect=stop_on_sleep), \
                redirect_stdout(output), redirect_stderr(error):
            code = main(["--db", str(self.path), "--config", str(config), "start", "--interval", "1"])
        self.assertEqual(code, 0, error.getvalue())
        self.assertEqual(json.loads(output.getvalue()), {"stopped": True})

    def test_start_preserves_auth_observation_halt_and_worker_pause(self):
        self.controller.dispatch()
        reads = []
        def rejected(task_id):
            reads.append(task_id)
            raise UpstreamRejected(429)
        self.adapter.get = rejected
        self.controller.reconcile()
        before = self.attempt_row()
        outbox = self.outbox()
        self.start_one_tick()
        self.assertEqual(len(reads), 1)
        self.assertEqual(self.attempt_row(), before)
        self.assertEqual(self.outbox(), outbox)
        self.assertEqual(self.controller.db.execute("SELECT paused FROM workers WHERE id='dot-b'").fetchone()[0], 1)
        self.assertEqual(self.controller.get_job(self.job)["state"], "NEEDS_REVIEW")

    def test_start_preserves_exhausted_observation_counter(self):
        self.controller.dispatch()
        reads = []
        def unavailable(task_id):
            reads.append(task_id)
            raise TransportUncertain()
        self.adapter.get = unavailable
        for _ in range(5):
            self.controller.db.execute("UPDATE attempts SET next_observe=0")
            self.controller.reconcile()
        before = self.attempt_row()
        self.assertEqual(before["observation_errors"], 5)
        self.assertEqual(before["observation_halted"], 1)
        self.start_one_tick()
        self.assertEqual(len(reads), 5)
        self.assertEqual(self.attempt_row(), before)

    def test_start_preserves_auth_cancellation_halt(self):
        self.controller.dispatch()
        self.controller.request_cancel(self.job, "MOCK stop", contact_upstream=False)
        cancels = []
        def rejected(task_id, reason):
            cancels.append(task_id)
            raise UpstreamRejected(403)
        self.adapter.cancel = rejected
        self.controller.cancel_known()
        before = self.attempt_row()
        self.start_one_tick()
        after = self.attempt_row()
        self.assertEqual(len(cancels), 1)
        for key in ("cancel_halted", "cancel_errors", "next_cancel"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after["cancel_halted"], 1)
        self.assertEqual(self.controller.db.execute("SELECT paused FROM workers WHERE id='dot-b'").fetchone()[0], 1)

    def test_start_preserves_exhausted_cancellation_counter(self):
        self.controller.dispatch()
        self.controller.request_cancel(self.job, "MOCK stop", contact_upstream=False)
        cancels = []
        def unavailable(task_id, reason):
            cancels.append(task_id)
            raise TransportUncertain()
        self.adapter.cancel = unavailable
        for _ in range(5):
            self.controller.db.execute("UPDATE attempts SET next_cancel=0")
            self.controller.cancel_known()
        before = self.attempt_row()
        self.assertEqual(before["cancel_errors"], 5)
        self.start_one_tick()
        after = self.attempt_row()
        self.assertEqual(len(cancels), 5)
        for key in ("cancel_halted", "cancel_errors", "next_cancel"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after["cancel_halted"], 1)

    def test_crashed_send_keeps_exact_intent_and_does_not_contact_upstream(self):
        self.controller.db.execute("UPDATE dispatch_outbox SET state='SENDING',sends=1")
        self.controller.pause_worker("dot-b")
        before = self.attempt_row()
        plan = self.controller.recover_crashed_sends()
        self.assertEqual(self.attempt_row(), before)
        self.assertEqual(self.outbox()["state"], "RETRY")
        self.assertEqual(self.outbox()["sends"], 1)
        self.assertEqual(plan["plan"][0]["action"], "retry_exact_stored_request")
        self.assertEqual(self.adapter.submissions, [])
        self.assertEqual(self.controller.db.execute("SELECT paused FROM workers WHERE id='dot-b'").fetchone()[0], 1)
        self.assertEqual(self.controller.db.execute("SELECT count(*) FROM reservations").fetchone()[0], 1)
        self.controller.dispatch()
        self.assertEqual(self.adapter.submissions, [before["body"]])

    def test_start_recovers_only_existing_exact_send(self):
        before = self.attempt_row()
        # Simulate upstream accepting the POST, then losing the controller process.
        task = self.adapter.submit(json.loads(before["body"]))
        self.controller.db.execute("UPDATE dispatch_outbox SET state='SENDING',sends=1")
        self.start_one_tick()
        self.assertEqual(self.adapter.submissions, [before["body"], before["body"]])
        self.assertEqual(len(self.adapter.tasks), 1)
        self.assertEqual(self.attempt_row()["upstream_id"], task["id"])
        self.assertEqual(self.controller.db.execute("SELECT count(*) FROM attempts").fetchone()[0], 1)

    def test_fifth_interrupted_send_requires_review_without_sixth_send(self):
        self.controller.db.execute("UPDATE dispatch_outbox SET state='SENDING',sends=5")
        self.start_one_tick()
        self.assertEqual(self.outbox()["state"], "REVIEW")
        self.assertEqual(self.outbox()["sends"], 5)
        self.assertEqual(self.controller.get_job(self.job)["state"], "NEEDS_REVIEW")
        self.assertEqual(self.adapter.submissions, [])
        self.assertEqual(self.controller.db.execute("SELECT count(*) FROM reservations").fetchone()[0], 1)

    def test_cancelled_uncertain_send_is_not_retried_on_start(self):
        self.controller.db.execute("UPDATE dispatch_outbox SET state='SENDING',sends=1")
        self.controller.request_cancel(self.job, "MOCK stop", contact_upstream=False)
        before = self.outbox()
        self.start_one_tick()
        self.assertEqual(self.outbox(), before)
        self.assertEqual(self.adapter.submissions, [])
        self.assertEqual(self.controller.get_job(self.job)["state"], "CANCEL_REQUESTED")

    def test_review_submission_is_untouched(self):
        self.controller.db.execute("UPDATE dispatch_outbox SET state='REVIEW',sends=2,last_error='HTTP_403'")
        self.controller.db.execute("UPDATE attempts SET observation_halted=1,cancel_halted=1,observation_errors=3,cancel_errors=4")
        before = self.attempt_row(), self.outbox()
        self.assertEqual(self.controller.recover_crashed_sends()["plan"], [])
        self.assertEqual((self.attempt_row(), self.outbox()), before)

    def test_halted_sending_intent_is_not_silently_reopened(self):
        self.controller.db.execute("UPDATE dispatch_outbox SET state='SENDING',sends=2")
        self.controller.db.execute("UPDATE attempts SET cancel_halted=1,cancel_errors=5")
        before = self.attempt_row(), self.outbox()
        self.assertEqual(self.controller.recover_crashed_sends()["plan"], [])
        self.assertEqual((self.attempt_row(), self.outbox()), before)

    def test_explicit_operator_recovery_still_reopens_known_observation(self):
        self.controller.dispatch()
        self.controller._read_failure(self.attempt, "HTTP_429", permanent=True)
        self.assertEqual(self.attempt_row()["observation_halted"], 1)
        self.controller.recover(apply=True)
        after = self.attempt_row()
        self.assertEqual(after["observation_halted"], 0)
        self.assertEqual(after["observation_errors"], 0)
        self.assertEqual(self.controller.db.execute("SELECT paused FROM workers WHERE id='dot-b'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
