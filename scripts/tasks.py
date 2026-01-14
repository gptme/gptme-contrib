#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Wrapper script for tasks CLI - calls the package entry point."""

import subprocess
import sys
from pathlib import Path

# Get workspace root (parent of scripts directory)
WORKSPACE_ROOT = Path(__file__).parent.parent

# Call the package via uv run
result = subprocess.run(
    ["uv", "run", "python3", "-m", "tasks"] + sys.argv[1:], cwd=str(WORKSPACE_ROOT)
)
sys.exit(result.returncode)
