"""MOCK/stdlib-only runtime boundary tests. No upstream or tunnel execution."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runtime_under_test", ROOT / "scripts/runtime_supervisor.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = json.loads((ROOT / "config/runtime.example.json").read_text())
        self.config["state_root"] = str(self.root)
        self.path = self.root / "runtime.json"

    def tearDown(self):
        self.temp.cleanup()

    def load(self, **changes):
        config = copy.deepcopy(self.config)
        config.update(changes)
        self.path.write_text(json.dumps(config))
        return runtime.load(self.path)

    def test_rejects_arbitrary_component_and_remote_control_plane(self):
        for change in ({"components": ["dot2api", "shell"]}, {"control_plane_url": "https://example.invalid"},
                       {"test_only": True}, {"test_only": True, "event_delivery_enabled": True}):
            with self.assertRaises(ValueError):
                self.load(**change)

    def test_tunnel_requires_b_ingress(self):
        with self.assertRaises(ValueError):
            self.load(components=["dot2api", "tunnel-b"])

    def test_rejects_duplicate_ports(self):
        with self.assertRaises(ValueError):
            self.load(ingress_port=self.config["upstream_port"])

    def test_render_has_file_references_and_no_bearer_url(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "MOCK-secret-do-not-render"}):
            runtime.render(self.config)
        raw = (self.root / "config/tunnel-b.yaml").read_text()
        config = json.loads(raw)
        self.assertNotIn("MOCK-secret", raw)
        self.assertTrue(config["control_plane"]["api_key"].startswith("file:"))
        self.assertTrue(config["mcp"]["extra_headers"]["Authorization"].startswith("file:"))
        self.assertEqual(config["mcp"]["server_urls"][0]["url"], "http://127.0.0.1:8789/mcp")

    def test_secret_requires_owner_only_regular_file(self):
        path = self.root / "fake-secret"
        path.write_text("LOCAL_TEST_ONLY\n")
        path.chmod(0o600)
        self.assertEqual(runtime.secret(path), "LOCAL_TEST_ONLY")
        link = self.root / "link"
        link.symlink_to(path)
        with self.assertRaises(OSError):
            runtime.secret(link)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            runtime.secret(path)

    def test_child_environment_drops_ambient_secrets_but_keeps_external_proxy(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "MOCK-key", "MCP_EXTRA_HEADERS": "MOCK-header",
            "HTTPS_PROXY": "http://proxy.invalid", "NO_PROXY": "internal.example"}, clear=True):
            env = runtime.child_environment(self.config)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("MCP_EXTRA_HEADERS", env)
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.invalid")
        self.assertTrue({"127.0.0.1", "localhost", "::1", "internal.example"} <= set(env["NO_PROXY"].split(",")))
        self.assertEqual(env["DOT2API_WORKER"], "0")

    def test_occupied_port_and_listener_ownership(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertFalse(runtime.free_port(port))
            self.assertTrue(runtime.owned_listener(runtime.identity(os.getpid()), port))
            self.assertFalse(runtime.owned_listener(None, port))

    def test_cross_namespace_status_is_unknown_not_stopped(self):
        runtime.render(self.config)
        record = runtime.identity(os.getpid())
        record["pid_namespace"] = "pid:[OTHER]"
        runtime.write_json(self.root / "run/status.json", {"supervisor": record, "children": {"dot2api": {"identity": record}}})
        status = runtime.status(self.config)
        self.assertIsNone(status["supervisor_running"])
        self.assertIsNone(status["children"]["dot2api"]["ready"])
        self.assertEqual(status["observation_scope"], "other_pid_namespace_last_report_only")


if __name__ == "__main__":
    unittest.main()
