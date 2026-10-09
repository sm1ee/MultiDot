"""Controller-owned database. All network operations happen outside transactions."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, goal TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots(project_id TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL,
 body TEXT NOT NULL, hash TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(project_id,id));
CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY, queue TEXT UNIQUE NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
 last_observed_at TEXT, last_running_at TEXT, last_completed_at TEXT, capabilities TEXT NOT NULL DEFAULT 'UNVERIFIED');
CREATE TABLE IF NOT EXISTS worker_projects(worker_id TEXT NOT NULL REFERENCES workers(id), project_id TEXT NOT NULL REFERENCES projects(id),
 PRIMARY KEY(worker_id,project_id));
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), request_id TEXT NOT NULL,
 requester TEXT NOT NULL, spec TEXT NOT NULL, hash TEXT NOT NULL, snapshot_hash TEXT NOT NULL, state TEXT NOT NULL,
 cancel_requested_at TEXT, cancel_reason TEXT, delivery TEXT NOT NULL DEFAULT 'not_ready', presentation_ref TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(project_id,request_id));
CREATE TABLE IF NOT EXISTS steps(job_id TEXT NOT NULL REFERENCES jobs(id), id TEXT NOT NULL, worker TEXT NOT NULL REFERENCES workers(id),
 kind TEXT NOT NULL, spec TEXT NOT NULL, state TEXT NOT NULL, result TEXT, result_hash TEXT, error_code TEXT,
 synthesis_version TEXT, PRIMARY KEY(job_id,id));
CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, job_id TEXT NOT NULL, step_id TEXT NOT NULL, worker TEXT NOT NULL,
 attempt_no INTEGER NOT NULL CHECK(attempt_no=1), producer TEXT NOT NULL, target TEXT NOT NULL, request_key TEXT UNIQUE NOT NULL,
 body TEXT NOT NULL, body_hash TEXT NOT NULL, upstream_id TEXT UNIQUE, upstream_status TEXT, revision INTEGER,
 observation_hash TEXT, lease_expires_at TEXT, observed_at TEXT, created_at TEXT NOT NULL,
 observation_errors INTEGER NOT NULL DEFAULT 0, next_observe REAL NOT NULL DEFAULT 0, observation_halted INTEGER NOT NULL DEFAULT 0,
 cancel_errors INTEGER NOT NULL DEFAULT 0, next_cancel REAL NOT NULL DEFAULT 0, cancel_halted INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(job_id,step_id) REFERENCES steps(job_id,id), UNIQUE(job_id,step_id,attempt_no));
CREATE TABLE IF NOT EXISTS reservations(worker TEXT PRIMARY KEY REFERENCES workers(id), attempt_id TEXT UNIQUE NOT NULL REFERENCES attempts(id));
CREATE TABLE IF NOT EXISTS dispatch_outbox(attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), state TEXT NOT NULL,
 sends INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0, last_error TEXT);
CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, job_id TEXT NOT NULL, step_id TEXT NOT NULL, name TEXT NOT NULL,
 media_type TEXT NOT NULL, size INTEGER NOT NULL, hash TEXT NOT NULL, content BLOB NOT NULL, snapshot_hash TEXT NOT NULL,
 result_hash TEXT NOT NULL, UNIQUE(job_id,step_id,name));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL,
 job_id TEXT, action TEXT NOT NULL, code TEXT NOT NULL);
PRAGMA user_version=1;
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("Database may not be a symlink")
        # Deployment requires a private state directory; local same-user callers are trusted.
        self.db = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise ValueError("Unsupported database version")
        self.db.executescript(SCHEMA)

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def close(self):
        self.db.close()
