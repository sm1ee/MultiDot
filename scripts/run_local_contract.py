"""Opt-in LOCAL_CONTRACT runner. Requires an already approved isolated install.

This does not install upstream, contact real accounts, create persistent access,
start background daemons, or publish anything. It exits after owned cleanup.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from multidot.dot2api_adapter import UPSTREAM_COMMIT

EXPECTED_TREE = "e83f79db4097321b6cf892935ba85f28a76aaa47"
EXPECTED_LOCK_SHA256 = "b233641bbb90ce6ebd1a9fedb82bc4fd9afcfc6837c496ea3e085a37bf8582be"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(upstream):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(upstream), *args], text=True).strip()
    if git("rev-parse", "HEAD") != UPSTREAM_COMMIT or git("rev-parse", "HEAD^{tree}") != EXPECTED_TREE:
        raise RuntimeError("Upstream checkout does not match the reviewed commit and tree")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("Upstream checkout must be clean")
    if sha(upstream / "uv.lock") != EXPECTED_LOCK_SHA256:
        raise RuntimeError("Upstream dependency lock differs from the reviewed pin")
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("Use an isolated virtual environment")
    normalize = lambda value: value.lower().replace("_", "-")
    lock = tomllib.loads((upstream / "uv.lock").read_text())
    locked = {normalize(item["name"]): item["version"] for item in lock["package"]}
    installed = {normalize(item.metadata["Name"]): item.version for item in importlib.metadata.distributions()}
    if any(locked.get(name) != version for name, version in installed.items()):
        raise RuntimeError("Installed package versions differ from the committed lock")
    import dot2api
    if Path(dot2api.__file__).resolve().parent != upstream / "src" / "dot2api":
        raise RuntimeError("Imported upstream implementation does not come from the verified checkout")
    return {"repository": "https://github.com/Pluviobyte/dot2api", "commit": UPSTREAM_COMMIT,
            "tree": EXPECTED_TREE, "lock_sha256": EXPECTED_LOCK_SHA256,
            "pyproject_sha256": sha(upstream / "pyproject.toml"), "license_sha256": sha(upstream / "LICENSE"),
            "version": importlib.metadata.version("dot2api"), "license": "MIT", "checkout_clean": True,
            "all_installed_versions_match_lock": True, "packages": dict(sorted(installed.items()))}


class ReceiptResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.records = []

    def startTest(self, test):
        self.started = time.monotonic()
        self.at = datetime.now(timezone.utc).isoformat()
        super().startTest(test)

    def record(self, test, outcome, error=None):
        self.records.append({"test_name": test.id(), "classification": "LOCAL_CONTRACT",
                             "started_at": self.at, "seconds": round(time.monotonic() - self.started, 6),
                             "result": outcome, "error_type": error})
        print(f"{outcome} {test.id()}" + (f" ({error})" if error else ""), flush=True)

    def addSuccess(self, test):
        self.record(test, "PASS")
        super().addSuccess(test)

    def addError(self, test, err):
        self.record(test, "FAIL", err[0].__name__)
        super().addError(test, err)

    def addFailure(self, test, err):
        self.record(test, "FAIL", err[0].__name__)
        super().addFailure(test, err)

    def addSubTest(self, test, subtest, err):
        if err:
            self.record(test, "FAIL", err[0].__name__)
        super().addSubTest(test, subtest, err)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "evidence" / "local-contract" / "report.json")
    parser.add_argument("--allow-temporary-fixtures", action="store_true", required=True,
                        help="Confirm this approved run may initialize temporary synthetic-only upstream state")
    args = parser.parse_args()
    upstream = args.upstream.resolve()
    provenance = verify(upstream)
    started = datetime.now(timezone.utc).isoformat()
    sys.path.insert(0, str(ROOT / "tests_local_contract"))
    import native_fixture
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests_local_contract"), pattern="test_*.py")
    result = ReceiptResult()
    suite.run(result)
    provenance_after = verify(upstream)
    manifest = {str(path.relative_to(ROOT)): sha(path) for directory in ("src", "tests_local_contract")
                for path in sorted((ROOT / directory).rglob("*.py"))}
    manifest["scripts/run_local_contract.py"] = sha(Path(__file__).resolve())
    cleanup_ok = bool(native_fixture.CLEANUPS) and all(
        row["server_stopped"] and row["loopback_listener_closed"] and row["temporary_state_removed"]
        and row["external_transport_calls"] == 0 for row in native_fixture.CLEANUPS)
    passed = result.wasSuccessful() and cleanup_ok and provenance == provenance_after
    receipt = {"classification": "LOCAL_CONTRACT", "started_at": started,
               "finished_at": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
               "upstream_executed": True, "upstream": provenance, "tests_run": result.testsRun,
               "passed": passed, "tests": result.records, "source_sha256": manifest,
               "transport": "Actual uvicorn/FastAPI loopback HTTP; client and server in the same process namespace",
               "identities": [{"principal": principal, "tenant": tenant, "queues": queues, "scopes": scopes}
                              for principal, tenant, queues, scopes in native_fixture.IDENTITIES],
               "identity_binding": "Fixture Security.authenticate verifies each temporary token's actual tenant/creator before retry tests; public task still omits principals",
               "fixture_methods": "Official initialize, Security.create_principal/issue_token/authenticate; injected Service/Security test clock; no upstream SQL in controller or harness",
               "credentials": "Ephemeral test-marked fixture tokens and temporary encryption key; never logged; fixture directories removed",
               "fault_injection": "Selected responses replaced or delayed only after actual upstream execution; named fault cases are synthetic transport faults",
               "lease_evidence": "Clock-injected exact-boundary tests, not real elapsed-time or real-worker evidence",
               "controller_database_separate": True, "background_worker_enabled": False,
               "external_transport_calls": sum(row["external_transport_calls"] for row in native_fixture.CLEANUPS),
               "cleanup_verified": cleanup_ok, "fixtures": native_fixture.CLEANUPS,
               "real_dot": "NOT_TESTED / NEEDS_USER_ACTION", "events": "NOT_TESTED",
               "public_service_or_tunnel": False, "persistent_access_created": False,
               "no_upstream_implementation_vendored": True}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"LOCAL_CONTRACT: {result.testsRun} tests; passed={passed}; owned cleanup={cleanup_ok}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
