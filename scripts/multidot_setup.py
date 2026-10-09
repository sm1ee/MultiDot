"""Explicit user-run generic setup. No external requests or service startup.

Only this operator module initializes a fresh upstream via reviewed APIs.
Controllers and runtime preflight never open upstream storage. Agents must not
run this module on real filled config. Tests use disposable synthetic fixtures.
"""
from __future__ import annotations

import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid

from runtime_credentials import directory, owner_only, private_parent
from runtime_supervisor import TUNNEL_SHA256

ROOT = Path(__file__).resolve().parents[1]
EMPTY = {"schema_version": 1, "dots": [{"name": "", "tunnel_id": "", "runtime_api_key": ""}]}
TOKEN_TTL = 2592000
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


class SetupError(Exception):
    def __init__(self, code, indices=()):
        self.code = code
        self.indices = sorted(set(i for i in indices if type(i) is int and i >= 0))


def write_new(path, data):
    """Private, non-overwriting write inside an unpublished private stage."""
    path = Path(path)
    parent = directory(path.parent, create=True)
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def initialize_config(path):
    parent, name = private_parent(path)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(json_bytes(EMPTY))
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)
    return {"ok": True, "blank_config_created": True, "next_step": "Edit config/dots.private.json yourself"}


def read_private_json(path, maximum=262144):
    parent, name = private_parent(path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            if not owner_only(os.fstat(stream.fileno())):
                raise SetupError("private_config_permissions_required")
            raw = stream.read(maximum + 1)
    finally:
        os.close(parent)
    if len(raw) > maximum:
        raise SetupError("config_too_large")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SetupError("invalid_config")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode(), object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise SetupError("invalid_config") from None


def load_dots(path):
    spec = read_private_json(path)
    if not isinstance(spec, dict) or set(spec) != {"schema_version", "dots"} or type(spec["schema_version"]) is not int or spec["schema_version"] != 1:
        raise SetupError("invalid_config")
    if not isinstance(spec["dots"], list) or not spec["dots"]:
        raise SetupError("at_least_one_dot_required")
    dots, tunnels = [], set()
    for index, row in enumerate(spec["dots"]):
        if not isinstance(row, dict) or set(row) - {"name", "tunnel_id", "runtime_api_key", "role"} or not {"name", "tunnel_id", "runtime_api_key"} <= set(row):
            raise SetupError("invalid_dot_fields", [index])
        name = row["name"]
        if not isinstance(name, str) or not name.strip() or len(name.encode()) > 320 or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise SetupError("dot_name_required", [index])
        tunnel = row["tunnel_id"]
        if not isinstance(tunnel, str) or not re.fullmatch(r"tunnel_[0-9a-f]{32}", tunnel) or tunnel == "tunnel_" + "0" * 32:
            raise SetupError("valid_existing_tunnel_id_required", [index])
        if tunnel in tunnels:
            raise SetupError("duplicate_tunnel_id", [index])
        tunnels.add(tunnel)
        key = row["runtime_api_key"]
        if not isinstance(key, str) or len(key) > 8191 or any(not 33 <= ord(char) <= 126 for char in key):
            raise SetupError("invalid_runtime_key_format", [index])
        role = row.get("role", "worker")
        if role not in ("worker", "synthesis"):
            raise SetupError("invalid_role", [index])
        dots.append({"name": name, "tunnel_id": tunnel, "runtime_api_key": key, "role": role})
    if sum(row["role"] == "synthesis" for row in dots) > 1 or not any(row["role"] == "worker" for row in dots):
        raise SetupError("worker_required_and_at_most_one_synthesis")
    return dots


def default_environment(state_root=None, tools_root=None):
    tools = (Path(tools_root) if tools_root else ROOT.parent / "multidot-tools").resolve()
    return {"repository": str(ROOT), "state_root": str(Path(state_root).absolute() if state_root else ROOT.parent / "MultiDot-state"),
            "upstream_checkout": str(tools / "dot2api-pinned"), "upstream_python": str(tools / "dot2api-venv/bin/python"),
            "tunnel_binary": str(tools / "tunnel-client-v0.0.16/installed/tunnel-client")}


def verify_environment(environment):
    if not os.access(environment["upstream_python"], os.X_OK) or not os.access(environment["tunnel_binary"], os.X_OK):
        raise SetupError("reviewed_runtime_installation_missing")
    if hashlib.sha256(Path(environment["tunnel_binary"]).read_bytes()).hexdigest() != TUNNEL_SHA256:
        raise SetupError("reviewed_tunnel_binary_mismatch")
    result = subprocess.run([environment["upstream_python"], str(ROOT / "scripts/runtime_verify_install.py"), environment["upstream_checkout"]],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    if result.returncode:
        raise SetupError("reviewed_upstream_installation_mismatch")


def choose_ports(count):
    held = []
    try:
        for _ in range(count):
            handle = socket.socket()
            handle.bind(("127.0.0.1", 0))
            held.append(handle)
        return [handle.getsockname()[1] for handle in held]
    except OSError:
        raise SetupError("local_port_capacity_unavailable") from None
    finally:
        for handle in held:
            handle.close()


def build_runtime(manifest, environment, *, test_only=False, control_plane_url="https://api.openai.com"):
    return {"schema_version": 2, **environment, "upstream_port": manifest["upstream_port"],
            "control_plane_url": control_plane_url, "workers": [{key: row[key] for key in
                ("id", "name", "role", "tunnel_id", "ingress_port", "health_port")} for row in manifest["workers"]],
            "event_delivery_enabled": False, "test_only": test_only}


def provision_stage(stage, manifest, dots):
    """Reviewed APIs, explicit operator setup only. Never runtime auto-recovery."""
    from dot2api.cli import initialize
    from dot2api.security import Security
    from dot2api.store import Store
    initialize(stage / "upstream")
    security = Security(Store(stage / "upstream/dot2api.sqlite3"), (stage / "upstream/encryption.key").read_bytes().strip())
    queues = [row["id"] for row in manifest["workers"]]
    security.create_principal("hub-producer", manifest["tenant"], queues, ["read", "submit"])
    producer = security.issue_token("hub-producer", TOKEN_TTL)
    binding = security.authenticate(producer)
    if binding.tenant != manifest["tenant"] or binding.id != "hub-producer" or set(binding.queues) != set(queues) or set(binding.scopes) != {"read", "submit"}:
        raise SetupError("producer_binding_verification_failed")
    write_new(stage / "secrets/producer-token", (producer + "\n").encode())
    by_tunnel = {row["tunnel_id"]: row for row in dots}
    for row in manifest["workers"]:
        security.create_principal(row["id"], manifest["tenant"], [row["id"]], ["read", "work", "subscribe"])
        token = security.issue_token(row["id"], TOKEN_TTL)
        binding = security.authenticate(token)
        if binding.tenant != manifest["tenant"] or binding.id != row["id"] or set(binding.queues) != {row["id"]} or set(binding.scopes) != {"read", "work", "subscribe"}:
            raise SetupError("worker_binding_verification_failed")
        write_new(stage / "secrets" / row["id"] / "worker-authorization", ("Bearer " + token + "\n").encode())
        write_new(stage / "secrets" / row["id"] / "runtime-api-key", (by_tunnel[row["tunnel_id"]]["runtime_api_key"] + "\n").encode())


def publish_directory(parent_fd, source_name, target_name):
    """Linux atomic directory publication, refusing even an empty target."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise SetupError("atomic_state_publication_unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent_fd, os.fsencode(source_name), parent_fd, os.fsencode(target_name), 1):
        raise OSError(ctypes.get_errno(), "State publication failed")
    try:
        os.fsync(parent_fd)
    except OSError:
        raise SetupError("state_published_sync_unconfirmed") from None


def read_manifest(state):
    manifest = read_private_json(state / "manifest.private.json", MAX_MANIFEST_BYTES)
    expected = {"schema_version", "installation_id", "tenant", "producer_id", "created_at",
                "internal_tokens_expire_no_earlier_than", "upstream_port", "workers"}
    if not isinstance(manifest, dict) or set(manifest) != expected or manifest.get("schema_version") != 1 or not isinstance(manifest.get("workers"), list):
        raise SetupError("invalid_existing_manifest")
    rows = manifest["workers"]
    if not rows or manifest["producer_id"] != "hub-producer" or not re.fullmatch(r"tenant-[0-9a-f]{32}", manifest["tenant"]):
        raise SetupError("invalid_existing_manifest")
    fields = {"id", "name", "role", "tunnel_id", "runtime_key_sha256", "ingress_port", "health_port"}
    if any(not isinstance(row, dict) or set(row) != fields or not re.fullmatch(r"worker-[0-9a-f]{32}", row["id"])
           or not re.fullmatch(r"[0-9a-f]{64}", row["runtime_key_sha256"]) for row in rows):
        raise SetupError("invalid_existing_manifest")
    if len({row["id"] for row in rows}) != len(rows) or len({row["tunnel_id"] for row in rows}) != len(rows):
        raise SetupError("invalid_existing_manifest")
    return manifest


def replace_metadata(path, value, validate=None):
    temporary = ".pending-" + uuid.uuid4().hex
    parent = directory(path.parent)
    try:
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SetupError("unsafe_existing_metadata_directory")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        if validate is not None:
            validate(Path(f"/proc/self/fd/{parent}") / temporary)
        os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def check_installed_files(state, manifest):
    """Metadata-only completeness checks. Never reopen existing key contents."""
    required = [state / "secrets/producer-token", state / "upstream/encryption.key",
                state / "upstream/dot2api.sqlite3", state / "controller/hub.sqlite"]
    for row in manifest["workers"]:
        required += [state / "secrets" / row["id"] / name for name in ("runtime-api-key", "worker-authorization")]
    for path in required:
        parent = directory(path.parent)
        try:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not owner_only(info) or info.st_size == 0 or (path.parent.name != "upstream" and not path.name.endswith(".sqlite") and info.st_size > 8192):
                raise SetupError("existing_setup_incomplete_requires_review")
        finally:
            os.close(parent)


def sync_tree(root):
    for directory_path, dirs, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            fd = os.open(Path(directory_path) / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = directory(directory_path)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def check_generated_bounds(manifest):
    if len(json_bytes(manifest)) > MAX_MANIFEST_BYTES:
        raise SetupError("generated_manifest_exceeds_byte_limit")
    controller = {"mode": "native", "base_url": f"http://127.0.0.1:{manifest['upstream_port']}",
                  "producer": "hub-producer", "token_env": "MULTIDOT_PRODUCER_TOKEN", "projects": ["demo"],
                  "workers": [{key: row[key] for key in ("id", "name", "role")} for row in manifest["workers"]]}
    if len((json.dumps(controller, indent=2) + "\n").encode()) > 262144:
        raise SetupError("generated_controller_config_exceeds_byte_limit")


def configure(dots, environment, *, test_only=False, control_plane_url="https://api.openai.com"):
    """Create once. Exact reruns reuse identities; only display names may change."""
    state = Path(environment["state_root"])
    parent_fd = directory(state.parent)
    lock_fd = stage = runtime_lock = None
    try:
        parent_info = os.fstat(parent_fd)
        if parent_info.st_uid != os.getuid() or parent_info.st_mode & 0o022:
            raise SetupError("unsafe_state_parent")
        lock_fd = os.open("." + state.name + ".setup.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent_fd)
        if not owner_only(os.fstat(lock_fd)):
            raise SetupError("unsafe_setup_lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError("another_setup_is_running") from None
        if state.exists() or state.is_symlink():
            info = state.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise SetupError("unsafe_existing_state")
            run_fd = directory(state / "run")
            try:
                runtime_lock = os.open("supervisor.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=run_fd)
            finally:
                os.close(run_fd)
            if not owner_only(os.fstat(runtime_lock)):
                raise SetupError("unsafe_runtime_lock")
            try:
                fcntl.flock(runtime_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SetupError("stop_runtime_before_setup") from None
            manifest = read_manifest(state)
            check_installed_files(state, manifest)
            old = {row["tunnel_id"]: row for row in manifest["workers"]}
            if set(old) != {row["tunnel_id"] for row in dots}:
                raise SetupError("installed_dot_set_change_requires_migration")
            for index, dot in enumerate(dots):
                row = old[dot["tunnel_id"]]
                if row["role"] != dot["role"]:
                    raise SetupError("installed_role_change_requires_migration", [index])
                if dot["runtime_api_key"] and hashlib.sha256(dot["runtime_api_key"].encode()).hexdigest() != row["runtime_key_sha256"]:
                    raise SetupError("runtime_key_change_requires_rotation", [index])
                row["name"] = dot["name"]
            check_generated_bounds(manifest)
            runtime = build_runtime(manifest, environment, test_only=test_only, control_plane_url=control_plane_url)
            from runtime_supervisor import load
            # A crash between metadata writes can leave stale labels; identity,
            # role, scope, keys, ports and jobs stay unchanged. Re-run repairs it.
            replace_metadata(state / "config/runtime.json", runtime, load)
            replace_metadata(state / "manifest.private.json", manifest)
            return {"ok": True, "configured_dots": len(dots), "identities_reused": True,
                    "credentials_issued": False, "network_contacted": False, "runtime_started": False}
        missing = [i for i, dot in enumerate(dots) if not dot["runtime_api_key"]]
        if missing:
            raise SetupError("runtime_api_key_required_for_new_setup", missing)
        ports = choose_ports(1 + 2 * len(dots))
        created = time.time()
        manifest = {"schema_version": 1, "installation_id": uuid.uuid4().hex,
                    "tenant": "tenant-" + uuid.uuid4().hex, "producer_id": "hub-producer",
                    "created_at": created, "internal_tokens_expire_no_earlier_than": created + TOKEN_TTL,
                    "upstream_port": ports[0], "workers": []}
        for index, dot in enumerate(dots):
            manifest["workers"].append({"id": "worker-" + uuid.uuid4().hex,
                "name": dot["name"], "role": dot["role"], "tunnel_id": dot["tunnel_id"],
                "runtime_key_sha256": hashlib.sha256(dot["runtime_api_key"].encode()).hexdigest(),
                "ingress_port": ports[1 + 2 * index], "health_port": ports[2 + 2 * index]})
        check_generated_bounds(manifest)
        stage_name = "." + state.name + ".setup-" + uuid.uuid4().hex
        os.mkdir(stage_name, 0o700, dir_fd=parent_fd)
        stage = state.parent / stage_name
        provision_stage(stage, manifest, dots)
        sys.path.insert(0, str(ROOT / "src"))
        from multidot.controller import Controller
        from multidot.cli import OfflineAdapter
        registry = [{key: row[key] for key in ("id", "name", "role")} for row in manifest["workers"]]
        (stage / "controller").mkdir(mode=0o700, exist_ok=True)
        controller = Controller(stage / "controller/hub.sqlite", OfflineAdapter(f"http://127.0.0.1:{ports[0]}", "hub-producer"), ["demo"], workers=registry)
        try:
            controller.bootstrap("demo", "Default local MultiDot workspace")
        finally:
            controller.close()
        runtime = build_runtime(manifest, environment, test_only=test_only, control_plane_url=control_plane_url)
        from runtime_supervisor import load, render
        render(dict(runtime, state_root=str(stage)))
        for path in (stage / "config").glob("*"):
            if path.is_file():
                path.write_text(path.read_text().replace(str(stage), str(state)))
        write_new(stage / "config/runtime.json", json_bytes(runtime))
        load(stage / "config/runtime.json")
        write_new(stage / "manifest.private.json", json_bytes(manifest))
        sync_tree(stage)
        try:
            publish_directory(parent_fd, stage_name, state.name)
        except SetupError as exc:
            if exc.code == "state_published_sync_unconfirmed":
                stage = None  # Published target must never be removed/replaced.
            raise
        stage = None
        return {"ok": True, "configured_dots": len(dots), "identities_reused": False,
                "internal_credentials_created": True, "network_contacted": False, "runtime_started": False,
                "internal_token_lifetime_days": TOKEN_TTL // 86400}
    finally:
        if runtime_lock is not None:
            os.close(runtime_lock)
        if stage is not None:
            shutil.rmtree(stage)
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(parent_fd)
