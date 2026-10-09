#!/usr/bin/env python3
"""Loopback-only MCP ingress for one explicitly configured stable worker ID.

Only the selected worker's exact Authorization value is accepted, including
when a connector forwards a header that replaces the tunnel's static value.
No arbitrary proxy destinations, credential setup, or other upstream routes.
"""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import signal
import sys
import threading
import urllib.error
import urllib.request

from runtime_supervisor import get_worker, load, NoRedirect, probe, secret, worker_secret_path


def create_server(c, worker_id):
    """Bind one loopback listener; start no serving thread or upstream request."""
    entry = get_worker(c, worker_id)
    authorization = secret(worker_secret_path(c, worker_id, "worker-authorization"))
    if not authorization.startswith("Bearer ") or not authorization[7:]:
        raise ValueError("Worker authorization must contain the complete Bearer header")
    expected = authorization.encode("utf-8")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    slots = threading.BoundedSemaphore(16)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_args):
            pass

        def respond(self, code, body=b"", media="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/readyz":
                self.respond(200 if probe(c["upstream_port"]) else 503, b'{}')
            else:
                # Never proxy UI, API, OAuth metadata, health details, or
                # capability-token URLs. MCP is available only via POST /mcp.
                self.respond(404)

        def do_POST(self):
            if self.path != "/mcp":
                return self.respond(404)
            headers = self.headers.get_all("Authorization", [])
            if len(headers) != 1 or not hmac.compare_digest(headers[0].encode("utf-8"), expected):
                return self.respond(401)
            lengths = self.headers.get_all("Content-Length", [])
            if (len(lengths) != 1 or self.headers.get_all("Transfer-Encoding", []) or
                    not re.fullmatch(r"[0-9]+", lengths[0])):
                return self.respond(400)
            try:
                length = int(lengths[0])
            except ValueError:
                return self.respond(400)
            if not 0 < length <= 262144:
                return self.respond(413)
            if not slots.acquire(blocking=False):
                return self.respond(503)
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    return self.respond(400)
                request = urllib.request.Request(f"http://127.0.0.1:{c['upstream_port']}/mcp", data=body,
                    headers={"Content-Type": "application/json", "Authorization": authorization}, method="POST")
                try:
                    with opener.open(request, timeout=30) as response:
                        result = response.read(262145)
                        if len(result) > 262144:
                            return self.respond(502)
                        return self.respond(response.status, result)
                except urllib.error.HTTPError as error:
                    # Upstream error bodies cannot leak through this boundary.
                    error.close()
                    return self.respond(error.code)
                except (OSError, ValueError):
                    return self.respond(502)
            finally:
                slots.release()

    server = ThreadingHTTPServer(("127.0.0.1", entry["ingress_port"]), Handler)
    server.daemon_threads = True
    return server


def serve(c, worker_id):
    server = create_server(c, worker_id)
    def stop(_sig, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--worker-id", required=True)
    args = parser.parse_args(argv)
    try:
        return serve(load(args.config), args.worker_id)
    except Exception as error:
        # Do not expose file contents, headers, transport data, or error text.
        print(json.dumps({"error": type(error).__name__, "message": "Worker ingress startup failed"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
