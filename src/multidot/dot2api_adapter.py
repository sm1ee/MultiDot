"""Native Task API only. No upstream DB access, worker lease, or credential setup."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .models import ValidationError, canonical, identifier, upstream_identifier, wire_bounded

UPSTREAM_COMMIT = "66a761505ff73c5dca730260efcaeb5697db81ad"


class TransportUncertain(Exception):
    """The operation may have reached upstream. Retry only the exact stored body."""


class UpstreamRejected(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Upstream HTTP {status}; operator review required")


class ProtocolError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NativeTaskAdapter:
    def __init__(self, base_url, token, producer="hub-producer", timeout=10):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                or parsed.scheme not in ("http", "https") or not parsed.hostname):
            raise ValidationError("Use a base origin without credentials, path, query or fragment")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValidationError("Plain HTTP is allowed only on loopback")
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise ValidationError("A securely supplied producer token is required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.producer = identifier(producer)
        self.target = self.base_url
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def _request(self, method, path, body=None):
        if body is not None:
            wire_bounded(body)
        request = urllib.request.Request(self.base_url + path, data=None if body is None else canonical(body).encode(),
                                         method=method, headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                data = response.read(262145)
                if len(data) > 262144:
                    raise ProtocolError("Oversized upstream response")
        except urllib.error.HTTPError as exc:
            # Never log untrusted error bodies, headers, URLs, or credentials.
            status = exc.code
            exc.close()
            if status >= 500 or status == 408:
                raise TransportUncertain("Upstream response uncertain") from None
            raise UpstreamRejected(status) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise TransportUncertain("Upstream transport uncertain") from None
        try:
            parsed = json.loads(data)
            task = parsed["task"]
            if not isinstance(task, dict):
                raise ValueError
            return task
        except (ValueError, KeyError, TypeError):
            raise ProtocolError("Invalid native task response") from None

    def submit(self, body):
        return self._request("POST", "/v1/tasks", body)

    def get(self, task_id):
        upstream_identifier(task_id)
        return self._request("GET", "/v1/tasks/" + urllib.parse.quote(task_id, safe=""))

    def cancel(self, task_id, reason):
        upstream_identifier(task_id)
        return self._request("POST", "/v1/tools/cancel_task", {"task_id": task_id, "reason": reason})
