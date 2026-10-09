"""MOCK-only generic runtime contracts. Local stdlib fake HTTP fixtures only.

No actual upstream, tunnel executable, external request, or real credential.
"""
from contextlib import ExitStack, redirect_stderr
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
from io import StringIO
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import runtime_supervisor as runtime
import runtime_ingress as ingress
import runtime_b_ingress as legacy_ingress

EXPECTED_TOOLS = {"get_task", "list_tasks", "claim_task", "heartbeat_task", "complete_task", "release_task"}
MARKER = "LOCAL_GENERIC_TEST_ONLY"


def config_for(root, count=5):
    names = ["研究员 α", "Captain 🐋", "dot-a", "A / B / C", "Résumé finisher"]
    return {"schema_version": 2, "state_root": str(root), "repository": str(ROOT),
            "upstream_python": sys.executable, "upstream_checkout": str(root / "fake-upstream"),
            "tunnel_binary": str(root / "fake-tunnel"), "upstream_port": 22000,
            "control_plane_url": "http://127.0.0.1:22001", "test_only": True, "event_delivery_enabled": False,
            "workers": [{"id": "worker-" + f"{index:032x}", "name": names[index % len(names)],
                         "role": "synthesis" if index == count - 1 else "worker",
                         "tunnel_id": "tunnel_" + f"{index:032x}",
                         "ingress_port": 22100 + index * 2, "health_port": 22101 + index * 2}
                        for index in range(count)]}


class RuntimeFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multidot-generic-mock-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = config_for(self.root)
        self.path = self.root / "runtime.json"

    def load(self, c=None):
        self.path.write_text(json.dumps(self.config if c is None else c))
        return runtime.load(self.path)



