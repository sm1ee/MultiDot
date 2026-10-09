# MultiDot Quick Setup

Run `setup` to enter each dot's **name, tunnel ID, and hidden runtime API key**.
The wizard saves the private config and prepares internal tokens and ports
after one final confirmation. No manual JSON editing or `init` step is needed.

Use any display names you like, including names in other languages, and add as
many dots as you need. There is no fixed limit of 3, 4, or 16 dots, but
configuration size, available ports, and host resources still apply. Every dot
entered through the wizard defaults to `worker`.

> This is currently at the code and local-validation stage. Neither setup nor
> tunnel connections have been run with real keys. The target environment is
> **dot's cloud**, rather than your Mac. A supported private terminal/input
> route into that runtime is still unverified. Do not enter real keys until
> that route and the required setup/run approvals are in place. A native
> terminal and an executor may have different home directories and namespaces;
> a blank config in one does not establish access to the runtime in the other.

## 1. Open the Setup Wizard

These steps are for a fresh installation with no existing live or retained
state to preserve. For an existing installation, first follow the
[existing-installation guidance](persistent-runtime.md#existing-installations-and-manual-path-changes).
Changing defaults does not move old files or renew credentials.

Run this command yourself from the repository in the intended Linux runtime
environment. Python 3.11+ and the reviewed, pinned toolchain must already be
installed. Use a trusted private terminal that supports hidden key entry.

```sh
python3 scripts/multidot.py setup
```

The wizard asks for:

1. A name for the dot
2. Its existing tunnel ID
3. Its runtime API key, with input hidden
4. Whether to add another dot, defaulting to no

Repeat for each dot you want. There is no initial dot-count question. The wizard
refuses to accept keys if hidden terminal entry is unavailable; it does not
fall back to echoing keys or accepting piped input.

Use an existing ID from
[OpenAI Tunnels settings](https://platform.openai.com/settings/organization/tunnels).
Each dot must use a different tunnel ID. Use a runtime key from
[OpenAI API keys settings](https://platform.openai.com/settings/organization/api-keys)
with **Read + Use** permissions for that tunnel. Enter the raw key without
`Bearer `, whitespace, or line breaks. Do not use an admin key.

You may reuse a key only when **both the key and its principal have access to
every selected tunnel and its associated workspace/organization**. Sharing an
account alone does not grant that access. See the
[official permissions guide](https://github.com/openai/tunnel-client/blob/v0.0.16/docs/permissions.md).
Local setup validates format, not remote permissions.

## 2. Confirm Saving and Local Preparation

After collecting the complete list, the wizard explains the private config
and state destinations, plaintext key storage, and creation of local internal
credentials. It asks once:

```text
Save and prepare local setup? [y/N]
```

Pressing Enter declines. No config is written before approval. After approval,
the complete config is saved as `~/.multidot/config.json`, with owner-only 0600
permissions. A missing `~/.multidot` application directory is created with 0700
permissions. Unsafe or symlinked application directories are refused; existing
directory permissions are not changed, and the user's home directory is never
created. Runtime state and credentials go under `~/.multidot/state/`. Both
defaults use the current OS user's home directory, independent of the checkout
or calling directory.

Setup prepares local queues, internal tokens, ports, and state using the
already-installed toolchain. It does not download tools, contact OpenAI, create
tunnels, or start services. Success does not confirm account connectivity.

The wizard can fill the reviewed blank template produced by `init`. It refuses
a populated existing config and leaves it unchanged; use the file-based flow
below to reuse one. Do not edit the config while the wizard is open. Its
advisory lock does not prevent unrelated editors from writing the file.

Declining or canceling before config publication leaves the config unchanged
and retains no secret temporary file during normal cleanup. Abrupt process
death, SIGKILL, or host loss cannot guarantee cleanup. If local preparation
fails after the config is saved, the **complete config stays saved**, and the
command reports a fixed error without displaying keys. Do not enter the keys
again or delete existing state to bypass a failure. After resolving the
reported problem, use `setup --non-interactive` to retry the saved default
config. See [private config handling](private-key-config.md) before retrying
after an interrupted publication.

For `reviewed_runtime_installation_missing`, install the reviewed tools first;
re-entering a key will not help. See
[prerequisites and runtime paths](persistent-runtime.md#prerequisites-and-paths).

## 3. Start the Runtime Separately

After separate approval for service execution and the specified tunnel
connections, run:

```sh
python3 scripts/multidot.py run
```

This runs the full configuration in the **foreground**. Keep its session open
and press Ctrl-C there to stop. No boot service is installed. Continued
operation is not guaranteed if the session or cloud host shuts down.

Never paste keys in chat, command arguments, screenshots, logs, or Git. The
private config is unencrypted plaintext. Do not ask an agent to open a filled
real config or operate on its secrets without a separately supported secure
flow. Run these real setup and runtime commands yourself.

## Existing Files and Other Paths

The command selects its mode as follows:

- `setup`: wizard at `~/.multidot/config.json`
- `setup --non-interactive`: use an existing default config without prompts
- `setup --config /ABS/PATH/dots.private.json`: preserve the existing file-based,
  non-interactive flow at that path
- `setup --interactive --config /ABS/PATH/dots.private.json`: wizard at a custom
  private path

`--interactive` and `--non-interactive` are mutually exclusive. Non-interactive
setup still requires prior approval for local credential creation; it is not
a way to bypass that requirement. `init` remains available to create a blank
template only, without overwriting an existing file, for advanced file-based
workflows. New users do not need it.

Custom config filenames must end in `.private.json` or `.local.json`; bare
`config.json` is accepted only at the default private home location. Use
`--state-root /ABS/PATH/state` on `setup`, `run`, `status`, and `stop` to select
another state directory. Reuse the same override later: a different state root
is a different installation target. Quoted `~/...` paths are expanded, and
relative paths resolve from the calling directory, not the repository.

The reviewed tools still default to a `multidot-tools` sibling of the
repository; `--tools-root` selects another approved location. Keep private
config and state outside the checkout. See the
[runtime paths](persistent-runtime.md#prerequisites-and-paths) before changing
an existing installation.

## Advanced Changes Later

- To rename dots or reorder an existing list, stop the runtime, edit the config
  privately, and rerun file-based setup using `--non-interactive` or an explicit
  `--config`. UUIDs, queues, tenants, and keys are preserved.
- The wizard creates regular workers. An advanced file-based config may set
  `"role": "synthesis"` for at most one dot and must retain at least one worker.
- Existing installations reject added/removed dots, changed tunnels or roles,
  and replacement keys pending a separate migration/rotation procedure. Do not
  delete state to bypass those checks.
- Internal tokens expire after 30 days and are not renewed automatically.
- Actual dot execution, Events connections, and result delivery require
  separate validation.

See the [key configuration guide](private-key-config.md) for storage and recovery
precautions, and the [runtime guide](persistent-runtime.md) for operational and
hosting limits.
