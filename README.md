# MultiDot

A small Python controller for a configurable list of dots, backed by one
pinned dot2api Native Task service. Give each dot a display name, an existing
tunnel ID and a runtime API key. Local setup creates the internal queue
identities, tokens, ports and state for you.

**Start here: [Quick setup guide](docs/simple-setup.md).**
Names and the number of dots come from your list; A/B/C and three-dot examples
are not requirements. Names support Unicode. Every entry defaults to `worker`;
advanced setups may select at most one `synthesis` entry and must retain at
least one regular worker. Configuration byte limits, available ports and host
resources still apply.

## Preferred setup

Python 3.11+ and a Linux runtime with the reviewed, pinned toolchain are required.
Run these commands yourself in the intended runtime environment:

```sh
python3 scripts/multidot.py init
# Privately edit ~/.multidot/config.json with your dots list.
python3 scripts/multidot.py setup
python3 scripts/multidot.py run
```

For a fresh installation, the default private config is
`~/.multidot/config.json`, and generated runtime state and credentials live
under `~/.multidot/state/`. These paths use the current OS user's home directory,
not the repository or calling directory. Existing installations are not moved
or reprovisioned automatically: use explicit `--config` and `--state-root`
paths to retain them and review the
[existing-installation guidance](docs/persistent-runtime.md#existing-installations-and-manual-path-changes)
before using the new defaults. The pinned toolchain still defaults to the
repository's `multidot-tools` sibling.

`init` creates a missing `~/.multidot` directory with 0700 permissions and a
0600 config file. It refuses unsafe or symlinked existing application
directories, leaves existing directory permissions unchanged, and does not
create the user's home directory. Custom config paths must keep a
`.private.json` or `.local.json` suffix; bare `config.json` is allowed only at
the default private home location.

The blank [public template](config/dots.example.json) contains only
`name`, `tunnel_id` and `runtime_api_key` per entry. You do not need to find or
enter internal producer/worker tokens. `setup` verifies an **already-installed**
toolchain, initializes a fresh local installation and creates its internal
credentials. It does not download software, contact OpenAI, create tunnels or
start services. `run` starts the configured stack in the foreground and attempts
the approved tunnel connections. Keep its session open; Ctrl-C stops it.

**Preparation is not a live deployment.** No real setup, authenticated tunnel
connection, account execution, Events delivery or always-on hosting has been
verified. The intended host remains **dot's own cloud**, not the user's Mac;
a supported private editing route for the user in that cloud is still
unverified. Do not enter real keys until that route and the required setup/run
approvals are in place. Never paste keys in chat, command arguments or Git.

Use the [private config guide](docs/private-key-config.md) for key permissions
and handling, and [runtime guide](docs/persistent-runtime.md) for toolchain,
state, recovery and hosting limits. Internal tokens last 30 days; there is no
silent renewal or key rotation. Setup reruns preserve identities when labels or
list order change. Adding/removing dots or changing tunnels, roles or keys in an
existing installation requires an explicit migration/rotation workflow.

## Try the offline demo

The controller itself has no third-party runtime dependencies:

```sh
PYTHONPATH=src python3 -m multidot doctor
PYTHONPATH=src python3 -m multidot demo
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 scripts/check.py
```

The demo uses an isolated temporary database and an in-memory mock. Its legacy
B/C/A names illustrate two workers followed by synthesis; they do not define
the new setup's names or count. It reopens controller state and reports
`notification_pending`. It does not contact accounts, execute upstream software
or prove semantic correctness of generated analysis.

## Evidence and boundaries

MOCK, actual-upstream LOCAL_CONTRACT and fake-control-plane runtime evidence
remain separate from REAL_DOT and Events acceptance. See the
[generic setup validation](docs/generic-setup-report.md),
[contract report](docs/local-contract-report.md),
[runtime evidence](evidence/runtime/acceptance.json) and
[acceptance report](docs/acceptance-report.md) for the scope and counts of their
respective checkpoints. Historical test counts are not a claim about every
later source change. Tests used disposable synthetic identities.

The dot2api source is pinned to
`66a761505ff73c5dca730260efcaeb5697db81ad`; the official OpenAI tunnel client is
pinned to v0.0.16. The controller owns its SQLite DB and talks to dot2api only
through the Native Task HTTP API. Only the explicit, user-run setup initializes
upstream storage through the reviewed upstream APIs.

- Bounded provided-materials analysis; no shell runner or artifact execution
- One active reservation per worker, atomic with the outbox before network POST
- Exact producer, target, key and canonical body retained across ambiguous sends
- `max_attempts=1`; no account failover or automatic external-work reexecution
- Revision and terminal-result fencing; schema acceptance is not factual approval
- Accepted result hashes determine a requested synthesis version
- Cancellation is distinct from proof that a worker stopped
- Small text artifacts, safe names, byte/hash checks and best-effort redaction
- Same-user local CLI trust boundary, not OS isolation or remote authorization

Keep controller and upstream in the **same tested network context**. Shared
files do not imply shared loopback/PID namespaces, and a localhost success does
not prove a deployed cloud endpoint. `get`, `status` and `workers` show last
observed state, not exact progress, account quota or proof of user delivery.

## Advanced and legacy compatibility

The original v1/B-only runtime, manual credential helper and B/C/A controller
examples remain for existing installations and regression coverage. They are
not the preferred setup for a new installation. Do not mix their config schema
with the new dots list. See the explicitly labeled legacy sections in the
[private config](docs/private-key-config.md#advanced-legacy-v1-compatibility)
and [runtime](docs/persistent-runtime.md#advanced-legacy-v1-compatibility) guides.

For durable offline work, controller commands and historical design details,
see [operations](docs/operations.md), [architecture](docs/architecture.md),
[recovery](docs/recovery.md), [environment](docs/environment-report.md),
[user actions](docs/user-actions.md) and
[upstream contract](docs/upstream-verification.md). Older A/B/C examples in those
documents describe the legacy fixture, not a limit on configurable dots.
