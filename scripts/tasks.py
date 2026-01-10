#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Simple wrapper script for tasks CLI.

This script wraps the tasks package module to provide a consistent
entry point across agent workspaces: ./scripts/tasks.py

All functionality lives in packages/tasks - this just forwards calls.
"""

import os
import subprocess
import sys
from pathlib import Path

# Get the directory containing this script
SCRIPT_DIR = Path(__file__).parent.absolute()
# packages/tasks is relative to the gptme-contrib root
TASKS_PKG_DIR = SCRIPT_DIR.parent / "packages" / "tasks"

if not TASKS_PKG_DIR.exists():
    print(
        f"Error: tasks package directory not found at {TASKS_PKG_DIR}", file=sys.stderr
    )
    print(
        "This script must be run from within the gptme-contrib repository",
        file=sys.stderr,
    )
    sys.exit(1)

if __name__ == "__main__":
    # Capture the original working directory before changing cwd
    # This allows the CLI to discover tasks in the agent workspace
    original_cwd = os.getcwd()

    # Set up environment with original cwd for the CLI to use
    env = os.environ.copy()
    env["TASKS_REPO_ROOT"] = original_cwd

    # Forward all arguments to the tasks package module
    # Run from packages/tasks directory so uv can find the module
    result = subprocess.run(
        ["uv", "run", "python3", "-m", "tasks"] + sys.argv[1:],
        cwd=str(TASKS_PKG_DIR),
        env=env,
    )
    sys.exit(result.returncode)
