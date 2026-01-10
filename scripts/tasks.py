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

import subprocess
import sys

if __name__ == "__main__":
    # Forward all arguments to the tasks package module
    result = subprocess.run(
        ["uv", "run", "python3", "-m", "tasks"] + sys.argv[1:],
        cwd=None,  # Use current directory
    )
    sys.exit(result.returncode)
