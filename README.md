# MultiDot

A small Python controller for one dot2api Native Task service, separate `dot-b`
and `dot-c` queues, and one `dot-a` synthesis queue. The controller owns a SQLite
database; it never accesses dot2api's database. Runtime dependencies: **none**.

**Current verification: 103 MOCK and 21 actual-upstream LOCAL_CONTRACT tests pass.**
See the [contract report](docs/local-contract-report.md) for exact scope and
cleanup. Real B/C accounts, authenticated MCP ingress, Events, user delivery,
and always-on hosting have not been verified. Only disposable synthetic test
identities were used; no public service or persistent access was created.
The reference upstream is pinned to `66a761505ff73c5dca730260efcaeb5697db81ad`.

Portable supervision is now prepared locally; see
[persistent-runtime preparation and limits](docs/persistent-runtime.md).
For entering your existing keys yourself, use the
[private key config guide](docs/private-key-config.md). The template is blank,
the filled config is Git-ignored, and the user-run helper makes no network calls.
The expanded suite has 103 MOCK tests, 21 actual-upstream LOCAL_CONTRACT tests,
and 15 fake-control-plane runtime checks. This remains separate from REAL_DOT
and Events acceptance; no real credential or tunnel connection was created.

## Run without installing anything

From this repository, with Python 3.11+:

```sh
PYTHONPATH=src python3 -m multidot doctor
PYTHONPATH=src python3 -m multidot demo
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 scripts/check.py
```

The demo uses an isolated temporary database and an in-memory mock. It completes
B, then C, then A synthesis, reopens controller state, and reports
`notification_pending`. It does not contact accounts or run upstream software.
The demo does not prove semantic correctness of generated analysis.

## Create durable local work

The example endpoint is a **placeholder**, not a discovered running service.
No network or credentials are needed for these commands:

```sh
export PYTHONPATH=src
python3 -m multidot --config config/controller.example.json bootstrap --project demo --goal 'Provided-materials comparison'
python3 -m multidot --config config/controller.example.json snapshot --file examples/snapshot.json
python3 -m multidot --config config/controller.example.json submit --file examples/job.json
python3 -m multidot --config config/controller.example.json status
python3 -m multidot --config config/controller.example.json get JOB_ID
python3 -m multidot --config config/controller.example.json request-cancel JOB_ID --reason 'User request'
python3 -m multidot --config config/controller.example.json recover --dry-run
```

State goes to ignored `var/hub.sqlite` (override with global `--db`). Bootstrap
does not overwrite project goals, snapshots, credentials, or worker pause state.
Snapshots and request IDs are immutable: use a new version or ID to change work.
Small text artifacts live as integrity-checked BLOBs in the controller DB.

`get`, `status`, and `workers` show **last observed** state. After authorized
native setup, run `tick` before a status query to synchronize. No exact progress,
global account load, remaining quota, worker principal, or user delivery is
inferred from queued/running/completed states.

## Native setup is gated

Read [user actions](docs/user-actions.md) and [upstream contract](docs/upstream-verification.md).
Installing/executing upstream, creating credentials or persistent access,
opening a tunnel/public endpoint, publishing this repository, and connecting
other accounts require their respective approvals. Local upstream tests and
publication were approved for this development task; that does not approve real
account setup or public exposure. Nothing here performs those actions automatically. Do not paste tokens in chat, a command line, or Git.

After a separately approved and verified installation, the operator supplies
the correct origin and producer identity in a private config and injects the
producer token into the configured environment variable through an approved
secret mechanism. Then these commands become available:

```sh
python3 -m multidot --config PRIVATE_CONFIG tick
python3 -m multidot --config PRIVATE_CONFIG start --interval 5
# Ctrl-C or SIGTERM stops this foreground process; no daemon is installed.
python3 -m multidot --config PRIVATE_CONFIG recover --dry-run
python3 -m multidot --config PRIVATE_CONFIG recover --apply
```

Keep controller and upstream in the **same tested network context**. Phase 0
found shared files but separate executor/cloud-desktop loopbacks. An executor's
localhost success is not evidence of a deployed dot cloud endpoint.

## Boundaries and evidence

- Only bounded provided-materials analysis; no shell runner or artifact execution
- One active reservation per worker, atomic with the outbox before network POST
- Exact producer, target, key, and canonical body retained across ambiguous sends
- `max_attempts=1`; no account failover or automatic external-work reexecution
- Revision and terminal-result fencing; schema acceptance is not factual approval
- Exact accepted result hashes determine the single synthesis version
- Cancellation request and upstream cancellation are distinct from worker-stop proof
- Small text files, safe names, byte/hash checks, best-effort secret rejection/redaction
- Same-user local CLI trust boundary, not an OS isolation or remote authorization service

See [architecture](docs/architecture.md), [operations](docs/operations.md),
[recovery](docs/recovery.md), [environment](docs/environment-report.md), and
[acceptance status](docs/acceptance-report.md).
