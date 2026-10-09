"""Local CLI; start is foreground and does not promise platform persistence."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import tempfile
import time

from . import __version__
from .controller import Controller
from .dot2api_adapter import NativeTaskAdapter
from .mock import MockNative, complete_mock_task
from .models import AccessError, ConflictError, ValidationError, canonical, now, redact


class OfflineAdapter:
    token = ""

    def __init__(self, target, producer):
        self.target, self.producer = target, producer

    def __getattr__(self, name):
        raise ValidationError("This action requires explicitly configured native connectivity")


def read_json(path):
    with open(path, "rb") as f:
        data = f.read(262145)
    if len(data) > 262144:
        raise ValidationError("Input file exceeds byte limit")
    try:
        return json.loads(data)
    except ValueError:
        raise ValidationError("Invalid JSON file") from None


def demo(db_path):
    adapter = MockNative()
    controller = Controller(db_path, adapter, ["demo"])
    controller.bootstrap("demo", "Bounded MOCK demonstration")
    controller.add_snapshot({"schema_version": "multidot.snapshot.v1", "project_id": "demo", "snapshot_id": "demo-v1", "artifacts": [{"name": "brief.md", "media_type": "text/markdown", "content": "MOCK material for reliability review."}]})
    spec = {"schema_version": "multidot.job.v1", "project_id": "demo", "request_id": "demo-run", "title": "MOCK B then C then A", "goal": "Exercise stored data flow without real accounts", "input_snapshot_id": "demo-v1", "policy_id": "provided-materials-only", "steps": [{"step_id": "b-review", "worker": "dot-b", "instructions": "Review material", "acceptance_criteria": ["Evidence"], "depends_on": []}, {"step_id": "c-review", "worker": "dot-c", "instructions": "Independently review material", "acceptance_criteria": ["Gaps"], "depends_on": []}], "synthesis": {"worker": "dot-a", "trigger": "all_required_steps_accepted", "instructions": "Compare exact accepted results"}}
    job = controller.submit_job(spec)["job_id"]
    controller.tick()
    milestones = []
    for worker in ("dot-b", "dot-c"):
        task_id = next(t["id"] for t in adapter.tasks.values() if t["queue"] == worker)
        complete_mock_task(adapter, task_id)
        controller.tick()
        milestones.append({"worker": worker, "task_id": task_id, "state": controller.get_job(job)["state"], "classification": "MOCK"})
    synth_id = next(t["id"] for t in adapter.tasks.values() if t["queue"] == "dot-a")
    complete_mock_task(adapter, synth_id)
    controller.tick()
    controller.close()
    # Reopen the controller DB; mock upstream remains the explicitly separate in-memory double.
    controller = Controller(db_path, adapter, ["demo"])
    result = {"classification": "MOCK", "version": __version__, "ran_at": now(), "milestones": milestones,
              "job": controller.get_job(job), "real_dot": "NEEDS_USER_ACTION", "local_contract": "NOT_TESTED"}
    controller.close()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog="multidot")
    parser.add_argument("--db", default="var/hub.sqlite")
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("demo")
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--project", required=True)
    bootstrap.add_argument("--goal", required=True)
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--file", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--file", required=True)
    get = sub.add_parser("get")
    get.add_argument("job_id")
    jobs = sub.add_parser("jobs")
    jobs.add_argument("--project", required=True)
    sub.add_parser("workers")
    sub.add_parser("status")
    cancel = sub.add_parser("request-cancel")
    cancel.add_argument("job_id")
    cancel.add_argument("--reason", required=True)
    pause = sub.add_parser("pause")
    pause.add_argument("worker")
    resume = sub.add_parser("resume")
    resume.add_argument("worker")
    recover = sub.add_parser("recover")
    recovery_mode = recover.add_mutually_exclusive_group()
    recovery_mode.add_argument("--apply", action="store_true")
    recovery_mode.add_argument("--dry-run", action="store_true")
    sub.add_parser("tick")
    start = sub.add_parser("start")
    start.add_argument("--interval", type=float, default=5)
    artifact = sub.add_parser("artifact")
    artifact.add_argument("artifact_id")
    presented = sub.add_parser("mark-presented")
    presented.add_argument("job_id")
    presented.add_argument("--reference", required=True)
    export = sub.add_parser("export")
    export.add_argument("job_id")
    export.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            out = {"version": __version__, "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
                   "runtime_dependencies": [], "database_exists": Path(args.db).exists(),
                   "network_checked": False, "always_on": "UNVERIFIED", "real_dot": "NEEDS_USER_ACTION"}
        elif args.command == "demo":
            # Always isolated: do not overwrite or share a live controller DB.
            with tempfile.TemporaryDirectory(prefix="multidot-mock-") as root:
                out = demo(Path(root) / "hub.sqlite")
        else:
            if not args.config:
                raise ValidationError("Supply --config with explicit project allowlist and target")
            config = read_json(args.config)
            if set(config) != {"mode", "base_url", "producer", "token_env", "projects"} or config["mode"] != "native":
                raise ValidationError("Invalid controller configuration")
            needs_network = args.command in ("start", "tick") or (args.command == "recover" and args.apply) or (args.command == "request-cancel" and bool(os.environ.get(config["token_env"])))
            adapter = NativeTaskAdapter(config["base_url"], os.environ.get(config["token_env"], ""), config["producer"]) if needs_network else OfflineAdapter(config["base_url"], config["producer"])
            controller = Controller(args.db, adapter, config["projects"])
            try:
                if args.command == "bootstrap":
                    controller.bootstrap(args.project, args.goal)
                    out = {"bootstrapped": args.project, "credentials_created": False}
                elif args.command == "snapshot":
                    out = controller.add_snapshot(read_json(args.file))
                elif args.command == "submit":
                    out = controller.submit_job(read_json(args.file))
                elif args.command == "get":
                    out = controller.get_job(args.job_id)
                elif args.command == "jobs":
                    out = controller.list_jobs(args.project)
                elif args.command == "workers":
                    out = controller.list_workers()
                elif args.command == "status":
                    out = {"workers": controller.list_workers(), "jobs": [j for p in config["projects"] for j in controller.list_jobs(p)]}
                elif args.command == "request-cancel":
                    out = controller.request_cancel(args.job_id, args.reason, contact_upstream=needs_network)
                elif args.command in ("pause", "resume"):
                    controller.pause_worker(args.worker, args.command == "pause")
                    out = {"worker": args.worker, "paused": args.command == "pause"}
                elif args.command == "recover":
                    out = controller.recover(apply=args.apply)
                elif args.command == "tick":
                    controller.tick()
                    out = {"synchronized_at": now()}
                elif args.command == "artifact":
                    out = controller.get_artifact(args.artifact_id)
                elif args.command == "mark-presented":
                    controller.mark_presented(args.job_id, args.reference)
                    out = {"presentation_recorded": True, "verification": "operator_supplied_reference"}
                elif args.command == "export":
                    target = Path(args.output)
                    if target.exists() or target.is_symlink():
                        raise ValidationError("Export never overwrites an existing path")
                    data = canonical(controller.get_job(args.job_id)).encode()
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    out = {"exported": str(target), "bytes": len(data)}
                elif args.command == "start":
                    if not 1 <= args.interval <= 60:
                        raise ValidationError("Interval must be 1 to 60 seconds")
                    lock_path = str(Path(args.db).resolve()) + ".lock.pid"
                    with open(lock_path, "a+") as lock:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            raise ValidationError("Controller already running for this database") from None
                        lock.seek(0)
                        lock.truncate()
                        lock.write(str(os.getpid()))
                        lock.flush()
                        stopping = False
                        def stop(_signum, _frame):
                            nonlocal stopping
                            stopping = True
                        signal.signal(signal.SIGTERM, stop)
                        signal.signal(signal.SIGINT, stop)
                        controller.recover(apply=True)
                        while not stopping:
                            controller.tick()
                            time.sleep(args.interval)
                    out = {"stopped": True}
            finally:
                controller.close()
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except (ValidationError, AccessError, ConflictError, OSError, sqlite3.Error) as exc:
        # Do not expose exception text from filesystem, database or transport errors.
        print(json.dumps({"error": type(exc).__name__, "message": str(exc) if isinstance(exc, (ValidationError, AccessError, ConflictError)) else "Operation failed; inspect private local configuration"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
