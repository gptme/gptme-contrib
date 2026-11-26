"""Run the dependency-free bridge regressions in the package test suite."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_node_bridge_regressions():
    node_dir = Path(__file__).resolve().parents[1] / "node"
    result = subprocess.run(
        ["node", "--test", *map(str, sorted(node_dir.glob("*.test.js")))],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
