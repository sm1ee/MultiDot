#!/usr/bin/env python3
"""User commands: guided setup, optional blank config, then foreground runtime.

Never pass keys in command arguments. Agents must not read or operate a filled
real private config through these commands. Only the user runs real setup.
"""
import json
import os
from pathlib import Path
import sys

from runtime_credentials import PrivateParser
from multidot_setup import SetupError, configure, default_config, default_environment, initialize_config, load_dots, verify_environment


def main(argv=None):
    os.umask(0o077)
    parser = PrivateParser(description=__doc__)
    parser.add_argument("command", choices=("init", "setup", "run", "status", "stop"))
    parser.add_argument("--config", type=Path, default=None,
                        help="Private config (default: ~/.multidot/config.json)")
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--tools-root", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--interactive", action="store_true", help="Use the setup wizard, including with --config")
    mode.add_argument("--non-interactive", action="store_true", help="Prepare setup from an existing private config without prompts")
    args = parser.parse_args(argv)
    wizard_saved = False
    try:
        if args.command != "setup" and (args.interactive or args.non_interactive):
            raise SetupError("setup_mode_requires_setup_command")
        config_path = args.config if args.config is not None else default_config()
        if args.command == "init":
            result = initialize_config(config_path)
        else:
            environment = default_environment(args.state_root, args.tools_root)
            if args.command == "setup":
                interactive = args.interactive or (args.config is None and not args.non_interactive)
                if interactive:
                    from multidot_wizard import collect_and_save, require_tty
                    require_tty()
                verify_environment(environment)
                desired_prefix = Path(environment["upstream_python"]).parent.parent.resolve()
                if Path(sys.prefix).resolve() != desired_prefix:
                    os.execv(environment["upstream_python"], [environment["upstream_python"], str(Path(__file__).resolve()), *(sys.argv[1:] if argv is None else argv)])
                if interactive:
                    wizard = collect_and_save(config_path, state_root=environment["state_root"])
                    wizard_saved = bool(wizard["saved"])
                    if not wizard["requested_setup"]:
                        print(json.dumps(wizard))
                        return 0
                result = configure(load_dots(config_path), environment)
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
        if wizard_saved or getattr(exc, "config_saved", False):
            result["wizard_config_saved"] = True
        if exc.indices:
            result["dot_indices"] = exc.indices
        print(json.dumps(result), file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print(json.dumps({"ok": False, "error": "setup_cancelled", "wizard_config_saved": wizard_saved}), file=sys.stderr)
        return 130
    except Exception:
        print(json.dumps({"ok": False, "error": "local_setup_failed_no_values_displayed", "wizard_config_saved": wizard_saved}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
