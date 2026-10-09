# MultiDot Quick Setup

For each dot, you provide a **name, tunnel ID, and runtime API key**.
The `setup` command prepares the internal producer/worker tokens and ports.

A/B/C and a three-dot configuration are only examples. Use any display names
you like, including names in other languages, and add as many entries to the
`dots` list as you need. There is no fixed limit of 3, 4, or 16 dots, but limits
such as configuration file size, available ports, and memory still apply.

> This is currently at the code and local-validation stage. Neither setup nor
> tunnel connections have been run with real keys. The target environment is
> **dot's cloud**, rather than your Mac. A secure way for you to edit files
> directly in that environment has not yet been confirmed. Do not enter real
> keys until that is available.

## 1. Create an Empty Configuration

Run this command yourself from the repository in the Linux environment where
MultiDot will run. Python 3.11+ and the verified, pinned tool versions must be
installed first.

```sh
python3 scripts/multidot.py init
```

This creates `config/dots.private.json` with read and write access restricted
to its owner. It does not overwrite an existing file.

## 2. Edit the File Yourself

Open `config/dots.private.json` in a trusted private editor. Fill in the empty
strings. To use more dots, add entries to the list in the same format.

```json
{
  "schema_version": 1,
  "dots": [
    {"name": "Researcher", "tunnel_id": "", "runtime_api_key": ""}
  ]
}
```

- `name`: Your preferred display name. The name does not determine the dot's role or permissions.
- `tunnel_id`: An existing ID from [OpenAI Tunnels settings](https://platform.openai.com/settings/organization/tunnels). Each entry must use a different ID.
- `runtime_api_key`: A key prepared in [OpenAI API keys settings](https://platform.openai.com/settings/organization/api-keys) with **Read + Use** permissions for that tunnel. Do not add the `Bearer ` prefix or use an admin key.

You may use one key for multiple entries, but **both the key and its principal
must have access to each tunnel and the associated workspace/organization**.
Belonging to the same account does not automatically grant access to every
tunnel. See the [official permissions guide](https://github.com/openai/tunnel-client/blob/v0.0.16/docs/permissions.md).

If you omit the role, every dot defaults to `worker`. Add `"role": "synthesis"`
to an entry only for an advanced configuration that needs a dot to combine
results. At most one synthesis dot is allowed, and at least one regular
`worker` is required.

Do not share a file containing keys through chat, Git, screenshots, or shared
documents. Do not ask an agent to open or inspect the file. Run the following
commands yourself as well. The file is unencrypted plain text.

## 3. Run Setup Once

After approving the creation of local internal credentials, run this command
yourself:

```sh
python3 scripts/multidot.py setup
```

The command verifies the installed, pinned tool versions, then prepares
internal queues, tokens, ports, and state files. It does not download tools,
connect to OpenAI, create new tunnels, or start services. A success message
does not confirm a real account connection or the key's remote permissions.

If you see `reviewed_runtime_installation_missing`, the verified tools must
be installed first. Re-entering the key will not fix this. See
[prerequisites and runtime paths](persistent-runtime.md#prerequisites-and-paths).

## 4. Start the Runtime

Run this command yourself in an environment approved for connecting to the
specified tunnels and running the runtime:

```sh
python3 scripts/multidot.py run
```

This runs the full configuration in the **foreground**. Keep the session open
and press Ctrl-C in that session to stop it. No service is installed to start
automatically at boot. Continued operation is not guaranteed if the session
or cloud host shuts down.

## Making Changes Later

- Choose the names, number of dots, and roles during initial setup. To change display names or reorder the list, stop the runtime, edit the file, and rerun `setup`. Internal UUIDs, queues, tenants, and keys are preserved.
- For an existing installation, `setup` currently rejects adding or removing dots, changing tunnels or roles, and replacing keys. These changes require a separate migration or replacement procedure. Do not delete existing state to bypass this restriction.
- Internal tokens expire after 30 days. They are not renewed automatically, so continued operation after expiration requires an explicit renewal procedure.
- Real task execution by each dot, Events connections, and delivery of results to the user require separate validation.

See the [key configuration guide](private-key-config.md) for storage and reset
precautions, and the [runtime guide](persistent-runtime.md) for operational and
recovery limits.
