"""Strict, small data contracts. Policies are not an OS sandbox."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

MAX_DOCUMENT_BYTES = 48_000
MAX_RESULT_BYTES = 48_000
MAX_ARTIFACT_BYTES = 16_000
MAX_WIRE_BYTES = 120_000  # below pinned upstream's 131072-byte normalized operation limit
WORKERS = ("dot-b", "dot-c", "dot-a")
TERMINAL_UPSTREAM = {"completed", "failed", "cancelled", "expired"}


class ValidationError(ValueError):
    pass


class ConflictError(ValueError):
    pass


class AccessError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Value is not finite JSON") from exc


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def text(value, field, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise ValidationError(f"Invalid {field}")
    return value


def identifier(value, field="identifier"):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", value):
        raise ValidationError(f"Invalid {field}")
    return value


def upstream_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}", value):
        raise ValidationError("Invalid upstream task ID")
    return value


def fields(obj, required, optional=()):
    if not isinstance(obj, dict) or set(obj) - set(required) - set(optional) or set(required) - set(obj):
        raise ValidationError("Unknown or missing fields")


def bounded(obj, limit):
    if len(canonical(obj).encode()) > limit:
        raise ValidationError("Document exceeds byte limit; content was not truncated")


def wire_bounded(obj):
    # Pinned upstream model_dump is serialized with ensure_ascii=True and spaces.
    if len(json.dumps(obj, allow_nan=False).encode()) > MAX_WIRE_BYTES:
        raise ValidationError("Upstream normalized operation would exceed safe byte limit")


_SECRET = re.compile(r"(?i)(bearer\s+[^\s\"']+|sk-[a-zA-Z0-9_-]{12,}|(?:api[_-]?key|access[_-]?token|password|authorization|lease[_-]?token)\s*[=:]\s*[^\s,;]+)")


def redact(value, secrets=()):
    """Best-effort display redaction, never a claim of arbitrary-secret detection."""
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if re.search(r"(?i)(token|secret|password|authorization|lease)$", k) else redact(v, secrets)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        # Redact Bearer pairs first so an authorization-prefix match cannot consume
        # just the word Bearer and leave the credential behind.
        value = re.sub(r"(?i)bearer\s+[^\s\"']+", "[REDACTED]", value)
        return _SECRET.sub("[REDACTED]", value)
    return value


def reject_secrets(value):
    if redact(value) != value:
        raise ValidationError("Potential credential material is not allowed in job data")


def artifact_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}", name) or ".." in name:
        raise ValidationError("Artifact name must be a plain safe filename")
    return name


def validate_artifacts(items, required=(), allow_empty=False):
    if not isinstance(items, list) or len(items) > 8 or (not items and not allow_empty):
        raise ValidationError("Invalid artifact list")
    names = set()
    for item in items:
        fields(item, ("name", "media_type", "content"))
        name = artifact_name(item["name"])
        if name in names:
            raise ValidationError("Duplicate artifact name")
        names.add(name)
        if item["media_type"] not in ("text/plain", "text/markdown", "application/json"):
            raise ValidationError("Only small text artifacts are supported")
        text(item["content"], "artifact content", MAX_ARTIFACT_BYTES)
    if not set(required) <= names:
        raise ValidationError("Required artifact missing")


def validate_snapshot(spec):
    fields(spec, ("schema_version", "project_id", "snapshot_id", "artifacts"))
    if spec["schema_version"] != "multidot.snapshot.v1":
        raise ValidationError("Unsupported snapshot schema")
    identifier(spec["project_id"])
    identifier(spec["snapshot_id"])
    bounded(spec, MAX_DOCUMENT_BYTES)
    reject_secrets(spec)
    validate_artifacts(spec["artifacts"])
    return spec


def validate_job(spec):
    fields(spec, ("schema_version", "project_id", "request_id", "title", "goal", "input_snapshot_id", "policy_id", "steps"), ("synthesis",))
    if spec["schema_version"] != "multidot.job.v1" or spec["policy_id"] != "provided-materials-only":
        raise ValidationError("Unsupported schema or policy")
    bounded(spec, MAX_DOCUMENT_BYTES)
    reject_secrets(spec)
    for key in ("project_id", "request_id", "input_snapshot_id"):
        identifier(spec[key], key)
    text(spec["title"], "title", 200)
    text(spec["goal"], "goal")
    if not isinstance(spec["steps"], list) or not 1 <= len(spec["steps"]) <= 8:
        raise ValidationError("One to eight bounded steps required")
    ids = set()
    for step in spec["steps"]:
        fields(step, ("step_id", "worker", "instructions", "acceptance_criteria", "depends_on"), ("required_artifacts",))
        identifier(step["step_id"])
        if step["step_id"] in ids or step["step_id"] == "synthesis":
            raise ValidationError("Duplicate or reserved step ID")
        ids.add(step["step_id"])
        if step["worker"] not in ("dot-b", "dot-c"):
            raise ValidationError("P0 requires an explicitly selected B or C")
        text(step["instructions"], "instructions")
        if not isinstance(step["acceptance_criteria"], list) or not 1 <= len(step["acceptance_criteria"]) <= 12:
            raise ValidationError("Acceptance criteria required")
        for criterion in step["acceptance_criteria"]:
            text(criterion, "criterion", 1000)
        if not isinstance(step["depends_on"], list) or any(not isinstance(d, str) for d in step["depends_on"]) or len(step["depends_on"]) != len(set(step["depends_on"])):
            raise ValidationError("Invalid dependencies")
        required = step.get("required_artifacts", ["report.md"])
        if not isinstance(required, list) or not 1 <= len(required) <= 8 or any(not isinstance(n, str) for n in required) or len(required) != len(set(required)):
            raise ValidationError("Required artifact names must be unique")
        for name in required:
            artifact_name(name)
    graph = {s["step_id"]: s["depends_on"] for s in spec["steps"]}
    done = set()
    while len(done) < len(ids):
        ready = {k for k, deps in graph.items() if k not in done and set(deps) <= done}
        if not ready:
            raise ValidationError("Unknown or cyclic dependency")
        done |= ready
    if "synthesis" in spec:
        synth = spec["synthesis"]
        fields(synth, ("worker", "trigger", "instructions"))
        if synth["worker"] != "dot-a" or synth["trigger"] != "all_required_steps_accepted":
            raise ValidationError("Synthesis is one bounded A-only stage")
        text(synth["instructions"], "synthesis instructions")
    return spec


def validate_result(result, job_id, step_id, snapshot_id, required):
    fields(result, ("schema_version", "job_id", "step_id", "input_snapshot_id", "outcome", "summary", "findings", "artifacts", "checks", "open_questions", "external_changes"))
    bounded(result, MAX_RESULT_BYTES)
    reject_secrets(result)
    if (result["schema_version"], result["job_id"], result["step_id"], result["input_snapshot_id"]) != ("multidot.result.v1", job_id, step_id, snapshot_id):
        raise ValidationError("Result schema, job, step, or immutable input version mismatch")
    if result["outcome"] not in ("completed", "blocked", "needs_approval", "failed"):
        raise ValidationError("Unknown result outcome")
    text(result["summary"], "summary", 8000)
    if result["external_changes"] != []:
        raise ValidationError("External changes are outside the allowed policy")
    for key in ("findings", "checks", "open_questions"):
        if not isinstance(result[key], list) or len(result[key]) > 40:
            raise ValidationError(f"Invalid {key}")
    for finding in result["findings"]:
        fields(finding, ("claim", "evidence_ref"))
        text(finding["claim"], "claim")
        text(finding["evidence_ref"], "evidence reference", 400)
    for check in result["checks"]:
        fields(check, ("name", "status", "evidence_ref"))
        if check["status"] not in ("passed", "failed", "not_tested"):
            raise ValidationError("Invalid check status")
        text(check["name"], "check name", 200)
        text(check["evidence_ref"], "evidence reference", 400)
    for question in result["open_questions"]:
        text(question, "open question")
    validate_artifacts(result["artifacts"], required if result["outcome"] == "completed" else (), allow_empty=result["outcome"] != "completed")
    return result
