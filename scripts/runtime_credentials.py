#!/usr/bin/env python3
"""User-run private config preparation. No network, key issuance, or startup.

Enter values in the private file yourself, never in command arguments or chat.
The install command explicitly writes existing values to the runtime's file:
references. It never displays values, reads an existing destination, or replaces
an existing credential. This module must not be run by an agent on real values.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from runtime_supervisor import load as load_runtime

FIELDS = {"runtime_api_key": ("runtime-api-key", "tunnel-b"),
          "worker_b_token": ("worker-b-authorization", "b-ingress"),
          "producer_token": ("producer-token", "controller")}
EMPTY = {"schema_version": 1, **dict.fromkeys(FIELDS, "")}


class ConfigError(Exception):
    def __init__(self, code, fields=()):
        self.code = code
        self.fields = sorted(set(fields) & FIELDS.keys())


def directory(path, create=False):
    """Walk with directory FDs: reject symlink components and unsafe owners."""
    path = Path(os.path.abspath(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            if info.st_uid not in (0, os.getuid()):
                raise ConfigError("unsafe_directory_owner")
            # Root-owned sticky temporary parents are safe for a subsequent
            # owned, non-symlink directory. Never accept an unsafe final dir.
            if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                raise ConfigError("unsafe_directory_permissions")
        return fd
    except BaseException:
        os.close(fd)
        raise


def owner_only(info):
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077 and info.st_nlink == 1


def private_parent(path):
    path = Path(os.path.abspath(path))
    if not path.name.endswith((".private.json", ".local.json")):
        raise ConfigError("private_filename_required")
    fd = directory(path.parent)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        os.close(fd)
        raise ConfigError("unsafe_private_config_parent")
    return fd, path.name


def initialize(path):
    parent, name = private_parent(path)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(EMPTY, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)
    return {"ok": True, "created_blank_private_config": True, "values_displayed": False}


def read_private(path):
    parent, name = private_parent(path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            if not owner_only(os.fstat(stream.fileno())):
                raise ConfigError("private_config_permissions_required")
            raw = stream.read(32769)
    finally:
        os.close(parent)
    if len(raw) > 32768:
        raise ConfigError("private_config_too_large")
    try:
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ConfigError("invalid_private_config")
                result[key] = value
            return result
        values = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError):
        raise ConfigError("invalid_private_config") from None
    if not isinstance(values, dict) or set(values) != set(EMPTY) or type(values["schema_version"]) is not int or values["schema_version"] != 1:
        raise ConfigError("invalid_private_config")
    bad = [field for field in FIELDS if not isinstance(values[field], str) or len(values[field]) > (8184 if field == "worker_b_token" else 8191)
           or any(ord(char) < 33 or ord(char) > 126 for char in values[field])]
    if bad:
        raise ConfigError("invalid_value_format", bad)
    return values


def private_state_directory(path):
    fd = directory(path, create=True)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ConfigError("private_state_permissions_required")
    return fd


def existing_field(fd, name, field):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not owner_only(info):
        raise ConfigError("unsafe_existing_file", [field])
    if not 1 <= info.st_size <= 8192:
        raise ConfigError("invalid_existing_file_size", [field])
    return True


def install(private_path, runtime_path):
    values = read_private(private_path)
    try:
        runtime = load_runtime(Path(runtime_path))
    except Exception:
        raise ConfigError("invalid_runtime_config") from None
    state = Path(runtime["state_root"])
    # Take the same file lock as the supervisor; never alter a running stack.
    root_fd = private_state_directory(state)
    os.close(root_fd)
    run_fd = private_state_directory(state / "run")
    try:
        secrets_fd = private_state_directory(state / "secrets")
    except BaseException:
        os.close(run_fd)
        raise
    lock_fd = None
    created = []
    pending = []
    try:
        lock_fd = os.open("supervisor.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=run_fd)
        if not owner_only(os.fstat(lock_fd)):
            raise ConfigError("unsafe_runtime_lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigError("stop_runtime_before_install") from None
        required = {field for field, (_, role) in FIELDS.items() if role in runtime["components"]}
        unneeded = {field for field in FIELDS if values[field] and field not in required}
        if unneeded:
            raise ConfigError("values_not_needed_by_components", unneeded)
        existing = {field for field, (name, _) in FIELDS.items() if existing_field(secrets_fd, name, field)}
        overwrite = {field for field in required & existing if values[field]}
        if overwrite:
            raise ConfigError("existing_values_not_replaced", overwrite)
        missing = {field for field in required - existing if not values[field]}
        if missing:
            raise ConfigError("missing_required_values", missing)
        for field in sorted(required - existing):
            name, _ = FIELDS[field]
            value = ("Bearer " if field == "worker_b_token" else "") + values[field]
            temporary = ".pending-" + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=secrets_fd)
            info = os.fstat(fd)
            pending.append((temporary, info.st_dev, info.st_ino))
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(value + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            # link() publishes the already-complete inode atomically and fails
            # if the final name exists. It never replaces an existing value.
            os.link(temporary, name, src_dir_fd=secrets_fd, dst_dir_fd=secrets_fd, follow_symlinks=False)
            created.append((name, info.st_dev, info.st_ino))
            os.unlink(temporary, dir_fd=secrets_fd)
            pending.pop()
            os.fsync(secrets_fd)
        os.fsync(secrets_fd)
        return {"ok": True, "installed_fields": sorted(required - existing), "reused_fields": sorted(required & existing),
                "network_contacted": False, "runtime_started": False, "values_displayed": False}
    except BaseException:
        # Roll back only files created by this call, never an existing entry or
        # a file another process has replaced. This is not secret erasure.
        for name, device, inode in created + pending:
            try:
                current = os.stat(name, dir_fd=secrets_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (device, inode):
                    os.unlink(name, dir_fd=secrets_fd)
            except OSError:
                pass
        raise
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(secrets_fd)
        os.close(run_fd)


class PrivateParser(argparse.ArgumentParser):
    def error(self, _message):
        print(json.dumps({"ok": False, "error": "invalid_arguments", "values_displayed": False}), file=sys.stderr)
        raise SystemExit(2)


def main(argv=None):
    os.umask(0o077)
    parser = PrivateParser(description=__doc__)
    parser.add_argument("command", choices=("init", "install"))
    parser.add_argument("--private-config", type=Path, default=Path("config/credentials.private.json"))
    parser.add_argument("--runtime-config", type=Path, default=Path("config/runtime.local.json"))
    args = parser.parse_args(argv)
    try:
        result = initialize(args.private_config) if args.command == "init" else install(args.private_config, args.runtime_config)
        print(json.dumps(result))
        return 0
    except ConfigError as exc:
        result = {"ok": False, "error": exc.code, "values_displayed": False}
        if exc.fields:
            result["fields"] = exc.fields
        print(json.dumps(result), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({"ok": False, "error": "private_config_operation_failed", "values_displayed": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
