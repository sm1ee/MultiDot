"""Disposable actual-upstream fixture, kept outside the ordinary MOCK suite.

Only fixture provisioning and test-clock injection touch upstream objects. The
controller itself receives the normal NativeTaskAdapter and has its own DB.
"""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import tempfile
from threading import Thread
import time
import urllib.error
import urllib.request
from unittest.mock import patch
import uuid

import uvicorn

from dot2api.app import create_app
from dot2api.cli import initialize
from dot2api.config import Settings
from multidot.dot2api_adapter import NativeTaskAdapter

CLEANUPS = []
IDENTITIES = [
    ("contract-producer", "contract-tenant", ["dot-a", "dot-b", "dot-c"], ["read", "submit"]),
    ("contract-peer", "contract-tenant", ["dot-a", "dot-b", "dot-c"], ["read", "submit"]),
    ("contract-worker-b", "contract-tenant", ["dot-b"], ["read", "work"]),
    ("contract-worker-b-peer", "contract-tenant", ["dot-b"], ["read", "work"]),
    ("contract-worker-c", "contract-tenant", ["dot-c"], ["read", "work"]),
    ("contract-outsider", "contract-other-tenant", ["dot-b"], ["read", "submit", "work"]),
]


class FaultBoundary:
    """Opt-in response fault after the actual upstream operation has executed."""
    def __init__(self, app):
        self.app = app
        self.next_fault = None
        self.faults = []

    async def __call__(self, scope, receive, send):
        fault = self.next_fault
        if scope["type"] != "http" or not fault or (scope["method"], scope["path"]) != fault[:2]:
            return await self.app(scope, receive, send)
        self.next_fault = None
        messages = []

        async def capture(message):
            messages.append(message)

        await self.app(scope, receive, capture)
        mode = fault[2]
        self.faults.append(mode)
        if mode == "timeout":
            await asyncio.sleep(0.2)
            for message in messages:
                await send(message)
            return
        status, body = {
            "503_after_commit": (503, b""),
            "invalid_json": (200, b"{"),
            "oversized_response": (200, b"x" * 262145),
        }[mode]
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class NativeFixture:
    def __init__(self, max_body_bytes=262144):
        self.temp = tempfile.TemporaryDirectory(prefix="multidot-local-contract-")
        self.root = Path(self.temp.name)
        self.server = None
        self.thread = None
        self.socket = None
        self.tokens = {}
        self.transport_calls = 0
        self.closed = False
        self.now = [time.time()]
        try:
            # Official initialization API and the same Security fixture methods
            # used by upstream tests. No test/controller SQL opens upstream data.
            initialize(self.root / "upstream")
            self.app = create_app(Settings(data_dir=self.root / "upstream", worker_enabled=False,
                                           rate_per_minute=10000, max_body_bytes=max_body_bytes), transport=self.forbid_transport)
            self.app.state.service.clock = lambda: self.now[0]
            self.app.state.security.clock = lambda: self.now[0]
            self.identity_bindings = []
            for principal, tenant, queues, scopes in IDENTITIES:
                self.app.state.security.create_principal(principal, tenant, queues, scopes)
                # Temporary deliberately test-marked credentials; never printed
                # or saved in the controller, repository, or evidence.
                with patch("dot2api.security.secrets.token_urlsafe",
                           return_value="LOCAL_CONTRACT_ONLY_" + uuid.uuid4().hex):
                    token = self.app.state.security.issue_token(principal, 3600)
                self.tokens[principal] = token
                authenticated = self.app.state.security.authenticate(token)
                assert authenticated.id == principal and authenticated.tenant == tenant
                self.identity_bindings.append({"principal": principal, "tenant": tenant,
                                               "queues": list(authenticated.queues),
                                               "scopes": list(authenticated.scopes)})
            self.boundary = FaultBoundary(self.app)
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.bind(("127.0.0.1", 0))
            self.socket.listen(128)
            self.port = self.socket.getsockname()[1]
            self.base_url = f"http://127.0.0.1:{self.port}"
            config = uvicorn.Config(self.boundary, host="127.0.0.1", port=self.port,
                                    access_log=False, log_level="critical", proxy_headers=False,
                                    timeout_graceful_shutdown=3)
            self.server = uvicorn.Server(config)
            self.thread = Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=False)
            self.thread.start()
            deadline = time.monotonic() + 5
            while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not self.server.started:
                raise RuntimeError("Owned loopback test server did not start")
            assert self.http("GET", "/readyz", principal=None)[0] == 200
        except BaseException:
            self.close()
            raise

    def forbid_transport(self, *_args, **_kwargs):
        self.transport_calls += 1
        raise AssertionError("External transport is forbidden in LOCAL_CONTRACT")

    def adapter(self, principal="contract-producer", timeout=2):
        return NativeTaskAdapter(self.base_url, self.tokens[principal], producer=principal, timeout=timeout)

    def http(self, method, path, body=None, principal="contract-producer", raw=None):
        headers = {"Content-Type": "application/json"}
        if principal:
            headers["Authorization"] = "Bearer " + self.tokens[principal]
        data = raw if raw is not None else None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            try:
                return error.code, error.read(), dict(error.headers)
            finally:
                error.close()

    def tool(self, name, body, principal="contract-producer"):
        status, raw, _headers = self.http("POST", "/v1/tools/" + name, body, principal)
        return status, json.loads(raw) if raw else None

    def fault(self, method, path, mode):
        assert self.boundary.next_fault is None
        self.boundary.next_fault = (method, path, mode)

    def advance(self, seconds):
        self.now[0] += seconds

    def close(self):
        if self.closed:
            return
        self.closed = True
        stopped = True
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=5)
            stopped = not self.thread.is_alive()
        if self.socket:
            self.socket.close()
        port_closed = True
        if hasattr(self, "port"):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                port_closed = probe.connect_ex(("127.0.0.1", self.port)) != 0
        self.tokens.clear()
        self.temp.cleanup()
        record = {"at": datetime.now(timezone.utc).isoformat(), "server_stopped": stopped,
                  "loopback_listener_closed": port_closed, "temporary_state_removed": not self.root.exists(),
                  "external_transport_calls": self.transport_calls,
                  "fault_modes": getattr(getattr(self, "boundary", None), "faults", [])}
        CLEANUPS.append(record)
        assert stopped and port_closed and record["temporary_state_removed"] and self.transport_calls == 0
