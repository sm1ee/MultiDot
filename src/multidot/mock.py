"""Explicit MOCK Native Task double. It does not execute dot2api or contact any Dot."""
from __future__ import annotations

from copy import deepcopy
import time

from .dot2api_adapter import TransportUncertain, UpstreamRejected
from .models import canonical, digest, now


class MockNative:
    producer = "hub-producer"
    target = "mock://native-task-api"
    token = ""

    def __init__(self):
        self.tasks = {}
        self.keys = {}
        self.leases = {}
        self.submissions = []
        self.lose_next_response = False
        self.reject_status = None
        self.disconnect = False

    def submit(self, body, principal="hub-producer"):
        if principal != self.producer:
            raise UpstreamRejected(403)
        if self.reject_status:
            raise UpstreamRejected(self.reject_status)
        self.submissions.append(canonical(body))
        key, hash_ = body["idempotency_key"], digest(body)
        if key in self.keys:
            task_id, previous = self.keys[key]
            if previous != hash_:
                raise UpstreamRejected(409)
            return deepcopy(self.tasks[task_id])
        task_id = f"task-mock-{len(self.tasks) + 1}"
        at = now()
        task = {"id": task_id, "sequence": len(self.tasks) + 1, "queue": body["queue"], "instructions": body["instructions"],
                "payload": deepcopy(body["payload"]), "status": "queued", "result": None, "error": None, "attempts": 0,
                "max_attempts": body["max_attempts"], "revision": 1, "created_at": at, "updated_at": at,
                "expires_at": at, "available_at": at, "lease_expires_at": None}
        self.tasks[task_id] = task
        self.keys[key] = (task_id, hash_)
        if self.lose_next_response:
            self.lose_next_response = False
            raise TransportUncertain("MOCK response lost after durable submit")
        return deepcopy(task)

    def get(self, task_id, principal="hub-producer"):
        if self.disconnect:
            raise TransportUncertain("MOCK disconnect")
        if task_id not in self.tasks:
            raise UpstreamRejected(404)
        task = self.tasks[task_id]
        if principal != self.producer and principal != "worker-" + task["queue"][-1]:
            raise UpstreamRejected(404)
        return deepcopy(task)

    def claim(self, task_id, principal):
        task = self.get(task_id, principal)
        if principal == self.producer:
            raise UpstreamRejected(403)
        if task["status"] != "queued":
            raise UpstreamRejected(409)
        task.update(status="running", attempts=1, revision=task["revision"] + 1, lease_expires_at=now())
        self.tasks[task_id] = task
        lease = "MOCK-lease-" + task_id
        self.leases[task_id] = (principal, lease)
        return {"task": deepcopy(task), "lease_token": lease}

    def heartbeat(self, task_id, principal, lease):
        if self.leases.get(task_id) != (principal, lease) or self.tasks[task_id]["status"] != "running":
            raise UpstreamRejected(409)
        self.tasks[task_id]["lease_expires_at"] = now()
        return self.get(task_id, principal)

    def complete(self, task_id, principal, lease, result):
        task = self.get(task_id, principal)
        if self.leases.get(task_id) != (principal, lease):
            raise UpstreamRejected(409)
        if task["status"] == "completed" and task["result"] == result:
            return task
        if task["status"] != "running":
            raise UpstreamRejected(409)
        task.update(status="completed", revision=task["revision"] + 1, result=deepcopy(result), lease_expires_at=None)
        self.tasks[task_id] = task
        return deepcopy(task)

    def expire_lease(self, task_id):
        task = self.tasks[task_id]
        task.update(status="failed", revision=task["revision"] + 1, error="MOCK lease expired", lease_expires_at=None)
        self.leases.pop(task_id, None)

    def cancel(self, task_id, reason):
        task = self.get(task_id)
        if task["status"] in ("completed", "failed", "expired"):
            raise UpstreamRejected(409)
        if task["status"] != "cancelled":
            task.update(status="cancelled", revision=task["revision"] + 1, lease_expires_at=None)
            self.tasks[task_id] = task
        return deepcopy(task)


def mock_result(task, summary="MOCK analysis of provided material", outcome="completed"):
    payload = task["payload"]
    return {"schema_version": "multidot.result.v1", "job_id": payload["job_id"], "step_id": payload["step_id"],
            "input_snapshot_id": payload["input_snapshot_id"], "outcome": outcome, "summary": summary,
            "findings": [{"claim": "MOCK fixture only; no real Dot ran", "evidence_ref": "brief.md"}],
            "artifacts": [{"name": name, "media_type": "text/markdown", "content": summary} for name in payload["required_artifacts"]],
            "checks": [{"name": "MOCK envelope fixture", "status": "passed", "evidence_ref": "brief.md"}],
            "open_questions": ["Real Dot execution and authentication are unverified."], "external_changes": []}


def complete_mock_task(adapter, task_id):
    task = adapter.get(task_id)
    principal = "worker-" + task["queue"][-1]
    lease = adapter.claim(task_id, principal)["lease_token"]
    return adapter.complete(task_id, principal, lease, mock_result(task))
