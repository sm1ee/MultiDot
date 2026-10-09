#!/usr/bin/env python3
"""User commands: init blank dots config, setup locally, then run together.

Never pass keys in command arguments. Agents must not read or operate a filled
real private config through these commands. Only the user runs real setup.
"""
import json
import os
from pathlib import Path
import sys

from runtime_credentials import PrivateParser
from multidot_setup import SetupError, configure, default_environment, initialize_config, load_dots, verify_environment


def main(argv=None):
    os.umask(0o077)
    parser = PrivateParser(description=__doc__)
    parser.add_argument("command", choices=("init", "setup", "run", "status", "stop"))
    parser.add_argument("--config", type=Path, default=Path("config/dots.private.json"))
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--tools-root", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = initialize_config(args.config)
        else:
            environment = default_environment(args.state_root, args.tools_root)
            if args.command == "setup":
                verify_environment(environment)
                desired_prefix = Path(environment["upstream_python"]).parent.parent.resolve()
                if Path(sys.prefix).resolve() != desired_prefix:
                    os.execv(environment["upstream_python"], [environment["upstream_python"], str(Path(__file__).resolve()), *(sys.argv[1:] if argv is None else argv)])
                result = configure(load_dots(args.config), environment)
            else:
                # Never open the user config on this path. Runtime startup reads
                # its file: secrets only after completed user-run setup.
                runtime = Path(environment["state_root"]) / "config/runtime.json"
                os.execv(sys.executable, [sys.executable, str(Path(__file__).with_name("runtime_supervisor.py")), "--config", str(runtime), args.command])
                raise SetupError("runtime_launch_failed")
        print(json.dumps(result))
        return 0
    except SetupError as exc:
        result = {"ok": False, "error": exc.code}
        if exc.indices:
            result["dot_indices"] = exc.indices
        print(json.dumps(result), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({"ok": False, "error": "local_setup_failed_no_values_displayed"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
