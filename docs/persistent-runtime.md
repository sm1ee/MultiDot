# Configurable runtime, preparation only

Use the [quick setup guide](simple-setup.md) for the preferred user flow:
`multidot.py init` → private dots-list edit → `setup` → foreground `run`.
The list selects the names and number of dots. A/B/C and a count of three are
legacy examples, not architecture requirements.

**No real setup, authenticated tunnel connection, actual dot-account execution,
Events acceptance or always-on deployment has been verified.** Tests use
synthetic identities. The intended host remains dot's own cloud, not the user's
Mac, and a safe private editing route for the user in that cloud is still
unverified. Code support does not establish that access or a live connection.

## Prerequisites and paths

The controller is standard-library-only on Python 3.11+. Runtime supervision
also uses Linux `/proc`, `flock`, pidfd, parent-death signals and atomic
`renameat2` directory publication. This is not a native macOS supervisor.

The operator needs an already-installed, reviewed toolchain:

- [dot2api](upstream-verification.md), pinned to
  `66a761505ff73c5dca730260efcaeb5697db81ad`, in its isolated frozen environment
- Official [OpenAI tunnel-client v0.0.16](https://github.com/openai/tunnel-client/releases/tag/v0.0.16)

The reviewed Linux amd64 tunnel archive matched the release's SHA256SUMS; its
reported version was `0.0.16+5f99daabd4aa4a77049e6d81d54a0d8c18335397`.
Setup/preflight verify the pinned executable and upstream installation rather
than fetching or upgrading them. The upstream verifier checks commit, tree,
clean checkout, lockfile, import location and installed package versions.
See the [installation evidence](../evidence/local-contract/install.json) for the
historical local installation. No OS boot service has been installed.

By default, the toolchain is in a `multidot-tools` sibling of the repository:

```text
multidot-tools/
  dot2api-pinned/
  dot2api-venv/bin/python
  tunnel-client-v0.0.16/installed/tunnel-client
```

The default state directory is the repository's sibling `MultiDot-state`.
For a different approved location, supply paths, never keys:

```sh
python3 scripts/multidot.py setup --tools-root /ABS/PATH/multidot-tools --state-root /ABS/PATH/multidot-state
python3 scripts/multidot.py run --state-root /ABS/PATH/multidot-state
```

`/ABS/PATH/…` values are placeholders. Reuse the same state root on later
commands; changing it creates a different installation target, not a migration.
Tool verification may execute the installed verifier/interpreter, but setup
makes no external request and starts no service. Missing/mismatched toolchains
are refused; the simple setup command does not install them automatically.

## What user-run setup does

After the required approval for local credential creation, the user runs:

```sh
python3 scripts/multidot.py init
# Privately edit config/dots.private.json yourself.
python3 scripts/multidot.py setup
```

Each entry supplies `name`, `tunnel_id`, `runtime_api_key`, plus optional `role`.
`worker` is the default. At least one regular worker is required; at most one
entry may have `role: synthesis`. Names are nonempty Unicode display labels,
not queue identities or role selectors. There is no fixed 3/4/16-dot count
limit; configuration byte budgets, OS ports, process capacity and memory bound
what can run. Each entry needs a distinct existing tunnel ID.

A new setup selects free loopback ports, creates stable UUID-based worker/queue
IDs and a tenant, initializes fresh upstream storage through its reviewed APIs,
issues internal tokens, verifies their local bindings and initializes the
separate controller database. The producer gets `read`/`submit` on configured
queues. Each worker gets only its own queue with `read`/`work`/`subscribe`.
The controller and supervisor never directly access upstream storage during
normal runtime; explicit user-run provisioning is the separate exception.

**Internal tokens have a 30-day lifetime.** The private manifest records the
earliest expected expiry. Setup reruns do not renew them. Rotation/migration is not implemented
as a silent setup or recovery side effect; continued operation beyond expiry
requires an explicitly approved credential-renewal workflow.

Setup publishes fresh state atomically from a private stage. It does not make
OpenAI API calls, create tunnels, configure ChatGPT accounts, subscribe to
Events or start services. A successful setup does not verify the external key's
permissions. Crashes can leave a private stage; see
[private config handling](private-key-config.md) before retrying.

On a stopped existing installation, entries are matched by tunnel ID against
the private manifest. Display-name edits and list reordering preserve UUIDs,
queues, tenant, ports, keys and queued work. Adding/removing dots, replacing a
tunnel, changing a role or replacing a key is refused pending an explicit
migration/rotation workflow. Missing state is not silently reprovisioned.
Never discard existing state to work around those checks.

## Runtime layout

The generated v2 runtime uses a fixed set of executable types, with no shell
evaluation or arbitrary command runner:

1. One pinned dot2api service on loopback
2. One queue-bound loopback MCP ingress per configured dot
3. One MultiDot controller with its own SQLite database
4. One official tunnel client per configured dot, each with loopback health

Ports are chosen during setup and retained in the manifest. An entry's stable
ID identifies its ingress, tunnel process, queue and secret directory. Display
renames do not change these bindings.

```text
multidot-state/
  manifest.private.json
  config/runtime.json
  config/controller.json
  config/tunnel-worker-<stable-uuid>.yaml
  controller/hub.sqlite
  upstream/dot2api.sqlite3
  upstream/encryption.key
  secrets/producer-token
  secrets/worker-<stable-uuid>/runtime-api-key
  secrets/worker-<stable-uuid>/worker-authorization
  run/status.json
  run/supervisor.lock
  logs/lifecycle.jsonl
```

Paths represent generated files, not an invitation to open or share real
credentials. State is private plaintext, not encrypted storage. A chosen path
is not a promise that the platform preserves it after host replacement.
Back up each SQLite DB with its own SQLite-consistent mechanism; preserve the
upstream encryption key and identity manifest through an approved secure
mechanism. Never copy only an active database while ignoring its WAL.

## Authorization and per-dot ingress

See [private config](private-key-config.md) for user-entered keys. Each runtime
key/principal needs **Tunnels Read + Use** for its selected tunnel and applicable
workspace/organization. One key may serve several tunnels only if it is
authorized for every one of them. An admin key is not required. The official
[permissions guide](https://github.com/openai/tunnel-client/blob/v0.0.16/docs/permissions.md)
and [configuration contract](https://github.com/openai/tunnel-client/blob/v0.0.16/docs/configuration.md)
describe access and `file:` references for `control_plane.api_key` and
`mcp.extra_headers.Authorization`. No bearer appears in a URL or argument.

Forwarded connector headers can override the tunnel client's static
Authorization. Therefore each local ingress requires exactly its own worker
header, rejects another worker's valid token, and forwards a fresh fixed
header. It forwards only POST `/mcp`, without caller-supplied headers, arbitrary
paths, URLs or redirects. The producer token is not exposed through ingress.

Before ingress/tunnel launch, read-only upstream HTTP checks require the exact
worker tool set, read access to the selected queue and rejection of other
configured queues. These HTTP checks alone cannot identify the principal or
prove absence of every possible extra grant. The user-run setup's verified
local issuance binds tokens to the exact tenant/principal/queue scopes, retained
in the installation state; manual legacy provisioning needs its own receipt.

The trust boundary is one service owner on the local host. File permissions and
loopback listeners do not isolate processes running as the same OS user.
Sharing use of a tunnel gives those users access to that tunnel's worker
binding; it does not give access only because a display label matches.

The relay intentionally supports this pinned upstream's POST-only, stateless
JSON/202 MCP implementation. No GET/SSE transport or session-header behavior is
claimed. Its Events model uses POST RPC subscriptions plus outbound webhooks.
Events and actual account execution remain separately unverified.

## Run, stop and observe

After separate approval for service execution and the selected tunnel
connections, the user runs:

```sh
python3 scripts/multidot.py run
```

Keep the foreground session open. In this cloud executor, use one managed
foreground session with a PTY and retain its session handle. Stop with Ctrl-C
in that owning session. The wrapper does not read the private input JSON on
`run`, but startup must read the installed secrets locally. Agents must not run
it on real credentials without a separately supported secure flow.

On a normal host, another shell in the **same PID/network namespace** can use:

```sh
python3 scripts/multidot.py status
python3 scripts/multidot.py stop
```

Supply the same `--state-root` if customized. From a different executor
namespace, status returns the stored report and marks live process/health
observations unknown. A stale status file is only evidence of an earlier
observation. Cross-namespace stop refuses signaling; use the owning session.

For advanced diagnostics, the generated runtime config can be passed to
`runtime_supervisor.py` with `status`, `reconcile` or `check`. `reconcile` is
read-only. `check` reads installed secrets for local preflight; it does not prove
authenticated OpenAI connectivity.

## Failure and recovery behavior

- An owned child exit triggers exponential restart delay capped at 30 seconds.
  Eight consecutive failures without a 60-second stable run block that
  component for operator review. Explicit supervisor relaunch resets only this
  process-level restart budget.
- Readiness requires an owned namespace/start-time-verified PID, its expected
  loopback listener and a safe health response. Occupied ports are refused;
  ports selected at setup are not reserved until launch.
- An unready upstream stops dependent tunnels; an unready ingress stops its
  corresponding tunnel. They return only after owned dependencies are ready.
- Controller startup repairs interrupted SENDING intents using the original
  body and key. It does not reopen REVIEW, observation/cancellation halts,
  exhausted send budgets or paused workers. `recover --apply` stays an explicit
  operator decision; auth/limit failures do not silently resume on restart.
- Graceful shutdown stops tunnels, controller, ingresses and upstream in that
  order, escalating only verified owned children that fail to stop. Bounded
  lifecycle logs contain process metadata, not child stdout, bodies, headers,
  environment values or secrets.
- Supervisor SIGKILL triggers kernel SIGKILL for its direct children. SQLite/WAL
  and exact-request recovery handle abrupt state interruption. The supervisor
  does not restart itself; an authorized operator must review and relaunch it.
- Ending the execution session or replacing the cloud host can stop everything.
  No boot-time service manager or verified platform recreation mechanism is
  configured. Process restart supervision is not an always-on hosting promise.

Controller offline `status` and `recover --dry-run` may read the shared
controller DB without a producer token. Other offline changes can affect a live
dispatcher. Do not run extra `start`, `tick` or `recover --apply` concurrently
with the supervised controller. Network operations require the same service
namespace and the approved, verified producer binding. No arbitrary shell or
network-command bridge is provided.

For advanced operators, the default generated registry and controller state
can be inspected without a credential or network request:

```sh
PYTHONPATH=src python3 -m multidot --config ../MultiDot-state/config/controller.json --db ../MultiDot-state/controller/hub.sqlite workers
PYTHONPATH=src python3 -m multidot --config ../MultiDot-state/config/controller.json --db ../MultiDot-state/controller/hub.sqlite status
```

Use the returned stable IDs in bounded job files; display names are labels.
The existing CLI uses `snapshot --file /ABS/PATH/snapshot.json` followed by
`submit --file /ABS/PATH/job.json` with the same `--config` and `--db` options.
Those commands write only the controller DB, but a running dispatcher can then
send the submitted job. There is no separate `plan` or `approve` command.
Task submission therefore needs the operator's intended execution authorization.
Each job has at most eight analysis steps, independently of the number of
registered dots. Actual dispatch and observation run in the supervised session.

`event_delivery_enabled` defaults to false and is forced false in local tests.
It controls dot2api's maintenance/webhook loop, not a task-executing agent.
Real `task.available` Events require separately authorized and verified
subscriptions/callbacks. Persistent processes alone do not make dots claim or
execute tasks, or prove delivery to the user.

## Execution-context evidence

Earlier environment probes found shared files but separate command-executor
and cloud-desktop loopbacks. Separate executor invocations also have different
PID/network namespaces. A background process launched by a completed exec call
was removed. Detached `start` is not a deployment route on this executor.

The earlier **v1/B-only fixture** kept all four components alive in one managed
foreground exec session across other tool calls, with same-session heartbeats.
That proves only the bounded observation in
[runtime evidence](../evidence/runtime/acceptance.json), not indefinite uptime or
live generic multi-dot account connectivity.

A secret-free executor request reached `api.openai.com/v1/tunnels` and returned
401; it established routing, not authentication. The cloud desktop returned
network-unreachable for the same destination. No network restriction was
changed. A fake-only secure-entry submit attempt was blocked; no real secret
was submitted. A supported private-entry path remains an outstanding gate.

## Repeatable validation

The standard-library tests use synthetic fixtures:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 scripts/check.py
```

For the exact scope and counts of recorded checkpoints, see
[LOCAL_CONTRACT](local-contract-report.md),
[runtime evidence](../evidence/runtime/acceptance.json) and
[acceptance status](acceptance-report.md). Do not infer a generic-runtime,
real-account or Events pass from the older B/C/A fixtures. A fake control plane
does not verify the entire official control-plane protocol.

## Advanced legacy v1 compatibility

**For existing v1/B-only installations and historical fixtures only.** The
original runtime schema retains fixed B ingress/tunnel components and the
legacy B/C/A controller registry. It is not the recommended new setup and is
not silently migrated by `multidot.py setup`.

The old example uses placeholders; its all-zero tunnel ID must not be used for
a real connection. Under separate provisioning/execution authorization:

```sh
cp config/runtime.example.json config/runtime.local.json
# Privately set verified non-secret paths and the existing tunnel ID.
python3 scripts/runtime_supervisor.py --config config/runtime.local.json render
# Use the legacy private-key guide for separately provisioned credentials.
python3 scripts/runtime_supervisor.py --config config/runtime.local.json check
python3 scripts/runtime_supervisor.py --config config/runtime.local.json run
```

Legacy `render` creates configuration/directories only: no upstream storage,
identities, keys or tunnel connection. Legacy secrets remain
`secrets/runtime-api-key`, `secrets/worker-b-authorization` and
`secrets/producer-token`; worker authorization must be independently verified
as B-only. See [legacy credential compatibility](private-key-config.md#advanced-legacy-v1-compatibility).

The prior local runtime harness also remains explicitly legacy:

```sh
PYTHONPATH=src /ABS/PATH/dot2api-venv/bin/python scripts/run_local_contract.py --upstream /ABS/PATH/dot2api-pinned --allow-temporary-fixtures --output var/local-contract-restored.json
PYTHONPATH=src /ABS/PATH/dot2api-venv/bin/python scripts/run_runtime_checks.py --tunnel-binary /ABS/PATH/tunnel-client-v0.0.16/tunnel-client --output var/runtime-acceptance.json --hold-seconds 20 --session-receipt var/runtime-session-probe.json
```

These opt-in commands execute real pinned software with disposable local
fixtures and need the corresponding execution approval. The runtime harness
uses a fake control plane, checks legacy B header boundaries, process death,
relaunch and cleanup. It does not use a real ChatGPT account or prove Events.
