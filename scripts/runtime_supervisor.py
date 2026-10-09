#!/usr/bin/env python3
"""MultiDot stack supervision. No shell commands, installation or provisioning.

This survives child exits, not host/container loss. Run on Linux in the same
network namespace as dot2api and the tunnel client. Secrets stay in private files.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.request

SCRIPT = Path(__file__).resolve()
# Explicit schema-v1 compatibility only. Schema v2 selects each configured ID.
ROLES = ("dot2api", "b-ingress", "controller", "tunnel-b")
WORKER_ROLES = ("dot2api", "ingress", "controller", "tunnel")
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
BOOT = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
TUNNEL_SHA256 = "01260ee973d5510861bc32979739561edd33869f99f9f8cd324f6d0da5b2e692"


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def identity(pid):
    try:
        data = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        if data[0] == "Z":
            return None
        return {"pid": pid, "start_ticks": data[19], "boot_id": BOOT,
                "pid_namespace": os.readlink(f"/proc/{pid}/ns/pid")}
    except (OSError, IndexError):
        return None


def alive(record):
    return bool(record and identity(record.get("pid", -1)) == record)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate configuration key")
        result[key] = value
    return result


def load(path):
    """Validate exact versioned input, preserving its public dictionary shape."""
    c = json.loads(path.read_text(), object_pairs_hook=_unique_object)
    common = {"schema_version", "state_root", "repository", "upstream_python", "upstream_checkout",
              "event_delivery_enabled", "tunnel_binary", "upstream_port", "control_plane_url", "test_only"}
    if not isinstance(c, dict) or type(c.get("schema_version")) is not int:
        raise ValueError("Invalid runtime configuration")
    version = c["schema_version"]
    required = common | ({"tunnel_id", "ingress_port", "health_port", "components"} if version == 1 else {"workers"})
    if version not in (1, 2) or set(c) != required:
        raise ValueError("Invalid versioned runtime configuration")
    if not isinstance(c["test_only"], bool):
        raise ValueError("test_only must be boolean")
    if not isinstance(c["event_delivery_enabled"], bool) or (c["test_only"] and c["event_delivery_enabled"]):
        raise ValueError("Events delivery must be explicit and disabled in tests")
    for key in ("state_root", "repository", "upstream_python", "upstream_checkout", "tunnel_binary"):
        if not isinstance(c[key], str) or not Path(c[key]).is_absolute() or "\x00" in c[key]:
            raise ValueError("Runtime paths must be explicit and absolute")
    ports = [c["upstream_port"]]
    if version == 1:
        if (not isinstance(c["components"], list) or not c["components"] or
                not all(isinstance(role, str) for role in c["components"]) or
                len(set(c["components"])) != len(c["components"]) or
                set(c["components"]) - set(ROLES) or "dot2api" not in c["components"]):
            raise ValueError("Only fixed legacy stack components are supported")
        if "tunnel-b" in c["components"] and "b-ingress" not in c["components"]:
            raise ValueError("Legacy tunnel requires legacy ingress")
        entries = [{"tunnel_id": c["tunnel_id"], "ingress_port": c["ingress_port"], "health_port": c["health_port"]}]
    else:
        entries = c["workers"]
        if not isinstance(entries, list) or not entries:
            raise ValueError("At least one worker is required")
        seen_ids = set()
        synthesis_count = 0
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"id", "name", "role", "tunnel_id", "ingress_port", "health_port"}:
                raise ValueError("Invalid worker configuration")
            identifier = entry["id"]
            if not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier) or identifier in seen_ids:
                raise ValueError("Worker IDs must be unique stable identifiers")
            seen_ids.add(identifier)
            if (not isinstance(entry["name"], str) or not entry["name"].strip() or
                    any(ord(ch) < 32 or ord(ch) == 127 for ch in entry["name"])):
                raise ValueError("Worker display names must be nonempty text")
            if entry["role"] not in ("worker", "synthesis"):
                raise ValueError("Unknown worker role")
            synthesis_count += entry["role"] == "synthesis"
        if synthesis_count > 1:
            raise ValueError("At most one synthesis worker is allowed")
    tunnel_ids = set()
    for entry in entries:
        identifier = entry["tunnel_id"]
        if not isinstance(identifier, str) or not re.fullmatch(r"tunnel_[0-9a-f]{32}", identifier) or identifier in tunnel_ids:
            raise ValueError("Tunnel IDs must be valid and unique")
        tunnel_ids.add(identifier)
        ports.extend((entry["ingress_port"], entry["health_port"]))
    if any(type(port) is not int or not 1024 <= port <= 65535 for port in ports):
        raise ValueError("Invalid loopback port")
    if len(set(ports)) != len(ports):
        raise ValueError("All runtime ports must differ")
    if c["control_plane_url"] != "https://api.openai.com":
        if (not c["test_only"] or not isinstance(c["control_plane_url"], str) or
                not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", c["control_plane_url"])):
            raise ValueError("Control plane must be official or an explicit local test")
        if not 1 <= int(c["control_plane_url"].rsplit(":", 1)[1]) <= 65535:
            raise ValueError("Invalid local control-plane port")
    if c["test_only"] and c["control_plane_url"] == "https://api.openai.com":
        raise ValueError("Test mode must not contact the external control plane")
    return c


def worker_entries(c):
    """Return configured workers; the fixed B entry is legacy-v1-only."""
    if c["schema_version"] == 2:
        return c["workers"]
    return [{"id": "dot-b", "name": "dot-b", "role": "worker", "tunnel_id": c["tunnel_id"],
             "ingress_port": c["ingress_port"], "health_port": c["health_port"]}]


def get_worker(c, worker_id):
    for entry in worker_entries(c):
        if entry["id"] == worker_id:
            return entry
    raise ValueError("Worker ID is not configured")


def worker_secret_path(c, worker_id, kind):
    get_worker(c, worker_id)
    if kind not in ("runtime-api-key", "worker-authorization"):
        raise ValueError("Unknown credential file role")
    root = Path(c["state_root"]) / "secrets"
    if c["schema_version"] == 1:
        return root / ("worker-b-authorization" if kind == "worker-authorization" else kind)
    return root / worker_id / kind


def tunnel_config_path(c, worker_id):
    get_worker(c, worker_id)
    name = "b" if c["schema_version"] == 1 else worker_id
    return Path(c["state_root"]) / "config" / f"tunnel-{name}.yaml"


def component_specs(c):
    """Fixed executable roles keyed by stable per-worker process identities."""
    specs = {"dot2api": {"role": "dot2api", "worker_id": None, "port": c["upstream_port"]}}
    if c["schema_version"] == 1:
        legacy = {"b-ingress": {"role": "b-ingress", "worker_id": "dot-b", "port": c["ingress_port"]},
                  "controller": {"role": "controller", "worker_id": None, "port": None},
                  "tunnel-b": {"role": "tunnel-b", "worker_id": "dot-b", "port": c["health_port"]}}
        specs.update({key: value for key, value in legacy.items() if key in c["components"]})
        return specs
    for entry in c["workers"]:
        specs["ingress:" + entry["id"]] = {"role": "ingress", "worker_id": entry["id"], "port": entry["ingress_port"]}
    specs["controller"] = {"role": "controller", "worker_id": None, "port": None}
    for entry in c["workers"]:
        specs["tunnel:" + entry["id"]] = {"role": "tunnel", "worker_id": entry["id"], "port": entry["health_port"]}
    return specs


def ingress_component(c, worker_id):
    get_worker(c, worker_id)
    return "b-ingress" if c["schema_version"] == 1 else "ingress:" + worker_id


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
        raise ValueError("State directories must be owned private directories")


def secret(path):
    flags = os.O_RDONLY | os.O_NOFOLLOW
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        s = os.fstat(stream.fileno())
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
            raise ValueError("Secrets must be owner-only regular files")
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise ValueError("Secret exceeds size limit")
    value = raw.decode("utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("Secret must be one nonempty line")
    return value


def render(c):
    root = Path(c["state_root"])
    for p in (root, root / "run", root / "logs", root / "controller", root / "secrets", root / "config"):
        private_dir(p)
    # JSON is a YAML 1.2 subset, accepted by the pinned client's YAML parser.
    for entry in worker_entries(c):
        identifier = entry["id"]
        if c["schema_version"] == 2:
            private_dir(root / "secrets" / identifier)
        health_name = "tunnel-health-url" if c["schema_version"] == 1 else f"tunnel-{identifier}-health-url"
        tunnel = {
            "config_version": 1,
            "control_plane": {"base_url": c["control_plane_url"], "tunnel_id": entry["tunnel_id"],
                              "api_key": "file:" + str(worker_secret_path(c, identifier, "runtime-api-key")),
                              "poll_channels": ["main"], "max_inflight_requests": 2},
            "health": {"listen_addr": f"127.0.0.1:{entry['health_port']}",
                       "url_file": str(root / "run" / health_name)},
            "admin_ui": {"open_browser": False, "log_buffer_events": 100},
            "log": {"level": "warn", "format": "json"},
            "mcp": {"server_urls": [{"channel": "main", "url": f"http://127.0.0.1:{entry['ingress_port']}/mcp"}],
                    "extra_headers": {"Authorization": "file:" + str(worker_secret_path(c, identifier, "worker-authorization"))},
                    "startup_wait_timeout": "30s", "max_concurrent_requests": 1},
        }
        write_json(tunnel_config_path(c, identifier), tunnel)
    controller = {"mode": "native", "base_url": f"http://127.0.0.1:{c['upstream_port']}",
                  "producer": "hub-producer", "token_env": "MULTIDOT_PRODUCER_TOKEN", "projects": ["demo"]}
    if c["schema_version"] == 2:
        controller["workers"] = [{key: entry[key] for key in ("id", "name", "role")} for entry in c["workers"]]
    write_json(root / "config/controller.json", controller)


def preflight(c):
    root = Path(c["state_root"])
    for key in ("upstream_python", "tunnel_binary"):
        if not os.access(c[key], os.X_OK):
            raise ValueError("Required pinned executable is unavailable")
    if not (Path(c["repository"]) / "src/multidot/cli.py").is_file():
        raise ValueError("Controller source is unavailable")
    if hashlib.sha256(Path(c["tunnel_binary"]).read_bytes()).hexdigest() != TUNNEL_SHA256:
        raise ValueError("Tunnel binary differs from the verified official Linux amd64 release")
    # Reuse the existing reviewed read-only source/version verification. It
    # checks commit, tree, cleanliness, uv.lock and installed package versions.
    source_check = subprocess.run([c["upstream_python"], str(SCRIPT.parent / "runtime_verify_install.py"), c["upstream_checkout"]],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    if source_check.returncode:
        raise ValueError("Pinned upstream installation verification failed")
    # Deliberately do not initialize storage or issue any credentials here.
    if not (root / "upstream/encryption.key").is_file() or not (root / "upstream/dot2api.sqlite3").is_file():
        raise ValueError("Approved upstream provisioning is required")
    for component in component_specs(c).values():
        if component["role"] in ("tunnel", "tunnel-b"):
            secret(worker_secret_path(c, component["worker_id"], "runtime-api-key"))
        elif component["role"] in ("ingress", "b-ingress"):
            header = secret(worker_secret_path(c, component["worker_id"], "worker-authorization"))
            if not header.startswith("Bearer ") or not header[7:] or header[7:].startswith("Bearer "):
                raise ValueError("Worker authorization must contain the complete Bearer header")
        elif component["role"] == "controller":
            secret(root / "secrets/producer-token")


def binding(c, worker_id=None):
    """Read-only HTTP boundary check. Never opens upstream storage.

    Every configured worker has exactly the worker HTTP capabilities, access to
    its own stable-ID queue, and no access to any other configured worker queue.
    Principal/tenant assignment still requires the provisioning receipt.
    """
    import urllib.error
    selected = worker_entries(c) if worker_id is None else [get_worker(c, worker_id)]
    queues = [entry["id"] for entry in worker_entries(c)] if c["schema_version"] == 2 else ["dot-b", "dot-a", "dot-c"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    base = f"http://127.0.0.1:{c['upstream_port']}"
    expected = {"get_task", "list_tasks", "claim_task", "heartbeat_task", "complete_task", "release_task"}
    for entry in selected:
        header = secret(worker_secret_path(c, entry["id"], "worker-authorization"))
        request = urllib.request.Request(base + "/v1/capabilities", headers={"Authorization": header})
        with opener.open(request, timeout=2) as response:
            raw = response.read(262145)
            if response.status != 200 or len(raw) > 262144:
                raise ValueError("Invalid worker HTTP capabilities response")
            tools = json.loads(raw)["tools"]
            names = {tool["name"] for tool in tools}
        if names != expected or len(tools) != len(expected):
            raise ValueError("Worker HTTP capabilities differ from bounded worker contract")
        for queue in queues:
            request = urllib.request.Request(base + "/v1/tools/list_tasks", data=json.dumps({"queue": queue, "limit": 1}).encode(),
                headers={"Authorization": header, "Content-Type": "application/json"}, method="POST")
            try:
                with opener.open(request, timeout=2) as response:
                    if len(response.read(262145)) > 262144:
                        raise ValueError("Worker queue response exceeds size limit")
                    code = response.status
            except urllib.error.HTTPError as error:
                code = error.code
                error.close()
            if code != (200 if queue == entry["id"] else 403):
                raise ValueError("Worker HTTP queue boundary differs from its configured queue")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def child_environment(c):
    # Ambient connector secrets/config must not override the fixed file config.
    keep = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    proxy = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    env = {k: v for k, v in os.environ.items() if k in keep | proxy}
    # Fixed local services must not send their credentials to an ambient HTTP
    # proxy. External control-plane requests retain the environment's proxy.
    bypass = set(filter(None, (env.get("NO_PROXY", "") + "," + env.get("no_proxy", "")).split(",")))
    bypass.update({"127.0.0.1", "localhost", "::1"})
    env["NO_PROXY"] = env["no_proxy"] = ",".join(sorted(bypass))
    env["PYTHONPATH"] = str(Path(c["repository"]) / "src")
    env.update({"PYTHONUNBUFFERED": "1", "DOT2API_HOST": "127.0.0.1", "DOT2API_PORT": str(c["upstream_port"]),
                "DOT2API_CAPABILITY_URLS": "0", "DOT2API_WORKER": "1" if c["event_delivery_enabled"] else "0"})
    if c["schema_version"] == 1:
        env["DOT2API_QUEUE"] = "dot-b"
    return env


def worker(c, role, parent, worker_id=None):
    # Selection is restricted to configured, fixed-role executables. Neither
    # display names nor arbitrary command arguments become executable input.
    matches = [spec for spec in component_specs(c).values() if spec["role"] == role and
               (spec["worker_id"] == worker_id or (c["schema_version"] == 1 and worker_id is None))]
    if len(matches) != 1:
        raise ValueError("Worker requires one configured fixed role and ID")
    worker_id = matches[0]["worker_id"]
    # Linux parent-death cleanup, including supervisor SIGKILL. Never signal a
    # stale recorded PID; kernel ties this to the actual parent relationship.
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0):
        raise OSError("Parent-death protection unavailable")
    if os.getppid() != parent:
        return 1
    root = Path(c["state_root"])
    env = child_environment(c)
    if role == "dot2api":
        argv = [c["upstream_python"], "-m", "dot2api.cli", "--data-dir", str(root / "upstream"), "serve"]
    elif role in ("ingress", "b-ingress"):
        argv = [c["upstream_python"], str(SCRIPT.parent / "runtime_ingress.py"), "--config", str(CONFIG_PATH),
                "--worker-id", worker_id]
    elif role == "controller":
        env["MULTIDOT_PRODUCER_TOKEN"] = secret(root / "secrets/producer-token")
        argv = [c["upstream_python"], "-m", "multidot", "--config", str(root / "config/controller.json"),
                "--db", str(root / "controller/hub.sqlite"), "start", "--interval", "1"]
    else:
        argv = [c["tunnel_binary"], "run", "--config", str(tunnel_config_path(c, worker_id))]
    os.chdir(c["repository"])
    os.execve(argv[0], argv, env)


def probe(port, route="/readyz"):
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}{route}")
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, timeout=0.3) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def owned_listener(record, port):
    if not alive(record):
        return False
    try:
        sockets = set()
        for p in Path(f"/proc/{record['pid']}/fd").iterdir():
            try:
                target = os.readlink(p)
            except OSError:
                continue  # Descriptor lists can change during inspection.
            if target.startswith("socket:["):
                sockets.add(target[8:-1])
        address = f"0100007F:{port:04X}"
        return any(row.split()[1] == address and row.split()[3] == "0A" and row.split()[9] in sockets
                   for row in Path("/proc/net/tcp").read_text().splitlines()[1:])
    except OSError:
        return False


def free_port(port):
    try:
        with socket.socket() as check:
            check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            check.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def read_state(c):
    try:
        return json.loads((Path(c["state_root"]) / "run/status.json").read_text())
    except (OSError, ValueError):
        return {}


def status(c):
    s = read_state(c)
    record = s.get("supervisor")
    other_namespace = bool(record and record.get("pid_namespace") != os.readlink("/proc/self/ns/pid"))
    running = None if other_namespace else alive(record)
    s["supervisor_running"] = running
    s["observation_scope"] = "other_pid_namespace_last_report_only" if other_namespace else "same_pid_namespace"
    s["observed_at"] = time.time()
    s["os_autostart"] = False
    s["host_lifetime_guarantee"] = False
    specs = component_specs(c)
    for role, child in s.get("children", {}).items():
        if other_namespace:
            child["running"] = None
            child["ready"] = None
            continue
        child["running"] = alive(child.get("identity"))
        port = specs.get(role, {}).get("port")
        child["ready"] = bool(child["running"] and owned_listener(child.get("identity"), port) and probe(port)) if port else None

    return s


def supervisor(c):
    root = Path(c["state_root"])
    with open(root / "run/supervisor.lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Supervisor already running") from None
        # Do not start duplicates while orphaned children from an abrupt exit
        # are still draining. Reconcile is intentionally inspection-only.
        prior = read_state(c)
        if any(alive(ch.get("identity")) for ch in prior.get("children", {}).values()):
            raise ValueError("Previous owned children still draining; inspect status")
        specs = component_specs(c)
        if any(not free_port(spec["port"]) for spec in specs.values() if spec["port"]):
            raise ValueError("A fixed service port is occupied; refusing unrelated listeners")
        stopping = False
        def stop(_sig, _frame):
            nonlocal stopping
            stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        log = logging.getLogger("multidot.runtime")
        log.setLevel(logging.INFO)
        handler = RotatingFileHandler(root / "logs/lifecycle.jsonl", maxBytes=1024 * 1024, backupCount=3)
        log.addHandler(handler)
        def event(action, role=None, **fields):
            log.info(json.dumps({"at": time.time(), "event": action, "role": role, **fields}))
        processes = {}
        meta = {role: {"restarts": 0, "next_start": 0, "failures": 0} for role in specs}
        s = {"supervisor": identity(os.getpid()), "test_only": c["test_only"], "state_root": str(root), "children": meta}
        event("supervisor_started")
        def ready(role):
            p = processes.get(role)
            if p is None or p.poll() is not None:
                return False
            port = specs[role]["port"]
            return bool(port and owned_listener(meta[role].get("identity"), port) and probe(port))
        try:
            while not stopping:
                for role, spec in specs.items():
                    is_tunnel = spec["role"] in ("tunnel", "tunnel-b")
                    is_ingress = spec["role"] in ("ingress", "b-ingress")
                    ingress = ingress_component(c, spec["worker_id"]) if is_tunnel else None
                    row = meta[role]
                    process = processes.get(role)
                    if (is_tunnel and process is not None and process.poll() is None
                            and (not ready("dot2api") or not ready(ingress))):
                        process.terminate()
                    if process is not None and process.poll() is not None:
                        elapsed = time.monotonic() - row.pop("started_monotonic")
                        row["last_exit"] = process.returncode
                        row["failures"] = 0 if elapsed >= 60 else row["failures"] + 1
                        row["next_start"] = time.time() + min(30, 2 ** min(row["failures"], 5))
                        row["identity"] = None
                        row["restarts"] += 1
                        event("child_exited", role, exit_code=process.returncode, restart_in_seconds=round(row["next_start"] - time.time(), 2))
                        del processes[role]
                    if role in processes or time.time() < row["next_start"]:
                        continue
                    if row["failures"] >= 8:
                        row["needs_operator_review"] = True
                        continue
                    if role != "dot2api" and not ready("dot2api"):
                        continue
                    if is_tunnel and not ready(ingress):
                        continue
                    if is_ingress or is_tunnel:
                        try:
                            binding(c, spec["worker_id"])
                        except Exception:
                            row.update({"failures": 8, "needs_operator_review": True, "reason": "Worker HTTP authorization boundary not verified"})
                            event("binding_rejected", role)
                            continue
                    argv = [sys.executable, str(SCRIPT), "--config", str(CONFIG_PATH), "_worker", "--role", spec["role"], "--parent", str(os.getpid())]
                    if spec["worker_id"] is not None:
                        argv.extend(("--worker-id", spec["worker_id"]))
                    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, start_new_session=True)
                    processes[role] = p
                    row.update({"identity": identity(p.pid), "started_monotonic": time.monotonic()})
                    event("child_started", role, pid=p.pid)
                s["updated_at"] = time.time()
                s["last_reported_health"] = {role: {"running": p.poll() is None,
                    "ready": ready(role) if specs[role]["port"] else None}
                    for role, p in processes.items()}
                write_json(root / "run/status.json", s)
                time.sleep(0.2)
        finally:
            # Stop ingress, then dispatch, then storage. Each child owns its
            # session; never kill an unverified or reused recorded PID/group.
            for role in reversed(specs):
                p = processes.get(role)
                if p is not None and p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=50)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait(timeout=5)
                    meta[role]["last_exit"] = p.returncode
                    meta[role]["identity"] = None
            s.update({"stopped_at": time.time(), "updated_at": time.time()})
            write_json(root / "run/status.json", s)
            event("supervisor_stopped")
            log.removeHandler(handler)
            handler.close()
    return 0


def main():
    global CONFIG_PATH
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("command", choices=("render", "check", "start", "run", "status", "reconcile", "stop", "_worker", "_binding"))
    parser.add_argument("--role", choices=tuple(dict.fromkeys((*WORKER_ROLES, *ROLES))))
    parser.add_argument("--worker-id")
    parser.add_argument("--parent", type=int)
    args = parser.parse_args()
    CONFIG_PATH = args.config.resolve()
    try:
        c = load(CONFIG_PATH)
        if args.command == "_worker":
            if args.role is None or args.parent is None:
                raise ValueError("Worker requires its fixed role and parent")
            return worker(c, args.role, args.parent, args.worker_id)
        if args.command == "_binding":
            binding(c, args.worker_id)
            return 0
        if args.command == "render":
            render(c)
            print(json.dumps({"rendered": True, "credentials_created": False, "state_root": c["state_root"]}))
            return 0
        if args.command in ("status", "reconcile"):
            print(json.dumps(status(c), indent=2))
            return 0
        if args.command == "stop":
            old = read_state(c).get("supervisor")
            if old and old.get("pid_namespace") != os.readlink("/proc/self/ns/pid"):
                raise ValueError("Stop must run inside the owning process namespace/session")
            if alive(old):
                # pidfd binds signaling to this observed process, not a PID
                # that could be recycled between inspection and signal.
                fd = os.pidfd_open(old["pid"])
                try:
                    if alive(old):
                        signal.pidfd_send_signal(fd, signal.SIGTERM)
                finally:
                    os.close(fd)
                deadline = time.monotonic() + 55 * len(component_specs(c)) + 10
                while alive(old) and time.monotonic() < deadline:
                    time.sleep(0.1)
                if alive(old):
                    raise ValueError("Supervisor has not stopped; inspect owned processes")
            print(json.dumps({"stopped": True, "data_preserved": True}))
            return 0
        render(c)
        preflight(c)
        if args.command == "check":
            print(json.dumps({"preflight_passed": True, "external_connection_checked": False}))
            return 0
        if args.command == "run":
            return supervisor(c)
        if status(c).get("supervisor_running"):
            print(json.dumps({"already_running": True}))
            return 0
        process = subprocess.Popen([sys.executable, str(SCRIPT), "--config", str(CONFIG_PATH), "run"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ValueError("Supervisor startup failed; inspect configuration and prior state")
            state = status(c)
            if state.get("supervisor_running") and state.get("supervisor", {}).get("pid") == process.pid:
                print(json.dumps({"started": True, "pid": process.pid, "ready": False, "os_autostart": False}))
                return 0
            time.sleep(0.1)
        raise ValueError("Supervisor startup unconfirmed; inspect status before retry")
    except Exception as exc:
        # Never print secret values, exception text, transport bodies or child
        # argv/env. Operational details live in fixed lifecycle metadata only.
        print(json.dumps({"error": type(exc).__name__, "message": "Runtime operation failed; check private files, binding, executable paths and status"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