class GenericConfigTests(RuntimeFixture):
    def test_one_and_many_workers_keep_exact_ids_and_unicode_names(self):
        for count in (1, 2, 5, 12):
            with self.subTest(count=count):
                c = config_for(self.root, count)
                loaded = self.load(c)
                self.assertEqual(loaded, c)
                self.assertNotIn("components", loaded)
                specs = runtime.component_specs(loaded)
                self.assertEqual(len(specs), 2 + 2 * count)
                self.assertEqual(sum(s["role"] == "dot2api" for s in specs.values()), 1)
                self.assertEqual(sum(s["role"] == "controller" for s in specs.values()), 1)
                self.assertNotIn("b-ingress", specs)
                for entry in c["workers"]:
                    self.assertEqual(specs["ingress:" + entry["id"]]["worker_id"], entry["id"])

    def test_duplicate_display_names_and_no_synthesis_are_supported(self):
        for entry in self.config["workers"]:
            entry.update(name="Same arbitrary 名字", role="worker")
        self.assertEqual(self.load(), self.config)

    def test_rejects_duplicate_ids_tunnels_and_all_port_collisions(self):
        for field in ("id", "tunnel_id", "ingress_port", "health_port"):
            with self.subTest(field=field):
                c = copy.deepcopy(self.config)
                c["workers"][1][field] = c["workers"][0][field]
                with self.assertRaises(ValueError):
                    self.load(c)
        for field, value in (("ingress_port", 22000), ("health_port", 22100), ("ingress_port", True)):
            c = copy.deepcopy(self.config)
            c["workers"][1][field] = value
            with self.assertRaises(ValueError):
                self.load(c)

    def test_rejects_unknown_roles_multiple_synthesis_and_unsafe_ids(self):
        for value in ("shell", "producer", "synthesis ", [], None):
            c = copy.deepcopy(self.config)
            c["workers"][0]["role"] = value
            with self.assertRaises(ValueError):
                self.load(c)
        for value in ("../escape", "研究员", "x/y", "ingress:other", "", "a" * 65, "-x"):
            c = copy.deepcopy(self.config)
            c["workers"][0]["id"] = value
            with self.assertRaises(ValueError):
                self.load(c)
        self.config["workers"][0]["role"] = "synthesis"
        with self.assertRaises(ValueError):
            self.load()

    def test_exact_v2_schema_rejects_legacy_fields_and_command_injection(self):
        for field, value in (("components", ["shell"]), ("command", ["sh"]), ("tunnel_id", "unused")):
            c = dict(self.config, **{field: value})
            with self.assertRaises(ValueError):
                self.load(c)
        self.config["workers"][0]["command"] = ["sh"]
        with self.assertRaises(ValueError):
            self.load()
        self.path.write_text('{"schema_version":2,"schema_version":2}')
        with self.assertRaises(ValueError):
            runtime.load(self.path)

    def test_events_and_control_plane_fail_closed_in_test_mode(self):
        for changes in ({"event_delivery_enabled": True}, {"event_delivery_enabled": 1},
                        {"control_plane_url": "https://api.openai.com"},
                        {"control_plane_url": "http://localhost:22001"},
                        {"control_plane_url": "http://127.0.0.1:65536"},
                        {"control_plane_url": "https://example.invalid"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.load(dict(self.config, **changes))
        self.assertEqual(runtime.child_environment(self.config)["DOT2API_WORKER"], "0")
        with patch.dict(os.environ, {"DOT2API_QUEUE": "dot-b", "DOT2API_WORKER": "1"}):
            self.assertNotIn("DOT2API_QUEUE", runtime.child_environment(self.config))
            self.assertEqual(runtime.child_environment(self.config)["DOT2API_WORKER"], "0")

    def test_render_uses_stable_ids_and_per_worker_secret_file_references(self):
        self.load()
        with patch.object(runtime, "secret", side_effect=AssertionError("Render must not read credentials")):
            runtime.render(self.config)
        controller = json.loads((self.root / "config/controller.json").read_text())
        self.assertEqual(controller["workers"], [{k: e[k] for k in ("id", "name", "role")} for e in self.config["workers"]])
        self.assertEqual(controller["producer"], "hub-producer")
        self.assertEqual(controller["projects"], ["demo"])
        self.assertEqual(controller["token_env"], "MULTIDOT_PRODUCER_TOKEN")
        for entry in self.config["workers"]:
            identifier = entry["id"]
            tunnel = json.loads((self.root / "config" / f"tunnel-{identifier}.yaml").read_text())
            self.assertEqual(tunnel["control_plane"]["tunnel_id"], entry["tunnel_id"])
            self.assertEqual(tunnel["control_plane"]["api_key"], "file:" + str(self.root / "secrets" / identifier / "runtime-api-key"))
            self.assertEqual(tunnel["mcp"]["extra_headers"]["Authorization"], "file:" + str(self.root / "secrets" / identifier / "worker-authorization"))
            self.assertEqual(tunnel["mcp"]["server_urls"][0]["url"], f"http://127.0.0.1:{entry['ingress_port']}/mcp")
            self.assertEqual(tunnel["health"]["url_file"], str(self.root / "run" / f"tunnel-{identifier}-health-url"))
            self.assertEqual(list((self.root / "secrets" / identifier).iterdir()), [])
        self.assertFalse((self.root / "config/tunnel-b.yaml").exists())

    def test_fixed_worker_dispatch_rejects_unconfigured_roles_and_ids(self):
        for role, identifier in (("shell", None), ("ingress", None), ("ingress", "unknown"),
                                 ("b-ingress", None), ("tunnel-b", "dot-b"),
                                 ("controller", self.config["workers"][0]["id"])):
            with self.subTest(role=role, identifier=identifier), self.assertRaises(ValueError), \
                    patch.object(runtime.ctypes, "CDLL", side_effect=AssertionError("Do not reach executable dispatch")):
                runtime.worker(self.config, role, 123, identifier)

    def test_fixed_worker_dispatch_uses_configured_id_and_parent_death_protection(self):
        entry = self.config["workers"][0]
        libc = MagicMock()
        libc.prctl.return_value = 0
        with patch.object(runtime, "CONFIG_PATH", self.path, create=True), \
                patch.object(runtime.ctypes, "CDLL", return_value=libc), \
                patch.object(runtime.os, "getppid", return_value=123), \
                patch.object(runtime.os, "chdir"), patch.object(runtime.os, "execve") as execute:
            runtime.worker(self.config, "ingress", 123, entry["id"])
            args = execute.call_args.args[1]
            self.assertEqual(args[-2:], ["--worker-id", entry["id"]])
            self.assertTrue(args[1].endswith("runtime_ingress.py"))
            self.assertNotIn(entry["name"], args)
            libc.prctl.assert_called_once_with(1, signal.SIGKILL, 0, 0, 0)
            runtime.worker(self.config, "tunnel", 123, entry["id"])
            self.assertEqual(execute.call_args.args[1][-1], str(self.root / "config" / f"tunnel-{entry['id']}.yaml"))

    def test_parent_exit_race_prevents_worker_execution(self):
        libc = MagicMock()
        libc.prctl.return_value = 0
        with patch.object(runtime.ctypes, "CDLL", return_value=libc), \
                patch.object(runtime.os, "getppid", return_value=1), patch.object(runtime.os, "execve") as execute:
            self.assertEqual(runtime.worker(self.config, "dot2api", 123), 1)
            execute.assert_not_called()

    def test_legacy_ingress_wrapper_refuses_v2(self):
        self.load()
        output = StringIO()
        with patch.object(legacy_ingress, "serve") as serve, redirect_stderr(output):
            self.assertEqual(legacy_ingress.main(["--config", str(self.path)]), 2)
        serve.assert_not_called()

    def test_generic_status_has_per_id_ports_and_preserves_namespace_uncertainty(self):
        runtime.render(self.config)
        record = runtime.identity(os.getpid())
        children = {key: {"identity": record} for key in runtime.component_specs(self.config)}
        runtime.write_json(self.root / "run/status.json", {"supervisor": record, "children": children})
        with patch.object(runtime, "owned_listener", return_value=False), patch.object(runtime, "probe") as probe:
            result = runtime.status(self.config)
            self.assertTrue(all(row["ready"] is False for key, row in result["children"].items() if key != "controller"))
            probe.assert_not_called()
        record["pid_namespace"] = "pid:[OTHER]"
        runtime.write_json(self.root / "run/status.json", {"supervisor": record, "children": children})
        result = runtime.status(self.config)
        self.assertIsNone(result["supervisor_running"])
        self.assertTrue(all(row["running"] is None and row["ready"] is None for row in result["children"].values()))


class FakeHTTPRuntimeTests(RuntimeFixture):
    # These tests use loopback-only fake HTTP services, never the upstream binary.
    def setUp(self):
        super().setUp()
        self.config = config_for(self.root, 3)
        self.seen = []
        self.extra_capability = False
        self.allow_other_queue = False
        self.reject_own_queue = False
        self.mcp_response = (200, b'{"jsonrpc":"2.0","id":1,"result":{}}')
        self.headers = {entry["id"]: "Bearer " + MARKER + "_" + str(index) for index, entry in enumerate(self.config["workers"])}
        owner = self
        class FakeUpstream(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def respond(self, code, body):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                owner.seen.append((self.path, self.headers.get("Authorization"), None))
                if self.path == "/readyz":
                    return self.respond(200, b'{}')
                names = EXPECTED_TOOLS | ({"submit_task"} if owner.extra_capability else set())
                self.respond(200, json.dumps({"tools": [{"name": name} for name in names]}).encode())
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                auth = self.headers.get("Authorization")
                owner.seen.append((self.path, auth, raw))
                if self.path == "/mcp":
                    return self.respond(*owner.mcp_response)
                queue = json.loads(raw)["queue"]
                allowed = owner.allow_other_queue or (auth == owner.headers[queue] and not owner.reject_own_queue)
                self.respond(200 if allowed else 403, b'{}')
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        upstream.daemon_threads = True
        self.config["upstream_port"] = upstream.server_port
        runtime.render(self.config)
        for identifier, header in self.headers.items():
            path = runtime.worker_secret_path(self.config, identifier, "worker-authorization")
            path.write_text(header + "\n")
            path.chmod(0o600)
        self.servers = [upstream]
        self.threads = []
        self.addCleanup(self.stop_servers)
        for entry in self.config["workers"]:
            # Kernel-selected ephemeral fixture ports; production load() rejects 0.
            entry["ingress_port"] = 0
            server = ingress.create_server(self.config, entry["id"])
            entry["ingress_port"] = server.server_port
            self.servers.append(server)
        for server in self.servers:
            thread = Thread(target=lambda current=server: current.serve_forever(poll_interval=0.01), daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop_servers(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)

    def request(self, index, route="/mcp", authorization=None, method="POST", extra_headers=None, body=b'{}'):
        conn = http.client.HTTPConnection("127.0.0.1", self.config["workers"][index]["ingress_port"], timeout=2)
        try:
            headers = {"Authorization": authorization} if authorization is not None else {}
            headers.update(extra_headers or {})
            conn.request(method, route, body=body if method == "POST" else None, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_every_ingress_accepts_only_its_exact_authorization(self):
        for index, entry in enumerate(self.config["workers"]):
            for identifier, header in self.headers.items():
                with self.subTest(ingress=entry["id"], token=identifier):
                    status, _ = self.request(index, authorization=header)
                    self.assertEqual(status, 200 if identifier == entry["id"] else 401)
            self.assertEqual(self.request(index)[0], 401)
            self.assertEqual(self.request(index, authorization="Bearer " + MARKER + "_producer")[0], 401)
        forwarded = [row for row in self.seen if row[0] == "/mcp"]
        self.assertEqual(len(forwarded), 3)
        self.assertEqual({row[1] for row in forwarded}, set(self.headers.values()))

    def test_ingress_only_proxies_post_mcp_and_rejects_ambiguous_headers(self):
        header = next(iter(self.headers.values()))
        for route in ("/", "/v1/tasks", "/v1/capabilities", "/mcp/token", "/mcp?token=x", "/.well-known/oauth-authorization-server"):
            self.assertEqual(self.request(0, route, header)[0], 404)
        self.assertEqual(self.request(0, "/mcp", header, "GET")[0], 404)
        self.assertEqual(self.request(0, "/readyz", method="GET")[0], 200)
        self.assertEqual(self.request(0, authorization=header, extra_headers={"Transfer-Encoding": "chunked"})[0], 400)
        conn = http.client.HTTPConnection("127.0.0.1", self.config["workers"][0]["ingress_port"], timeout=2)
        try:
            conn.putrequest("POST", "/mcp")
            conn.putheader("Authorization", header)
            conn.putheader("Authorization", header)
            conn.putheader("Content-Length", "2")
            conn.endheaders(b'{}')
            self.assertEqual(conn.getresponse().status, 401)
        finally:
            conn.close()
        self.assertFalse(any(row[0] == "/mcp" for row in self.seen))

    def test_ingress_suppresses_upstream_error_bodies_and_limits_response_size(self):
        header = next(iter(self.headers.values()))
        self.mcp_response = (403, (MARKER + " upstream internal details").encode())
        self.assertEqual(self.request(0, authorization=header), (403, b""))
        self.mcp_response = (200, b"x" * 262145)
        self.assertEqual(self.request(0, authorization=header), (502, b""))

    def test_binding_checks_every_worker_and_all_other_configured_queues(self):
        runtime.binding(self.config)
        for identifier, header in self.headers.items():
            reads = [row for row in self.seen if row[1] == header and row[0] == "/v1/tools/list_tasks"]
            self.assertEqual({json.loads(row[2])["queue"] for row in reads}, set(self.headers))
        self.assertEqual(len(self.seen), 3 * 4)

    def test_binding_fails_closed_for_broad_tools_broad_queues_and_missing_own_access(self):
        identifier = self.config["workers"][0]["id"]
        for attribute in ("extra_capability", "allow_other_queue", "reject_own_queue"):
            with self.subTest(attribute=attribute):
                setattr(self, attribute, True)
                with self.assertRaises(ValueError):
                    runtime.binding(self.config, identifier)
                setattr(self, attribute, False)


class GenericSupervisorTests(RuntimeFixture):
    def run_fake_supervisor(self, *, reject_id=None, owned=True, lose_upstream_after=None, crash_role=None, cycles=1):
        runtime.render(self.config)
        callbacks = {}
        launched = []
        cycle = [0]
        class FakeProcess:
            def __init__(self, argv, **kwargs):
                self.argv, self.kwargs = argv, kwargs
                self.pid = 100000 + len(launched)
                self.returncode = None
                self.role = argv[argv.index("--role") + 1]
                launched.append(self)
            def poll(self):
                if self.role == crash_role:
                    self.returncode = 1
                return self.returncode
            def terminate(self):
                self.returncode = 0
            def kill(self):
                self.returncode = -9
            def wait(self, timeout):
                return self.returncode
        def sleep(_seconds):
            cycle[0] += 1
            if cycle[0] >= cycles:
                callbacks[signal.SIGTERM](None, None)
        def listener_owned(_record, port):
            return owned and not (lose_upstream_after is not None and cycle[0] >= lose_upstream_after
                                  and port == self.config["upstream_port"])
        def binding(c, identifier):
            if identifier == reject_id:
                raise ValueError(MARKER)
        with ExitStack() as stack:
            stack.enter_context(patch.object(runtime, "CONFIG_PATH", self.path, create=True))
            stack.enter_context(patch.object(runtime.signal, "signal", side_effect=lambda sig, fun: callbacks.setdefault(sig, fun)))
            stack.enter_context(patch.object(runtime, "free_port", return_value=True))
            stack.enter_context(patch.object(runtime, "owned_listener", side_effect=listener_owned))
            stack.enter_context(patch.object(runtime, "probe", return_value=True))
            stack.enter_context(patch.object(runtime, "identity", side_effect=lambda pid: {"pid": pid}))
            stack.enter_context(patch.object(runtime, "alive", return_value=False))
            stack.enter_context(patch.object(runtime, "binding", side_effect=binding))
            stack.enter_context(patch.object(runtime.subprocess, "Popen", side_effect=FakeProcess))
            stack.enter_context(patch.object(runtime.time, "sleep", side_effect=sleep))
            stack.enter_context(patch.object(runtime.time, "time", side_effect=lambda: cycle[0] * 60 + 1))
            self.assertEqual(runtime.supervisor(self.config), 0)
        return launched, runtime.read_state(self.config)

    def test_supervisor_launches_fixed_roles_for_each_worker(self):
        launched, state = self.run_fake_supervisor()
        self.assertEqual(len(launched), 12)
        self.assertEqual({p.role for p in launched}, {"dot2api", "ingress", "controller", "tunnel"})
        for process in launched:
            self.assertTrue(process.kwargs["start_new_session"])
            if process.role in ("ingress", "tunnel"):
                self.assertIn(process.argv[process.argv.index("--worker-id") + 1], {e["id"] for e in self.config["workers"]})
        self.assertEqual(set(state["children"]), set(runtime.component_specs(self.config)))
        self.assertTrue(all(row["identity"] is None for row in state["children"].values()))
        self.assertNotIn(MARKER, (self.root / "logs/lifecycle.jsonl").read_text())

    def test_failed_worker_binding_blocks_its_ingress_and_tunnel_only(self):
        identifier = self.config["workers"][1]["id"]
        launched, state = self.run_fake_supervisor(reject_id=identifier)
        self.assertEqual(len(launched), 10)
        self.assertTrue(state["children"]["ingress:" + identifier]["needs_operator_review"])
        self.assertFalse(any(identifier in p.argv for p in launched))
        self.assertNotIn(MARKER, json.dumps(state))
        self.assertNotIn(MARKER, (self.root / "logs/lifecycle.jsonl").read_text())

    def test_unowned_upstream_listener_prevents_all_dependents(self):
        launched, _ = self.run_fake_supervisor(owned=False)
        self.assertEqual([p.role for p in launched], ["dot2api"])

    def test_upstream_ownership_loss_stops_every_tunnel(self):
        launched, state = self.run_fake_supervisor(lose_upstream_after=1, cycles=2)
        self.assertEqual(len(launched), 12)
        for entry in self.config["workers"]:
            row = state["children"]["tunnel:" + entry["id"]]
            self.assertEqual(row["restarts"], 1)
            self.assertIsNone(row["identity"])

    def test_restart_budget_is_per_component_and_keeps_working_workers(self):
        launched, state = self.run_fake_supervisor(crash_role="ingress", cycles=20)
        for entry in self.config["workers"]:
            row = state["children"]["ingress:" + entry["id"]]
            self.assertEqual(row["restarts"], 8)
            self.assertTrue(row["needs_operator_review"])
        self.assertEqual(sum(p.role == "dot2api" for p in launched), 1)
        self.assertEqual(sum(p.role == "controller" for p in launched), 1)
        self.assertFalse(any(p.role == "tunnel" for p in launched))


if __name__ == "__main__":
    unittest.main()
