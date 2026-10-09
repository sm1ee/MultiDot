"""Opt-in local runtime acceptance. Actual pinned services, fake identities only.

Run with the reviewed dot2api virtualenv. No real tunnel is contacted. All owned
processes and disposable credential/storage files are cleaned up before exit.
"""
import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
from threading import Thread
import time
from unittest.mock import patch
import urllib.error
import urllib.request

from dot2api.cli import initialize
from dot2api.security import Security
from dot2api.store import Store

import runtime_supervisor as runtime

ROOT = Path(__file__).resolve().parents[1]
# Direct script execution puts scripts/ first, where the friendly multidot.py
# launcher must not shadow the actual multidot package used below.
sys.path.insert(0, str(ROOT / "src"))
records = []


def wait_for(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("Local acceptance deadline exceeded")


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(base, method, path, token=None, data=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token
    req = urllib.request.Request(base + path, method=method, headers=headers,
                                 data=json.dumps(data).encode() if data is not None else None)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=3) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        try:
            return error.code, error.read()
        finally:
            error.close()


class FakePlane(BaseHTTPRequestHandler):
    seen = []
    def log_message(self, *_args):
        pass
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.seen.append({"method": "POST", "path": self.path.split("?")[0],
                          "fake_authorization_correct": self.headers.get("Authorization") == "Bearer LOCAL_RUNTIME_TEST_ONLY"})
        time.sleep(0.2)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()
    def do_GET(self):
        self.seen.append({"method": "GET", "path": self.path.split("?")[0],
                          "fake_authorization_correct": self.headers.get("Authorization") == "Bearer LOCAL_RUNTIME_TEST_ONLY"})
        time.sleep(0.2)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tunnel-binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hold-seconds", type=int, default=0)
    parser.add_argument("--session-receipt", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    plane = ThreadingHTTPServer(("127.0.0.1", 0), FakePlane)
    thread = Thread(target=plane.serve_forever, daemon=True)
    thread.start()
    supervisors = []
    children = []
    with tempfile.TemporaryDirectory(prefix="multidot-runtime-test-") as temporary:
        state = Path(temporary)
        try:
            initialize(state / "upstream")
            security = Security(Store(state / "upstream/dot2api.sqlite3"), (state / "upstream/encryption.key").read_bytes().strip())
            tokens = {}
            for name, queues, scopes in (("worker-b", ["dot-b"], ["read", "work", "subscribe"]),
                                        ("broad-test", ["dot-a", "dot-b", "dot-c"], ["read", "work", "submit"]),
                                        ("hub-producer", ["dot-a", "dot-b", "dot-c"], ["read", "submit"])):
                security.create_principal(name, "local-test", queues, scopes)
                with patch("dot2api.security.secrets.token_urlsafe", return_value="LOCAL_RUNTIME_TEST_ONLY_" + name):
                    tokens[name] = security.issue_token(name, 3600)
            config = {"schema_version": 1, "state_root": str(state), "repository": str(ROOT),
                      "upstream_checkout": str(Path(__import__('dot2api').__file__).resolve().parents[2]), "event_delivery_enabled": False,
                      "upstream_python": sys.executable, "tunnel_binary": str(args.tunnel_binary.resolve()),
                      "tunnel_id": "tunnel_" + "0" * 32, "upstream_port": port(), "ingress_port": port(), "health_port": port(),
                      "control_plane_url": f"http://127.0.0.1:{plane.server_port}", "components": list(runtime.ROLES), "test_only": True}
            path = state / "runtime.json"
            path.write_text(json.dumps(config))
            runtime.render(config)
            command = [sys.executable, str(ROOT / "scripts/runtime_supervisor.py"), "--config", str(path)]
            missing_key = subprocess.run(command + ["check"], capture_output=True, text=True, timeout=30)
            assert missing_key.returncode == 2
            assert not runtime.read_state(config)
            (state / "secrets/runtime-api-key").write_text("LOCAL_RUNTIME_TEST_ONLY\n")
            (state / "secrets/worker-b-authorization").write_text("Bearer " + tokens["worker-b"] + "\n")
            (state / "secrets/producer-token").write_text(tokens["hub-producer"] + "\n")
            def call(action):
                return subprocess.run(command + [action], capture_output=True, text=True, timeout=240)
            check = call("check")
            assert check.returncode == 0, check.stderr
            records.append("missing-key gate fails closed; private files and exact pinned installation pass")
            def launch():
                p = subprocess.Popen(command + ["run"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                supervisors.append(p)
                wait_for(lambda: runtime.status(config).get("supervisor_running"))
                wait_for(lambda: all(runtime.status(config).get("children", {}).get(role, {}).get("running") for role in runtime.ROLES))
                wait_for(lambda: runtime.probe(config["upstream_port"]) and runtime.probe(config["ingress_port"]))
                children.extend(row["identity"] for row in runtime.status(config)["children"].values() if row.get("identity"))
                return p
            first = launch()
            records.append("actual dot2api, controller, B ingress and pinned tunnel-client start")
            if args.hold_seconds:
                receipt = {"classification": "LOCAL_RUNTIME_CONTRACT", "fake_only": True,
                           "upstream_port": config["upstream_port"], "ingress_port": config["ingress_port"],
                           "health_port": config["health_port"], "started_at": time.time(),
                           "hold_seconds": args.hold_seconds, "pid_namespace": os.readlink('/proc/self/ns/pid'),
                           "network_namespace": os.readlink('/proc/self/ns/net')}
                if args.session_receipt:
                    args.session_receipt.parent.mkdir(parents=True, exist_ok=True)
                    args.session_receipt.write_text(json.dumps(receipt))
                print(json.dumps({"managed_foreground_test_ready": True, **receipt}), flush=True)
                deadline = time.monotonic() + args.hold_seconds
                receipt["same_session_heartbeats"] = []
                while time.monotonic() < deadline:
                    time.sleep(min(5, max(0, deadline - time.monotonic())))
                    assert first.poll() is None
                    heart = {"at": time.time(), "owned_upstream_ready": runtime.status(config)["children"]["dot2api"]["ready"],
                             "owned_ingress_ready": runtime.status(config)["children"]["b-ingress"]["ready"],
                             "tunnel_process_running": runtime.status(config)["children"]["tunnel-b"]["running"]}
                    assert all(heart[key] for key in ("owned_upstream_ready", "owned_ingress_ready", "tunnel_process_running"))
                    receipt["same_session_heartbeats"].append(heart)
                    if args.session_receipt:
                        args.session_receipt.write_text(json.dumps(receipt, indent=2))
                    print(json.dumps({"managed_foreground_heartbeat": heart}), flush=True)
                records.append("managed foreground session retained stack throughout observation window")
            assert call("start").returncode == 0
            assert runtime.status(config)["supervisor"]["pid"] == first.pid
            records.append("idempotent repeated start retains one supervisor")
            wait_for(lambda: FakePlane.seen)
            assert all(row["fake_authorization_correct"] for row in FakePlane.seen)
            records.append("official tunnel client resolves runtime API key file to local fake plane")
            ingress = f"http://127.0.0.1:{config['ingress_port']}"
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            status, raw = http(ingress, "POST", "/mcp", "Bearer " + tokens["worker-b"], {
                "jsonrpc": "2.0", "id": 10, "method": "initialize", "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "LOCAL_RUNTIME_CONTRACT", "version": "1"}}})
            assert status == 200 and json.loads(raw)["result"]["serverInfo"]["name"] == "dot2api"
            status, raw = http(ingress, "POST", "/mcp", "Bearer " + tokens["worker-b"], payload)
            assert status == 200 and "submit_task" not in [tool["name"] for tool in json.loads(raw)["result"]["tools"]]
            status, raw = http(ingress, "POST", "/mcp", "Bearer " + tokens["worker-b"], {
                "jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {
                    "name": "list_tasks", "arguments": {"queue": "dot-b"}}})
            assert status == 200 and json.loads(raw)["result"]["isError"] is False
            assert http(ingress, "POST", "/mcp", "Bearer " + tokens["broad-test"], payload)[0] == 401
            assert http(ingress, "POST", "/mcp", data=payload)[0] == 401
            assert http(ingress, "POST", "/v1/tasks", "Bearer " + tokens["worker-b"], {})[0] == 404
            assert http(ingress, "POST", "/mcp/token", "Bearer " + tokens["worker-b"], payload)[0] == 404
            result = http(ingress, "POST", "/mcp", "Bearer " + tokens["worker-b"], {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_tasks","arguments":{"queue":"dot-c"}}})
            assert json.loads(result[1])["result"]["isError"] is True
            records.append("actual pinned MCP initialize/tools/list/tools/call; B-only tools/queue and ingress auth/path rejection")
            # Persist a sentinel in controller-owned storage; startup must reuse
            # it, never initialize another DB or discard pending state.
            from multidot.controller import Controller
            from multidot.cli import OfflineAdapter
            controller = Controller(state / "controller/hub.sqlite", OfflineAdapter(f"http://127.0.0.1:{config['upstream_port']}", "hub-producer"), ["demo"])
            controller.bootstrap("demo", "LOCAL_RUNTIME_TEST_ONLY durable sentinel")
            controller.pause_worker("dot-c", True)
            controller.close()
            for role in runtime.ROLES:
                old = runtime.status(config)["children"][role]["identity"]
                os.kill(old["pid"], signal.SIGKILL)
                wait_for(lambda: (r := runtime.status(config)["children"].get(role, {})).get("running") and r["identity"] != old, seconds=20)
                records.append(role + " SIGKILL triggers owned child restart")
            old_children = [r["identity"] for r in runtime.status(config)["children"].values()]
            children.extend(old_children)
            first.kill()
            first.wait(timeout=5)
            wait_for(lambda: not any(runtime.alive(r) for r in old_children), seconds=12)
            records.append("supervisor SIGKILL causes kernel parent-death cleanup of children")
            assert not runtime.status(config)["supervisor_running"]
            assert call("reconcile").returncode == 0
            second = launch()
            records.append("inspection-only reconcile and manual supervisor relaunch")
            controller = Controller(state / "controller/hub.sqlite", OfflineAdapter(f"http://127.0.0.1:{config['upstream_port']}", "hub-producer"), ["demo"])
            assert next(row for row in controller.list_workers() if row["id"] == "dot-c")["paused"]
            controller.close()
            records.append("controller database and worker pause survive child/supervisor restarts")
            stopped = call("stop")
            assert stopped.returncode == 0, stopped.stderr
            second.wait(timeout=5)
            wait_for(lambda: not any(runtime.alive(r) for r in children))
            assert not runtime.probe(config["upstream_port"])
            assert not runtime.probe(config["ingress_port"])
            assert not runtime.probe(config["health_port"], "/healthz")
            records.append("safe shutdown closes all owned listeners and preserves storage")
            all_text = "\n".join(p.read_text() for p in (state / "logs").glob("*") if p.is_file())
            assert not any(token in all_text for token in tokens.values())
            assert "LOCAL_RUNTIME_TEST_ONLY" not in all_text
            records.append("lifecycle logs contain no credential or header values")
        finally:
            for process in supervisors:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=55)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            # Only identities recorded in this disposable run may be signaled.
            for record in children:
                if runtime.alive(record):
                    os.kill(record["pid"], signal.SIGTERM)
            plane.shutdown()
            plane.server_close()
            thread.join(timeout=5)
    assert not Path(temporary).exists()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"classification": "LOCAL_RUNTIME_CONTRACT", "at": datetime.now(timezone.utc).isoformat(),
        "passed": True, "checks": records, "control_plane": "local fake only", "real_credentials": False,
        "real_tunnel_connected": False, "real_dot_jobs": False, "ephemeral_state_removed": True,
        "os_autostart": False, "supervisor_automatic_restart": False, "host_lifetime_guarantee": False,
        "fake_plane_requests": len(FakePlane.seen)}, indent=2) + "\n")
    print(json.dumps({"passed": True, "checks": len(records), "cleanup": True}))


if __name__ == "__main__":
    main()
