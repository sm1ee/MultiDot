"""Short-lived loopback-only collection of a new private configuration.

This collector never provisions, launches services, or opens a browser. It shares
terminal setup's owner-only lock, blank-only snapshot, and atomic publication.
Tests and agents must use disposable homes and synthetic values only.
"""
from __future__ import annotations

from html import escape
import json
import re
import secrets
import socket
import socketserver
import time

import multidot_setup as setup
import multidot_wizard as wizard

SESSION_SECONDS = 600
REQUEST_SECONDS = 5
MAX_REQUESTS = 128
MAX_HEADER_BYTES = 16384
MAX_HEADER_LINES = 64
MAX_BODY_BYTES = wizard.MAX_CONFIG_BYTES

# Static responses never include request values, parser errors, or tracebacks.
RESPONSES = {
    200: ("OK", b'{"ok":true}'),
    400: ("Bad Request", b'{"ok":false,"error":"invalid_request"}'),
    403: ("Forbidden", b'{"ok":false,"error":"request_refused"}'),
    404: ("Not Found", b'{"ok":false,"error":"not_found"}'),
    405: ("Method Not Allowed", b'{"ok":false,"error":"method_not_allowed"}'),
    408: ("Request Timeout", b'{"ok":false,"error":"request_timeout"}'),
    410: ("Gone", b'{"ok":false,"error":"session_closed"}'),
    413: ("Content Too Large", b'{"ok":false,"error":"request_too_large"}'),
    500: ("Internal Server Error", b'{"ok":false,"error":"save_failed"}'),
}

PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MultiDot local setup</title>
<style nonce="__NONCE__">
:root { color-scheme: light dark; font: 16px system-ui, sans-serif; }
body { max-width: 48rem; margin: 2rem auto; padding: 0 1rem; }
h1 { font-size: 1.7rem; } fieldset { margin: 1rem 0; padding: 1rem; }
label { display: block; margin: .8rem 0; } input { display: block;
box-sizing: border-box; width: 100%; padding: .65rem; font: inherit; }
button { padding: .6rem .9rem; margin: .3rem; font: inherit; cursor: pointer; }
.paths { overflow-wrap: anywhere; } #status { min-height: 1.5rem; }
</style></head><body>
<h1>MultiDot local setup</h1>
<p>Enter existing dot details. Each dot will have the worker role.
API keys stay hidden and are saved in an owner-only local plaintext file.</p>
<p class="paths">Configuration file: __CONFIG__<br>Local state directory: __STATE__</p>
<p>Save and prepare local setup authorizes saving this configuration and preparing
local state, including issuing internal runtime credentials. Preparation continues
in your terminal. It does not contact external services or start the runtime.</p>
<p>This local form expires in 10 minutes. Closing it cancels the session.
Your browser must be on the same computer as the terminal.</p>
<form id="setup" autocomplete="off" method="post">
<div id="dots"></div>
<button id="add" type="button">Add dot</button>
<button id="save" type="submit">Save and prepare local setup</button>
<button id="cancel" type="button">Cancel</button>
</form>
<p id="status" role="status" aria-live="polite"></p>
<noscript>JavaScript is required. Close this page and use terminal setup instead.</noscript>
<script nonce="__NONCE__">
"use strict";
(() => {
  let csrf = "__CSRF__";
  const expires = Date.now() + __REMAINING_MS__;
  let finished = false;
  let submitting = false;
  let payload = null;
  let wire = null;
  const form = document.getElementById("setup");
  const dots = document.getElementById("dots");
  const status = document.getElementById("status");
  function clearValues() {
    form.querySelectorAll("input").forEach(input => { input.value = ""; });
    if (payload && payload.dots) {
      payload.dots.forEach(row => {
        row.name = ""; row.tunnel_id = ""; row.runtime_api_key = "";
      });
      payload.dots.length = 0;
    }
    payload = null;
    wire = null;
  }
  function finish(message) {
    finished = true;
    clearValues();
    csrf = "";
    form.replaceChildren();
    status.textContent = message;
  }
  function expired() {
    if (!finished && Date.now() >= expires) {
      finish("This session expired. Close this page and run setup again.");
      return true;
    }
    return finished;
  }
  function addDot() {
    if (expired() || submitting) return;
    const row = document.createElement("fieldset");
    const legend = document.createElement("legend");
    legend.textContent = "Dot";
    row.appendChild(legend);
    for (const [key, title, type, maximum] of [
      ["name", "Dot name", "text", 320],
      ["tunnel_id", "Tunnel ID", "text", 39],
      ["runtime_api_key", "Runtime API key (hidden)", "password", 8191]
    ]) {
      const label = document.createElement("label");
      label.textContent = title;
      const input = document.createElement("input");
      input.type = type;
      input.dataset.field = key;
      input.required = true;
      input.maxLength = maximum;
      input.autocomplete = "off";
      input.spellcheck = false;
      input.setAttribute("autocapitalize", "off");
      input.setAttribute("data-lpignore", "true");
      input.setAttribute("data-1p-ignore", "true");
      label.appendChild(input);
      row.appendChild(label);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Remove dot";
    remove.addEventListener("click", () => {
      if (expired() || submitting) return;
      row.querySelectorAll("input").forEach(input => { input.value = ""; });
      row.remove();
    });
    row.appendChild(remove);
    dots.appendChild(row);
  }
  async function submit(action) {
    if (expired() || submitting) return;
    if (action === "save" && (!dots.children.length || !form.reportValidity())) {
      status.textContent = "Enter at least one complete dot.";
      return;
    }
    submitting = true;
    form.querySelectorAll("button").forEach(button => { button.disabled = true; });
    payload = action === "save" ? {consent: true, dots: []} : {};
    if (action === "save") {
      for (const row of dots.children) {
        const item = {};
        row.querySelectorAll("input").forEach(input => {
          item[input.dataset.field] = input.dataset.field === "runtime_api_key"
            ? input.value : input.value.trim();
        });
        payload.dots.push(item);
      }
    }
    wire = JSON.stringify(payload);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const pending = fetch("/" + action, {
        method: "POST", mode: "same-origin", credentials: "same-origin",
        cache: "no-store", redirect: "error", signal: controller.signal,
        headers: {"Content-Type": "application/json", "X-MultiDot-CSRF": csrf},
        body: wire
      });
      clearValues();
      const response = await pending;
      if (response.ok) {
        finish(action === "save"
          ? "Configuration saved. Local preparation continues in your terminal. You can close this page."
          : "Setup cancelled. You can close this page.");
      } else {
        finish("Setup was not confirmed. Check your terminal before trying again. Values have been cleared.");
      }
    } catch (_) {
      finish("The result could not be confirmed. Check your terminal before trying again. Values have been cleared.");
    } finally {
      clearTimeout(timeout);
      clearValues();
    }
  }
  form.addEventListener("submit", event => { event.preventDefault(); submit("save"); });
  document.getElementById("add").addEventListener("click", addDot);
  document.getElementById("cancel").addEventListener("click", () => submit("cancel"));
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); submit("cancel"); }
  });
  window.addEventListener("pagehide", () => {
    clearValues();
    if (!finished && !submitting) {
      fetch("/cancel", {
        method: "POST", mode: "same-origin", credentials: "same-origin",
        cache: "no-store", redirect: "error", keepalive: true,
        headers: {"Content-Type": "application/json", "X-MultiDot-CSRF": csrf},
        body: "{}"
      }).catch(() => {});
    }
    finish("Session closed. Check your terminal for the final result.");
  });
  window.addEventListener("pageshow", event => {
    if (event.persisted) finish("Session closed. Run setup again if needed.");
    else expired();
  });
  document.addEventListener("visibilitychange", expired);
  setTimeout(expired, Math.max(0, expires - Date.now()));
  addDot();
})();
</script></body></html>'''


class _RequestError(Exception):
    def __init__(self, status):
        self.status = status


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_key")
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError("invalid_constant")


class _Server(socketserver.TCPServer):
    allow_reuse_address = False
    request_queue_size = 4
    address_family = socket.AF_INET

    def __init__(self, publish, result, path, state_root):
        self.publish = publish
        self.result = result
        self.path = path
        self.state_root = state_root
        self.deadline = time.monotonic() + SESSION_SECONDS
        self.done = False
        self.error = None
        self.requests = 0
        self.cookie_issued = False
        self.cookie_name = "multidot_setup_" + secrets.token_hex(12)
        self.cookie = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", 0), _Handler)
        self.host = "127.0.0.1:" + str(self.server_address[1])
        self.origin = "http://" + self.host
        self.timeout = 0.25

    def handle_error(self, request, client_address):
        # socketserver's default prints tracebacks. Keep all failures static.
        self.error = setup.SetupError("web_setup_failed")
        self.done = True

    def page(self):
        return (PAGE.replace("__NONCE__", self.nonce)
                .replace("__CSRF__", self.csrf)
                .replace("__REMAINING_MS__", str(max(0, int(
                    (self.deadline - time.monotonic()) * 1000))))
                .replace("__CONFIG__", escape(str(self.path), quote=True))
                .replace("__STATE__", escape(str(self.state_root), quote=True))
                .encode("utf-8"))


class _Handler(socketserver.BaseRequestHandler):
    """Strict one-request HTTP connection with absolute time and size bounds."""

    def handle(self):
        self.server.requests += 1
        self.deadline = min(self.server.deadline,
                            time.monotonic() + REQUEST_SECONDS)
        self.closed_session = False
        self.issue_cookie = False
        try:
            if time.monotonic() >= self.server.deadline:
                raise _RequestError(410)
            method, path, headers, body = self._read_request()
            self._dispatch(method, path, headers, body)
        except _RequestError as exc:
            self._reply(exc.status)
        except (TimeoutError, socket.timeout):
            self._reply(408)
        except (ConnectionError, OSError):
            # A closed client never changes whether atomic publication happened.
            pass
        finally:
            if (time.monotonic() >= self.server.deadline or
                    self.server.requests >= MAX_REQUESTS):
                self.server.done = True
            if self.server.done and not self.server.result["requested_setup"]:
                self.server.result["cancelled"] = True

    def _receive(self, maximum):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _RequestError(408)
        self.request.settimeout(remaining)
        data = self.request.recv(maximum)
        if not data:
            raise _RequestError(400)
        return data

    def _read_request(self):
        raw = bytearray()
        while b"\r\n\r\n" not in raw:
            if len(raw) >= MAX_HEADER_BYTES:
                raise _RequestError(413)
            raw.extend(self._receive(min(4096, MAX_HEADER_BYTES - len(raw))))
        header, body = bytes(raw).split(b"\r\n\r\n", 1)
        lines = header.split(b"\r\n")
        if len(lines) > MAX_HEADER_LINES or len(lines[0]) > 4096:
            raise _RequestError(413)
        try:
            method, path, version = lines[0].decode("ascii").split(" ")
        except (ValueError, UnicodeError):
            raise _RequestError(400) from None
        if version != "HTTP/1.1" or not re.fullmatch(r"[A-Z]+", method):
            raise _RequestError(400)
        headers = {}
        for line in lines[1:]:
            if b":" not in line:
                raise _RequestError(400)
            name, value = line.split(b":", 1)
            if not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise _RequestError(400)
            if any(char < 32 or char > 126 for char in value):
                raise _RequestError(400)
            name = name.decode("ascii").lower()
            if name in headers:
                raise _RequestError(400)
            headers[name] = value.decode("ascii").strip(" ")
        if headers.get("host") != self.server.host:
            raise _RequestError(403)
        if headers.get("origin", self.server.origin) != self.server.origin:
            raise _RequestError(403)
        if headers.get("sec-fetch-site", "none") not in ("none", "same-origin"):
            raise _RequestError(403)
        if any(name in headers for name in ("transfer-encoding", "expect", "trailer")):
            raise _RequestError(400)
        length = headers.get("content-length", "0")
        if not re.fullmatch(r"[0-9]{1,9}", length):
            raise _RequestError(400)
        length = int(length)
        if length > MAX_BODY_BYTES:
            raise _RequestError(413)
        if method == "POST":
            # Authenticate before deliberately reading the remaining request body.
            self._authenticate(headers)
            if headers.get("content-type") != "application/json":
                raise _RequestError(400)
            if "content-length" not in headers or not length:
                raise _RequestError(400)
        elif length or body:
            raise _RequestError(400)
        if len(body) > length:
            raise _RequestError(400)
        while len(body) < length:
            body += self._receive(min(65536, length - len(body)))
        if time.monotonic() >= self.deadline:
            raise _RequestError(408)
        return method, path, headers, body

    def _authenticate(self, headers):
        if headers.get("origin") != self.server.origin:
            raise _RequestError(403)
        if not self.server.cookie_issued:
            raise _RequestError(403)
        if not self._has_cookie(headers):
            raise _RequestError(403)
        if not secrets.compare_digest(headers.get("x-multidot-csrf", ""),
                                      self.server.csrf):
            raise _RequestError(403)

    def _has_cookie(self, headers):
        found = []
        for part in headers.get("cookie", "").split(";"):
            key, separator, value = part.strip().partition("=")
            if key == self.server.cookie_name and separator:
                found.append(value)
        return len(found) == 1 and secrets.compare_digest(found[0], self.server.cookie)

    def _dispatch(self, method, path, headers, body):
        if self.server.done:
            raise _RequestError(410)
        if method == "GET":
            if path != "/":
                raise _RequestError(404)
            if self.server.cookie_issued and not self._has_cookie(headers):
                raise _RequestError(403)
            self.server.cookie_issued = True
            self.issue_cookie = True
            self._reply(200, self.server.page(), "text/html; charset=utf-8")
            return
        if method != "POST":
            raise _RequestError(405)
        if path not in ("/save", "/cancel"):
            raise _RequestError(404)
        try:
            value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_pairs,
                               parse_constant=_invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise _RequestError(400) from None
        finally:
            body = b""
        if not isinstance(value, dict):
            raise _RequestError(400)
        if path == "/cancel":
            if value:
                raise _RequestError(400)
            self.server.result["cancelled"] = True
            self.server.done = self.closed_session = True
            self._reply(200)
            return
        data = None
        try:
            if set(value) != {"consent", "dots"} or value["consent"] is not True:
                raise _RequestError(400)
            if not isinstance(value["dots"], list):
                raise _RequestError(400)
            for row in value["dots"]:
                if (not isinstance(row, dict) or
                        set(row) != {"name", "tunnel_id", "runtime_api_key"} or
                        not row["runtime_api_key"]):
                    raise _RequestError(400)
            spec = {"schema_version": 1, "dots": value["dots"]}
            try:
                setup.validate_dots(spec)
                data = setup.json_bytes(spec)
            except (setup.SetupError, UnicodeError, ValueError):
                raise _RequestError(400) from None
            if len(data) > wizard.MAX_CONFIG_BYTES:
                raise _RequestError(413)
            # Consume approval exactly once, before attempting publication.
            self.server.done = self.closed_session = True
            try:
                self.server.publish(data, self.server.result)
                self.server.result["configured_dots"] = len(value["dots"])
                self.server.result["requested_setup"] = True
            except setup.SetupError as exc:
                self.server.error = exc
            except Exception:
                self.server.error = setup.SetupError("web_setup_failed")
            self._reply(500 if self.server.error else 200)
        finally:
            for row in value.get("dots", []) if isinstance(value.get("dots"), list) else []:
                if isinstance(row, dict):
                    row.clear()
            value.clear()
            data = None

    def _reply(self, status, body=None, content_type="application/json"):
        reason, default = RESPONSES[status]
        body = default if body is None else body
        # The final permitted request closes the collector even if it failed
        # validation. Do not leave its cookie alive until the normal expiry.
        self.closed_session = (self.closed_session or self.server.done or
                               self.server.requests >= MAX_REQUESTS)
        csp = ("default-src 'none'; script-src 'nonce-" + self.server.nonce +
               "'; style-src 'nonce-" + self.server.nonce +
               "'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
               "frame-ancestors 'none'; object-src 'none'")
        headers = ["HTTP/1.1 " + str(status) + " " + reason,
                   "Content-Type: " + content_type,
                   "Content-Length: " + str(len(body)),
                   "Connection: close", "Cache-Control: no-store",
                   "Pragma: no-cache", "Referrer-Policy: no-referrer",
                   "X-Content-Type-Options: nosniff", "X-Frame-Options: DENY",
                   "Cross-Origin-Resource-Policy: same-origin",
                   "Cross-Origin-Opener-Policy: same-origin",
                   "Content-Security-Policy: " + csp]
        if self.issue_cookie or self.closed_session:
            age = 0 if self.closed_session else max(1, int(
                self.server.deadline - time.monotonic()))
            value = "" if self.closed_session else self.server.cookie
            headers.append("Set-Cookie: " + self.server.cookie_name + "=" + value +
                           "; HttpOnly; SameSite=Strict; Path=/; Max-Age=" + str(age))
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            self.request.settimeout(remaining)
            self.request.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body)
        except (ConnectionError, OSError):
            pass


def collect_and_save(path, state_root=None):
    """Collect a single local approval; return public metadata, never provision."""
    result = {"ok": True, "saved": False, "requested_setup": False,
              "configured_dots": 0, "cancelled": False,
              "values_displayed": False}
    try:
        path = setup.expand_path(path)
        state_root = (setup.expand_path(state_root) if state_root is not None else
                      setup.application_home() / "state")
        # Refuse unsafe or filled destinations before opening a port or showing
        # the URL. Keep the lock and initial snapshot through final approval.
        with wizard.private_destination(path) as publish:
            with _Server(publish, result, path, state_root) as server:
                print(server.origin + "/", flush=True)
                while not server.done:
                    remaining = server.deadline - time.monotonic()
                    if remaining <= 0 or server.requests >= MAX_REQUESTS:
                        result["cancelled"] = True
                        break
                    server.timeout = min(0.25, remaining)
                    server.handle_request()
                if server.error:
                    raise server.error
                if not result["requested_setup"]:
                    result["cancelled"] = True
        return result
    except (EOFError, KeyboardInterrupt):
        result["cancelled"] = True
        result["requested_setup"] = False
        return result
    except setup.SetupError as exc:
        exc.config_saved = result["saved"]
        raise
    except Exception:
        exc = setup.SetupError("web_setup_failed")
        exc.config_saved = result["saved"]
        raise exc from None
