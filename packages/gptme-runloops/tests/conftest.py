"""Shared isolation for gptme-runloops tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Project-monitoring slots export these to their child sessions. A pytest run
# inside such a slot inherits them, which flips config defaults (e.g. the gptme
# canary timeout) and the self-merge env precedence that tests assert on.
INHERITED_ENV_PREFIXES = ("PM_", "PROJECT_MONITORING_")
INHERITED_ENV_NAMES = ("WORKSPACE_REPO", "SELF_MERGE_ALLOWED_PATHS")


@pytest.fixture(autouse=True)
def isolate_session_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GPTME_SESSIONS_DIR", str(tmp_path / "sessions"))


@pytest.fixture(autouse=True)
def isolate_inherited_pm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop inherited PM slot variables; tests set the ones they need."""
    for name in list(os.environ):
        if name.startswith(INHERITED_ENV_PREFIXES) or name in INHERITED_ENV_NAMES:
            monkeypatch.delenv(name)
