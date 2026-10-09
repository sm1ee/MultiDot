# Phase 0: observed environment, not always-on deployment

Date: 2026-10-09 UTC. The non-destructive environment checks are summarized in
[sanitized evidence](../evidence/environment/summary.json). Raw host/network
identifiers and personal-computer inventory are omitted from this repository.
Source receipt hashes are retained to bind the observations.

## Verified

- Executor Python 3.12.14 / SQLite 3.53.1; separate dot cloud native desktop
  Python 3.13.5 / SQLite 3.46.1. This code suite was run on Python 3.12.14 only.
- A benign file and canary SQLite DB remained byte-identical across repeated
  access over **405.289434 seconds**, from 02:39:58.258945 to 02:46:43.548379 UTC.
- The native cloud terminal saw the same selected shared/project file bytes and
  passed SQLite integrity. This is not cross-account filesystem access.
- One own native-cloud stdlib HTTP probe returned an initial local HTTP 200.
- Executor and native cloud use different loopback/network contexts. Separate
  executor calls could not reach the probes. The cloud browser blocked localhost.
  No bypass was attempted; a successful in-process HTTP test proves only itself.
- Project-only CLI and mock tests run locally without third-party installation.
  A later CLI test verifies duplicate-start rejection and graceful idle SIGTERM
  stop for the controller's own foreground process, not loaded native upstream.

## Failures and cleanup

The diagnostic native-cloud HTTP probe did not stop gracefully. Its single-
threaded HTTPServer could stall on a partial local connection; the exact cause
was not proven. Only the verified owned job was forcibly stopped and cleanup
confirmed. It was not reused as production service code. Both diagnostic probes
were stopped; no autostart or daemon was installed.

Cross-tool localhost reachability failed. Consequently, README does not present
an executor localhost URL as an existing cloud deployment. Controller and
upstream must run together in a specifically tested runtime/network context.

## Status

| Check | Status | Actual scope |
|---|---|---|
| Specific shared files and SQLite re-entry | PASS | 405.289434-second observed window |
| Native cloud initial local process/health | PASS | One own self-health response |
| Diagnostic native cloud graceful stop | FAIL | Owned job forcibly cleaned up |
| Cross-tool localhost | FAIL | Connection refused / browser policy block |
| Local controller DB reopen | PASS | MOCK data persisted and reopened |
| Local controller idle start/stop | PASS | Own-process lock/SIGTERM regression |
| Same-process executor controller + upstream | PASS | 21 LOCAL_CONTRACT tests; ephemeral loopback and synthetic identities |
| Native cloud desktop controller + upstream service | NOT_TESTED | No upstream run in that separate network context |
| User absence / task-end / multi-hour idle | NOT_TESTED | No controlled observation |
| Host restart and long-term DB durability | NOT_TESTED | No host restart attempted |
| Authenticated MCP ingress | NEEDS_USER_ACTION | No route/credential created |
| Actual B/C work and A Events return | NEEDS_USER_ACTION | No accounts/subscriptions connected |

No installed tunnel client was found by the limited PATH checks. No public
listener, tunnel, real credential lookup, external service, paid host, or personal
computer operation was performed. Later LOCAL_CONTRACT tests used only temporary
synthetic credentials, removed with their disposable service state. Unauthenticated public docs/repository access
succeeded; an API root HEAD response did not verify authenticated tunnel use.

## Next supported route

The approved pinned upstream was subsequently executed beside the controller in
one executor process/network context: [21 LOCAL_CONTRACT tests pass](local-contract-report.md).
All owned listeners and fixture state were cleaned up. This does not change the
cross-tool loopback or continuous-hosting findings above.
Resolve authenticated per-account MCP routing before live B setup; static
upstream bearer auth cannot be assumed compatible with the documented OAuth
custom-server setup. Test one real B task, then C, then A Events and actual user
delivery. See [user actions](user-actions.md).

The host cannot wake its own stopped service. Files surviving a few minutes do
not prove continuous daemon hosting or an automatic return to A. No platform
idle-limit bypass, fake activity, or keep-alive workload was added.
