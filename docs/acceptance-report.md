# Acceptance report: local implementation slice

Target: MultiDot 0.1.0, source snapshot identified by the evidence hashes below.
Upstream pin: `66a761505ff73c5dca730260efcaeb5697db81ad`.
Evidence date: 2026-10-09 UTC. Exact per-test times, source SHA-256 values,
versions and outcomes are in [mock-test-report.json](../evidence/mock-test-report.json).

**62 MOCK tests pass**, including 13 independently developed regression tests.
The [independent review receipt](../evidence/independent-review.json) reran all
62 tests, verified source hashes, and found no remaining blocker in this local slice.
[The mock demo](../evidence/mock-demo.json) completes B/C/A with three accepted
steps, three stored artifacts, and notification_pending after a DB reopen.
Compilation succeeds. Those MOCK runs did not execute upstream or access real
accounts. Historical MOCK receipts retain their original classifications.

**21 separate LOCAL_CONTRACT tests now pass** against actual pinned dot2api and
the unmodified adapter, after explicitly approved isolated installation. The
[contract report](local-contract-report.md) records exact scope, lock, versions,
source hashes, synthetic identities and verified cleanup. Its response-fault
cases are injected after actual service execution; lease boundaries use an
injected clock. Neither proves real-worker behavior or elapsed uptime.

REAL_DOT requires actual separate accounts and identity evidence; **none ran or
passed**. No Events subscription, real-account credential, persistent access or
public deployment was created. Source review and environment observations have
their own reports. This is not a declaration of full P0 readiness.

## Requested 24 scenarios

| ID | Scenario | MOCK | LOCAL_CONTRACT | REAL_DOT | Evidence / qualification |
|---|---|---|---|---|---|
| T01 | Host re-entry | NOT_TESTED | NOT_TESTED | NOT_TESTED | Separate LOCAL_ENVIRONMENT file/SQLite PASS over 405.289434 s; no uptime claim |
| T02 | B connection | PASS | PASS | NEEDS_USER_ACTION | Synthetic B API/controller round trip; no Dot account; public task omits authenticated principal |
| T03 | C connection | PASS | NOT_TESTED | NEEDS_USER_ACTION | Mock C result in two-worker/synthesis flow |
| T04 | Queue isolation | PASS | PASS | NEEDS_USER_ACTION | Native tenant/creator/queue grants; B→C/A read/claim and C→B read/claim denied |
| T05 | Unauthorized submit | PASS | PASS | NEEDS_USER_ACTION | Native worker submit and producer claim rejected |
| T06 | Duplicate request | PASS | PASS | NEEDS_USER_ACTION | Same native request reuses one task; duplicate controller job remains covered by MOCK |
| T07 | Same key, changed body | PASS | PASS | NEEDS_USER_ACTION | Native same key changed payload/queue/TTL returns 409 |
| T08 | Lost submission response | PASS | PASS | NEEDS_USER_ACTION | Injected 503 after actual native commit; exact stored request retry yields one task and retains slot |
| T09 | Duplicate event | PASS | NOT_TESTED | NEEDS_USER_ACTION | Repeated observations/ticks dedupe; actual Events not implemented |
| T10 | Result validation | PASS | PARTIAL | NEEDS_USER_ACTION | Native valid fixture result accepted; invalid result variants remain MOCK coverage |
| T11 | Lease expiry | PASS | PASS | NEEDS_USER_ACTION | Native injected-clock boundary fails max_attempts=1; stale completion rejected; heartbeat same revision |
| T12 | Controller restart | PASS | PASS | NEEDS_USER_ACTION | Controller DB reopen accepts actual native task/result; no host restart claim |
| T13 | Result after disconnect | PASS | PASS | NEEDS_USER_ACTION | Injected uncertain GET retains slot; actual native read reconciles after fault clears |
| T14 | Subscription reconnect | NOT_TESTED | NOT_TESTED | NEEDS_USER_ACTION | Events/first-page rescan documented, not implemented/tested |
| T15 | A synthesis | PASS | NOT_TESTED | NEEDS_USER_ACTION | One A task keyed by accepted dependency result hashes |
| T16 | User delivery | PASS | NOT_TESTED | NEEDS_USER_ACTION | State separation only; manual reference recording is not actual delivery |
| T17 | Cancel | PASS | PASS | NEEDS_USER_ACTION | Native leased-task cancel is idempotent and fences completion; no worker-stop claim |
| T18 | Partial failure | PASS | NOT_TESTED | NEEDS_USER_ACTION | Accepted sibling artifacts preserved, no overall success |
| T19 | Authentication revoke | NOT_TESTED | NOT_TESTED | NEEDS_USER_ACTION | Real key revocation and existing subscriptions both untested |
| T20 | Recursion prevention | PASS | NOT_TESTED | NEEDS_USER_ACTION | A excluded from regular steps; unique synthesis without delegation |
| T21 | Secrets/artifact paths | PASS | NOT_TESTED | NEEDS_USER_ACTION | Known patterns/config token checks, safe names and integrity hashes |
| T22 | Approval/limit failure | PASS | NOT_TESTED | NEEDS_USER_ACTION | Pause/halt, no account failover or substitute attempt |
| T23 | Size bound | PASS | PARTIAL | NEEDS_USER_ACTION | Actual native ASCII/Korean request boundaries and empty 413; oversize response is fault-injected |
| T24 | Concurrent assignment | PASS | NOT_TESTED | NEEDS_USER_ACTION | Six concurrent controllers / 12 queued jobs keep one reservation |

PASS under MOCK or LOCAL_CONTRACT applies only to the stated behavior. A
LOCAL_CONTRACT row is scoped to its qualification and does not mean the entire
real-account scenario passed. PARTIAL identifies narrower native coverage.
No modeled or synthetic-identity test substitutes for REAL_DOT evidence.

## Review findings resolved

- Terminal-state checks repeated under the write lock to fence observation races
- Delayed GET/cancel/POST failures cannot downgrade newer authoritative completion
- Unexpected upstream cancellation becomes review, not CREATED
- Cancellation after completed work preserves successful state and delivery
- Offline local cancellation works without producer credentials
- Recovery flags mutually exclusive; resume does not rerun failed external work
- Read/cancel backoff bounded; explicit auth/limit rejection halts rather than loops
- HTTP error resources closed; untrusted bodies and credentials not logged

## Remaining gates and deliberate deferrals

1. LOCAL_CONTRACT is complete for the 21 executed cases. Native Events,
   subscriptions/revocation and remaining scenario coverage are still untested.
2. Supported OAuth/bearer identity route, secure secrets and per-account setup.
   Verify the producer token's actual tenant/creator; a configured label alone
   is not sufficient to preserve upstream's creator-scoped retry idempotency.
3. Real B first, then C, then A synthesis Events and observed user delivery.
4. Controlled lifecycle/idle tests in the chosen cloud runtime. Current CLI idle
   shutdown is not a long-running-host or multi-request shutdown guarantee.
5. Controller MCP, subscription lifecycle, large files, verified automatic user
   notification and operator resolution of ambiguous slots remain deferred.

At this evidence checkpoint no source commit or remote push had occurred;
publication has been separately approved pending final review. Runtime state is
ignored by Git, and all LOCAL_CONTRACT fixture storage/listeners were removed.
