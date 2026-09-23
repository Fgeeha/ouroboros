#!/usr/bin/env python3
"""Run a verification command with disposable filesystem and configuration roots.

Use `python -I -S scripts/safe_test.py -- <venv-python> -m pytest ...`.
This stdlib launcher audits its environment helper before loading it, then prints
the boundary before starting the command. It installs nothing and is not a sandbox.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temp-parent", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("supply a verification command after --")
    repo = Path(__file__).resolve().parents[1]
    if args.temp_parent is not None:
        parent = Path(args.temp_parent).resolve()
        if parent == repo or repo in parent.parents:
            # A disposable root nested in the checkout is not disposable: `git`
            # started in it walks up into THIS working tree, and a run that
            # snapshots or resets its own temp path then operates on the
            # operator's source. Refuse instead of accepting a false boundary.
            parser.error("--temp-parent must be outside the repository working tree")
    helper = repo / "ouroboros" / "test_environment.py"
    tree = ast.parse(helper.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = ([item.name for item in node.names] if isinstance(node, ast.Import)
                 else [node.module] if isinstance(node, ast.ImportFrom) else [])
        if any(name not in {"__future__", "ast", "os", "pathlib"} for name in names):
            raise RuntimeError("test environment helper must import only its audited stdlib leaves")
    boundary = runpy.run_path(str(helper))["isolated_environment"]
    with tempfile.TemporaryDirectory(prefix="ouroboros-check-", dir=args.temp_parent) as directory:
        env = boundary(Path(directory), repo, keep=(
            "OUROBOROS_RUN_UI_SMOKE", "OUROBOROS_EXPECT_BROWSER_ENGINES",
            "OUROBOROS_PREFLIGHT_TEST_WORKERS", "OUROBOROS_PREFLIGHT_SERIAL",
            "PLAYWRIGHT_BROWSERS_PATH", "OUROBOROS_E2E_DEEP",
        ))
        print("SAFE_TEST_BOUNDARY " + json.dumps({key: value for key, value in env.items()
              if key in {"HOME", "PYTHONUSERBASE", "PYTHONPYCACHEPREFIX"}
              or key.startswith("OUROBOROS_")}), flush=True)
        # Complete environment replacement reaches the interpreter BEFORE site,
        # plugins, conftest or any application module can import.
        return subprocess.call(command, cwd=repo, env=env)


if __name__ == "__main__":
    sys.exit(main())
