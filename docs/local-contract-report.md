# Actual upstream LOCAL_CONTRACT verification

2026-10-09 UTC; MultiDot 0.1.0. **21/21 LOCAL_CONTRACT tests pass.**
The executed receipt records 03:21:41.098579–03:21:46.171419 UTC, each test,
source hashes, exact installed versions, and cleanup observations:
[report.json](../evidence/local-contract/report.json),
[suite.log](../evidence/local-contract/suite.log),
[installation receipt](../evidence/local-contract/install.json).
An [independent repeat](../evidence/local-contract/independent-report.json)
passed the same 21 tests against identical current source hashes, with all 22
fixture cleanups confirmed and zero external transport calls.
These are separate from the 62-test MOCK suite. **No REAL_DOT test passed or
ran.** This is API/controller compatibility with synthetic fixture identities,
not a connection to B/C/A accounts, a deployment, or full P0 acceptance.

## Provenance and isolated installation

- Actual repository: https://github.com/Pluviobyte/dot2api
- Commit: `66a761505ff73c5dca730260efcaeb5697db81ad`
- Tree: `e83f79db4097321b6cf892935ba85f28a76aaa47`
- Version 0.1.0, MIT, declared Python >=3.11; executed on Python 3.12.14
- Committed `uv.lock` SHA-256:
  `b233641bbb90ce6ebd1a9fedb82bc4fd9afcfc6837c496ea3e085a37bf8582be`
- `uv 0.12.23`; `uv sync --frozen --no-default-groups` into a sibling isolated
  `.venv`, with a sibling package cache. No global/system install.
- All 18 installed runtime/project package versions matched the committed lock.
  Build isolation separately resolved upstream's `hatchling>=1.27,<2` to
  hatchling 1.32.4; observed transient build-package versions are in the install
  receipt. The installation is not claimed to be a fully hermetic build lock.
- Exact HEAD, tree, clean working tree, lock hash, installed versions and imported
  source location were checked before and after execution. No upstream patch or
  implementation source is vendored in MultiDot.

Installation was explicitly authorized for local testing. Reproduction requires
an already approved isolated installation; the test runner does not install it.
From the MultiDot checkout:

```sh
../MultiDot-local-contract-20261009/.venv/bin/python scripts/run_local_contract.py \
  --upstream ../MultiDot-local-contract-20261009/upstream \
  --allow-temporary-fixtures
```

For an independent receipt, append `--output /tmp/multidot-independent-local-contract.json`.
Default `tests/` discovery stays MOCK-only; actual-upstream tests live in the
separate opt-in `tests_local_contract/` directory.

## What executed

The actual upstream FastAPI app served HTTP through uvicorn on an ephemeral
`127.0.0.1` listener. The controller's unmodified `NativeTaskAdapter` and the
fixture client ran in that same process/network namespace. Controller storage
was separate from the disposable upstream database.

Upstream's supported initialization and test-fixture methods provisioned
synthetic principals: one producer, another creator, queue-B workers, a queue-C
worker, and an unrelated tenant. Temporary test-marked credentials were issued
only inside that disposable service, kept in memory, and never logged or saved
in Git. Initialization also created a temporary encryption key. The runner
removed all fixture storage afterward. No real credential/account was used.

Only fixture setup and test-clock injection touched upstream objects. No
controller or harness SQL read or modified upstream tables. Worker claim,
heartbeat, complete and release operations were fixture HTTP calls; the
controller remained producer-only. `list_tasks` was verified through the Native
tool endpoint; a production controller list/subscription method was not added.

### Passed coverage

- Native create/get envelopes, task fields and Korean content round trip
- Native list sequence pagination and first-page reread after an old task changes
- Same key/body reuses one task; payload, queue or TTL changes return HTTP 409
- Actual tenant/creator-scoped idempotency: another creator gets a distinct task
- Queue, tenant, creator and scope isolation, including B→C/A read/claim denial;
  worker submit and producer claim denied
- Idempotent cancellation; cancellation fences completion and new claims
- Completion is idempotent only for the same principal, lease and result;
  changed result and other-worker completion conflict; terminal cancel is 409
- `max_attempts=1` lease boundary fails instead of requeueing; release cannot retry
- Heartbeat keeps revision unchanged and caps renewal at the task deadline
- Native sanitized 422 and actual empty-body 413; adapter preserves error status
- Exact 131,072-byte normalized upstream limit, Korean ASCII-escaping expansion,
  and the controller's conservative 120,000-byte boundary, without truncation
- Missing/invalid auth and unknown-task errors
- Named synthetic response faults after actual upstream execution: timeout,
  non-JSON, oversize, and 503 are rejected or remain uncertain as appropriate
- Controller lost-response retry reuses exact stored request and task, with the
  actual fixture creator/tenant established before retry
- Controller reopen accepts a schema-valid fixture result and durable artifact,
  while keeping claim identity unverified and delivery notification pending
- Uncertain reads retain the reservation and later reconcile authoritatively
- Controller cancellation releases the slot without claiming the worker stopped

Lease/deadline tests use an injected clock, following upstream's own test
pattern. They prove boundary handling, **not real elapsed lease timing**.
Response-fault cases replace or delay selected responses after the real service
has processed them; these are explicitly fault-injected LOCAL_CONTRACT cases,
not claims that the unmodified upstream naturally produced those faults.

## Identity and retry limitation stays open

Fixture `Security.authenticate` established each token's actual principal and
tenant, so local retry assertions used one pinned identity. Production config's
`producer` remains only a label: changing a real token to another creator while
retaining that label can defeat upstream idempotency. Native public task JSON
omits creator, tenant and worker principals. A supported, trusted non-secret
identity binding is still required before real retry/recovery or account claims.

## Cleanup and untested boundaries

All **22 owned fixture servers** stopped, their loopback listeners closed, and
their temporary databases/encryption keys were removed. External webhook
transport was explicitly disabled and recorded **0 calls**. No background
worker, daemon, autostart, tunnel, public listener, external service, paid host,
real account setup or persistent access was created. The reviewed checkout,
venv and package cache remain locally for repeatable review; they are outside
this repository and contain no surviving fixture credentials.

Still untested: REAL_DOT B/C connections, Events/subscriptions, authenticated MCP
account routing, real key rotation/revocation and existing-subscription behavior,
actual user delivery, semantic correctness, always-on lifetime, cross-namespace
loopback reachability and host restart. The earlier environment limitations
still apply. No runtime adapter changes were needed for these contract tests.
