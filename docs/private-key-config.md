# Private dots config

For a new installation, use the [quick setup guide](simple-setup.md):

```sh
python3 scripts/multidot.py setup
# Run separately after approving service execution and tunnel connections.
python3 scripts/multidot.py run
```

`setup` opens an interactive wizard. Enter a name, an existing tunnel ID, and a
hidden runtime API key for each dot; choose whether to add another dot
(default: no). There is no fixed dot count. New users do not need to run `init`
or manually edit JSON. The wizard creates regular workers and requires a
terminal that supports hidden key entry. It refuses non-terminal input and
does not fall back to displaying the key.

The wizard collects the full configuration before writing it. One final
`Save and prepare local setup? [y/N]` confirmation explains the config and state
destinations, plaintext key storage, and local internal credential creation.
The default answer is no. Approval saves a complete private config, then runs
local preparation; it does not authorize or start tunnel connections.

The preferred config is a list of dots, each with just `name`, `tunnel_id` and
`runtime_api_key`. The public [template](../config/dots.example.json) has blank
values. The wizard may fill the reviewed blank template created by `init`, but
refuses a populated existing config without changing it. `init` remains an
optional blank-template command and never overwrites an existing file.

Private configs use owner-only 0600 permissions. At the default location, a
missing `~/.multidot` directory is created with 0700 permissions, but the user's
home directory is never created. Unsafe or symlinked existing application
directories are refused; existing directory permissions are not changed.
Advanced file-based configs may include a role: `worker` is the default and at
most one entry may be `synthesis`, alongside at least one regular worker.

The default user config is `~/.multidot/config.json`; generated runtime
configuration, databases and credentials live under `~/.multidot/state/`.
These locations follow the current OS user's home directory and do not depend
on the checkout or calling directory. Override the private input with
`--config` on `init`/`setup` and the runtime location with `--state-root` on
`setup`/`run`/`status`/`stop`. Quoted `~/...` paths are expanded; relative
overrides resolve from the caller's working directory. The pinned tools still
default to the repository's `multidot-tools` sibling.

For an explicit config override, retain a `.private.json` or `.local.json`
filename, such as `/ABS/PATH/dots.private.json`. The bare `config.json` name is
accepted only at the default `~/.multidot/config.json` private home location.

Existing files are not discovered, moved, copied or reissued automatically.
For an existing installation, keep the old config and state paths explicitly
selected, and follow the
[manual path-change guidance](persistent-runtime.md#existing-installations-and-manual-path-changes).
Do not run a fresh setup at the default state path while an old installation
needs to be preserved.

Names are Unicode display labels, not queue IDs or permissions. Each label must
be nonempty, without control characters, and fit within 320 UTF-8 bytes. The
list determines the dot count; fixed A/B/C names and counts are not required.
Config byte limits and host resources still bound the installation.

The intended deployment remains **dot's own cloud runtime**, not the user's
Mac. A supported private terminal/input route for the user into that runtime
has not yet been verified. A native terminal and an executor may have different
home directories and PID/network namespaces. Creating a blank config in one
does not prove private access or runtime readiness in the other. No real wizard
setup or tunnel connection has run. This guide does not resolve that access
requirement.

## Interactive and file-based setup

- `setup` opens the wizard at the default private config path.
- `setup --non-interactive` loads an existing default config without prompts.
- `setup --config /ABS/PATH/dots.private.json` preserves the non-interactive
  file-based flow for an explicit path.
- `setup --interactive --config /ABS/PATH/dots.private.json` chooses the wizard
  with a custom private destination.

`--interactive` and `--non-interactive` are mutually exclusive. A file-based
setup requires prior authorization for local credential creation. An existing
populated config is reused through this mode, not overwritten by the wizard.
The optional `init` command creates only a blank template for a file workflow;
it does not issue credentials or run setup.

Do not edit or replace the destination while a wizard is open. Its advisory
lock coordinates cooperating wizard processes only; it cannot prevent a
separate editor or other same-user process from writing. Concurrent editing is
unsupported, even when the destination was initially a blank template.

Declining or canceling before config publication leaves the destination
unchanged and retains no secret temporary file during normal cleanup. Once
published, the complete private config remains if later local setup fails;
errors report fixed codes without printing entered values. After resolving the
problem, retry that saved config with `setup --non-interactive` or an explicit
`--config`, rather than entering the keys again. Config saving and runtime
state publication are separate steps, not one all-or-nothing transaction.
SIGKILL, abrupt process death, or host loss cannot guarantee temporary-file
cleanup or publication durability. Review the stopped installation privately
before retrying an uncertain result.

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
recorded in its private manifest. On a stopped installation, file-based setup
matches entries by tunnel ID, so display-name edits and list reordering preserve
those identities, queues, keys and queued work. On such a rerun a blank
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
Hidden terminal entry avoids displaying the key; it does not encrypt storage
or isolate the OS account. Anyone with access to that account, privileged
processes, recovery files, or backups may obtain the keys. Setup does not erase
the filled JSON. Use a trusted private terminal without shared session
recording. For advanced file edits, use a trusted editor without cloud
sync/shared backups and preserve 0600 permissions after saving; setup does not
erase editor history. Generated private directories use 0700.

Private `*.private.json`, `*.local.json`, common editor backup/swap files and
`secrets/` trees are Git-ignored. Ignore rules are not encryption, do not protect
already-tracked files and can be bypassed by `git add -f`. Never force-add keys.
The default config filename, `config.json`, is not protected by those filename
patterns if copied into the checkout. The upstream `encryption.key` is also
ignored. Keep both the private input config and the entire state directory
outside the checkout as the defaults do, including customized paths.
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
