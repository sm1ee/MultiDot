# Generic dots setup validation

Checkpoint: 2026-10-09 UTC. This report covers the configurable dots-list
implementation. Earlier B/C/A reports remain historical compatibility evidence.
No real private config, account key, tunnel attachment or dot job was used.

## Implemented behavior

- The public template has blank `name`, `tunnel_id`, `runtime_api_key` fields.
  The list determines the initial installation's count; there is no fixed
  three/four/16-dot limit. Byte limits and OS resources still apply.
- Optional roles default to `worker`; at least one regular worker and at most
  one `synthesis` are supported. Names are Unicode display labels, never roles,
  queue names, file paths or executable arguments.
- Explicit user-run setup prepares fresh private state using the reviewed
  upstream APIs, with one installation tenant, distinct random worker/queue
  IDs, producer read/submit grants and exact per-worker read/work/subscribe
  grants. Each worker gets a separate ingress and tunnel process.
- Display-name changes and list reordering match existing entries by tunnel
  ID and preserve IDs, tenant, ports, tokens and jobs. Existing identity grants
  are never silently rebuilt or widened. Runtime/controller paths consume the
  same registry.
- Fresh state publication is non-overwriting and atomic after file/directory
  synchronization. A post-publication sync failure is reported as uncertain;
  the published state is not removed. Metadata edits validate the candidate
  before replacement and reject symlink directories.
- Keys stay in owner-only private files, referenced using `file:`. No bearer is
  placed in a URL. Parser/runtime errors omit values, and process logs omit
  child output and credentials. Private files, generated secret trees and the
  upstream encryption key are Git-ignored.

## Completed test groups

These groups overlap in coverage and must not be added together as independent
real-account scenarios.

- **162 MOCK/unit tests** passed, including 19 generic setup tests, 18 generic
  controller tests and 22 generic runtime tests. Fixtures cover initial
  1/2/4/10-profile setup, 25-entry input parsing, arbitrary names, stable rename
  and reorder, bounds, redaction, ignored secrets, scopes and generic routing.
  The [per-test receipt](../evidence/mock-test-report.json) identifies source
  hashes and exact results. MOCK does not execute upstream.
- **13 independent fake-only setup checks** passed; the reviewer also reran
  the then-current 16 setup tests and 30 non-network controller/runtime tests.
  [Review scope and source hashes](../evidence/generic/independent-setup-review.json)
  distinguish synthetic API-argument verification from actual issuance.
- **21 actual pinned-upstream LOCAL_CONTRACT tests** and **15 legacy local
  runtime checks** were rerun successfully after the generic changes. The
  runtime run included 20 seconds of foreground observation and four healthy
  observations. Owned listeners closed and fixture state was removed.
  [Regression receipt](../evidence/generic/legacy-regression.json).
- **60 generic LOCAL_RUNTIME_CONTRACT checks** passed: 17 checks each for
  one- and two-profile running stacks, 18 for four profiles including a custom
  synthesis role, and eight for ten-profile actual setup CLI/configuration and
  preflight. Ten profiles were configured, not launched. The actual supervised
  controller loop completed seven native fixture tasks across the running
  cases, with no harness-driven controller ticks. Native and MCP checks
  confirmed own-queue access, peer-queue rejection, exact ingress header
  matching and same-queue/different-tenant isolation. Every fixture process,
  listener and temporary tree was cleaned up.
  [Generic runtime receipt](../evidence/generic/runtime-contract.json).

The official tunnel clients contacted a loopback fake control plane with fake
keys. MCP task traffic was tested directly against local ingresses, not through
an authenticated external tunnel. These checks do not establish real connector
header behavior, remote account permissions or tunnel delivery.

## Deliberate limits

This is source and local integration work, not a live deployment. A supported
private editing route into dot's cloud is still unverified. There has been no
real key setup, authenticated OpenAI tunnel connection, separate-dot task
execution, Events delivery or user-result delivery test.

The runtime requires the already-installed pinned Linux toolchain. It has
child-process recovery but no supervisor self-restart, OS autostart, host
replacement restoration or indefinite session-lifetime guarantee. Native
upstream MCP is POST JSON/202; no GET/SSE transport is exposed by this ingress.

Adding/removing installed dots, replacing tunnel IDs, changing roles and
rotating keys need a separate migration/rotation workflow. Internal tokens last
30 days; setup reruns do not extend them. The number of registered dots is
separate from the existing eight-analysis-step limit per job. Actual dispatch
remains the existing bounded snapshot/submit flow, without a separate
plan/approve command.

Start with [the simple guide](simple-setup.md), then consult
[runtime operations](persistent-runtime.md) and
[private-key handling](private-key-config.md).
