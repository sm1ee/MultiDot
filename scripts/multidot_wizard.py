"""Explicit terminal-only collection of a new private MultiDot configuration.

No credentials are issued and no services or network operations run here.
The returned metadata tells the caller whether local preparation was approved.
Tests and agents must use disposable homes and synthetic values only.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import fcntl
import json
import os
import signal
import sys
import uuid

import multidot_setup as setup
from runtime_credentials import owner_only
from multidot_terminal import read_secret

MAX_CONFIG_BYTES = 262144


def require_tty():
    """Refuse pipes and redirected output before asking for any values."""
    if not all(getattr(stream, "isatty", lambda: False)()
               for stream in (sys.stdin, sys.stdout, sys.stderr)):
        raise setup.SetupError("interactive_terminal_required")


def _answer(prompt):
    while True:
        answer = input(prompt).strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        print("Please answer yes or no.")


def _secret():
    return read_secret()


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_nlink)


def _blank_snapshot(parent, name):
    """Accept only the exact private template emitted by the reviewed init."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                     os.O_CLOEXEC, dir_fd=parent)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not owner_only(before):
            raise setup.SetupError("private_config_permissions_required")
        blank = setup.json_bytes(setup.EMPTY)
        # Refuse filled files without reading their full contents.
        if before.st_size != len(blank):
            raise setup.SetupError("existing_config_not_blank_use_non_interactive")
        raw = stream.read(len(blank) + 1)
        after = os.fstat(stream.fileno())
        if raw != blank or _identity(before) != _identity(after):
            raise setup.SetupError("existing_config_not_blank_use_non_interactive")
        return _identity(after)


def _lock(parent, name):
    # Keep this empty lock file: unlinking a lock can let concurrent processes
    # acquire different inodes. All wizard instances use this same sidecar.
    fd = os.open("." + name + ".wizard.lock", os.O_RDWR | os.O_CREAT |
                 os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600,
                 dir_fd=parent)
    try:
        if not owner_only(os.fstat(fd)):
            raise setup.SetupError("unsafe_config_setup_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise setup.SetupError("config_setup_already_in_progress") from None
        return fd
    except BaseException:
        os.close(fd)
        raise


def _recheck(parent, name, original):
    try:
        current = _blank_snapshot(parent, name)
    except (OSError, setup.SetupError):
        raise setup.SetupError("config_changed_during_setup") from None
    if current != original:
        raise setup.SetupError("config_changed_during_setup")


@contextmanager
def _defer_interrupts():
    """Finish publication and temporary-file cleanup before terminal signals.

    This is not a power-loss or SIGKILL guarantee. The sidecar lock is advisory;
    an editor that ignores it can still race a blank-template replacement.
    """
    blocked = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _save(parent, name, original, data, result):
    temporary = ".multidot-config-" + uuid.uuid4().hex + ".pending"
    created = None
    with _defer_interrupts():
        try:
            _recheck(parent, name, original)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
            try:
                info = os.fstat(fd)
                created = (info.st_dev, info.st_ino)
                os.fchmod(fd, 0o600)
                stream = os.fdopen(fd, "wb")
                fd = None
                with stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                if fd is not None:
                    os.close(fd)
            # Recheck after writing too. The original must still be absent or
            # the exact same reviewed blank inode and contents.
            _recheck(parent, name, original)
            if original is None:
                # Atomic and no-clobber, even if another writer appeared after
                # the recheck. The temporary alias is removed in finally.
                os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent,
                        follow_symlinks=False)
            else:
                os.replace(temporary, name, src_dir_fd=parent,
                           dst_dir_fd=parent)
            result["saved"] = True
        finally:
            if created is not None:
                try:
                    info = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                except OSError:
                    raise setup.SetupError("config_temporary_cleanup_unconfirmed") from None
                else:
                    if (info.st_dev, info.st_ino) != created:
                        raise setup.SetupError("config_temporary_cleanup_unconfirmed")
                    try:
                        os.unlink(temporary, dir_fd=parent)
                    except OSError:
                        raise setup.SetupError("config_temporary_cleanup_unconfirmed") from None
        try:
            os.fsync(parent)
        except OSError:
            raise setup.SetupError("config_saved_sync_unconfirmed") from None


@contextmanager
def private_destination(path):
    """Hold the same owner-only lock and blank snapshot for either collector.

    The yielded publisher retains the reviewed atomic persistence implementation.
    Keep this context open through the user's approval and publication.
    """
    parent = lock = None
    try:
        parent, name = setup.config_parent(path, create=True)
        lock = _lock(parent, name)
        original = _blank_snapshot(parent, name)

        def publish(data, result):
            _save(parent, name, original, data, result)

        yield publish
    finally:
        if lock is not None:
            os.close(lock)
        if parent is not None:
            os.close(parent)


def collect_and_save(path, state_root=None):
    """Collect and validate all entries, confirm once, then publish privately.

    Returns public metadata only. Approval permits the caller to prepare local
    state; this function never provisions it. Cancellation after the atomic
    commit leaves the complete approved configuration and cancels preparation.
    """
    require_tty()
    result = {"ok": True, "saved": False, "requested_setup": False,
              "configured_dots": 0, "cancelled": False,
              "values_displayed": False}
    destination = ExitStack()
    try:
        path = setup.expand_path(path)
        state_root = (setup.expand_path(state_root) if state_root is not None else
                      setup.application_home() / "state")
        publish = destination.enter_context(private_destination(path))
        dots = []
        while True:
            print("Dot " + str(len(dots) + 1))
            row = {"name": input("Dot name: ").strip(),
                   "tunnel_id": input("Tunnel ID: ").strip(),
                   "runtime_api_key": _secret()}
            candidate = {"schema_version": 1, "dots": [*dots, row]}
            try:
                if not row["runtime_api_key"]:
                    raise setup.SetupError("runtime_api_key_required")
                setup.validate_dots(candidate)
            except setup.SetupError:
                print("Invalid dot entry. Use a nonempty name, a unique existing "
                      "tunnel ID, and a nonempty API key. Please enter this dot again.")
                continue
            dots.append(row)
            if not _answer("Add another dot? [y/N]: "):
                break
        spec = {"schema_version": 1, "dots": dots}
        setup.validate_dots(spec)
        data = setup.json_bytes(spec)
        if len(data) > MAX_CONFIG_BYTES:
            raise setup.SetupError("config_too_large")
        print("Ready to save " + str(len(dots)) + " dot(s), all with the worker role.")
        # JSON string escaping makes even unusual path characters safe to print.
        print("Configuration file: " + json.dumps(str(path), ensure_ascii=True))
        print("Local state directory: " + json.dumps(str(state_root), ensure_ascii=True))
        print("The owner-only private configuration file will store API keys in plaintext.")
        print("Local setup will prepare local state and issue internal runtime credentials.")
        print("This will not contact the network or start services.")
        if not _answer("Save and prepare local setup? [y/N]: "):
            result["cancelled"] = True
            return result
        publish(data, result)
        result["configured_dots"] = len(dots)
        result["requested_setup"] = True
        return result
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled.")
        result["cancelled"] = True
        return result
    except setup.SetupError as exc:
        exc.config_saved = result["saved"]
        raise
    except Exception:
        exc = setup.SetupError("interactive_setup_failed")
        exc.config_saved = result["saved"]
        raise exc from None
    finally:
        destination.close()
