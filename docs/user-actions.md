# User actions and live-connection gates

No B/C account is connected. No real-account endpoint/tunnel ID or persistent
credential was found or created. Disposable test-service credentials existed only
during LOCAL_CONTRACT fixtures and were cleaned up. Placeholder config values
are **not a connection recipe**.
The next goal is one small real B round trip; C and A Events follow only after B.

## Local upstream contract: approved and completed

The pinned dot2api commit was installed/executed in an isolated, project-only
environment after explicit approval. **21 LOCAL_CONTRACT tests pass** against
actual upstream with synthetic fixture identities, a separate disposable upstream
DB and no real-account access. Exact versions, lock, execution and cleanup are
in the [contract report](local-contract-report.md). No account passwords, browser
cookies or private APIs were used.

Publication to the requested MultiDot remote was also explicitly approved for
this development task, subject to final verification. This grants no authority
to create persistent credentials, connect accounts, or expose a public service.

## Approval gate 2: authenticated account ingress

Before requesting keys, verify a supported path for this account/workspace:

- Preferred candidate: official Secure MCP Tunnel, subject to platform
  organization/workspace, runtime-key and tunnel permissions.
- The official custom MCP setup documentation describes OAuth/no-auth/mixed
  modes. dot2api's current server uses static Bearer/x-api-key authentication
  and does not implement OAuth. A generic static bearer-entry UI cannot be
  assumed. Resolve supported per-account OAuth/identity mapping or another
  explicitly approved constrained authenticated route first.
- Preserve B/C/A identities. Do not inject a shared admin token for all
  callers, disable TLS/callback checks, or create an unauthenticated fallback.
- New credentials, persistent access, external exposure, domains, hosts or
  costs need their applicable approval/secure user entry. Nothing is created
  just because the user owns three subscriptions.

Relevant official references:
[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels),
[custom MCP setup](https://developers.openai.com/api/docs/guides/custom-mcp-server),
[MCP Events](https://developers.openai.com/plugins/build/mcp-events).

## Per-account setup checklist

| Account/role | Suggested connection name | Exact endpoint/tunnel | Queue | Required access |
|---|---|---|---|---|
| Controller producer | MultiDot controller | NOT_CREATED | all three | read + submit, no work |
| B | MultiDot B | NOT_CREATED | dot-b only | read + work + subscribe |
| C | MultiDot C | NOT_CREATED | dot-c only | read + work + subscribe |
| A synthesis | MultiDot A synthesis | NOT_CREATED | dot-a only | read + work + subscribe |

These are suggested names, not existing account IDs. Each account's owner must
connect the approved plugin/route and explicitly authorize watching its queue.
Enter secrets only in the approved secure setup UI, never in a prompt, URL,
Git file, or terminal argument. Account ID, identity ID, worker ID and queue are
separate identifiers; bind them using verified setup evidence.
The producer configuration label alone does not bind a credential to an actual
tenant/creator. Verify that identity before any native idempotent retry, including
after token rotation. A replacement credential for a different creator must not
be used to recover an existing intent.

Copy worker instructions only after tool discovery confirms exact APIs:
[B](../prompts/dot-b-worker.md), [C](../prompts/dot-c-worker.md),
[A synthesis](../prompts/dot-a-synthesis.md).

## Success evidence to collect

1. B's actual authenticated request/claim identity maps to dot-b and the
   controller's task ID; native public task JSON alone lacks that identity.
2. One small provided-materials task completes, and A retrieves the exact
   schema-valid result and content hash. No external app action is needed.
3. Cross-queue reads/claims and worker-originated submissions are rejected.
4. Repeat for C with a distinct account/identity.
5. A subscribes to dot-a `task.available`, reads exact accepted B/C versions,
   completes synthesis, and actually posts the result in the user's conversation.
6. Verify heartbeat, reconnect rescan, identity/key revocation and subscription
   termination separately. Token revocation alone does not end old subscriptions.

Subscription support or cloud-file retention does not prove Events delivery,
long-lived lease renewal, continuous daemon uptime or user notification.
