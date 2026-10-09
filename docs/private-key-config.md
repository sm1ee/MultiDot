# Private dots config

For a new installation, use the [quick setup guide](simple-setup.md):

```sh
python3 scripts/multidot.py init
# Edit config/dots.private.json yourself in a trusted private editor.
python3 scripts/multidot.py setup
python3 scripts/multidot.py run
```

The preferred config is a list of dots, each with just `name`, `tunnel_id` and
`runtime_api_key`. The public [template](../config/dots.example.json) has blank
values. `init` creates an owner-only 0600 private file and refuses to overwrite
an existing file. Roles are optional: `worker` is the default and at most one
entry may be `synthesis`, alongside at least one regular worker.

Names are Unicode display labels, not queue IDs or permissions. Each label must
be nonempty, without control characters, and fit within 320 UTF-8 bytes. The
list determines the dot count; fixed A/B/C names and counts are not required.
Config byte limits and host resources still bound the installation.

The intended deployment remains **dot's own cloud runtime**, not the user's
Mac. A supported way for the user to privately edit its files has not yet been
verified. No real setup or tunnel connection has run. This guide does not
resolve that access requirement.

## Which external key goes here?

Use an existing tunnel from
[OpenAI Tunnels settings](https://platform.openai.com/settings/organization/tunnels)
and a runtime key from
[OpenAI API keys settings](https://platform.openai.com/settings/organization/api-keys).
The key and its principal need **Tunnels Read + Use** authorization for the
selected tunnel and its relevant workspace/organization; no admin key or
Manage permission is needed for runtime operation. See the pinned client's
[official permissions guide](https://github.com/openai/tunnel-client/blob/v0.0.16/docs/permissions.md).

- Each entry must have a distinct, valid existing tunnel ID.
- A key may be reused for multiple entries **only if its authorization covers
  every corresponding tunnel and workspace/organization**. Sharing an account
  alone does not establish that access.
- Enter the raw key, without `Bearer `, whitespace or line breaks. The field is
  printable ASCII and at most 8,191 characters.
- For a new installation every entry needs its key. Local setup checks format,
  not OpenAI authorization; no external API call is made.

There are no manual internal producer/worker token fields in this preferred
config. With the required user authorization, `setup` initializes them through
the reviewed, pinned dot2api APIs, verifies their local bindings, and stores
owner-only `file:` secrets. Internal tokens have a 30-day lifetime. There is no
silent renewal, overwrite or key rotation.

## What setup changes

`setup` requires the already-installed, verified toolchain described in the
[runtime guide](persistent-runtime.md#prerequisites-and-paths). It creates local
state and credentials; it does not download software, start services, create
OpenAI tunnels or authenticate any remote account.

A fresh installation gets stable UUID-based queue/worker IDs and a tenant,
recorded in its private manifest. On a stopped installation, setup matches
entries by tunnel ID, so display-name edits and list reordering preserve those
identities, queues, keys and queued work. On such a rerun a blank
`runtime_api_key` reuses the installed value; a supplied key must match the
recorded key fingerprint. Existing secret-file reuse checks metadata, not remote
validity. Missing or unsafe state is refused rather than silently rebuilt.

Adding/removing an entry, changing a tunnel ID or role, or supplying a changed
key is deliberately refused pending an explicit migration/rotation workflow.
Do not delete old state to bypass that refusal. Stop the runtime before any
setup rerun; setup refuses a running supervisor. Rerunning setup does not extend
internal token expiry.

Fresh setup builds a private staging directory and publishes the complete state
directory using a non-overwriting atomic rename. Ordinary pre-publication
failures remove that invocation's stage. A killed process or host failure can
leave a private staging directory containing credentials; there is no automatic
secure deletion or crash-cleanup promise. If publication durability is
unconfirmed, inspect the stopped installation before retrying. Display-name
updates span metadata files; a crash can leave stale labels, but a setup rerun
repairs them without reissuing identities or tokens.

## Plaintext and handling limits

Private JSON and generated key files are **plaintext, not a secrets vault**.
Anyone with access to the OS account, privileged processes, editor recovery
files or backups may obtain them. Setup does not erase the filled JSON or editor
history. Use a trusted editor without cloud sync/shared backups, and preserve
0600 permissions after saving. Generated private directories use 0700.

Private `*.private.json`, `*.local.json`, common editor backup/swap files and
`secrets/` trees are Git-ignored. Ignore rules are not encryption, do not protect
already-tracked files and can be bypassed by `git add -f`. Never force-add keys.
The upstream `encryption.key` is also ignored. Keep the entire state directory
outside the checkout as the default does, including any customized state root.
Symlinks, unsafe file metadata, duplicate/unknown fields and oversized input are
rejected. Errors do not echo values or parser details.

Do not paste real values in chat, shell arguments, screenshots, logs, issue
reports or commits. After entering real keys, **do not ask an agent to open,
read, validate or install the filled config**, or to run setup/startup against
it. Run those user commands yourself unless a separately supported secure flow
exists. `run` does not reopen the private input JSON, but it must read installed
secret files locally. Redacted output does not make credential handling safe.
For help, share only the fixed error code and affected field/index information.

## Advanced legacy v1 compatibility

**Only for an existing v1/B-only installation.** New setups should use the dots
list above. The old `runtime_credentials.py` helper remains compatible with
`config/runtime.local.json` and is not a migration tool for generic installs.
It does not provision identities, connect a tunnel or start any process.

For the already-provisioned legacy runtime, the operator can create and edit its
separate private template, then install existing values:

```sh
python3 scripts/runtime_credentials.py init
# Privately edit config/credentials.private.json yourself.
python3 scripts/runtime_credentials.py install --private-config config/credentials.private.json --runtime-config config/runtime.local.json
```

Its three legacy fields are:

- `runtime_api_key`: raw key with Read + Use for that legacy tunnel
- `worker_b_token`: raw dot2api token, independently verified as queue `dot-b`
  only with `read`, `work` and optionally `subscribe`; the helper adds `Bearer `
- `producer_token`: raw dot2api producer token, separate from the worker token

They become `secrets/runtime-api-key`, `secrets/worker-b-authorization` and
`secrets/producer-token` under the configured state root. Values must be
single-line printable ASCII without whitespace or a `Bearer ` prefix. Blank
fields reuse existing owner-only files by metadata only; if a required file is
missing, the helper reports missing field names without writing credentials.
A supplied nonempty value cannot overwrite an existing destination. A running
supervisor blocks installation.

This legacy helper publishes each complete file without overwriting. The batch
is not atomic: a crash can leave complete files and `.pending-*` plaintext
copies. Retry with installed fields blank and only missing values supplied.
Hard-linked final/pending pairs require private stopped-runtime review before a
retry; there is no automatic overwrite or cleanup. Empty, oversized and
hard-linked existing entries are refused using metadata only. The helper's
success establishes only local file preparation, not account connectivity.

See [legacy runtime operation](persistent-runtime.md#advanced-legacy-v1-compatibility)
for the corresponding commands. Keep legacy configs separate from the new flow.
