"""Real loopback HTTP checks of the fake-only manual fixture, without a browser."""
import http.client
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
MARKER = "WEB_SETUP_FAKE_KEY_ONLY"


class ManualHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multidot-manual-http-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        home = self.root / "unused-home"
        home.mkdir(mode=0o700)
        env = {"PATH": os.defpath, "LANG": "C.UTF-8", "HOME": str(home), "TMPDIR": str(self.root)}
        self.process = subprocess.Popen([sys.executable, "-u", str(ROOT / "tests/manual_web_setup_fixture.py")],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env)
        self.addCleanup(self.stop)
        output = b""
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            for _ in range(20):
                if not selector.select(0.5):
                    continue
                chunk = os.read(self.process.stdout.fileno(), 4096)
                if not chunk:
                    break
                output += chunk
                match = re.search(rb"http://127\.0\.0\.1:[0-9]+/\n", output)
                if match:
                    self.origin = match.group().decode().strip().rstrip("/")
                    break
            else:
                self.fail("The synthetic listener did not start")
        if not hasattr(self, "origin"):
            self.fail("The synthetic listener did not start")
        self.initial_output = output.decode()
        split = urlsplit(self.origin)
        self.host, self.port = split.hostname, split.port
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        self.csrf = re.search(r'let csrf = "([A-Za-z0-9_-]+)";', body.decode()).group(1)

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process.stdout.close()
        self.process.stderr.close()

    def request(self, method, path, value=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        headers = {}
        data = None
        if value is not None:
            data = json.dumps(value).encode()
            headers = {"Origin": self.origin, "Cookie": self.cookie,
                       "X-MultiDot-CSRF": self.csrf, "Content-Type": "application/json"}
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            body = response.read()
            return response, body
        finally:
            connection.close()

    def finish(self, expected=0):
        stdout, stderr = self.process.communicate(timeout=5)
        self.assertEqual(self.process.returncode, expected)
        output = self.initial_output + stdout.decode() + stderr.decode()
        self.assertNotIn("REJECTED_FAKE_MARKER_ONLY", output)
        self.assertEqual(list(self.root.glob("multidot-manual-web-fixture-*")), [])
        return output

    def payload(self, key=MARKER):
        return {"consent": True, "dots": [{"name": "Fixture dot 1",
            "tunnel_id": "tunnel_" + "0" * 31 + "1", "runtime_api_key": key}]}

    def test_fake_save_finishes_without_preparing_any_runtime(self):
        response, body = self.request("POST", "/save", self.payload())
        self.assertEqual(response.status, 200)
        self.assertNotIn(MARKER.encode(), body)
        output = self.finish()
        self.assertIn('"save_passed": true', output)
        self.assertIn('"preparation_skipped": true', output)

    def test_nonfixture_input_is_refused_server_side_and_never_echoed(self):
        response, body = self.request("POST", "/save", self.payload("REJECTED_FAKE_MARKER_ONLY"))
        self.assertEqual(response.status, 500)
        self.assertNotIn(b"REJECTED_FAKE_MARKER_ONLY", body)
        self.assertIn("fixture_values_only", self.finish(expected=2))

    def test_cancel_exits_and_removes_disposable_fixture(self):
        response, _ = self.request("POST", "/cancel", {})
        self.assertEqual(response.status, 200)
        self.assertIn('"save_passed": false', self.finish())


if __name__ == "__main__":
    unittest.main()
