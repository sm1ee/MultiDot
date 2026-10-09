"""Generic topology tests use only local controller state and explicit MOCK data."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from multidot.cli import main
from multidot.controller import Controller
from multidot.mock import MockNative, mock_result
from multidot.models import AccessError, ConflictError, ValidationError, digest, validate_job, validate_workers


def registry(count=2, synthesis=False):
    # Stable IDs deliberately have no relationship to the Unicode display names.
    ids = ("w-8e92", "w-177f", "w-b6ad", "w-429c")
    names = ("검토 담당 🦊", "Résumé / Reviewer #2", "تحليل", "🧪 Alex")
    workers = [{"id": ids[i], "name": names[i], "role": "worker"} for i in range(count)]
    if synthesis:
        workers.append({"id": "s-47ac", "name": "정리 · Synthèse ✨", "role": "synthesis"})
    return workers


def snapshot():
    return {"schema_version": "multidot.snapshot.v1", "project_id": "generic", "snapshot_id": "input-v1",
            "artifacts": [{"name": "brief.md", "media_type": "text/markdown", "content": "MOCK materials."}]}


def job_spec(workers, request_id="request-1"):
    spec = {"schema_version": "multidot.job.v1", "project_id": "generic", "request_id": request_id,
            "title": "Registry routing", "goal": "Review only supplied material", "input_snapshot_id": "input-v1",
            "policy_id": "provided-materials-only", "steps": [
                {"step_id": f"review-{i}", "worker": worker["id"], "instructions": "Review this material",
                 "acceptance_criteria": ["Cite evidence"], "depends_on": []}
                for i, worker in enumerate(workers) if worker["role"] == "worker"]}
    synthesis = next((worker for worker in workers if worker["role"] == "synthesis"), None)
    if synthesis:
        spec["synthesis"] = {"worker": synthesis["id"], "trigger": "all_required_steps_accepted",
                             "instructions": "Compare the exact accepted findings"}
    return spec


class GenericWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.serial = 0

    def controller(self, workers, path=None, adapter=None):
        self.serial += 1
        controller = Controller(path or self.root / f"hub-{self.serial}.sqlite", adapter or MockNative(),
                                ["generic"], workers=workers)
        self.addCleanup(controller.close)
        controller.bootstrap("generic", "Generic registry tests")
        controller.add_snapshot(snapshot())
        return controller

    def complete(self, controller, task_id):
        # Supply a simulated public upstream observation without claiming live
        # worker authentication or deriving any principal from a display name.
        task = controller.adapter.tasks[task_id]
        task.update(status="completed", attempts=1, revision=task["revision"] + 1,
                    result=mock_result(task), lease_expires_at=None)

    def test_one_two_four_worker_topologies_and_unicode_display_names(self):
        for count in (1, 2, 4):
            with self.subTest(count=count):
                workers = registry(count)
                controller = self.controller(workers)
                spec = job_spec(workers)
                job = controller.submit_job(spec)["job_id"]
                controller.tick()
                self.assertEqual({task["queue"] for task in controller.adapter.tasks.values()},
                                 {worker["id"] for worker in workers})
                self.assertEqual(len(controller.adapter.tasks), count)
                self.assertEqual(controller.submit_job(deepcopy(spec)), {"job_id": job, "duplicate": True})
                listed = controller.list_workers()
                self.assertEqual([(worker["id"], worker["name"], worker["role"]) for worker in listed],
                                 [(worker["id"], worker["name"], worker["role"]) for worker in workers])
                self.assertTrue(all(worker["queue"] == worker["id"] for worker in listed))
                self.assertTrue(all(worker["active_reservations"] == 1 for worker in listed))
                for task_id in tuple(controller.adapter.tasks):
                    self.complete(controller, task_id)
                controller.tick()
                self.assertEqual(controller.get_job(job)["state"], "COMPLETED")
                self.assertTrue(all(worker["active_reservations"] == 0 for worker in controller.list_workers()))

    def test_generic_synthesis_uses_exact_target_and_runs_once(self):
        workers = registry(2, synthesis=True)
        controller = self.controller(workers)
        job = controller.submit_job(job_spec(workers))["job_id"]
        controller.tick()
        self.assertEqual(len(controller.adapter.tasks), 2)
        for task_id in tuple(controller.adapter.tasks):
            self.complete(controller, task_id)
        controller.tick()
        synth = next(task for task in controller.adapter.tasks.values() if task["queue"] == "s-47ac")
        self.assertEqual(synth["payload"]["kind"], "synthesis")
        self.assertEqual(len(synth["payload"]["dependency_results"]), 2)
        for dependency in synth["payload"]["dependency_results"]:
            self.assertEqual(dependency["result_hash"], digest(dependency["result"]))
        step = controller.db.execute("SELECT worker,spec FROM steps WHERE kind='synthesis'").fetchone()
        self.assertEqual(step["worker"], "s-47ac")
        self.assertEqual(json.loads(step["spec"])["worker"], "s-47ac")
        self.complete(controller, synth["id"])
        for _ in range(3):
            controller.tick()
        self.assertEqual(controller.get_job(job)["state"], "COMPLETED")
        self.assertEqual(len(controller.adapter.tasks), 3)
        self.assertNotIn("dot-a", {task["queue"] for task in controller.adapter.tasks.values()})

    def test_no_synthesis_role_is_required(self):
        workers = registry(1)
        controller = self.controller(workers)
        self.assertEqual(validate_job(job_spec(workers), workers)["steps"][0]["worker"], workers[0]["id"])
        spec = job_spec(workers)
        spec["synthesis"] = {"worker": "s-47ac", "trigger": "all_required_steps_accepted", "instructions": "Compare"}
        with self.assertRaises(ValidationError):
            controller.submit_job(spec)

    def test_familiar_names_and_ids_do_not_imply_legacy_roles(self):
        workers = [{"id": "dot-a", "name": "dot-c", "role": "worker"},
                   {"id": "dot-b", "name": "dot-a", "role": "synthesis"}]
        controller = self.controller(workers)
        job = controller.submit_job(job_spec(workers))["job_id"]
        controller.tick()
        analysis = next(iter(controller.adapter.tasks.values()))
        self.assertEqual((analysis["queue"], analysis["payload"]["kind"]), ("dot-a", "analysis"))
        self.complete(controller, analysis["id"])
        controller.tick()
        synth = next(task for task in controller.adapter.tasks.values() if task["queue"] == "dot-b")
        self.assertEqual(synth["payload"]["kind"], "synthesis")
        self.complete(controller, synth["id"])
        controller.tick()
        self.assertEqual(controller.get_job(job)["state"], "COMPLETED")

    def test_unregistered_or_wrong_role_targets_are_rejected(self):
        workers = registry(1, synthesis=True)
        controller = self.controller(workers)
        for target in ("missing", "dot-b", "s-47ac", workers[0]["name"], [workers[0]["id"]]):
            with self.subTest(analysis_target=target):
                spec = job_spec(workers)
                spec["steps"][0]["worker"] = target
                with self.assertRaises(ValidationError):
                    controller.submit_job(spec)
        for target in ("missing", "dot-a", workers[0]["id"], workers[-1]["name"], ["s-47ac"]):
            with self.subTest(synthesis_target=target):
                spec = job_spec(workers)
                spec["synthesis"]["worker"] = target
                with self.assertRaises(ValidationError):
                    controller.submit_job(spec)
        self.assertEqual(controller.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_registry_validation_and_independent_names(self):
        invalid = [[], {}, "workers", [None], [{"id": "id", "name": "Name"}],
                   [{"id": "bad id", "name": "Name", "role": "worker"}],
                   [{"id": "id", "name": " ", "role": "worker"}],
                   [{"id": "id", "name": "Name", "role": "other"}],
                   [{"id": "id", "name": "Name", "role": ["worker"]}],
                   [{"id": "id", "name": "Name", "role": "synthesis"}],
                   [{"id": "id", "name": "Name", "role": "worker", "queue": "other"}],
                   registry(1) * 2,
                   registry(1, True) + [{"id": "s-other", "name": "Another", "role": "synthesis"}]]
        for workers in invalid:
            with self.subTest(workers=workers), self.assertRaises(ValidationError):
                validate_workers(workers)
        workers = registry(2)
        workers[1]["name"] = workers[0]["name"]
        self.assertEqual(len(validate_workers(workers)), 2)
        copied = validate_workers(workers)
        workers[0]["name"] = "Changed by caller"
        self.assertNotEqual(copied[0]["name"], workers[0]["name"])

    def test_bad_registry_is_rejected_before_database_creation(self):
        path = self.root / "not-created.sqlite"
        with self.assertRaises(ValidationError):
            Controller(path, MockNative(), ["generic"], workers=[])
        self.assertFalse(path.exists())

    def test_reservation_is_per_stable_id_across_jobs_and_steps(self):
        workers = registry(1)
        controller = self.controller(workers)
        first = job_spec(workers)
        duplicate_step = deepcopy(first["steps"][0])
        duplicate_step["step_id"] = "second-review"
        first["steps"].append(duplicate_step)
        controller.submit_job(first)
        controller.submit_job(job_spec(workers, request_id="request-2"))
        for _ in range(3):
            controller.tick()
        self.assertEqual(len(controller.adapter.tasks), 1)
        self.assertEqual(controller.db.execute("SELECT count(*) FROM reservations").fetchone()[0], 1)
        for expected in (2, 3):
            latest = next(task for task in controller.adapter.tasks.values() if task["status"] == "queued")
            self.complete(controller, latest["id"])
            controller.tick()
            self.assertEqual(len(controller.adapter.tasks), expected)
            self.assertEqual(controller.db.execute("SELECT count(*) FROM reservations").fetchone()[0], 1)

    def test_rename_preserves_identity_pauses_and_exact_crash_recovery(self):
        workers = registry(1)
        path, adapter = self.root / "renamed.sqlite", MockNative()
        controller = self.controller(workers, path, adapter)
        spec = job_spec(workers)
        job = controller.submit_job(spec)["job_id"]
        attempt_id = controller.schedule()[0]
        original = dict(controller.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone())
        adapter.submit(json.loads(original["body"]))  # MOCK durable submit before process crash.
        controller.db.execute("UPDATE dispatch_outbox SET state='SENDING',sends=1")
        controller.pause_worker(workers[0]["id"])
        controller.close()
        renamed = deepcopy(workers)
        renamed[0]["name"] = "새 이름 🌈 / Renamed"
        controller = self.controller(renamed, path, adapter)
        self.assertEqual(dict(controller.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()), original)
        listed = controller.list_workers()[0]
        self.assertEqual((listed["id"], listed["queue"], listed["name"], listed["paused"]),
                         (workers[0]["id"], workers[0]["id"], renamed[0]["name"], 1))
        self.assertEqual(controller.submit_job(spec), {"job_id": job, "duplicate": True})
        self.assertEqual(controller.recover_crashed_sends()["plan"],
                         [{"attempt_id": attempt_id, "action": "retry_exact_stored_request"}])
        controller.dispatch()
        self.assertEqual(len(adapter.tasks), 1)
        self.assertEqual(adapter.submissions, [original["body"], original["body"]])
        recovered = controller.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        for key in ("id", "worker", "producer", "target", "request_key", "body", "body_hash"):
            self.assertEqual(recovered[key], original[key])
        self.assertEqual(controller.db.execute("SELECT count(*) FROM reservations").fetchone()[0], 1)

    def test_pause_uses_registry_and_preserves_queue(self):
        workers = registry(1)
        controller = self.controller(workers)
        worker = workers[0]
        controller.pause_worker(worker["id"])
        controller.submit_job(job_spec(workers))
        self.assertEqual(controller.schedule(), [])
        for target in (worker["name"], "dot-b", "missing"):
            with self.assertRaises(ValidationError):
                controller.pause_worker(target)
        controller.pause_worker(worker["id"], False)
        self.assertEqual(len(controller.schedule()), 1)
        self.assertEqual(controller.list_workers()[0]["queue"], worker["id"])

    def test_project_authorization_includes_custom_synthesis(self):
        workers = registry(1, synthesis=True)
        for worker in workers:
            with self.subTest(worker=worker["id"]):
                controller = self.controller(workers)
                controller.db.execute("DELETE FROM worker_projects WHERE worker_id=?", (worker["id"],))
                with self.assertRaises(AccessError):
                    controller.submit_job(job_spec(workers))

    def test_reconfigured_registry_cannot_schedule_or_dispatch_removed_worker(self):
        original_registry = registry(2)
        path, adapter = self.root / "removed.sqlite", MockNative()
        controller = self.controller(original_registry, path, adapter)
        controller.submit_job(job_spec(original_registry))
        controller.schedule()
        original_attempts = [dict(row) for row in controller.db.execute("SELECT * FROM attempts ORDER BY id")]
        controller.close()
        reduced = [original_registry[1]]
        controller = self.controller(reduced, path, adapter)
        controller.dispatch()
        self.assertEqual({task["queue"] for task in adapter.tasks.values()}, {reduced[0]["id"]})
        self.assertEqual({worker["id"] for worker in controller.list_workers()}, {reduced[0]["id"]})
        removed_attempt = next(row for row in original_attempts if row["worker"] == original_registry[0]["id"])
        self.assertEqual(dict(controller.db.execute("SELECT * FROM attempts WHERE id=?", (removed_attempt["id"],)).fetchone()), removed_attempt)
        self.assertEqual(controller.db.execute("SELECT count(*) FROM reservations").fetchone()[0], 2)
        with self.assertRaises(ValidationError):
            controller.pause_worker(original_registry[0]["id"])

    def test_role_change_does_not_reinterpret_pending_intent(self):
        original_registry = registry(2)
        path, adapter = self.root / "role-change.sqlite", MockNative()
        controller = self.controller(original_registry, path, adapter)
        controller.submit_job(job_spec(original_registry))
        controller.schedule()
        changed_id = original_registry[0]["id"]
        original = dict(controller.db.execute("SELECT * FROM attempts WHERE worker=?", (changed_id,)).fetchone())
        controller.close()
        changed_registry = deepcopy(original_registry)
        changed_registry[0]["role"] = "synthesis"
        controller = self.controller(changed_registry, path, adapter)
        controller.dispatch()
        self.assertEqual({task["queue"] for task in adapter.tasks.values()}, {original_registry[1]["id"]})
        self.assertEqual(dict(controller.db.execute("SELECT * FROM attempts WHERE worker=?", (changed_id,)).fetchone()), original)
        self.assertEqual(controller.db.execute("SELECT state,sends FROM dispatch_outbox WHERE attempt_id=?",
                                              (original["id"],)).fetchone()[:], ("PENDING", 0))

    def test_scheduling_and_dispatch_recheck_project_authorization(self):
        workers = registry(1)
        controller = self.controller(workers)
        controller.submit_job(job_spec(workers))
        controller.db.execute("DELETE FROM worker_projects")
        self.assertEqual(controller.schedule(), [])
        controller.bootstrap("generic", "Generic registry tests")
        self.assertEqual(len(controller.schedule()), 1)
        controller.db.execute("DELETE FROM worker_projects")
        self.assertEqual(controller.dispatch(), [])
        self.assertEqual(len(controller.adapter.tasks), 0)

    def test_bootstrap_does_not_retarget_existing_queue(self):
        workers = registry(1)
        controller = self.controller(workers)
        controller.db.execute("UPDATE workers SET queue='unexpected-queue'")
        with self.assertRaises(ConflictError):
            controller.bootstrap("generic", "Generic registry tests")
        controller.submit_job(job_spec(workers))
        self.assertEqual(controller.schedule(), [])
        self.assertEqual(controller.db.execute("SELECT queue FROM workers").fetchone()[0], "unexpected-queue")

    def test_legacy_registry_is_only_the_omitted_config_default(self):
        legacy = validate_workers()
        self.assertEqual([(worker["id"], worker["role"]) for worker in legacy],
                         [("dot-b", "worker"), ("dot-c", "worker"), ("dot-a", "synthesis")])
        spec = job_spec(registry(1))
        spec["steps"][0]["worker"] = "dot-b"
        validate_job(spec)
        with self.assertRaises(ValidationError):
            validate_job(spec, registry(1))

    def test_neutral_actor_for_generic_registry_and_legacy_actor_compatibility(self):
        generic = self.controller(registry(1))
        self.assertEqual(generic.actor, "controller")
        legacy = Controller(self.root / "legacy-actor.sqlite", MockNative(), ["generic"])
        self.addCleanup(legacy.close)
        self.assertEqual(legacy.actor, "controller-a")
        explicit = Controller(self.root / "explicit-actor.sqlite", MockNative(), ["generic"],
                              actor="operator-42", workers=registry(1))
        self.addCleanup(explicit.close)
        self.assertEqual(explicit.actor, "operator-42")

    def test_cli_forwards_explicit_registry_and_keeps_legacy_config(self):
        for workers in (registry(4, synthesis=True), None):
            with self.subTest(workers=workers):
                self.serial += 1
                config_path = self.root / f"controller-{self.serial}.json"
                db_path = self.root / f"cli-{self.serial}.sqlite"
                config = {"mode": "native", "base_url": "http://127.0.0.1:9", "producer": "mock-producer",
                          "token_env": "MULTIDOT_GENERIC_TEST_ONLY", "projects": ["generic"]}
                if workers is not None:
                    config["workers"] = workers
                config_path.write_text(json.dumps(config), encoding="utf-8")
                common = ["--config", str(config_path), "--db", str(db_path)]
                with patch.dict(os.environ, {}, clear=True):
                    for args in (("bootstrap", "--project", "generic", "--goal", "CLI registry"), ("workers",)):
                        output, error = StringIO(), StringIO()
                        with redirect_stdout(output), redirect_stderr(error):
                            code = main(common + list(args))
                        self.assertEqual(code, 0, error.getvalue())
                    listed = json.loads(output.getvalue())
                expected = validate_workers(workers)
                self.assertEqual([(worker["id"], worker["name"], worker["role"]) for worker in listed],
                                 [(worker["id"], worker["name"], worker["role"]) for worker in expected])


if __name__ == "__main__":
    unittest.main()
