# Architecture and scope

## Implemented slice

`cli.py` calls `Controller`. `storage.py` owns local SQLite state. The controller
talks to `NativeTaskAdapter` through `submit`, `get`, and `cancel`, and never
claims or renews worker leases. `MockNative` is a deliberately separate testing
double. There is no controller REST/MCP server or Events implementation yet;
CLI is the initial management interface.

Projects, immutable snapshots, jobs, logical steps, attempts, dispatch outbox,
worker grants/reservations, artifacts, and redacted audit codes are persisted.
Worker reservations and full submission intent are committed in one
`BEGIN IMMEDIATE` transaction before sending. A unique reservation key enforces
one active assignment per worker even across concurrent controllers. A logical
step has exactly one attempt in P0.

Request idempotency is `(project_id, request_id)` plus canonical body hash.
Snapshot identity is `(project_id, snapshot_id)` plus complete content hash.
Changing either immutable body produces a conflict. Upstream idempotency keys
hash job, step, attempt 1, input hash, and synthesis version. They are never
changed automatically after uncertainty. Stored producer and target must also
match the active adapter before dispatch/reconciliation/cancellation.

## State and observation

Native status is stored separately from logical state. Jobs use CREATED, ACTIVE,
READY_TO_SYNTHESIZE, SYNTHESIZING, COMPLETED, NEEDS_REVIEW, FAILED,
CANCEL_REQUESTED, CANCELLED. Steps use WAITING, DISPATCHED, ACCEPTED,
NEEDS_REVIEW, FAILED, CANCELLED. Upstream failed/expired goes to NEEDS_REVIEW;
the controller cannot establish whether a lost worker already did work.

Known task IDs are re-read. Lower revisions cannot overwrite new observations.
Same-revision state changes and any changed terminal observation are fenced.
Checks repeat under the SQLite write lock, preventing a concurrent completion
from being overwritten after reservation release. Heartbeats may update lease
expiry without increasing revision; this is allowed. No controller heartbeat
pretends to represent worker progress.

The pinned public task schema omits worker/creator principal. `running` is a
real native status observation, **not authenticated B identity evidence**.
REAL_DOT acceptance requires a separate authorized evidence source. Result-body
claims such as "I am B" are not trusted as identity.

## Results and synthesis

Completed native transport is accepted only after checking schema, exact job,
step and snapshot ID, bounded payload, outcome, required artifact names, and
no external changes. Artifacts bind the stored snapshot hash and result hash.
Unknown fields, invalid paths, missing files, oversize or apparent credentials
are rejected, never silently truncated. Artifact contents are inert data.
Their bytes live in the controller database; worker-local paths are never read.

Accepted dependency results are included inline by exact hash. Once every
required regular step is accepted, a single reserved `synthesis` step is created.
Its version hashes every accepted result. It uses only dot-a, has no delegation
API, and never creates more regular work. New revisions require a new explicit
Job, with new snapshot/request IDs as needed.

`notification_pending` is set on completion. Only an explicit operator-supplied
message/acknowledgement reference may mark `presented`; the software does not
independently validate that reference. Real chat delivery remains unverified.

## Input and trust limits

Eight steps, eight artifacts per envelope, 16,000 UTF-8 bytes per artifact,
48,000 canonical UTF-8 bytes per snapshot/job/result, and a conservative 120,000
bytes under upstream's ASCII-escaped operation serialization. Korean input is
counted using the upstream representation too. Synthesis can exceed the bound
even when child results fit; it becomes NEEDS_REVIEW rather than truncating.

The CLI project allowlist protects accidental cross-project use. Anyone with
the same OS user's filesystem access can alter the DB/config; this is not a
security boundary against that user. The policy prompt cannot remove a real
Dot's app permissions; account-side authorization must enforce that boundary.
No full disk encryption or arbitrary-secret detector is claimed.

## Deliberately deferred

Controller MCP, real worker subscriptions, token/identity provisioning, OAuth
mapping, large files, automatic worker choice, arbitrary local development,
automatic retry attempts, UI, public exposure, service supervision, and verified
user notification delivery. These are not represented as completed P0 work.
