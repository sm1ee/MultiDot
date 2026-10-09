# Pinned upstream source review

Review and execution date: 2026-10-09 UTC. Source was first inspected read-only,
then installed and executed with explicit local-test approval. **21 actual
LOCAL_CONTRACT tests pass**; see the [execution report](local-contract-report.md).

- Repository: https://github.com/Pluviobyte/dot2api
- Commit: `66a761505ff73c5dca730260efcaeb5697db81ad`
- Source license: MIT; no upstream implementation source is vendored here
- Runtime declaration: Python >=3.11
- Verified tree: `e83f79db4097321b6cf892935ba85f28a76aaa47`
- Exact locked runtime versions and Python 3.12.14 compatibility: verified in
  [LOCAL_CONTRACT evidence](../evidence/local-contract/report.json)
- Build isolation version details/limits: [install receipt](../evidence/local-contract/install.json)

Reviewed source contracts:
[models](https://github.com/Pluviobyte/dot2api/blob/66a761505ff73c5dca730260efcaeb5697db81ad/src/dot2api/models.py),
[service](https://github.com/Pluviobyte/dot2api/blob/66a761505ff73c5dca730260efcaeb5697db81ad/src/dot2api/service.py),
[app](https://github.com/Pluviobyte/dot2api/blob/66a761505ff73c5dca730260efcaeb5697db81ad/src/dot2api/app.py),
[API](https://github.com/Pluviobyte/dot2api/blob/66a761505ff73c5dca730260efcaeb5697db81ad/docs/api.md),
[architecture](https://github.com/Pluviobyte/dot2api/blob/66a761505ff73c5dca730260efcaeb5697db81ad/docs/architecture.md),
[worker guide](https://github.com/Pluviobyte/dot2api/blob/66a761505ff73c5dca730260efcaeb5697db81ad/docs/dot.md),
[security](https://github.com/Pluviobyte/dot2api/blob/66a761505ff73c5dca730260efcaeb5697db81ad/SECURITY.md).

## Adapter contract

POST `/v1/tasks` accepts queue, instructions, payload, idempotency_key,
max_attempts, ttl_seconds; unknown fields are rejected. The adapter sends all
six explicitly. max_attempts is always 1; TTL is 3,600 seconds. Upstream permits
1–20 attempts and 60–2,592,000-second TTL. Identifiers use
`^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$`, length 1–128. Controller's own IDs use a
deliberately narrower alphabet.

GET `/v1/tasks/{task_id}` and submit return HTTP 200 with `{task: ...}`.
Task fields include id, sequence, queue, instructions, payload, status, result,
error, attempts, max_attempts, revision, created_at, updated_at, expires_at,
available_at, lease_expires_at. Worker/creator principals and lease hashes are
not public task fields. Cancellation is POST `/v1/tools/cancel_task` with
`task_id` and `reason`; terminal-state conflict is 409.

Idempotency scope is tenant + creator + key, not queue + key. Fingerprint is
normalized model dump excluding the idempotency key, canonical sorted compact
JSON. Same body reuses the original task; changed body returns 409. MultiDot
stores the complete explicit request body and its hash before network I/O.

Important live gate: `producer` in this controller is a configured label, not
cryptographic proof of the token's actual tenant/creator. Replacing a token with
one for a different principal while retaining the label can defeat upstream
idempotency. Before any real retry/recovery, establish the same actual
tenant/creator through trusted setup evidence. Public task JSON cannot prove
that binding; a verified non-secret identity binding is a future hardening need.

Normalized operation size is 131,072 bytes using `json.dumps(model_dump(),
allow_nan=False)` with default ASCII escaping and spaces. Outer HTTP body
defaults to 262,144 bytes. MultiDot uses the smaller 120,000-byte operation
budget. HTTP 413 and 408 may have empty bodies. The adapter does not echo
upstream error bodies or assume errors are JSON. It refuses redirects to avoid
forwarding its bearer credential and keeps TLS verification enabled.

Claim/heartbeat default to a 600-second lease, range 30–3,600. Heartbeat does not
increase task revision. Same principal + lease + result allows duplicate
completion; changed result conflicts. max_attempts=1 means lease expiry fails
rather than retries work. Producer has read/submit only; real workers own
claim/heartbeat/complete. Controller never calls those operations.

Events are `task.available` and `task.updated`, without replay. List pagination
uses sequence, not modification time; subscription reconnect must rescan from
the first page. Token revocation does not itself disable old subscriptions.
No controller Events code or subscription lifecycle is claimed here.

## Verification boundary

The ordinary `tests/test_http_adapter.py` tests run a purpose-built local schema
double: classification **MOCK**. They remain separate from the opt-in
`tests_local_contract/` suite, which executed the actual pinned upstream app and
unmodified controller adapter through loopback HTTP. All 21 contract tests pass;
exact source, lock, identity fixture, fault-injection, injected-clock and cleanup
evidence is in the [execution report](local-contract-report.md).

REAL_DOT, Events and account routing remain NOT_TESTED / NEEDS_USER_ACTION.
A temporary fixture identity is not a B/C/A account. No upstream patches or
production adapter changes were needed. Local creator-binding evidence does not
remove the production identity-binding limitation described above.
