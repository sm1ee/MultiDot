# Enter keys in a private config

You can enter existing keys yourself in a local JSON config, without a browser
form. This prepares the same `file:` references used by the runtime. It does
not issue credentials, initialize accounts, attach a tunnel, or start a service.

The intended deployment is still **dot's own cloud runtime**. This feature does
not move deployment to your Mac. A supported way for you to privately edit a
file in that cloud environment has not yet been verified; code support alone
does not resolve that access requirement or establish a live connection.

## Three steps

Run these commands yourself in the intended runtime environment. First prepare
`config/runtime.local.json` from the existing runtime example with its actual
non-secret paths and selected tunnel ID. Do not put keys in that runtime config.

1. Create a private copy of the blank key template:

   ```sh
   python3 scripts/runtime_credentials.py init
   ```

   This creates `config/credentials.private.json` with mode 0600 and refuses to
   overwrite an existing file. The public template is
   `config/credentials.example.json`; all three values there are empty.

2. Open `config/credentials.private.json` in your own trusted editor and fill in
   the values you already have approval to use:

   ```json
   {
     "schema_version": 1,
     "runtime_api_key": "",
     "worker_b_token": "",
     "producer_token": ""
   }
   ```

   - `runtime_api_key`: the raw OpenAI runtime key, with Tunnels Read + Use for
     the selected tunnel. Do not use an admin key.
   - `worker_b_token`: the raw dot2api B worker token. The helper adds `Bearer `.
     Its independently verified grant must be queue `dot-b` only, with `read`
     and `work`, optionally `subscribe`.
   - `producer_token`: the raw dot2api producer token, separate from B's token.

   An empty field reuses an already-present owner-only secret file without
   reading its value. Reuse checks file metadata only; it does not validate the
   key's contents or service permissions. If a required file is absent, the command lists the
   missing field names and writes no credential files. Do not invent missing
   tokens; their approved provisioning remains a separate step. Values must
   be single-line printable ASCII without spaces or a `Bearer ` prefix.

3. Apply once:

   ```sh
   python3 scripts/runtime_credentials.py install
   ```

   Only field names and a success/error code are printed. The helper writes
   0600 files under the configured state's 0700 `secrets` directory:

   | Private config field | Existing runtime file |
   | --- | --- |
   | `runtime_api_key` | `secrets/runtime-api-key` |
   | `worker_b_token` | `secrets/worker-b-authorization` |
   | `producer_token` | `secrets/producer-token` |

   No existing value is overwritten. Supplying a nonempty field for an existing
   destination fails; leave that field empty to reuse the existing file. A
   running supervisor blocks installation. Rotation/replacement is deliberately
   a separate stopped-runtime operation, not a silent side effect of this helper.

   Each final file is published only after its complete value has been written
   and synced, using an atomic operation that cannot overwrite an existing name.
   An ordinary error rolls back only files created by that invocation. A process
   or host crash can still leave some complete files installed and `.pending-*`
   plaintext copies; the whole batch is not atomic. On retry, leave fields for
   already-installed files empty and provide only missing values. Empty,
   oversized or hard-linked existing entries are refused using metadata only.
   If a crash left a hard-linked final/pending pair, the user must resolve those
   stopped-runtime files privately before retrying; no automatic overwrite or
   secret-file cleanup is attempted by the next invocation.

For different file locations, pass paths, never values:

```sh
python3 scripts/runtime_credentials.py install --private-config config/credentials.private.json --runtime-config config/runtime.local.json
```

Once all separate provisioning, identity verification and execution approvals
are satisfied, the user can run the existing foreground command in the same
runtime environment:

```sh
python3 scripts/runtime_supervisor.py --config config/runtime.local.json run
```

See [persistent runtime](persistent-runtime.md) for the managed-session, Events,
shutdown and recovery limits. Installing config does not prove readiness,
account connectivity, Events delivery, or survival after host replacement.

## Plaintext and handling limits

This is **plaintext storage**, not encryption or a secrets vault. Anyone who can
read your account's files, privileged processes, editor recovery files or
backups may obtain the values. The private JSON and generated files are two
copies; the helper does not erase the private JSON, clean editor history or
claim secure deletion. Use a trusted editor without cloud sync or shared
backups for this file, and keep the file owner-only after saving.

Private `*.private.json` and `*.local.json` files, common editor backups/swap
files, and generated `secrets/` trees are ignored by Git. Git ignore is not
encryption, does not protect an already-tracked file, and can be bypassed by
`git add -f`; never force-add credentials. Symlink paths, unsafe permissions,
unexpected fields and oversized/multiline values are refused without echoing
input or parser details. The helper performs no network call and starts no
process, so its success only means local files were prepared.

Do not paste real values in chat, shell arguments, screenshots, issue reports,
logs or commits. After entering real keys, **do not ask an agent to open, cat,
read, validate or install the filled config**. Run the user commands yourself.
Even though normal `status` and `check` output is redacted, `check` and startup
must read key files locally; an agent should not run those against your real
values without a separately supported secure flow. Share only the helper's
fixed error code and listed field names if you need help.
