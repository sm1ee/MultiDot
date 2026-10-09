# C setup, after B is verified

Suggested name: MultiDot C. Queue: dot-c. Endpoint/tunnel: NOT_CREATED.
Authenticated account and service identity binding: UNVERIFIED.

Use C's own approved connection and queue-limited read/work/subscribe identity.
Do not copy B's token or use shared administrator authentication. Follow
[user-actions.md](user-actions.md), then [C instructions](../prompts/dot-c-worker.md).
Verify cross-queue rejection and independently retrieve C's exact result.
No automatic failover from a blocked B task is permitted.
