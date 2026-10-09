#!/usr/bin/env python3
"""Explicit schema-v1 compatibility wrapper. Use runtime_ingress.py for v2."""
import argparse
import json
from pathlib import Path
import sys

from runtime_ingress import serve
from runtime_supervisor import load


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
        if config["schema_version"] != 1:
            raise ValueError("Legacy ingress requires schema version 1")
        return serve(config, "dot-b")
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "message": "Legacy ingress startup failed"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
