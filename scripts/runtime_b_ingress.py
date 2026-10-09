#!/usr/bin/env python3
"""Loopback-only, fixed B MCP ingress. No arbitrary proxying or credential setup.

Forwarded connector Authorization may override tunnel-client's static value.
This boundary requires exactly the configured B value and never accepts another
upstream principal's token, even if that token is valid at dot2api directly.
"""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import threading
import urllib.error
import urllib.request

from runtime_supervisor import load, probe, secret


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    c = load(args.config)
    authorization = secret(Path(c["state_root"]) / "secrets/worker-b-authorization")
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
                # No OAuth metadata is intentionally advertised. Never proxy
                # UI, API routes, health details, or capability-token URLs.
                self.respond(404)

        def do_POST(self):
            if self.path != "/mcp":
                return self.respond(404)
            headers = self.headers.get_all("Authorization", [])
            if len(headers) != 1 or not hmac.compare_digest(headers[0], authorization):
                return self.respond(401)
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
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

    server = ThreadingHTTPServer(("127.0.0.1", c["ingress_port"]), Handler)
    server.daemon_threads = True
    def stop(_sig, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


if __name__ == "__main__":
    main()
