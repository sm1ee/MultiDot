# Operations

Use the source commands in README. No package install is required; Python
3.11+ standard library is the runtime. `pyproject.toml` pins the optional
setuptools build tool; it is not downloaded by the source workflow. There are
no runtime third-party dependencies to lock. Packaging itself is untested.

`start` is foreground, single-process per DB via a POSIX file lock. SIGINT or
SIGTERM requests a graceful stop after the current tick and sleep. A tick may
contain several bounded HTTP calls, so shutdown can take longer than one request
timeout plus the interval. The idle shutdown test does not establish a hung
multi-request bound. No kill command is
issued to other processes. A second start for the same DB fails. The lock file
is bookkeeping; OS lock release, not stale PID text, controls admission.

`tick` is one sync/schedule/dispatch pass. Idle service ticks issue no upstream
calls when there are no active/uncertain tasks, and create no fake keep-alive
jobs. Active upstream observations use backoff on errors, halt after five
transport failures, and halt immediately on explicit rejected HTTP responses.
Submission ambiguity retries the exact stored body/key at most five sends;
auth/permission/limit errors pause the assigned worker without account failover.

`pause WORKER` blocks new reservations. It does not stop existing work.
`resume WORKER` allows future reservation, but does not silently retry an
ambiguous attempt or clear existing approval needs. `recover --dry-run` shows
the safe recovery plan; `--apply` reopens known-task observation and changes
crashed SENDING intents to exact-body retry candidates. See recovery.md.

`request-cancel JOB_ID --reason TEXT` always records local cancellation, even
without a producer credential. If a configured token is available, it also
attempts upstream cancellation. New steps and synthesis are blocked as soon
as the request commits. `CANCELLED` means local/no-sent work or authoritative
native cancellation/terminal observation; it is never proof that the worker's
computation or external effects have stopped. `worker_stop_confirmed` stays false.

`status`, `get`, and `workers` return last-observed data and timestamps. For a
fresh authorized observation run `tick` first. `artifact ARTIFACT_ID` verifies
stored length/hash. `export JOB_ID --output FILE` writes a private JSON file,
never overwrites an existing file, and never runs artifact text. Keep exports
private until separately authorized for sharing.

`mark-presented JOB_ID --reference ACTUAL_MESSAGE_REFERENCE` records an explicit
operator observation. Do not run it for a local mock or solely because synthesis
completed. It does not send a message or independently verify the reference.

State and backups contain plaintext user material, not just metadata. Keep
the state directory private and on one tested local filesystem. Do not share
live SQLite across hosts or copy only a WAL database file as a backup. Stop
dispatch and use SQLite's online backup API into a new private file; preserve a
manifest and verify integrity before restoring. No automatic deletion policy or
backup scheduler is implemented. Monitor available storage manually; write
failures are errors, not successful dispatch. No data is deleted by recovery.

The service cannot wake itself after its host stops. Long idle, daemon lifetime,
and automatic A return remain unverified. Reopen A and recheck state when needed;
no keep-alive workaround or external service is created.
