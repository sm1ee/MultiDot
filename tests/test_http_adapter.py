"""MOCK HTTP schema double. Not a LOCAL_CONTRACT test against pinned dot2api."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
import unittest

from multidot.dot2api_adapter import NativeTaskAdapter, ProtocolError, TransportUncertain, UpstreamRejected
from multidot.models import ValidationError


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.handle_request()

    def do_GET(self):
        self.handle_request()

    def handle_request(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        self.server.received.append((self.command, self.path, json.loads(body) if body else None, self.headers.get("Authorization")))
        self.send_response(self.server.reply_status)
        if self.server.reply_status == 302:
            self.send_header("Location", self.server.base_url + "/secret-destination")
        self.end_headers()
        self.wfile.write(self.server.reply_body)

    def log_message(self, *_):
        pass


class HTTPAdapterTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.base_url = "http://127.0.0.1:" + str(self.server.server_port)
        self.server.received = []
        self.server.reply_status = 200
        self.server.reply_body = b'{"task":{"id":"task-1"}}'
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.adapter = NativeTaskAdapter(self.server.base_url, "MOCK-only-credential")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_native_paths_and_no_proxy_api(self):
        body = {"queue": "dot-b", "instructions": "Review", "payload": {}, "idempotency_key": "md-example", "max_attempts": 1, "ttl_seconds": 3600}
        self.adapter.submit(body)
        self.adapter.get("task-1")
        self.adapter.cancel("task-1", "User request")
        received = self.server.received
        self.assertEqual([(r[0], r[1]) for r in received], [("POST", "/v1/tasks"), ("GET", "/v1/tasks/task-1"), ("POST", "/v1/tools/cancel_task")])
        self.assertEqual(received[0][2], body)
        self.assertEqual(received[2][2], {"task_id": "task-1", "reason": "User request"})
        self.assertEqual(received[0][3], "Bearer MOCK-only-credential")

    def test_empty_413_is_handled_without_echo(self):
        self.server.reply_status, self.server.reply_body = 413, b""
        with self.assertRaises(UpstreamRejected) as context:
            self.adapter.get("task-1")
        self.assertEqual(context.exception.status, 413)
        self.assertNotIn("MOCK-only-credential", str(context.exception))

    def test_empty_408_is_uncertain(self):
        self.server.reply_status, self.server.reply_body = 408, b""
        with self.assertRaises(TransportUncertain):
            self.adapter.get("task-1")

    def test_non_json_and_oversized_responses_fail_closed(self):
        for body in (b"not-json", b"x" * 262145):
            self.server.reply_body = body
            with self.assertRaises(ProtocolError):
                self.adapter.get("task-1")

    def test_redirect_never_forwards_credentials(self):
        self.server.reply_status = 302
        with self.assertRaises(UpstreamRejected):
            self.adapter.get("task-1")
        self.assertEqual(len(self.server.received), 1)

    def test_insecure_remote_and_embedded_credentials_rejected(self):
        for url in ("http://example.com", "https://user:pass@example.com", "https://example.com/?token=x", "file:///tmp/x"):
            with self.assertRaises(ValidationError):
                NativeTaskAdapter(url, "MOCK-token")


if __name__ == "__main__":
    unittest.main()
