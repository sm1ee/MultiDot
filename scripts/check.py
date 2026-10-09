"""Run bounded local MOCK tests and write a sanitized per-test evidence receipt."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import sys
import time
import unittest

from multidot import __version__
from multidot.dot2api_adapter import UPSTREAM_COMMIT

ROOT = Path(__file__).resolve().parents[1]


class ReceiptResult(unittest.TextTestResult):
    def startTest(self, test):
        self.started = time.monotonic()
        self.at = datetime.now(timezone.utc).isoformat()
        super().startTest(test)

    def addSuccess(self, test):
        self.record(test, "PASS")
        super().addSuccess(test)

    def addError(self, test, err):
        self.record(test, "FAIL", err[0].__name__)
        super().addError(test, err)

    def addFailure(self, test, err):
        self.record(test, "FAIL", err[0].__name__)
        super().addFailure(test, err)

    def record(self, test, status, error=None):
        records.append({"test_name": test.id(), "test_ids": re.findall(r"t\d{2}", test.id(), re.I), "classification": "MOCK",
                        "target_version": __version__, "run_at": self.at, "seconds": round(time.monotonic()-self.started, 6), "result": status, "error_type": error})


records = []
suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
result = unittest.TextTestRunner(verbosity=2, resultclass=ReceiptResult).run(suite)
manifest = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for directory in ("src", "tests", "scripts") for p in sorted((ROOT / directory).rglob("*.py"))}
receipt = {"classification": "MOCK", "target_version": __version__, "upstream_commit": UPSTREAM_COMMIT,
           "upstream_executed": False, "python": platform.python_version(), "ran_at": datetime.now(timezone.utc).isoformat(),
           "tests_run": result.testsRun, "passed": result.wasSuccessful(), "tests": records, "source_sha256": manifest,
           "local_contract": "NOT_TESTED", "real_dot": "NEEDS_USER_ACTION"}
(ROOT / "evidence").mkdir(exist_ok=True)
(ROOT / "evidence" / "mock-test-report.json").write_text(json.dumps(receipt, indent=2) + "\n")
raise SystemExit(0 if result.wasSuccessful() else 1)
