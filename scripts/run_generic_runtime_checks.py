"""Opt-in LOCAL_RUNTIME_CONTRACT checks for generic, disposable worker stacks.

Run with the already reviewed dot2api virtualenv, --upstream CHECKOUT,
--tunnel-binary BINARY, --output REPORT, and --allow-temporary-fixtures.
Only synthetic fixture identities and loopback endpoints are used. This runner
never reads a real filled config, connects a real tunnel, or starts real jobs.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from http.server import ThreadingHTTPServer
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
from threading import Thread
import time
import traceback
from unittest.mock import patch
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
# scripts/multidot.py is the operator entry point, not the multidot package.
sys.path.insert(0, str(ROOT / "src"))

from dot2api.security import Security
from dot2api.store import Store
from multidot.cli import OfflineAdapter
from multidot.controller import Controller
from multidot.dot2api_adapter import NativeTaskAdapter, NoRedirect
from multidot.models import canonical, digest
from multidot_setup import configure, read_manifest
from run_local_contract import verify
from run_runtime_checks import FakePlane, wait_for
import runtime_supervisor as runtime

NAMES = ("\uc624\ub85c\ub77c \U0001f98a", "R\u00e9sum\u00e9 / Review", "\u0645\u062d\u0644\u0644 \u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a", "\u7814\u7a76 \U0001f52c", "Shared display name")
EXPECTED_TOOLS = {"get_task", "list_tasks", "claim_task", "heartbeat_task", "complete_task", "release_task"}
FAKE_KEY = "LOCAL_RUNTIME_TEST_ONLY"


def local_http(base, method, path, header=None, data=None):
    """No proxies, redirects, remote origins, or unbounded response bodies."""
    assert re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", base)
    assert path.startswith("/") and "\r" not in path and "\n" not in path
    headers = {"Content-Type": "application/json"}
    if header:
        headers["Authorization"] = header
    request = urllib.request.Request(base + path, method=method, headers=headers,
        data=None if data is None else canonical(data).encode())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=4) as response:
            status, raw = response.status, response.read(262145)
    except urllib.error.HTTPError as error:
        try:
            status, raw = error.code, error.read(262145)
        finally:
            error.close()
    assert len(raw) <= 262144
    return status, raw


def mcp(base, header, method, params=None):
    status, raw = local_http(base, "POST", "/mcp", header,
                            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    assert status == 200
    response = json.loads(raw)
    assert "error" not in response
    return response["result"]


def tool(base, header, name, arguments):
    result = mcp(base, header, "tools/call", {"name": name, "arguments": arguments})
    assert result["isError"] is False
    return result["structuredContent"]


def safe_child_environment():
    # Do not propagate connector credentials, private config, or proxy routes.
    return {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(ROOT / "src"), "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}


class Case:
    def __init__(self, count, report):
        self.count, self.report, self.phase = count, report, "fixture_configuration"
        self.checks = []
        self.process = None
        self.config = None
        self.identities = []
        self.headers = {}
        self.tokens = []
        self.completed = 0

    def checked(self, name):
        self.checks.append(name)
        self.report["checks"].append({"profiles": self.count, "check": name, "result": "PASS"})

    def remember_children(self):
        if self.config is not None:
            self.identities.extend(row["identity"] for row in runtime.status(self.config).get("children", {}).values()
                                   if row.get("identity") and row["identity"] not in self.identities)

    def command(self, action):
        return subprocess.run([sys.executable, str(ROOT / "scripts/runtime_supervisor.py"),
            "--config", str(self.state / "config/runtime.json"), action], env=safe_child_environment(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)

    def launch(self):
        self.phase = "stack_startup"
        assert self.command("check").returncode == 0
        self.process = subprocess.Popen([sys.executable, str(ROOT / "scripts/runtime_supervisor.py"),
            "--config", str(self.state / "config/runtime.json"), "run"], env=safe_child_environment(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def ready():
            assert self.process.poll() is None
            status = runtime.status(self.config)
            self.remember_children()
            children = status.get("children", {})
            expected = runtime.component_specs(self.config)
            return (status.get("supervisor_running") and set(children) == set(expected)
                    and all(children[key].get("running") for key in expected)
                    and children["dot2api"].get("ready")
                    and all(children["ingress:" + row["id"]].get("ready") for row in self.config["workers"]))
        wait_for(ready, seconds=45)
        self.checked("actual_pinned_upstream_controller_and_all_ingress_tunnel_children_running")

    def cleanup_stack(self):
        if self.process is not None:
            self.remember_children()
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=70)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            for record in self.identities:
                if runtime.alive(record):
                    fd = os.pidfd_open(record["pid"])
                    try:
                        if runtime.alive(record):
                            signal.pidfd_send_signal(fd, signal.SIGTERM)
                    finally:
                        os.close(fd)
            wait_for(lambda: not any(runtime.alive(row) for row in self.identities), seconds=15)
            ports = [self.config["upstream_port"]]
            ports += [row[key] for row in self.config["workers"] for key in ("ingress_port", "health_port")]
            assert all(runtime.free_port(number) for number in ports)
            self.checked("owned_processes_stopped_and_all_loopback_listeners_closed")

    def run(self, args, plane):
        with tempfile.TemporaryDirectory(prefix="multidot-generic-contract-") as temporary:
            self.state = Path(temporary) / "state"
            environment = {"state_root": str(self.state), "repository": str(ROOT),
                "upstream_checkout": str(args.upstream.resolve()), "upstream_python": sys.executable,
                "tunnel_binary": str(args.tunnel_binary.resolve())}
            plane_url = f"http://127.0.0.1:{plane.server_port}"
            dots = [{"name": NAMES[index % len(NAMES)], "tunnel_id": "tunnel_" + uuid.uuid4().hex,
                     "runtime_api_key": FAKE_KEY, "role": "synthesis" if self.count in (4, 10) and index == self.count - 1 else "worker"}
                    for index in range(self.count)]
            try:
                with patch("dot2api.security.secrets.token_urlsafe", side_effect=lambda *_: "LOCAL_GENERIC_ONLY_" + uuid.uuid4().hex):
                    if self.count == 10:
                        fixture_config = Path(temporary) / "synthetic-dots.private.json"
                        fixture_config.write_text(json.dumps({"schema_version": 1, "dots": dots}), encoding="utf-8")
                        module_spec = importlib.util.spec_from_file_location("multidot_operator_fixture", ROOT / "scripts/multidot.py")
                        entry = importlib.util.module_from_spec(module_spec)
                        module_spec.loader.exec_module(entry)
                        output, errors = StringIO(), StringIO()
                        # Exercise the actual user CLI and pinned environment
                        # verification. Its production endpoint is metadata only;
                        # no generated production-mode config is ever launched.
                        with redirect_stdout(output), redirect_stderr(errors), patch.object(entry, "default_environment", return_value=environment):
                            code = entry.main(["setup", "--config", str(fixture_config), "--state-root", str(self.state)])
                        assert code == 0
                        result = json.loads(output.getvalue())
                        generated = runtime.load(self.state / "config/runtime.json")
                        assert generated["control_plane_url"] == "https://api.openai.com" and not generated["test_only"]
                        assert not runtime.read_state(generated)
                        self.checked("actual_operator_setup_cli_verifies_pinned_environment_without_start_or_external_contact")
                        # Reuse the same fake identities and replace metadata
                        # with loopback test-only endpoints before any preflight.
                        converted = configure(dots, environment, test_only=True, control_plane_url=plane_url)
                        assert converted["identities_reused"] and not converted["credentials_issued"]
                    else:
                        result = configure(dots, environment, test_only=True, control_plane_url=plane_url)
                assert result["ok"] and result["configured_dots"] == self.count and not result["runtime_started"]
                manifest = read_manifest(self.state)
                self.config = runtime.load(self.state / "config/runtime.json")
                assert self.config["test_only"] and not self.config["event_delivery_enabled"]
                assert self.config["control_plane_url"] == plane_url
                if self.count == 10:
                    # Existing-setup updates intentionally change only metadata;
                    # render the reviewed loopback metadata without launching.
                    runtime.render(self.config)
                registry = [{key: row[key] for key in ("id", "name", "role")} for row in manifest["workers"]]
                ids = [row["id"] for row in registry]
                assert len(set(ids)) == self.count and all(re.fullmatch(r"worker-[0-9a-f]{32}", value) for value in ids)
                assert [row["name"] for row in registry] == [dot["name"] for dot in dots]
                assert len(runtime.component_specs(self.config)) == 2 + 2 * self.count
                self.checked("generic_setup_unicode_names_stable_unique_ids_and_component_count")
                self.verify_fixture_security(manifest)
                for worker in self.config["workers"]:
                    rendered = json.loads(runtime.tunnel_config_path(self.config, worker["id"]).read_text())
                    assert rendered["control_plane"]["base_url"] == plane_url
                    assert rendered["control_plane"]["api_key"].startswith("file:")
                    assert rendered["mcp"]["extra_headers"]["Authorization"].startswith("file:")
                    assert rendered["mcp"]["server_urls"] == [{"channel": "main", "url": f"http://127.0.0.1:{worker['ingress_port']}/mcp"}]
                self.checked("all_rendered_routes_loopback_and_headers_file_referenced")
                if self.count == 10:
                    runtime.preflight(self.config)
                    assert not runtime.read_state(self.config)
                    self.checked("ten_profile_preflight_without_launch_or_jobs")
                else:
                    job_id = self.offline_job(registry)
                    self.launch()
                    self.phase = "mcp_authorization_contract"
                    self.verify_worker_boundaries(manifest)
                    self.phase = "controller_native_roundtrip"
                    self.finish_job(registry, job_id)
                    wanted = {dot["tunnel_id"] for dot in dots}
                    wait_for(lambda: all(any(tunnel in row["path"] for row in FakePlane.seen) for tunnel in wanted), seconds=20)
                    assert all(row["fake_authorization_correct"] for row in FakePlane.seen)
                    self.checked("every_official_tunnel_client_contacted_only_local_fake_plane")
            finally:
                self.cleanup_stack()
            self.phase = "rename_identity_preservation"
            before_manifest = deepcopy(manifest)
            secret_paths = [self.state / "secrets/producer-token"]
            secret_paths += [self.state / "secrets" / worker["id"] / key for worker in registry
                             for key in ("worker-authorization", "runtime-api-key")]
            fingerprints = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in secret_paths}
            renamed = [{**dot, "name": "\ub2e4\uc2dc \uc774\ub984 \u00b7 " + str(index), "runtime_api_key": ""} for index, dot in enumerate(dots)]
            with patch("dot2api.security.Security.issue_token", side_effect=AssertionError("Rename must not issue access")):
                rerun = configure(renamed, environment, test_only=True, control_plane_url=plane_url)
            after_manifest = read_manifest(self.state)
            assert rerun["identities_reused"] and not rerun["credentials_issued"]
            for before, after in zip(before_manifest["workers"], after_manifest["workers"]):
                assert {key: value for key, value in before.items() if key != "name"} == {key: value for key, value in after.items() if key != "name"}
            assert fingerprints == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in secret_paths}
            self.checked("display_rename_preserves_ids_queues_roles_ports_and_fixture_tokens")
            log_text = "\n".join(path.read_text() for path in (self.state / "logs").glob("*") if path.is_file())
            assert FAKE_KEY not in log_text and not any(token in log_text for token in self.tokens)
            self.checked("lifecycle_logs_do_not_contain_fixture_credentials")
            self.tokens.clear()
            self.headers.clear()
        assert not Path(temporary).exists()
        self.checked("disposable_fixture_tree_removed")
        self.report["cases"].append({"profiles": self.count, "runtime_executed": self.count != 10,
            "native_controller_tasks_completed": self.completed, "checks_passed": len(self.checks), "cleanup_verified": True})

    def verify_fixture_security(self, manifest):
        # Fixture setup only: use reviewed Security APIs, never raw upstream SQL.
        security = Security(Store(self.state / "upstream/dot2api.sqlite3"),
                            (self.state / "upstream/encryption.key").read_bytes().strip())
        ids = [row["id"] for row in manifest["workers"]]
        producer = (self.state / "secrets/producer-token").read_text().strip()
        assert producer.startswith("d2a_LOCAL_GENERIC_ONLY_")
        binding = security.authenticate(producer)
        assert binding.id == "hub-producer" and binding.tenant == manifest["tenant"]
        assert set(binding.queues) == set(ids) and set(binding.scopes) == {"read", "submit"}
        self.producer = producer
        self.tokens.append(producer)
        for identifier in ids:
            header = runtime.secret(self.state / "secrets" / identifier / "worker-authorization")
            assert header.startswith("Bearer d2a_LOCAL_GENERIC_ONLY_")
            binding = security.authenticate(header[7:])
            assert (binding.id, binding.tenant, binding.queues) == (identifier, manifest["tenant"], (identifier,))
            assert set(binding.scopes) == {"read", "work", "subscribe"}
            self.headers[identifier] = header
            self.tokens.append(header[7:])
        self.checked("actual_security_authenticates_exact_worker_queue_scope_and_producer_registry")
        self.extras = {}
        for name, tenant, queues, scopes in (
            ("fixture-peer", manifest["tenant"], [ids[0]], ["read", "work", "subscribe"]),
            ("fixture-broad", manifest["tenant"], ids, ["read", "work", "submit", "subscribe"]),
            ("fixture-outsider", "other-" + uuid.uuid4().hex, ids, ["read", "work", "submit"])):
            security.create_principal(name, tenant, queues, scopes)
            with patch("dot2api.security.secrets.token_urlsafe", side_effect=lambda *_: "LOCAL_GENERIC_ONLY_" + uuid.uuid4().hex):
                token = security.issue_token(name, 3600)
            binding = security.authenticate(token)
            assert binding.id == name and binding.tenant == tenant
            self.extras[name] = "Bearer " + token
            self.tokens.append(token)

    def offline_job(self, registry):
        base = f"http://127.0.0.1:{self.config['upstream_port']}"
        controller = Controller(self.state / "controller/hub.sqlite", OfflineAdapter(base, "hub-producer"), ["demo"], workers=registry)
        try:
            controller.add_snapshot({"schema_version": "multidot.snapshot.v1", "project_id": "demo", "snapshot_id": "local-input-v1",
                "artifacts": [{"name": "brief.md", "media_type": "text/markdown", "content": "Synthetic local fixture material."}]})
            spec = {"schema_version": "multidot.job.v1", "project_id": "demo", "request_id": "local-roundtrip",
                "title": "LOCAL_RUNTIME_CONTRACT generic dispatch", "goal": "Compare supplied synthetic fixture material",
                "input_snapshot_id": "local-input-v1", "policy_id": "provided-materials-only", "steps": [
                    {"step_id": f"review-{index}", "worker": worker["id"], "instructions": "Review only this synthetic input",
                     "acceptance_criteria": ["Return evidence"], "depends_on": []}
                    for index, worker in enumerate(registry) if worker["role"] == "worker"]}
            synthesis = next((worker for worker in registry if worker["role"] == "synthesis"), None)
            if synthesis:
                spec["synthesis"] = {"worker": synthesis["id"], "trigger": "all_required_steps_accepted", "instructions": "Compare exact fixture results"}
            submitted = controller.submit_job(spec)
            assert controller.submit_job(spec) == {"job_id": submitted["job_id"], "duplicate": True}
            assert controller.db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
            assert not runtime.probe(self.config["upstream_port"])
            self.checked("offline_snapshot_and_explicit_job_submission_create_no_network_attempts")
            return submitted["job_id"]
        finally:
            controller.close()

    def verify_worker_boundaries(self, manifest):
        upstream = f"http://127.0.0.1:{self.config['upstream_port']}"
        rows = manifest["workers"]
        for worker in rows:
            identifier, header = worker["id"], self.headers[worker["id"]]
            base = f"http://127.0.0.1:{worker['ingress_port']}"
            init = mcp(base, header, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "LOCAL_GENERIC_CONTRACT", "version": "1"}})
            assert init["serverInfo"]["name"] == "dot2api"
            tools = mcp(base, header, "tools/list")["tools"]
            assert {row["name"] for row in tools} == EXPECTED_TOOLS and len(tools) == len(EXPECTED_TOOLS)
            assert "tasks" in tool(base, header, "list_tasks", {"queue": identifier})
            denied = [row["id"] for row in rows if row["id"] != identifier] or ["fixture-unregistered-queue"]
            for queue in denied:
                result = mcp(base, header, "tools/call", {"name": "list_tasks", "arguments": {"queue": queue}})
                assert result["isError"] is True
                assert local_http(upstream, "POST", "/v1/tools/list_tasks", header, {"queue": queue})[0] == 403
            for wrong_header in (None, self.extras["fixture-peer"], self.extras["fixture-broad"], self.extras["fixture-outsider"]):
                assert local_http(base, "POST", "/mcp", wrong_header,
                                  {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0] == 401
            for peer in rows:
                if peer["id"] != identifier:
                    assert local_http(base, "POST", "/mcp", self.headers[peer["id"]],
                                      {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0] == 401
            for path in ("/v1/tasks", "/mcp/token"):
                assert local_http(base, "POST", path, header, {})[0] == 404
        self.checked("actual_mcp_initialize_tools_list_and_call_for_every_worker")
        self.checked("own_queue_allowed_peer_queues_denied_in_mcp_and_native_http")
        self.checked("ingress_rejects_missing_same_queue_peer_broad_foreign_and_other_worker_headers")
        self.checked("ingress_rejects_native_submit_and_capability_url_paths")

    def tenant_isolation(self, task, worker):
        upstream = f"http://127.0.0.1:{self.config['upstream_port']}"
        outsider = self.extras["fixture-outsider"]
        assert local_http(upstream, "GET", "/v1/tasks/" + task["id"], outsider)[0] == 404
        listed = tool(upstream, outsider, "list_tasks", {"queue": task["queue"]})
        assert listed["tasks"] == []
        foreign = tool(upstream, outsider, "submit_task", {"queue": task["queue"], "instructions": "Synthetic tenant isolation fixture",
            "payload": {}, "idempotency_key": "foreign-isolation", "max_attempts": 1, "ttl_seconds": 60})["task"]
        assert local_http(upstream, "GET", "/v1/tasks/" + foreign["id"], self.headers[worker["id"]])[0] == 404
        own = tool(upstream, self.headers[worker["id"]], "list_tasks", {"queue": task["queue"]})
        assert foreign["id"] not in {row["id"] for row in own["tasks"]}
        tool(upstream, outsider, "cancel_task", {"task_id": foreign["id"], "reason": "Fixture cleanup"})
        self.checked("actual_upstream_tenant_isolation_for_same_queue_read_list_and_foreign_task")

    def finish_job(self, registry, job_id):
        base = f"http://127.0.0.1:{self.config['upstream_port']}"
        adapter = NativeTaskAdapter(base, self.producer, producer="hub-producer", timeout=4)
        adapter.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        controller = Controller(self.state / "controller/hub.sqlite", adapter, ["demo"], workers=registry)
        completed, checked_tenant = set(), False
        rows = {row["id"]: row for row in self.config["workers"]}
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                # The actual supervised controller owns dispatch/reconciliation.
                # This harness only reads its state and acts as synthetic workers.
                job = controller.get_job(job_id)
                assert job["state"] not in ("FAILED", "NEEDS_REVIEW", "CANCELLED")
                for step in job["steps"]:
                    for attempt in step["attempts"]:
                        task_id = attempt["upstream_id"]
                        if not task_id or task_id in completed:
                            continue
                        task = adapter.get(task_id)
                        assert task["queue"] == step["worker"] and task["status"] == "queued"
                        worker = rows[step["worker"]]
                        if not checked_tenant:
                            self.tenant_isolation(task, worker)
                            checked_tenant = True
                        ingress = f"http://127.0.0.1:{worker['ingress_port']}"
                        header = self.headers[worker["id"]]
                        lease = tool(ingress, header, "claim_task", {"task_id": task_id})["lease_token"]
                        tool(ingress, header, "heartbeat_task", {"task_id": task_id, "lease_token": lease})
                        payload = task["payload"]
                        for dependency in payload["dependency_results"]:
                            assert dependency["result_hash"] == digest(dependency["result"])
                        result = {"schema_version": "multidot.result.v1", "job_id": job_id, "step_id": step["id"],
                            "input_snapshot_id": payload["input_snapshot_id"], "outcome": "completed",
                            "summary": "LOCAL_RUNTIME_CONTRACT synthetic bounded result", "findings": [],
                            "artifacts": [{"name": name, "media_type": "text/markdown", "content": "Synthetic fixture result."}
                                          for name in payload["required_artifacts"]],
                            "checks": [], "open_questions": [], "external_changes": []}
                        observed = tool(ingress, header, "complete_task", {"task_id": task_id, "lease_token": lease, "result": result})
                        assert observed["task"]["status"] == "completed"
                        completed.add(task_id)
                if controller.get_job(job_id)["state"] == "COMPLETED":
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("Controller completion deadline exceeded")
            # Observe two further real supervisor-owned controller cycles.
            time.sleep(2.2)
            assert controller.get_job(job_id)["state"] == "COMPLETED"
            assert len(completed) == len(registry)
            assert controller.db.execute("SELECT count(*) FROM attempts WHERE job_id=?", (job_id,)).fetchone()[0] == len(registry)
            assert controller.db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
            self.completed = len(completed)
            self.checked("controller_generic_dispatch_claim_heartbeat_completion_and_durable_artifacts")
            self.checked("exactly_one_attempt_per_step_no_duplicate_dispatch_and_no_remaining_reservations")
            if any(worker["role"] == "synthesis" for worker in registry):
                self.checked("configured_synthesis_target_runs_once_with_exact_accepted_dependency_hashes")
        finally:
            controller.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--tunnel-binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-temporary-fixtures", action="store_true", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    report = {"classification": "LOCAL_RUNTIME_CONTRACT", "started_at": datetime.now(timezone.utc).isoformat(),
        "passed": False, "checks": [], "cases": [], "real_credentials": False, "real_tunnel_connected": False,
        "real_dot_jobs": False, "control_plane": "loopback FakePlane only", "events_enabled": False,
        "os_autostart": False, "host_lifetime_guarantee": False, "supervisor_automatic_restart": False}
    plane = thread = case = None
    try:
        provenance = verify(args.upstream.resolve())
        assert hashlib.sha256(args.tunnel_binary.read_bytes()).hexdigest() == runtime.TUNNEL_SHA256
        report["upstream_commit"] = provenance["commit"]
        report["tunnel_sha256"] = runtime.TUNNEL_SHA256
        FakePlane.seen = []
        plane = ThreadingHTTPServer(("127.0.0.1", 0), FakePlane)
        thread = Thread(target=plane.serve_forever, daemon=True)
        thread.start()
        for count in (1, 2, 4, 10):
            case = Case(count, report)
            case.run(args, plane)
            print(json.dumps({"classification": "LOCAL_RUNTIME_CONTRACT", "profiles": count,
                              "checks_passed": len(case.checks), "cleanup_verified": True}), flush=True)
        assert verify(args.upstream.resolve()) == provenance
        report["passed"] = True
    except Exception as exc:
        frames = traceback.extract_tb(exc.__traceback__)
        report["failure"] = {"error_type": type(exc).__name__, "phase": case.phase if case else "pinned_installation_verification",
                             "profiles": case.count if case else None, "source_line": frames[-1].lineno if frames else None}
    finally:
        if plane is not None:
            plane.shutdown()
            plane.server_close()
        if thread is not None:
            thread.join(timeout=5)
        report["fake_plane_requests"] = len(FakePlane.seen)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["checks_passed"] = len(report["checks"])
        report["source_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "checks_passed": report["checks_passed"],
                      "cases_completed": len(report["cases"]), "failure": report.get("failure")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
