"""LOCAL_CONTRACT against pinned actual dot2api; no real Dot/account executes."""
import json
import unittest

from multidot.controller import Controller
from multidot.dot2api_adapter import NativeTaskAdapter, ProtocolError, TransportUncertain, UpstreamRejected
from multidot.models import ValidationError, canonical, digest
from native_fixture import NativeFixture


class NativeContractTests(unittest.TestCase):
    def setUp(self):
        self.native = NativeFixture()
        self.addCleanup(self.native.close)
        self.adapter = self.native.adapter()

    def body(self, key="contract-1", **updates):
        body = {"queue": "dot-b", "instructions": "Review only the supplied synthetic material.",
                "payload": {"text": "LOCAL_CONTRACT \uc790\ub8cc"}, "idempotency_key": key,
                "max_attempts": 1, "ttl_seconds": 3600}
        body.update(updates)
        return body

    def claim(self, task, principal="contract-worker-b", lease_seconds=30):
        status, response = self.native.tool("claim_task", {"task_id": task["id"], "lease_seconds": lease_seconds}, principal)
        self.assertEqual(status, 200)
        return response

    def complete(self, task, lease, result, principal="contract-worker-b"):
        return self.native.tool("complete_task", {"task_id": task["id"], "lease_token": lease,
                                                  "result": result}, principal)

    def list_tasks(self, principal="contract-producer", **updates):
        return self.native.tool("list_tasks", {"queue": "dot-b", **updates}, principal)

    def test_create_get_native_envelope_and_identity_omission(self):
        body = self.body()
        task = self.adapter.submit(body)
        self.assertEqual(task, self.adapter.get(task["id"]))
        self.assertEqual(task["payload"], body["payload"])
        self.assertEqual((task["status"], task["attempts"], task["max_attempts"]), ("queued", 0, 1))
        self.assertTrue({"sequence", "revision", "created_at", "lease_expires_at"} <= task.keys())
        self.assertTrue({"creator", "tenant", "worker", "principal", "lease_token", "lease_hash"}.isdisjoint(task))

    def test_list_native_tool_uses_sequence_pagination(self):
        expected = [self.adapter.submit(self.body(f"page-{i}"))["id"] for i in range(5)]
        seen, cursor = [], 0
        for _ in range(4):
            status, page = self.list_tasks(limit=2, after=cursor)
            self.assertEqual(status, 200)
            seen.extend(task["id"] for task in page["tasks"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(seen, expected)
        self.adapter.cancel(expected[0], "Fixture cancellation")
        status, after_last = self.list_tasks(after=5)
        self.assertEqual((status, after_last["tasks"]), (200, []))
        self.assertEqual(self.list_tasks()[1]["tasks"][0]["status"], "cancelled")

    def test_same_key_body_reuses_task_and_changed_body_conflicts(self):
        body = self.body()
        first = self.adapter.submit(body)
        self.assertEqual(self.adapter.submit(dict(reversed(list(body.items()))))["id"], first["id"])
        for change in ({"payload": {"changed": True}}, {"queue": "dot-c"}, {"ttl_seconds": 7200}):
            with self.assertRaises(UpstreamRejected) as error:
                self.adapter.submit({**body, **change})
            self.assertEqual(error.exception.status, 409)
        self.assertEqual(len(self.list_tasks()[1]["tasks"]), 1)

    def test_idempotency_is_bound_to_verified_tenant_and_creator(self):
        bindings = {x["principal"]: x for x in self.native.identity_bindings}
        self.assertEqual(bindings["contract-producer"]["tenant"], "contract-tenant")
        self.assertEqual(bindings["contract-peer"]["tenant"], "contract-tenant")
        first = self.adapter.submit(self.body())
        peer = self.native.adapter("contract-peer").submit(self.body())
        outsider = self.native.adapter("contract-outsider").submit(self.body())
        self.assertEqual(len({first["id"], peer["id"], outsider["id"]}), 3)
        self.assertEqual([t["id"] for t in self.list_tasks()[1]["tasks"]], [first["id"]])

    def test_queue_tenant_creator_acl_and_scope_rejections(self):
        task = self.adapter.submit(self.body())
        for principal in ("contract-peer", "contract-worker-c", "contract-outsider"):
            with self.subTest(principal=principal):
                self.assertEqual(self.native.http("GET", "/v1/tasks/" + task["id"], principal=principal)[0], 404)
                status, _ = self.native.tool("claim_task", {"task_id": task["id"]}, principal)
                self.assertIn(status, (403, 404))
        self.assertEqual(self.list_tasks("contract-worker-c")[0], 403)
        self.assertEqual(self.list_tasks("contract-peer")[1]["tasks"], [])
        self.assertEqual(self.list_tasks("contract-outsider")[1]["tasks"], [])
        self.assertEqual(self.native.http("POST", "/v1/tasks", self.body(), "contract-worker-b")[0], 403)
        self.assertEqual(self.native.tool("claim_task", {"task_id": task["id"]})[0], 403)
        self.assertEqual(self.native.http("POST", "/v1/tasks", self.body(queue="dot-c"), "contract-outsider")[0], 403)
        for queue in ("dot-c", "dot-a"):
            other = self.adapter.submit(self.body(key="isolation-" + queue, queue=queue))
            with self.subTest(denied_queue=queue):
                self.assertEqual(self.native.http("GET", "/v1/tasks/" + other["id"], principal="contract-worker-b")[0], 404)
                self.assertEqual(self.native.tool("claim_task", {"task_id": other["id"]}, "contract-worker-b")[0], 404)

    def test_cancel_idempotent_and_fences_leased_completion(self):
        task = self.adapter.submit(self.body())
        claim = self.claim(task)
        cancelled = self.adapter.cancel(task["id"], "LOCAL_CONTRACT cancellation")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.adapter.cancel(task["id"], "Repeated fixture cancellation"), cancelled)
        self.assertEqual(self.complete(task, claim["lease_token"], {"synthetic": True})[0], 409)
        self.assertEqual(self.native.tool("claim_task", {"task_id": task["id"]}, "contract-worker-b")[0], 409)

    def test_complete_same_principal_lease_result_is_idempotent(self):
        task = self.adapter.submit(self.body())
        claim = self.claim(task)
        status, result = self.complete(task, claim["lease_token"], {"synthetic": "\uc644\ub8cc"})
        self.assertEqual((status, result["task"]["status"]), (200, "completed"))
        self.assertEqual(self.complete(task, claim["lease_token"], {"synthetic": "\uc644\ub8cc"}), (status, result))
        self.assertEqual(self.complete(task, claim["lease_token"], {"synthetic": "changed"})[0], 409)
        self.assertEqual(self.complete(task, claim["lease_token"], {"synthetic": "\uc644\ub8cc"}, "contract-worker-b-peer")[0], 409)
        with self.assertRaises(UpstreamRejected) as error:
            self.adapter.cancel(task["id"], "Too late")
        self.assertEqual(error.exception.status, 409)

    def test_max_attempts_one_lease_boundary_with_injected_clock(self):
        task = self.adapter.submit(self.body())
        claim = self.claim(task)
        self.native.advance(29.999)
        self.assertEqual(self.adapter.get(task["id"])["status"], "running")
        self.native.advance(0.001)
        expired = self.adapter.get(task["id"])
        self.assertEqual((expired["status"], expired["attempts"], expired["error"]), ("failed", 1, "Lease expired"))
        self.assertEqual(self.complete(task, claim["lease_token"], {"synthetic": True})[0], 409)
        self.assertEqual(self.native.tool("claim_task", {"task_id": task["id"]}, "contract-worker-b-peer")[0], 409)
        self.assertEqual(len(self.list_tasks()[1]["tasks"]), 1)

    def test_heartbeat_same_revision_capped_by_task_deadline(self):
        task = self.adapter.submit(self.body(ttl_seconds=60))
        claim = self.claim(task)
        self.native.advance(20)
        status, heartbeat = self.native.tool("heartbeat_task", {"task_id": task["id"],
            "lease_token": claim["lease_token"], "lease_seconds": 3600}, "contract-worker-b")
        self.assertEqual(status, 200)
        self.assertEqual(heartbeat["task"]["revision"], claim["task"]["revision"])
        self.assertEqual(heartbeat["task"]["lease_expires_at"], task["expires_at"])
        self.native.advance(40)
        self.assertEqual(self.adapter.get(task["id"])["status"], "expired")
        self.assertEqual(self.complete(task, claim["lease_token"], {})[0], 409)

    def test_release_cannot_retry_with_one_attempt(self):
        task = self.adapter.submit(self.body())
        claim = self.claim(task)
        status, response = self.native.tool("release_task", {"task_id": task["id"],
            "lease_token": claim["lease_token"], "error": "Synthetic failure", "retry": True}, "contract-worker-b")
        self.assertEqual((status, response["task"]["status"], response["task"]["attempts"]), (200, "failed", 1))

    def test_validation_422_is_sanitized_and_adapter_preserves_status(self):
        for update in ({"extra": "PRIVATE_SYNTHETIC_TEXT"}, {"max_attempts": True}, {"ttl_seconds": 59}):
            status, raw, _ = self.native.http("POST", "/v1/tasks", self.body(**update))
            self.assertEqual(status, 422)
            self.assertNotIn(b"PRIVATE_SYNTHETIC_TEXT", raw)
            with self.assertRaises(UpstreamRejected) as error:
                self.adapter.submit(self.body(**update))
            self.assertEqual(error.exception.status, 422)
            self.assertEqual(str(error.exception), "Upstream HTTP 422; operator review required")

    def test_ascii_normalized_size_exact_native_boundary(self):
        body = self.body(payload={"text": ""})
        overhead = len(json.dumps(body, allow_nan=False).encode())
        body["payload"]["text"] = "x" * (131072 - overhead)
        self.assertEqual(len(json.dumps(body).encode()), 131072)
        self.assertEqual(self.native.http("POST", "/v1/tasks", body)[0], 200)
        body["payload"]["text"] += "x"
        self.assertEqual(self.native.http("POST", "/v1/tasks", body)[0], 422)

    def test_korean_ascii_escaping_and_adapter_safe_size_boundary(self):
        body = self.body(payload={"text": "\ud55c" * 22000})
        self.assertLess(len(canonical(body).encode()), 120000)
        self.assertGreater(len(json.dumps(body).encode()), 131072)
        self.assertEqual(self.native.http("POST", "/v1/tasks", body)[0], 422)
        with self.assertRaises(ValidationError):
            self.adapter.submit(body)
        safe = self.body(key="safe-size", payload={"text": ""})
        safe["payload"]["text"] = "x" * (120000 - len(json.dumps(safe).encode()))
        self.assertEqual(self.adapter.submit(safe)["payload"], safe["payload"])
        safe["payload"]["text"] += "x"
        with self.assertRaises(ValidationError):
            self.adapter.submit(safe)

    def test_actual_empty_413_guard_and_adapter_mapping(self):
        status, raw, _ = self.native.http("POST", "/v1/tasks", raw=b"x" * 262145)
        self.assertEqual((status, raw), (413, b""))
        smaller = NativeFixture(max_body_bytes=1024)
        self.addCleanup(smaller.close)
        with self.assertRaises(UpstreamRejected) as error:
            smaller.adapter().submit(self.body(payload={"text": "x" * 2048}))
        self.assertEqual(error.exception.status, 413)

    def test_actual_auth_missing_and_unknown_task_errors(self):
        self.assertEqual(self.native.http("GET", "/v1/capabilities", principal=None)[0], 401)
        invalid = NativeTaskAdapter(self.native.base_url, "INVALID_LOCAL_CONTRACT_PLACEHOLDER")
        with self.assertRaises(UpstreamRejected) as error:
            invalid.get("task-not-present")
        self.assertEqual(error.exception.status, 401)
        with self.assertRaises(UpstreamRejected) as error:
            self.adapter.get("task-not-present")
        self.assertEqual(error.exception.status, 404)

    def test_fault_injected_uncertain_read_and_recovery(self):
        task = self.adapter.submit(self.body())
        self.native.fault("GET", "/v1/tasks/" + task["id"], "timeout")
        with self.assertRaises(TransportUncertain):
            self.native.adapter(timeout=0.03).get(task["id"])
        self.assertEqual(self.adapter.get(task["id"]), task)

    def test_fault_injected_response_errors_fail_closed(self):
        task = self.adapter.submit(self.body())
        for mode in ("invalid_json", "oversized_response"):
            self.native.fault("GET", "/v1/tasks/" + task["id"], mode)
            with self.assertRaises(ProtocolError):
                self.adapter.get(task["id"])
        self.native.fault("GET", "/v1/tasks/" + task["id"], "503_after_commit")
        with self.assertRaises(TransportUncertain):
            self.adapter.get(task["id"])
        self.assertEqual(self.adapter.get(task["id"]), task)

    def controller(self):
        controller = Controller(self.native.root / "controller.sqlite", self.adapter, ["contract"])
        self.addCleanup(controller.close)
        controller.bootstrap("contract", "Synthetic contract fixture")
        snapshot = {"schema_version": "multidot.snapshot.v1", "project_id": "contract", "snapshot_id": "v1",
                    "artifacts": [{"name": "brief.md", "media_type": "text/markdown", "content": "Synthetic supplied material."}]}
        controller.add_snapshot(snapshot)
        spec = {"schema_version": "multidot.job.v1", "project_id": "contract", "request_id": "contract-job",
                "title": "Contract fixture", "goal": "Validate native transport", "input_snapshot_id": "v1",
                "policy_id": "provided-materials-only", "steps": [{"step_id": "review", "worker": "dot-b",
                "instructions": "Review the synthetic supplied material", "acceptance_criteria": ["Evidence"], "depends_on": []}]}
        job = controller.submit_job(spec)["job_id"]
        return controller, job, snapshot

    def result(self, task):
        payload = task["payload"]
        return {"schema_version": "multidot.result.v1", "job_id": payload["job_id"], "step_id": payload["step_id"],
                "input_snapshot_id": payload["input_snapshot_id"], "outcome": "completed",
                "summary": "LOCAL_CONTRACT synthetic fixture; no real Dot ran.",
                "findings": [{"claim": "Synthetic material checked", "evidence_ref": "brief.md"}],
                "artifacts": [{"name": "report.md", "media_type": "text/markdown", "content": "Synthetic fixture result."}],
                "checks": [{"name": "Native contract fixture", "status": "passed", "evidence_ref": "brief.md"}],
                "open_questions": ["Actual account identity and semantic correctness remain unverified."], "external_changes": []}

    def test_controller_lost_post_response_retries_exact_pinned_creator(self):
        controller, job, _ = self.controller()
        self.native.fault("POST", "/v1/tasks", "503_after_commit")
        controller.tick()
        attempt = controller.db.execute("SELECT * FROM attempts").fetchone()
        self.assertIsNone(attempt["upstream_id"])
        self.assertEqual(controller.list_workers()[0]["active_reservations"], 1)
        task = self.list_tasks()[1]["tasks"][0]
        stored = attempt["body"]
        self.assertEqual(self.native.app.state.security.authenticate(self.adapter.token).id, attempt["producer"])
        controller.db.execute("UPDATE dispatch_outbox SET next_try=0")
        controller.dispatch()
        current = controller.db.execute("SELECT * FROM attempts").fetchone()
        self.assertEqual((current["body"], current["upstream_id"]), (stored, task["id"]))
        self.assertEqual(len(self.list_tasks()[1]["tasks"]), 1)
        self.assertEqual(controller.get_job(job)["state"], "ACTIVE")

    def test_controller_restart_accepts_native_result_and_preserves_unverified_identity(self):
        controller, job, snapshot = self.controller()
        controller.tick()
        task = self.list_tasks()[1]["tasks"][0]
        claim = self.claim(task)
        controller.reconcile()
        controller.close()
        self.assertEqual(self.complete(task, claim["lease_token"], self.result(task))[0], 200)
        reopened = Controller(self.native.root / "controller.sqlite", self.adapter, ["contract"])
        self.addCleanup(reopened.close)
        reopened.recover(apply=True)
        result = reopened.get_job(job)
        self.assertEqual((result["state"], result["delivery"]), ("COMPLETED", "notification_pending"))
        self.assertIn("UNVERIFIED", result["claim_identity"])
        self.assertEqual(len(result["artifacts"]), 1)
        self.assertEqual(reopened.get_artifact(result["artifacts"][0]["id"])["snapshot_hash"], digest(snapshot))
        self.assertEqual(len(self.list_tasks()[1]["tasks"]), 1)

    def test_controller_uncertain_get_retains_slot_then_reconciles(self):
        controller, job, _ = self.controller()
        controller.tick()
        task = self.list_tasks()[1]["tasks"][0]
        self.native.fault("GET", "/v1/tasks/" + task["id"], "503_after_commit")
        controller.reconcile()
        self.assertEqual(controller.get_job(job)["state"], "NEEDS_REVIEW")
        self.assertEqual(controller.list_workers()[0]["active_reservations"], 1)
        controller.db.execute("UPDATE attempts SET next_observe=0")
        controller.reconcile()
        self.assertEqual(controller.get_job(job)["state"], "ACTIVE")
        self.assertEqual(len(self.list_tasks()[1]["tasks"]), 1)

    def test_controller_native_cancel_releases_slot_without_worker_stop_claim(self):
        controller, job, _ = self.controller()
        controller.tick()
        task = self.list_tasks()[1]["tasks"][0]
        claim = self.claim(task)
        controller.reconcile()
        cancelled = controller.request_cancel(job, "Synthetic cancellation")
        self.assertEqual(cancelled["state"], "CANCELLED")
        self.assertFalse(cancelled["worker_stop_confirmed"])
        self.assertEqual(controller.list_workers()[0]["active_reservations"], 0)
        self.assertEqual(self.complete(task, claim["lease_token"], self.result(task))[0], 409)
