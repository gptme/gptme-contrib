#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""DEPRECATED: Use 'gptodo' command instead.

This wrapper script is deprecated and will be removed in a future version.

Install gptodo for standalone usage:
    uv tool install git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptodo
    pipx install git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptodo

Or run directly:
    python3 -m gptodo [command]
"""

import os
import subprocess
import sys
from pathlib import Path

# Print deprecation warning to stderr
print(
    "\033[33mDeprecation Warning: 'scripts/tasks.py' is deprecated. "
    "Use 'gptodo' command instead.\033[0m",
    file=sys.stderr,
)

# Get the directory containing this script
SCRIPT_DIR = Path(__file__).resolve().parent.absolute()
GPTME_CONTRIB_ROOT = SCRIPT_DIR.parent
# packages/gptodo is relative to the gptme-contrib root
GPTODO_PKG_DIR = GPTME_CONTRIB_ROOT / "packages" / "gptodo"

if not GPTODO_PKG_DIR.exists():
    print(
        f"Error: gptodo package directory not found at {GPTODO_PKG_DIR}",
        file=sys.stderr,
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

    # Forward all arguments to the gptodo package module
    # Run from packages/gptodo directory so uv can find the module
    result = subprocess.run(
        ["uv", "run", "python3", "-m", "gptodo"] + sys.argv[1:],
        cwd=str(GPTODO_PKG_DIR),
        env=env,
    )
    sys.exit(result.returncode)
