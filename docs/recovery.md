# Recovery without duplicate work

1. Stop new dispatch with worker pause or stop the owned foreground controller.
2. Preserve the controller DB, including WAL via a proper SQLite backup.
3. Run `recover --dry-run` before `recover --apply`. The flags are mutually exclusive.
4. Verify producer identity and upstream origin are unchanged before applying.
   A configured producer label is not proof of the token's actual tenant/creator.
   Replacing a token must preserve that authenticated identity, verified through
   trusted setup evidence, or upstream idempotency may not protect the retry.
5. Use recorded task IDs for authoritative status reads. Never pick a new key,
   another Dot, or a new attempt just because a response is late.

## Cases

- **Intent persisted; no POST yet:** cancel locally without any network request.
- **POST response lost:** original producer/target/body/key are retained. Bounded
  resend uses upstream idempotency to recover the original task. Reservation
  remains occupied until authority is established.
- **Process stopped during SENDING:** dry-run reports exact-body retry. Apply
  returns it to RETRY; a subsequent tick uses that same intent.
- **Five uncertain submissions:** NEEDS_REVIEW; no further automatic POST. An
  operator must resolve upstream state before releasing/recreating work.
- **Cancel during unknown submission:** CANCEL_REQUESTED and reservation stay.
  The controller does not POST after cancellation merely to discover the task.
  Inspect upstream using authorized creator tooling and the recorded key/body;
  no blind reexecution or automatic reservation release exists in P0.
- **Known task temporarily unreadable:** backoff, then halt after five failures.
  After fixing connectivity, explicit recover --apply reopens observation.
- **401/403/429/404 or other explicit read rejection:** halt observations and
  pause worker. Resolve access/limit/state manually; no alternate-account route.
- **Lease lost/expired:** NEEDS_REVIEW, max_attempts=1. Previous result cannot
  overwrite a terminal state, and no replacement attempt is auto-generated.
- **One child fails:** keep accepted sibling results and report failed/review;
  do not synthesize a successful job.
- **Unexpected upstream cancellation:** NEEDS_REVIEW, not a fresh CREATED job.
- **Terminal observation changes:** retain the prior result and flag review.
- **Result invalid/too large:** preserve status and error code, reject result
  acceptance; no truncation, artifact execution, or automatic work rerun.
- **All results accepted:** a unique synthesis step references exact result
  hashes. Repeated polling does not create duplicate synthesis tasks.

P0 deliberately has no command that directly edits attempts, releases uncertain
slots, or retries failed external work. That recovery needs a separately
reviewed reconciliation procedure or a new explicitly authorized Job after the
old execution is resolved. `resume` only lifts the worker's pause flag.
