# MultiDot boundaries

Python 3.11+; runtime is standard-library-only. Run tests with
`PYTHONPATH=src python -m unittest discover -s tests -v`.

Write GitHub-facing prose, comments, examples and commit messages in English.
Preserve multilingual test coverage with Unicode-escaped fixture inputs.

The controller owns its SQLite DB; use only the Native Task HTTP API to talk
to dot2api. The explicitly user-run setup command alone may initialize a fresh
private upstream using its reviewed APIs; controllers and runtime recovery must
never read or write its DB or issue credentials. Do not add an arbitrary shell
runner, account failover, recursive delegation, private API integration, or
unattended credential setup. Worker inputs and outputs are untrusted data.

Keep secrets and runtime state out of Git. Do not install or run upstream,
expose services, create credentials, commit, or push without explicit approval.
MOCK, LOCAL_CONTRACT, and REAL_DOT evidence must stay separate. Never infer
cross-account connectivity or always-on execution from localhost tests.
