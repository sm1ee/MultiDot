"""Web setup MOCK fixtures: IPv4 loopback, disposable homes, fake keys only.

These tests never open a browser, provision upstream, or contact a real account.
UI checks cover source contracts; they do not claim visual browser verification.
"""
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from html.parser import HTMLParser
from io import StringIO
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import multidot_setup as setup
import multidot_web as web
import multidot_wizard as wizard

MARKER = "LOCAL_WEB_FIXTURE_ONLY_NEVER_A_REAL_KEY"


def candidate(count=1):
    return {"consent": True, "dots": [
        {"name": "\uc5f0\uad6c \ub2f4\ub2f9 " + str(index),
         "tunnel_id": "tunnel_" + format(index + 1, "032x"),
         "runtime_api_key": MARKER}
        for index in range(count)]}


def metadata():
    return {"ok": True, "saved": False, "requested_setup": False,
            "configured_dots": 0, "cancelled": False, "values_displayed": False}


def request_bytes(server, method="GET", path="/", body=b"", headers=None,
                  extra=(), authenticated=False):
    values = {"Host": server.host}
    if authenticated:
        values.update({"Origin": server.origin,
                       "Cookie": server.cookie_name + "=" + server.cookie,
                       "X-MultiDot-CSRF": server.csrf,
                       "Content-Type": "application/json"})
    if method == "POST" or body:
        values["Content-Length"] = str(len(body))
    for key, value in (headers or {}).items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    lines = [method + " " + path + " HTTP/1.1"]
    lines += [key + ": " + value for key, value in values.items()]
    lines += [key + ": " + value for key, value in extra]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def exchange(server, raw):
    # The only network target used by this suite is this fixture's bound socket.
    assert server.server_address[0] == "127.0.0.1"
    with socket.create_connection(server.server_address, timeout=2) as connection:
        connection.sendall(raw)
        chunks = []
        while True:
            try:
                chunk = connection.recv(65536)
            except ConnectionResetError:
                break
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def response_parts(raw):
    header, body = raw.split(b"\r\n\r\n", 1)
    lines = header.decode("ascii").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = dict(line.split(": ", 1) for line in lines[1:])
    return status, headers, body


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class WebFixtureTests(unittest.TestCase):
    def setUp(self):
        self.scope = ExitStack()
        self.addCleanup(self.scope.close)
        self.root = Path(self.scope.enter_context(
            tempfile.TemporaryDirectory(prefix="multidot-web-fixture-")))
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.scope.enter_context(patch.dict(os.environ, {"HOME": str(self.home)}))
        self.config = setup.default_config()
        self.state = self.home / "fixture-state"
        self.output, self.errors = StringIO(), StringIO()
        self.scope.enter_context(redirect_stdout(self.output))
        self.scope.enter_context(redirect_stderr(self.errors))
        self.provision = self.scope.enter_context(patch.object(
            setup, "provision_stage", side_effect=AssertionError("No provisioning")))
        self.configure = self.scope.enter_context(patch.object(
            setup, "configure", side_effect=AssertionError("No preparation")))
        self.addCleanup(self.check_private_contract)

    def check_private_contract(self):
        self.provision.assert_not_called()
        self.configure.assert_not_called()
        self.assertNotIn(MARKER, self.output.getvalue() + self.errors.getvalue())
        self.assertEqual(self.errors.getvalue(), "")
        self.assertFalse(list(self.home.rglob("*.pending")))
        self.assertFalse(self.state.exists())

    @contextmanager
    def serving(self, publish=None, path=None, state=None):
        saved = []
        def save(data, result):
            saved.append(json.loads(data))
            result["saved"] = True
        server = web._Server(save if publish is None else publish, metadata(),
                             self.config if path is None else path,
                             self.state if state is None else state)
        server.timeout = 0.01
        server.fixture_saved = saved
        stopped = threading.Event()
        def work():
            # Keep accepting only inside this test to verify repeated-submit
            # rejection after the production loop would already have stopped.
            while not stopped.is_set():
                server.handle_request()
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        try:
            yield server
        finally:
            stopped.set()
            worker.join(timeout=3)
            server.server_close()
            self.assertFalse(worker.is_alive(), "Fixture listener did not stop")

    @contextmanager
    def collecting(self, **kwargs):
        ready = threading.Event()
        observed = {}
        original = web._Server
        def make_server(*args, **options):
            server = original(*args, **options)
            observed["server"] = server
            ready.set()
            return server
        def run():
            try:
                observed["result"] = web.collect_and_save(
                    kwargs.get("path", self.config), state_root=self.state)
            except BaseException as exc:
                observed["exception"] = exc
            finally:
                ready.set()
        with patch.object(web, "_Server", side_effect=make_server):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            self.assertTrue(ready.wait(2), "Collector failed to become ready")
            try:
                yield observed
            finally:
                server = observed.get("server")
                if worker.is_alive() and server is not None:
                    server.done = True
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive(), "Collector did not stop")

    def get(self, server, **kwargs):
        return response_parts(exchange(server, request_bytes(server, **kwargs)))

    def post(self, server, value=None, path="/save", body=None, **kwargs):
        if body is None:
            body = json.dumps(candidate() if value is None else value).encode()
        return response_parts(exchange(server, request_bytes(
            server, method="POST", path=path, body=body,
            authenticated=True, **kwargs)))

    def assert_refused(self, response, status=403):
        actual, headers, body = response
        self.assertEqual(actual, status)
        self.assertNotIn(MARKER.encode(), body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Connection"], "close")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn("Access-Control-Allow-Credentials", headers)

    def test_binds_only_ipv4_loopback_with_ephemeral_port(self):
        with self.serving() as server:
            self.assertEqual(server.address_family, socket.AF_INET)
            self.assertEqual(server.server_address[0], "127.0.0.1")
            self.assertGreater(server.server_address[1], 0)
            self.assertEqual(server.origin, "http://" + server.host)
            self.assertFalse(server.allow_reuse_address)

    def test_page_has_session_cookie_and_strict_security_headers(self):
        with self.serving() as server:
            status, headers, body = self.get(server)
            self.assertEqual(status, 200)
            cookie = headers["Set-Cookie"]
            self.assertTrue(cookie.startswith(server.cookie_name + "=" + server.cookie + ";"))
            for attribute in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age="):
                self.assertIn(attribute, cookie)
            self.assertNotIn("Domain=", cookie)
            for name, value in {"Cache-Control": "no-store", "Pragma": "no-cache",
                                "Referrer-Policy": "no-referrer",
                                "X-Content-Type-Options": "nosniff",
                                "X-Frame-Options": "DENY",
                                "Cross-Origin-Resource-Policy": "same-origin",
                                "Cross-Origin-Opener-Policy": "same-origin"}.items():
                self.assertEqual(headers[name], value)
            csp = headers["Content-Security-Policy"]
            for directive in ("default-src 'none'", "connect-src 'self'",
                              "form-action 'none'", "frame-ancestors 'none'",
                              "script-src 'nonce-" + server.nonce + "'"):
                self.assertIn(directive, csp)
            self.assertEqual(int(headers["Content-Length"]), len(body))
            self.assertNotIn("Server", headers)

    def test_unique_session_values_and_path_safe_markup(self):
        with self.serving(path=self.root / '<config&".private.json',
                          state=self.root / '<state&"') as first, self.serving() as second:
            for name in ("cookie_name", "cookie", "csrf", "nonce"):
                self.assertNotEqual(getattr(first, name), getattr(second, name))
            page = first.page().decode()
            self.assertIn("&lt;config&amp;&quot;.private.json", page)
            self.assertIn("&lt;state&amp;&quot;", page)
            self.assertNotIn('<config&"', page)
            self.assertNotIn("__CSRF__", page)
            self.assertNotIn("__NONCE__", page)

    def test_host_is_required_and_must_match_exactly(self):
        with self.serving() as server:
            for host in (None, "localhost:" + str(server.server_address[1]),
                         "127.0.0.1", server.host + ".invalid", "evil.invalid",
                         "[::1]:" + str(server.server_address[1])):
                with self.subTest(host=host):
                    self.assert_refused(self.get(server, headers={"Host": host}))
            self.assertFalse(server.cookie_issued)

    def test_origin_and_fetch_site_block_cross_site_reads_and_writes(self):
        with self.serving() as server:
            for headers in ({"Origin": "null"}, {"Origin": "https://" + server.host},
                            {"Origin": "http://localhost"},
                            {"Origin": server.origin + "/"},
                            {"Sec-Fetch-Site": "cross-site"},
                            {"Sec-Fetch-Site": "same-site"}):
                with self.subTest(headers=headers):
                    self.assert_refused(self.get(server, headers=headers))
            self.get(server)
            for headers in ({"Origin": None}, {"Origin": "null"},
                            {"Origin": "https://evil.invalid"},
                            {"Sec-Fetch-Site": "cross-site"},
                            {"Sec-Fetch-Site": "same-site"}):
                with self.subTest(headers=headers):
                    self.assert_refused(self.post(server, headers=headers))
            self.assertEqual(server.fixture_saved, [])

    def test_first_page_claim_cannot_be_replaced_without_session_cookie(self):
        with self.serving() as server:
            self.get(server)
            self.assert_refused(self.get(server))
            self.assert_refused(self.get(server, headers={"Cookie": "unrelated=value"}))
            self.assertEqual(self.get(server, headers={"Cookie":
                server.cookie_name + "=" + server.cookie})[0], 200)

    def test_post_requires_issued_cookie_and_csrf(self):
        with self.serving() as server:
            self.assert_refused(self.post(server))
            self.get(server)
            for headers in ({"Cookie": None}, {"Cookie": "unrelated=value"},
                            {"Cookie": server.cookie_name + "=wrong"},
                            {"X-MultiDot-CSRF": None}, {"X-MultiDot-CSRF": "wrong"}):
                with self.subTest(headers=headers):
                    self.assert_refused(self.post(server, headers=headers))
            self.assertFalse(server.done)
            self.assertEqual(server.fixture_saved, [])

    def test_duplicate_session_cookie_is_refused(self):
        with self.serving() as server:
            self.get(server)
            cookie = server.cookie_name + "=" + server.cookie
            for value in (cookie + "; " + cookie, cookie + "; " + server.cookie_name + "=wrong"):
                self.assert_refused(self.post(server, headers={"Cookie": value}))
            self.assertEqual(server.fixture_saved, [])

    def test_unrelated_cookie_does_not_invalidate_correct_cookie(self):
        with self.serving() as server:
            self.get(server)
            cookie = "unrelated=value; " + server.cookie_name + "=" + server.cookie
            self.assertEqual(self.post(server, headers={"Cookie": cookie})[0], 200)
            self.assertEqual(len(server.fixture_saved), 1)

    def test_only_post_can_save_and_no_cors_preflight_is_allowed(self):
        with self.serving() as server:
            for method, path, status in (("GET", "/save", 404), ("GET", "/cancel", 404),
                                         ("OPTIONS", "/save", 405), ("PUT", "/save", 405),
                                         ("DELETE", "/save", 405), ("HEAD", "/", 405)):
                with self.subTest(method=method):
                    self.assert_refused(self.get(server, method=method, path=path), status)
            self.assertEqual(server.fixture_saved, [])

    def test_query_fragments_absolute_targets_and_unknown_paths_are_refused(self):
        with self.serving() as server:
            for path in ("/?key=" + MARKER, "/#fragment", server.origin + "/", "//save", "/favicon.ico"):
                self.assert_refused(self.get(server, path=path), 404)
            self.get(server)
            for path in ("/save?key=" + MARKER, "/cancel/", "/unknown"):
                self.assert_refused(self.post(server, path=path), 404)
            self.assertEqual(server.fixture_saved, [])

    def test_duplicate_headers_are_rejected_case_insensitively(self):
        with self.serving() as server:
            self.get(server)
            for name, value in (("host", server.host), ("oRiGiN", server.origin),
                                ("COOKIE", "x=y"), ("content-length", "1"),
                                ("content-type", "application/json"),
                                ("x-multidot-csrf", server.csrf)):
                with self.subTest(name=name):
                    self.assert_refused(self.post(server, extra=[(name, value)]), 400)
            self.assertEqual(server.fixture_saved, [])

    def test_content_type_is_required_and_exact(self):
        with self.serving() as server:
            self.get(server)
            for value in (None, "text/plain", "application/x-www-form-urlencoded",
                          "multipart/form-data", "application/json; charset=utf-8"):
                with self.subTest(value=value):
                    self.assert_refused(self.post(server, headers={"Content-Type": value}), 400)
            self.assertEqual(server.fixture_saved, [])

    def test_transfer_expect_and_trailer_headers_are_refused(self):
        with self.serving() as server:
            self.get(server)
            for headers in ({"Transfer-Encoding": "chunked"}, {"Expect": "100-continue"},
                            {"Trailer": "X-Fixture"}):
                self.assert_refused(self.post(server, headers=headers), 400)

    def test_invalid_content_lengths_and_missing_post_length_are_refused(self):
        with self.serving() as server:
            self.get(server)
            for value in (None, "", "0", "-1", "+1", "1, 1", "1.0", "1000000000"):
                with self.subTest(value=value):
                    self.assert_refused(self.post(server, headers={"Content-Length": value}), 400)

    def test_declared_oversized_body_is_refused_before_reading(self):
        with self.serving() as server:
            self.get(server)
            raw = request_bytes(server, method="POST", authenticated=True,
                                headers={"Content-Length": str(web.MAX_BODY_BYTES + 1)})
            self.assert_refused(response_parts(exchange(server, raw)), 413)
            self.assertEqual(server.fixture_saved, [])

    def test_unauthorized_body_is_not_read_before_refusal(self):
        with self.serving() as server:
            self.get(server)
            raw = request_bytes(server, method="POST", authenticated=True,
                                headers={"Content-Length": "1024", "X-MultiDot-CSRF": "wrong"})
            started = time.monotonic()
            self.assert_refused(response_parts(exchange(server, raw)))
            self.assertLess(time.monotonic() - started, 1)

    def test_body_on_get_extra_body_and_pipelining_are_refused(self):
        with self.serving() as server:
            self.assert_refused(self.get(server, body=b"{}"), 400)
            first = request_bytes(server)
            self.assert_refused(response_parts(exchange(server, first + first)), 400)
            self.get(server)
            raw = request_bytes(server, method="POST", path="/save", body=b"{}x",
                                authenticated=True, headers={"Content-Length": "2"})
            self.assert_refused(response_parts(exchange(server, raw)), 400)

    def test_malformed_request_lines_headers_and_control_bytes_are_refused(self):
        with self.serving() as server:
            requests = [b"GET / HTTP/1.0\r\n", b"get / HTTP/1.1\r\n", b"GET  / HTTP/1.1\r\n",
                        b"GET / HTTP/1.1\r\nMissing-Colon\r\n",
                        b"GET / HTTP/1.1\r\n Bad: header\r\n",
                        b"GET / HTTP/1.1\r\nBad\x00: header\r\n",
                        b"GET / HTTP/1.1\r\nBad: value\t\r\n",
                        b"GET / HTTP/1.1\r\nBad: \xff\r\n"]
            for prefix in requests:
                with self.subTest(prefix=prefix):
                    raw = prefix + b"Host: " + server.host.encode() + b"\r\n\r\n"
                    self.assert_refused(response_parts(exchange(server, raw)), 400)

    def test_header_byte_line_and_request_line_limits(self):
        with self.serving() as server:
            variants = [b"GET / HTTP/1.1\r\nX-Large: " + b"a" * web.MAX_HEADER_BYTES,
                        request_bytes(server, extra=[("X-" + str(i), "value")
                                                    for i in range(web.MAX_HEADER_LINES)]),
                        request_bytes(server, path="/" + "a" * 4096)]
            for raw in variants:
                self.assert_refused(response_parts(exchange(server, raw)), 413)
            self.assertFalse(server.cookie_issued)

    def test_duplicate_json_keys_at_all_levels_are_refused(self):
        with self.serving() as server:
            self.get(server)
            good = json.dumps(candidate()).encode()
            variants = [good.replace(b'"consent": true', b'"consent": true, "consent": true'),
                        good.replace(b'"name":', b'"name": "duplicate", "name":'),
                        good.replace(b'"runtime_api_key":', b'"runtime_api_key": "duplicate", "runtime_api_key":')]
            for body in variants:
                self.assert_refused(self.post(server, body=body), 400)
            self.assertEqual(server.fixture_saved, [])

    def test_malformed_json_constants_utf8_and_deep_nesting_are_refused(self):
        with self.serving() as server:
            self.get(server)
            for body in (b"{", b"\xff", b"[]", b"null", b"true", b'"' + MARKER.encode() + b'"',
                         b'{"consent": NaN, "dots": []}', b'{"value": Infinity}',
                         b"[" * 1100 + b"]" * 1100):
                with self.subTest(length=len(body)):
                    self.assert_refused(self.post(server, body=body), 400)
            self.assertEqual(server.fixture_saved, [])

    def test_save_requires_exact_true_consent_and_exact_fields(self):
        with self.serving() as server:
            self.get(server)
            values = [{}, {"dots": candidate()["dots"]}, {"consent": True},
                      {**candidate(), "extra": MARKER}]
            values += [{**candidate(), "consent": value} for value in (False, None, 1, "true")]
            values += [{"consent": True, "dots": value} for value in (None, {}, "", [], [None])]
            for value in values:
                self.assert_refused(self.post(server, value=value), 400)
            self.assertEqual(server.fixture_saved, [])

    def test_invalid_rows_and_unrequested_roles_are_refused(self):
        with self.serving() as server:
            self.get(server)
            variants = [{"runtime_api_key": ""}, {"runtime_api_key": 123},
                        {"runtime_api_key": "bad key"}, {"runtime_api_key": "x" * 8192},
                        {"name": ""}, {"name": "\x1b"}, {"name": "\ud800"},
                        {"name": "\ud55c" * 107}, {"tunnel_id": "tunnel_" + "0" * 32},
                        {"tunnel_id": "bad"}, {"role": "synthesis"}, {"extra": MARKER}]
            for updates in variants:
                value = candidate()
                value["dots"][0].update(updates)
                self.assert_refused(self.post(server, value=value), 400)
            for field in ("name", "tunnel_id", "runtime_api_key"):
                value = candidate()
                del value["dots"][0][field]
                self.assert_refused(self.post(server, value=value), 400)
            value = candidate(2)
            value["dots"][1]["tunnel_id"] = value["dots"][0]["tunnel_id"]
            self.assert_refused(self.post(server, value=value), 400)
            self.assertEqual(server.fixture_saved, [])

    def test_serialized_configuration_has_separate_size_limit(self):
        with self.serving() as server, patch.object(wizard, "MAX_CONFIG_BYTES", 1):
            self.get(server)
            self.assert_refused(self.post(server), 413)
            self.assertFalse(server.done)
            self.assertEqual(server.fixture_saved, [])

    def test_valid_many_dot_save_is_single_use_and_clears_cookie(self):
        with self.serving() as server:
            self.get(server)
            status, headers, body = self.post(server, value=candidate(37))
            self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
            self.assertIn("Max-Age=0", headers["Set-Cookie"])
            self.assertTrue(headers["Set-Cookie"].startswith(server.cookie_name + "=;"))
            self.assertNotIn(MARKER, json.dumps(server.result))
            self.assertEqual(server.result, {"ok": True, "saved": True, "requested_setup": True,
                "configured_dots": 37, "cancelled": False, "values_displayed": False})
            self.assertEqual(len(server.fixture_saved), 1)
            spec = server.fixture_saved[0]
            self.assertEqual(spec["schema_version"], 1)
            self.assertEqual(spec["dots"][0]["name"], "\uc5f0\uad6c \ub2f4\ub2f9 0")
            self.assertTrue(all(row["role"] == "worker" for row in setup.validate_dots(spec)))
            self.assert_refused(self.post(server), 410)
            self.assert_refused(self.post(server, path="/cancel", value={}), 410)
            self.assertEqual(len(server.fixture_saved), 1)

    def test_cancel_is_authenticated_empty_object_and_closes_cookie(self):
        with self.serving() as server:
            self.get(server)
            self.assert_refused(self.post(server, path="/cancel", value={"unexpected": MARKER}), 400)
            self.assert_refused(self.post(server, path="/cancel", value={}, headers={"X-MultiDot-CSRF": None}))
            status, headers, _ = self.post(server, path="/cancel", value={})
            self.assertEqual(status, 200)
            self.assertIn("Max-Age=0", headers["Set-Cookie"])
            self.assertTrue(server.result["cancelled"])
            self.assertFalse(server.result["saved"])
            self.assertFalse(server.result["requested_setup"])
            self.assertEqual(server.fixture_saved, [])
            self.assert_refused(self.post(server), 410)

    def test_publication_exception_is_static_and_never_retried(self):
        calls = []
        def fail(data, result):
            calls.append(data)
            raise RuntimeError(MARKER)
        with self.serving(publish=fail) as server:
            self.get(server)
            status, headers, body = self.post(server)
            self.assertEqual((status, json.loads(body)), (500, {"ok": False, "error": "save_failed"}))
            self.assertIn("Max-Age=0", headers["Set-Cookie"])
            self.assertEqual(server.error.code, "web_setup_failed")
            self.assert_refused(self.post(server), 410)
            self.assertEqual(len(calls), 1)
            self.assertTrue(server.result["cancelled"])
            self.assertFalse(server.result["requested_setup"])

    def test_unexpected_handler_failure_does_not_print_traceback(self):
        with self.serving() as server, patch.object(server, "page", side_effect=RuntimeError(MARKER)):
            self.assertEqual(exchange(server, request_bytes(server)), b"")
            self.assertTrue(server.done)
            self.assertEqual(server.error.code, "web_setup_failed")
            self.assertEqual(self.errors.getvalue(), "")

    def test_total_request_budget_closes_session_without_saving(self):
        with patch.object(web, "MAX_REQUESTS", 3), self.serving() as server:
            for _ in range(3):
                response = self.get(server, path="/unknown")
                self.assert_refused(response, 404)
            self.assertIn("Max-Age=0", response[1]["Set-Cookie"])
            self.assertTrue(server.done)
            self.assertTrue(server.result["cancelled"])
            self.assertEqual(server.requests, 3)
            self.assertEqual(server.fixture_saved, [])

    def test_partial_body_eof_never_saves(self):
        with self.serving() as server:
            self.get(server)
            raw = request_bytes(server, method="POST", path="/save", body=b"{",
                authenticated=True, headers={"Content-Length": "100"})
            with socket.create_connection(server.server_address, timeout=2) as connection:
                connection.sendall(raw)
                connection.shutdown(socket.SHUT_WR)
                response = connection.recv(65536)
            self.assert_refused(response_parts(response), 400)
            self.assertEqual(server.fixture_saved, [])

    def test_response_disconnect_cannot_undo_completed_publication(self):
        with self.serving() as server:
            self.get(server)
            with patch.object(web._Handler, "_reply", side_effect=ConnectionResetError):
                raw = exchange(server, request_bytes(server, method="POST", path="/save",
                    body=json.dumps(candidate()).encode(), authenticated=True))
            self.assertEqual(raw, b"")
            self.assertTrue(server.result["saved"])
            self.assertTrue(server.result["requested_setup"])
            self.assertFalse(server.result["cancelled"])
            self.assertEqual(len(server.fixture_saved), 1)

    def test_absolute_request_deadline_rejects_slow_headers_and_body(self):
        with patch.object(web, "REQUEST_SECONDS", 0.12), self.serving() as server:
            self.get(server)
            samples = [b"GET / HTTP/1.1\r\nHost: ", request_bytes(
                server, method="POST", path="/save", authenticated=True,
                headers={"Content-Length": "100"})]
            for prefix in samples:
                started = time.monotonic()
                with socket.create_connection(server.server_address, timeout=2) as connection:
                    connection.sendall(prefix)
                    for _ in range(5):
                        time.sleep(0.04)
                        try:
                            connection.sendall(b" ")
                        except (BrokenPipeError, ConnectionResetError):
                            break
                    try:
                        response = connection.recv(4096)
                    except ConnectionResetError:
                        response = b""
                self.assertLess(time.monotonic() - started, 0.7)
                self.assertTrue(not response or response.startswith(b"HTTP/1.1 408"))
            self.assertEqual(server.fixture_saved, [])

    def test_session_deadline_bounds_an_in_progress_read(self):
        with self.serving() as server:
            self.get(server)
            server.deadline = time.monotonic() + 0.1
            started = time.monotonic()
            response = exchange(server, b"GET /")
            self.assertLess(time.monotonic() - started, 0.7)
            self.assertTrue(not response or response.startswith(b"HTTP/1.1 408"))
            self.assertTrue(server.done)
            self.assertTrue(server.result["cancelled"])
            self.assertEqual(server.fixture_saved, [])

    def test_expired_session_never_publishes(self):
        with self.serving() as server:
            self.get(server)
            server.deadline = time.monotonic() - 1
            raw = exchange(server, request_bytes(server, method="POST", path="/save",
                body=json.dumps(candidate()).encode(), authenticated=True))
            self.assertTrue(not raw or raw.startswith(b"HTTP/1.1 410"))
            self.assertTrue(server.done)
            self.assertTrue(server.result["cancelled"])
            self.assertEqual(server.fixture_saved, [])

    def test_collection_saves_complete_config_owner_only_without_preparing(self):
        with self.collecting() as observed:
            server = observed["server"]
            self.get(server)
            self.assertEqual(self.post(server, value=candidate(4))[0], 200)
        self.assertNotIn("exception", observed)
        self.assertEqual(observed["result"]["configured_dots"], 4)
        self.assertTrue(observed["result"]["requested_setup"])
        self.assertEqual(len(setup.load_dots(self.config)), 4)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config.parent.stat().st_mode), 0o700)
        self.assertEqual(self.config.stat().st_nlink, 1)
        self.assertEqual(self.output.getvalue(), server.origin + "/\n")
        self.assertNotIn(server.csrf, self.output.getvalue())
        self.assertNotIn(server.cookie, self.output.getvalue())

    def test_collection_cancel_leaves_no_config_and_releases_listener(self):
        with self.collecting() as observed:
            server = observed["server"]
            self.get(server)
            self.post(server, path="/cancel", value={})
        self.assertTrue(observed["result"]["cancelled"])
        self.assertFalse(self.config.exists())
        self.assertEqual(server.socket.fileno(), -1)
        with wizard.private_destination(self.config):
            pass

    def test_collection_replaces_only_reviewed_blank_atomically(self):
        setup.initialize_config(self.config)
        inode = self.config.stat().st_ino
        with self.collecting() as observed:
            self.get(observed["server"])
            self.assertEqual(self.post(observed["server"])[0], 200)
        self.assertTrue(observed["result"]["saved"])
        self.assertNotEqual(self.config.stat().st_ino, inode)
        self.assertEqual(setup.load_dots(self.config)[0]["runtime_api_key"], MARKER)

    def test_filled_and_noncanonical_configs_refused_before_opening_listener(self):
        setup.initialize_config(self.config)
        for data in (MARKER.encode(), json.dumps(setup.EMPTY).encode(),
                     setup.json_bytes({"schema_version": 1, "dots": candidate()["dots"]})):
            self.config.write_bytes(data)
            with patch.object(web, "_Server") as listener, self.assertRaises(setup.SetupError) as caught:
                web.collect_and_save(self.config, state_root=self.state)
            self.assertEqual(caught.exception.code, "existing_config_not_blank_use_non_interactive")
            self.assertFalse(caught.exception.config_saved)
            self.assertEqual(self.config.read_bytes(), data)
            listener.assert_not_called()
        self.assertEqual(self.output.getvalue(), "")

    def test_unsafe_config_modes_and_symlinks_refused_before_listener(self):
        setup.initialize_config(self.config)
        original = self.config.read_bytes()
        self.config.chmod(0o644)
        with patch.object(web, "_Server") as listener, self.assertRaises(setup.SetupError):
            web.collect_and_save(self.config, state_root=self.state)
        listener.assert_not_called()
        self.assertEqual(self.config.read_bytes(), original)
        self.config.unlink()
        target = self.home / "other.private.json"
        target.write_bytes(original)
        target.chmod(0o600)
        self.config.symlink_to(target)
        with patch.object(web, "_Server") as listener, self.assertRaises(setup.SetupError):
            web.collect_and_save(self.config, state_root=self.state)
        listener.assert_not_called()
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(target.read_bytes(), original)

    def test_live_web_collection_holds_shared_terminal_destination_lock(self):
        with self.collecting() as observed:
            with self.assertRaises(setup.SetupError) as caught:
                with wizard.private_destination(self.config):
                    self.fail("Concurrent collector acquired the lock")
            self.assertEqual(caught.exception.code, "config_setup_already_in_progress")
            self.get(observed["server"])
            self.post(observed["server"], path="/cancel", value={})
        with wizard.private_destination(self.config):
            pass

    def test_config_appearing_or_changing_during_collection_is_not_overwritten(self):
        for originally_blank in (False, True):
            with self.subTest(originally_blank=originally_blank):
                if self.config.exists():
                    self.config.unlink()
                if originally_blank:
                    setup.initialize_config(self.config)
                with self.collecting() as observed:
                    self.get(observed["server"])
                    self.config.write_text(MARKER)
                    self.config.chmod(0o600)
                    self.assert_refused(self.post(observed["server"]), 500)
                self.assertEqual(observed["exception"].code, "config_changed_during_setup")
                self.assertFalse(observed["exception"].config_saved)
                self.assertEqual(self.config.read_text(), MARKER)

    def test_late_competing_writer_is_never_overwritten(self):
        real_link = wizard.os.link
        def competing_link(source, destination, **kwargs):
            self.config.write_text(MARKER)
            self.config.chmod(0o600)
            return real_link(source, destination, **kwargs)
        with patch.object(wizard.os, "link", side_effect=competing_link), self.collecting() as observed:
            self.get(observed["server"])
            self.assert_refused(self.post(observed["server"]), 500)
        self.assertEqual(observed["exception"].code, "web_setup_failed")
        self.assertFalse(observed["exception"].config_saved)
        self.assertEqual(self.config.read_text(), MARKER)

    def test_saved_config_flag_survives_sync_failure(self):
        original_sync = wizard.os.fsync
        def sync(fd):
            if self.config.exists() and stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(MARKER)
            return original_sync(fd)
        with patch.object(wizard.os, "fsync", side_effect=sync), self.collecting() as observed:
            self.get(observed["server"])
            self.assert_refused(self.post(observed["server"]), 500)
        self.assertEqual(observed["exception"].code, "config_saved_sync_unconfirmed")
        self.assertTrue(observed["exception"].config_saved)
        self.assertEqual(setup.load_dots(self.config)[0]["runtime_api_key"], MARKER)

    def test_idle_collector_expires_without_saving(self):
        with patch.object(web, "SESSION_SECONDS", 0.05), self.collecting() as observed:
            time.sleep(0.1)
        self.assertTrue(observed["result"]["cancelled"])
        self.assertFalse(observed["result"]["saved"])
        self.assertFalse(self.config.exists())

    def test_collection_request_limit_closes_socket_and_returns_cancelled(self):
        with patch.object(web, "MAX_REQUESTS", 2), self.collecting() as observed:
            server = observed["server"]
            self.get(server)
            response = self.get(server, path="/unknown")
            self.assert_refused(response, 404)
            self.assertIn("Max-Age=0", response[1]["Set-Cookie"])
        self.assertTrue(observed["result"]["cancelled"])
        self.assertFalse(observed["result"]["saved"])
        self.assertFalse(self.config.exists())
        self.assertEqual(server.socket.fileno(), -1)

    def test_listener_failure_is_static_and_releases_destination_lock(self):
        with patch.object(web, "_Server", side_effect=OSError(MARKER)):
            with self.assertRaises(setup.SetupError) as caught:
                web.collect_and_save(self.config, state_root=self.state)
        self.assertEqual(caught.exception.code, "web_setup_failed")
        self.assertFalse(caught.exception.config_saved)
        self.assertNotIn(MARKER, str(caught.exception))
        self.assertEqual(self.output.getvalue(), "")
        with wizard.private_destination(self.config):
            pass

    def test_collector_keyboard_interrupt_cancels_and_closes_listener(self):
        with patch.object(web._Server, "handle_request", side_effect=KeyboardInterrupt):
            result = web.collect_and_save(self.config, state_root=self.state)
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["requested_setup"])
        self.assertFalse(self.config.exists())
        with wizard.private_destination(self.config):
            pass

    def test_ui_uses_masked_dynamic_fields_and_one_explicit_save(self):
        with self.serving() as server:
            page = server.page().decode()
            parser = PageParser()
            parser.feed(page)
            buttons = [attrs for tag, attrs in parser.tags if tag == "button"]
            self.assertEqual(sum(attrs.get("type") == "submit" for attrs in buttons), 1)
            self.assertIn('method="post"', page)
            self.assertIn('["runtime_api_key", "Runtime API key (hidden)", "password", 8191]', page)
            self.assertIn('remove.textContent = "Remove dot"', page)
            self.assertIn('input.autocomplete = "off"', page)
            self.assertIn('input.setAttribute("data-lpignore", "true")', page)
            self.assertIn('input.setAttribute("data-1p-ignore", "true")', page)
            self.assertIn("owner-only local plaintext file", page)
            self.assertIn("issuing internal runtime credentials", page)
            self.assertIn("does not contact external services or start the runtime", page)
            self.assertNotIn("confirm(", page)
            self.assertNotIn(MARKER, page)

    def test_ui_has_no_storage_query_keys_remote_assets_or_browser_open(self):
        page = web.PAGE
        for token in ("localStorage", "sessionStorage", "indexedDB", "document.cookie",
                      "URLSearchParams", "sendBeacon", "window.open", "clipboard", "console."):
            self.assertNotIn(token, page)
        parser = PageParser()
        parser.feed(page)
        self.assertFalse(any("src" in attrs or "href" in attrs for _, attrs in parser.tags))
        self.assertIn('fetch("/" + action', page)
        self.assertIn('method: "POST", mode: "same-origin", credentials: "same-origin"', page)
        self.assertIn('headers: {"Content-Type": "application/json", "X-MultiDot-CSRF": csrf}', page)
        self.assertIn('body: wire', page)
        self.assertNotIn("webbrowser", Path(web.__file__).read_text())

    def test_ui_clears_values_on_completion_cancel_expiry_navigation_and_escape(self):
        page = web.PAGE
        for token in ('input.value = ""', 'row.runtime_api_key = ""', "payload.dots.length = 0",
                      "payload = null", "wire = null", 'csrf = ""', "form.replaceChildren()",
                      'submit("cancel")', 'event.key === "Escape"',
                      'window.addEventListener("pagehide"', 'window.addEventListener("pageshow"',
                      "event.persisted", 'document.addEventListener("visibilitychange", expired)',
                      "setTimeout(expired", "keepalive: true", "if (expired() || submitting) return",
                      "button.disabled = true", "controller.abort()", "clearTimeout(timeout)"):
            self.assertIn(token, page)
        self.assertRegex(page, r"const pending = fetch\([\s\S]+?\);\s+clearValues\(\);\s+const response = await pending")
        self.assertRegex(page, r'pagehide", \(\) => \{\s+clearValues\(\)')
        self.assertRegex(page, r"function finish\(message\) \{\s+finished = true;\s+clearValues\(\)")


if __name__ == "__main__":
    unittest.main()
