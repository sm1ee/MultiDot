# Portable runtime, preparation only

The restored base is `cd1a2e034decf4b871ec8a42a586c3b4ad2f195a`.
This change prepares a supervised stack and tests it with disposable identities.
**It is not a live B connection, an Events acceptance, or an always-on hosting
guarantee.** No real credentials were entered, no real tunnel was attached, and
no B/C/A account job was submitted. Source publication is a separate action.

## Runtime layout

The controller remains standard-library-only. The portable supervisor uses
Python's standard library and Linux `/proc`, `flock`, pidfd and parent-death
signals. It accepts only four fixed component roles, with no shell evaluation
or arbitrary command runner:

1. The previously reviewed `dot2api` source, pinned to
   `66a761505ff73c5dca730260efcaeb5697db81ad`, in its isolated frozen environment.
2. A loopback B-only MCP ingress on port 8789, forwarding only POST `/mcp` to
   dot2api's loopback port 8788.
3. The MultiDot controller, with its separate SQLite database.
4. Official `openai/tunnel-client` v0.0.16, with loopback health on port 8790.

The official Linux amd64 release archive was downloaded from
[the versioned official release](https://github.com/openai/tunnel-client/releases/tag/v0.0.16)
and matched the release's SHA256SUMS. The executable reports
`0.0.16+5f99daabd4aa4a77049e6d81d54a0d8c18335397`.
Preflight rechecks its executable SHA256 and the reviewed upstream commit,
tree, clean checkout, lockfile, import location and installed package versions.
No release installer, new upstream setup script, global installation, systemd
unit or operating-system startup setting was executed or installed.

State uses an explicit private directory, separate from the source checkout:

```text
/ABS/PATH/multidot-state/
  config/controller.json
  config/tunnel-b.yaml
  controller/hub.sqlite
  upstream/dot2api.sqlite3
  upstream/encryption.key
  secrets/runtime-api-key
  secrets/worker-b-authorization
  secrets/producer-token
  run/status.json
  run/supervisor.lock
  logs/lifecycle.jsonl
```

This is a chosen state path, not a promise that the platform preserves it across
replacement. Existing controller and upstream SQLite databases must be backed
up using their own SQLite-consistent backup mechanisms; preserve the upstream
encryption key separately through an approved secure mechanism. Never copy only
an active database file while ignoring its WAL.

## Authorization and B-only ingress

For user-entered values, see [private key config](private-key-config.md): create
the blank private JSON, edit it yourself, then explicitly install its values
into the existing `file:` secret files. The helper does not connect or run the
stack. Safe user editing access to this dot's cloud remains separately unverified.

`render` creates only non-secret configuration and private directories. It does
not initialize upstream storage, create identities, issue keys, authenticate a
real account, or connect a tunnel. Real provisioning and secret placement are
separate approval-gated actions. `check` and `run` fail without the required
already-provisioned files.

- `runtime-api-key`: raw runtime key plus an optional trailing newline. The
  runtime principal needs Tunnels Read + Use for the selected tunnel, without
  Manage or an admin key.
- `worker-b-authorization`: the complete `Bearer …` header for a principal with
  only queue `dot-b` and scopes `read`, `work`, optionally `subscribe`.
- `producer-token`: raw producer token, never exposed through B ingress. The
  producer's authenticated tenant and principal must be established during
  approved provisioning and retained across recovery.

The official client's
[v0.0.16 configuration contract](https://github.com/openai/tunnel-client/blob/v0.0.16/docs/configuration.md)
supports `file:` references for both `control_plane.api_key` and
`mcp.extra_headers.Authorization`. No bearer appears in a URL or an argument.

Forwarded connector headers can override the client's static Authorization.
Therefore the local B ingress additionally requires exactly the B file's header;
another valid upstream token is rejected. It forwards a fresh, fixed B header
and never forwards caller-supplied headers, arbitrary paths, redirects or URLs.
Before launching ingress, read-only upstream HTTP checks require the exact
worker-tool set, B read access, and rejection of A/C queues. Those checks do not
identify the actual principal or prove absence of every other queue; the
approved provisioning receipt must establish the exact B-only grant. Runtime
startup does not open or mutate the upstream database directly.

The trust boundary remains one user/service owner on the local host. Filesystem
ownership and loopback ports do not create isolation from that same OS user.
Sharing tunnel use with other users gives those users access to the B binding.

The relay intentionally matches this pinned upstream's POST-only, stateless
JSON/202 MCP implementation. The runtime tests exercise actual upstream
`initialize`, `tools/list`, and `tools/call` through the relay. No GET/SSE MCP
transport or session-header behavior is claimed; this pinned upstream does not
provide that transport. Its Events model uses POST RPC subscriptions plus
outbound webhook delivery, which remains separately unverified and disabled in
these tests.

## Failure and recovery behavior

- A child exit causes an owned-process restart, with exponential delay capped
  at 30 seconds. Eight consecutive failures before a 60-second stable run block
  that component for operator review; an explicit supervisor relaunch resets
  this process-level budget.
- Only owned, namespace/start-time-verified processes are considered alive.
  Readiness requires that the owned PID actually holds the expected loopback
  listener, plus a safe health response. Occupied ports are refused at startup.
- A dead/unready upstream or B ingress causes tunnel termination. It can return
  only after the owned dependencies become ready again.
- Controller startup repairs interrupted SENDING intents using their original
  body and key. It does not reopen REVIEW, observation/cancellation halts,
  exhausted send budgets or paused workers. Explicit `recover --apply` remains
  an operator decision. This fixes the previous startup behavior that could
  reopen a halted 401/403/429 observation after a process restart.
- Graceful shutdown stops tunnel, controller, ingress and upstream in that
  order. It escalates only owned children that fail to stop. Lifecycle logs are
  bounded and contain process metadata, not child stdout, request bodies,
  headers, environment values or secrets.
- Supervisor SIGKILL causes kernel SIGKILL of each direct child. SQLite/WAL and
  exact-request recovery handle the abrupt stop. The supervisor itself does
  **not** automatically restart. Reconciliation is read-only; a separate
  authorized operator must relaunch it after checking the stored state.
- A whole execution session, machine or cloud-runtime replacement can stop
  everything. There is no configured boot-time service manager or verified
  platform mechanism to recreate this stack automatically.

## Execution-context facts

This dot's command executor and native cloud desktop share these files but have
different loopbacks. Separate command-executor invocations also have separate
PID/network namespaces. A background child launched by a completed exec call
was removed; detached `start` is not a deployment route on this executor.

A **managed foreground exec session** was verified to keep all four components
alive together across other tool calls, with same-session readiness heartbeats.
This proves a bounded session observation, not indefinite service continuity.
Start the whole stack together inside one managed foreground session, request a
PTY, retain its session handle, and use Ctrl-C in that owning session for normal
shutdown. The foreground `run` command is the intended route here. `start` is
only suitable for a normal host whose process lifecycle independently permits
detached services.

From another executor namespace, runtime `status`/`reconcile` return the stored
report and label process/health observations unknown. They do not claim that an
unobservable process stopped. Cross-namespace `stop` refuses PID signaling;
use the owning managed session. A lifecycle status file includes freshness and
last reported health, which is evidence of the previous observation only.

Secret-free HTTPS from the executor reached `api.openai.com/v1/tunnels` and
returned 401, showing routing without authenticating. The native cloud desktop
returned network-unreachable for that same destination. No network setting or
access restriction was changed. The attempted native secure-entry page was
blocked on its fake-only submit request; no real secret was submitted. Live
provisioning is therefore still blocked pending a supported secure entry path.

## Operator commands

All `/ABS/PATH/…` paths below are editable absolute placeholders. Run from your
restored repository. Copy the example to ignored private local
configuration and substitute the selected existing tunnel ID and verified paths.
The checked-in example deliberately has an all-zero tunnel placeholder.

```sh
cp config/runtime.example.json config/runtime.local.json
python3 scripts/runtime_supervisor.py --config config/runtime.local.json render
python3 scripts/runtime_supervisor.py --config config/runtime.local.json check
```

After separately approved provisioning, launch with **one managed foreground
exec session and a PTY**, not a shell background command:

```sh
python3 scripts/runtime_supervisor.py --config config/runtime.local.json run
```

Keep that session open. Graceful stop is Ctrl-C to its session handle. On a
normal host, a second shell in the **same PID/network namespace** can use:

```sh
python3 scripts/runtime_supervisor.py --config config/runtime.local.json status
python3 scripts/runtime_supervisor.py --config config/runtime.local.json reconcile
python3 scripts/runtime_supervisor.py --config config/runtime.local.json stop
```

Controller **offline** commands can operate on the shared controller database
from a separate exec namespace. Do not set a producer token for offline use:

```sh
export PYTHONPATH=src
python3 -m multidot --db /ABS/PATH/multidot-state/controller/hub.sqlite --config /ABS/PATH/multidot-state/config/controller.json status
python3 -m multidot --db /ABS/PATH/multidot-state/controller/hub.sqlite --config /ABS/PATH/multidot-state/config/controller.json recover --dry-run
```

`bootstrap`, `snapshot`, `submit`, `pause`, and `resume` are local state commands,
but changes can affect a running dispatcher; use them only for authorized work.
`request-cancel` is offline only when the configured producer-token environment
variable is absent. `start`, `tick`, network cancellation, and
`recover --apply` require the **same service network namespace**, the approved
producer credential and verified identity binding. Do not run additional
`start`, `tick` or `recover --apply` against a supervised controller; stop it
first. No network command bridge or arbitrary remote shell is provided.

`event_delivery_enabled` defaults to false, and is forced false for local
tests. This controls dot2api's maintenance/webhook delivery loop, not a task
executing agent. Real `task.available` Events need separately verified and
authorized subscription/callback setup and this flag enabled. Persistent
processes alone do not establish Events delivery or B account execution.

## Repeatable validation

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src /ABS/PATH/dot2api-venv/bin/python scripts/run_local_contract.py --upstream /ABS/PATH/dot2api-pinned --allow-temporary-fixtures --output var/local-contract-restored.json
PYTHONPATH=src /ABS/PATH/dot2api-venv/bin/python scripts/run_runtime_checks.py --tunnel-binary /ABS/PATH/tunnel-client-v0.0.16/tunnel-client --output var/runtime-acceptance.json --hold-seconds 20 --session-receipt var/runtime-session-probe.json
```

The last command executes the pinned real services against a local fake control
plane and disposable fixture tokens. It tests child failures, supervisor death,
manual reconciliation/relaunch, B header boundaries, preserved controller state,
managed-session heartbeats and cleanup. It does not simulate a ChatGPT account,
validate the complete official control-plane protocol, or prove remote Events.
