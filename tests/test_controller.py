"""MOCK acceptance coverage. None of these tests use a real Dot or dot2api."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from multidot.controller import Controller
from multidot.dot2api_adapter import UpstreamRejected
from multidot.mock import MockNative, complete_mock_task, mock_result
from multidot.models import AccessError, ConflictError, ValidationError, canonical, digest, redact, validate_job, wire_bounded


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "hub.sqlite"
        self.up = MockNative()
        self.c = Controller(self.db, self.up, ["demo"])
        self.c.bootstrap("demo", "Test project")
        self.snapshot = {"schema_version": "multidot.snapshot.v1", "project_id": "demo", "snapshot_id": "v1", "artifacts": [{"name": "brief.md", "media_type": "text/markdown", "content": "Provided material"}]}
        self.c.add_snapshot(self.snapshot)
        self.spec = {"schema_version": "multidot.job.v1", "project_id": "demo", "request_id": "r1", "title": "Review", "goal": "Compare", "input_snapshot_id": "v1", "policy_id": "provided-materials-only", "steps": [{"step_id": "review", "worker": "dot-b", "instructions": "Review supplied material", "acceptance_criteria": ["Evidence"], "depends_on": []}]}

    def tearDown(self):
        self.c.close()
        self.temp.cleanup()

    def submit(self):
        return self.c.submit_job(self.spec)["job_id"]

    def dispatch(self):
        job = self.submit()
        self.c.tick()
        task = next(iter(self.up.tasks))
        return job, task

    def attempt(self, job):
        return self.c.db.execute("SELECT * FROM attempts WHERE job_id=?", (job,)).fetchone()

    def complete(self, job, task, mutate=None):
        principal = "worker-" + self.up.tasks[task]["queue"][-1]
        lease = self.up.claim(task, principal)["lease_token"]
        result = mock_result(self.up.get(task))
        if mutate:
            mutate(result)
        self.up.complete(task, principal, lease, result)
        self.c.reconcile()

    def test_t02_mock_b_roundtrip_and_durable_artifact(self):
        job, task = self.dispatch()
        self.complete(job, task)
        result = self.c.get_job(job)
        self.assertEqual(result["state"], "COMPLETED")
        self.assertEqual(result["delivery"], "notification_pending")
        self.assertIn("UNVERIFIED", result["claim_identity"])
        artifact = self.c.get_artifact(result["artifacts"][0]["id"])
        self.assertEqual(artifact["snapshot_hash"], digest(self.snapshot))
        self.assertEqual(artifact["content"], "MOCK analysis of provided material")

    def test_t04_mock_cross_queue_acl(self):
        job, task = self.dispatch()
        for method in (lambda: self.up.get(task, "worker-c"), lambda: self.up.claim(task, "worker-c")):
            with self.assertRaises(UpstreamRejected):
                method()

    def test_t05_mock_worker_cannot_submit_or_producer_claim(self):
        job, task = self.dispatch()
        with self.assertRaises(UpstreamRejected):
            self.up.submit(json.loads(self.attempt(job)["body"]), principal="worker-b")
        with self.assertRaises(UpstreamRejected):
            self.up.claim(task, "hub-producer")

    def test_t06_duplicate_job_and_attempt(self):
        job, task = self.dispatch()
        self.assertEqual(self.submit(), job)
        for _ in range(3):
            self.c.tick()
        self.assertEqual(len(self.up.tasks), 1)
        self.assertEqual(self.c.db.execute("SELECT count(*) FROM attempts").fetchone()[0], 1)

    def test_t07_request_key_conflict(self):
        self.submit()
        self.spec["goal"] = "Changed"
        with self.assertRaises(ConflictError):
            self.submit()

    def test_t07_upstream_key_conflict(self):
        job, task = self.dispatch()
        body = json.loads(self.attempt(job)["body"])
        body["ttl_seconds"] = 7200
        with self.assertRaises(UpstreamRejected) as context:
            self.up.submit(body)
        self.assertEqual(context.exception.status, 409)

    def test_t08_response_loss_reuses_exact_intent(self):
        self.up.lose_next_response = True
        job, task = self.dispatch()
        a = self.attempt(job)
        self.assertIsNone(a["upstream_id"])
        self.assertEqual(self.c.list_workers()[0]["active_reservations"], 1)
        self.c.db.execute("UPDATE dispatch_outbox SET next_try=0")
        self.c.dispatch()
        self.assertEqual(len(self.up.tasks), 1)
        self.assertEqual(self.up.submissions[0], self.up.submissions[1])
        self.assertEqual(self.attempt(job)["upstream_id"], task)

    def test_t08_intent_persisted_before_network(self):
        original = self.up.submit
        def inspect(body):
            row = self.c.db.execute("SELECT a.*,o.state FROM attempts a JOIN dispatch_outbox o ON o.attempt_id=a.id").fetchone()
            self.assertEqual(row["body"], canonical(body))
            self.assertEqual(row["state"], "SENDING")
            self.assertEqual(self.c.db.execute("SELECT count(*) FROM reservations").fetchone()[0], 1)
            return original(body)
        self.up.submit = inspect
        self.dispatch()

    def test_t09_t15_synthesis_dedupes_exact_results(self):
        self.spec["steps"].append({**deepcopy(self.spec["steps"][0]), "step_id": "independent", "worker": "dot-c"})
        self.spec["synthesis"] = {"worker": "dot-a", "trigger": "all_required_steps_accepted", "instructions": "Compare results"}
        job, _ = self.dispatch()
        tasks = list(self.up.tasks)
        self.complete(job, tasks[0])
        self.c.tick()
        self.assertEqual(len(self.up.tasks), 2)
        self.complete(job, tasks[1])
        for _ in range(5):
            self.c.tick()
        self.assertEqual(len(self.up.tasks), 3)
        synth = next(t for t in self.up.tasks.values() if t["queue"] == "dot-a")
        self.assertEqual(synth["payload"]["kind"], "synthesis")
        self.assertEqual(len(synth["payload"]["dependency_results"]), 2)
        for dep in synth["payload"]["dependency_results"]:
            self.assertEqual(dep["result_hash"], digest(dep["result"]))
        self.complete(job, synth["id"])
        for _ in range(3):
            self.c.tick()
        self.assertEqual(len(self.up.tasks), 3)
        self.assertEqual(self.c.get_job(job)["state"], "COMPLETED")

    def test_t10_wrong_snapshot_rejected(self):
        job, task = self.dispatch()
        self.complete(job, task, lambda r: r.update(input_snapshot_id="wrong"))
        self.assertEqual(self.c.get_job(job)["state"], "NEEDS_REVIEW")

    def test_t10_missing_required_artifact_rejected(self):
        job, task = self.dispatch()
        self.complete(job, task, lambda r: r.update(artifacts=[]))
        self.assertEqual(self.c.get_job(job)["state"], "NEEDS_REVIEW")

    def test_t10_unknown_schema_and_external_changes_rejected(self):
        for change in ({"schema_version": "wrong"}, {"external_changes": ["sent email"]}):
            self.spec["request_id"] += "x"
            job, task = self.dispatch()
            task = self.attempt(job)["upstream_id"]
            self.complete(job, task, lambda r: r.update(**change))
            self.assertEqual(self.c.get_job(job)["state"], "NEEDS_REVIEW")

    def test_t11_expired_lease_cannot_complete(self):
        job, task = self.dispatch()
        lease = self.up.claim(task, "worker-b")["lease_token"]
        self.c.reconcile()
        self.up.expire_lease(task)
        with self.assertRaises(UpstreamRejected):
            self.up.complete(task, "worker-b", lease, mock_result(self.up.tasks[task]))
        self.c.tick()
        self.assertEqual(self.c.get_job(job)["state"], "NEEDS_REVIEW")
        self.assertEqual(len(self.up.tasks), 1)

    def test_t11_heartbeat_same_revision_is_observed(self):
        job, task = self.dispatch()
        lease = self.up.claim(task, "worker-b")["lease_token"]
        self.c.reconcile()
        revision = self.attempt(job)["revision"]
        self.up.heartbeat(task, "worker-b", lease)
        self.c.reconcile()
        self.assertEqual(self.attempt(job)["revision"], revision)
        self.assertEqual(self.attempt(job)["lease_expires_at"], self.up.tasks[task]["lease_expires_at"])

    def test_t12_restart_recovers_existing_id(self):
        job, task = self.dispatch()
        self.c.close()
        self.c = Controller(self.db, self.up, ["demo"])
        complete_mock_task(self.up, task)
        self.c.recover(apply=True)
        self.assertEqual(self.c.get_job(job)["state"], "COMPLETED")
        self.assertEqual(len(self.up.tasks), 1)

    def test_t12_recover_dry_run_does_not_send(self):
        job = self.submit()
        self.c.schedule()
        self.c.db.execute("UPDATE dispatch_outbox SET state='SENDING',sends=1")
        plan = self.c.recover()
        self.assertTrue(plan["dry_run"])
        self.assertEqual(len(self.up.tasks), 0)
        self.c.recover(apply=True)
        self.c.dispatch()
        self.assertEqual(len(self.up.tasks), 1)

    def test_t13_disconnect_retains_slot_then_recovers(self):
        job, task = self.dispatch()
        self.up.disconnect = True
        self.c.tick()
        self.assertEqual(self.c.get_job(job)["state"], "NEEDS_REVIEW")
        self.assertEqual(self.c.db.execute("SELECT count(*) FROM reservations").fetchone()[0], 1)
        self.up.disconnect = False
        self.c.db.execute("UPDATE attempts SET next_observe=0")
        self.c.tick()
        self.assertEqual(self.c.get_job(job)["state"], "ACTIVE")
        self.assertEqual(len(self.up.tasks), 1)

    def test_t16_delivery_not_inferred_from_completion(self):
        job, task = self.dispatch()
        self.complete(job, task)
        self.assertEqual(self.c.get_job(job)["delivery"], "notification_pending")
        self.c.mark_presented(job, "MOCK-only-manual-reference")
        self.assertEqual(self.c.get_job(job)["delivery"], "presented")

    def test_t17_cancel_never_sent(self):
        job = self.submit()
        self.c.schedule()
        self.c.request_cancel(job, "User request")
        self.c.tick()
        self.assertEqual(len(self.up.tasks), 0)
        self.assertEqual(self.c.get_job(job)["state"], "CANCELLED")

    def test_t17_cancel_running_blocks_followup(self):
        self.spec["steps"].append({**deepcopy(self.spec["steps"][0]), "step_id": "after", "worker": "dot-c", "depends_on": ["review"]})
        job, task = self.dispatch()
        self.up.claim(task, "worker-b")
        self.c.reconcile()
        result = self.c.request_cancel(job, "User request")
        self.c.tick()
        self.assertEqual(result["state"], "CANCELLED")
        self.assertFalse(result["worker_stop_confirmed"])
        self.assertEqual(len(self.up.tasks), 1)

    def test_t17_cancel_uncertain_submission_retains_slot(self):
        self.up.lose_next_response = True
        job, task = self.dispatch()
        result = self.c.request_cancel(job, "User request")
        self.assertEqual(result["state"], "CANCEL_REQUESTED")
        self.c.db.execute("UPDATE dispatch_outbox SET next_try=0")
        self.c.tick()
        self.assertEqual(len(self.up.submissions), 1)
        self.assertEqual(self.c.db.execute("SELECT count(*) FROM reservations").fetchone()[0], 1)

    def test_t18_partial_success_preserved(self):
        self.spec["steps"].append({**deepcopy(self.spec["steps"][0]), "step_id": "other", "worker": "dot-c"})
        job, _ = self.dispatch()
        tasks = list(self.up.tasks)
        self.complete(job, tasks[0])
        self.complete(job, tasks[1], lambda r: r.update(outcome="failed"))
        result = self.c.get_job(job)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["steps"][0]["state"], "ACCEPTED")
        self.assertEqual(len(result["artifacts"]), 2)

    def test_t20_recursive_synthesis_job_rejected(self):
        self.spec["steps"][0]["worker"] = "dot-a"
        with self.assertRaises(ValidationError):
            self.submit()

    def test_t21_path_traversal_and_absolute_name_rejected(self):
        for name in ("../bad.py", "/tmp/bad", "sub/file.txt", "a\\b", "a..b"):
            snapshot = deepcopy(self.snapshot)
            snapshot["artifacts"][0]["name"] = name
            with self.assertRaises(ValidationError):
                self.c.add_snapshot(snapshot)

    def test_t21_output_path_rejected_without_file_creation(self):
        job, task = self.dispatch()
        self.complete(job, task, lambda r: r["artifacts"][0].update(name="../escape.py"))
        self.assertEqual(self.c.get_job(job)["state"], "NEEDS_REVIEW")
        self.assertEqual(self.c.get_job(job)["artifacts"], [])

    def test_t21_credentials_rejected_and_redacted(self):
        self.spec["goal"] = "Authorization: Bearer secret-do-not-log"
        with self.assertRaises(ValidationError):
            self.submit()
        self.assertNotIn("secret-do-not-log", canonical(redact({"message": self.spec["goal"]})))
        self.assertEqual(self.c.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_t22_auth_and_limit_errors_do_not_failover(self):
        for status in (401, 403, 429):
            self.spec["request_id"] += "x"
            self.up.reject_status = status
            self.c.pause_worker("dot-b", False)
            job = self.submit()
            # prior ambiguous/rejected attempts retain reservations, so use their single intent.
            if status == 401:
                self.c.tick()
                self.assertEqual(self.c.get_job(job)["state"], "NEEDS_REVIEW")
            else:
                self.c.tick()
            self.assertEqual(len(self.up.tasks), 0)
            self.assertEqual(self.c.db.execute("SELECT count(*) FROM attempts WHERE worker!='dot-b'").fetchone()[0], 0)

    def test_t23_oversized_input_rejected(self):
        self.snapshot["artifacts"][0]["content"] = "x" * 16001
        with self.assertRaises(ValidationError):
            self.c.add_snapshot(self.snapshot)

    def test_t23_upstream_escaped_unicode_limit(self):
        with self.assertRaises(ValidationError):
            wire_bounded({"payload": "한" * 25000})

    def test_t23_oversized_output_rejected(self):
        job, task = self.dispatch()
        self.complete(job, task, lambda r: r["artifacts"][0].update(content="x" * 16001))
        self.assertEqual(self.c.get_job(job)["state"], "NEEDS_REVIEW")

    def test_t24_concurrent_reservations_never_exceed_one(self):
        for i in range(12):
            self.spec["request_id"] = f"r{i}"
            self.submit()
        def schedule(_):
            controller = Controller(self.db, self.up, ["demo"])
            try:
                controller.schedule()
            finally:
                controller.close()
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(schedule, range(12)))
        self.assertEqual(self.c.db.execute("SELECT count(*) FROM reservations WHERE worker='dot-b'").fetchone()[0], 1)
        self.assertEqual(self.c.db.execute("SELECT count(*) FROM attempts").fetchone()[0], 1)

    def test_snapshot_immutability(self):
        self.snapshot["artifacts"][0]["content"] = "Changed version"
        with self.assertRaises(ConflictError):
            self.c.add_snapshot(self.snapshot)

    def test_unauthorized_project_and_unknown_fields(self):
        self.spec["project_id"] = "private"
        with self.assertRaises(AccessError):
            self.submit()
        self.spec["project_id"] = "demo"
        self.spec["shell"] = "echo no"
        with self.assertRaises(ValidationError):
            self.submit()

    def test_dependency_cycle_rejected(self):
        self.spec["steps"][0]["depends_on"] = ["review"]
        with self.assertRaises(ValidationError):
            self.submit()

    def test_stale_revision_does_not_overwrite_result(self):
        job, task = self.dispatch()
        old = self.up.get(task)
        self.complete(job, task)
        self.c.observe(self.attempt(job)["id"], old)
        self.assertEqual(self.c.get_job(job)["state"], "COMPLETED")

    def test_terminal_mutation_needs_review_preserves_result(self):
        job, task = self.dispatch()
        self.complete(job, task)
        malicious = self.up.get(task)
        malicious["revision"] += 1
        malicious["result"]["summary"] = "Replacement"
        self.c.observe(self.attempt(job)["id"], malicious)
        result = self.c.get_job(job)
        self.assertEqual(result["state"], "NEEDS_REVIEW")
        self.assertNotEqual(result["steps"][0]["result"]["summary"], "Replacement")

    def test_target_change_never_resends(self):
        self.submit()
        self.c.schedule()
        self.up.target = "mock://other-server"
        self.c.dispatch()
        self.assertEqual(len(self.up.tasks), 0)

    def test_observed_progress_never_invented(self):
        self.dispatch()
        worker = next(w for w in self.c.list_workers() if w["id"] == "dot-b")
        self.assertIsNone(worker["last_running_at"])
        self.assertIsNone(worker["progress_percent"])
        self.assertEqual(worker["account_global_load"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
