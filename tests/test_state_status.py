"""Regression tests for scripts/state-status.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "state-status.py"

spec = importlib.util.spec_from_file_location("state_status", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules["state_status"] = mod
spec.loader.exec_module(mod)

find_repo_root = mod.find_repo_root


def test_find_repo_root_raises_outside_git_repo(tmp_path: Path) -> None:
    """find_repo_root must not silently treat an arbitrary directory as a repo root."""
    with pytest.raises(RuntimeError, match="No git repository found above"):
        find_repo_root(tmp_path)
