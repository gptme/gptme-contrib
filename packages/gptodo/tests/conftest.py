"""Shared fixtures for the gptodo test suite."""

import os

import pytest

# Env vars that point gptodo at a real workspace. An agent session that exports
# one (Bob's launcher sets GPTODO_TASKS_DIR) would otherwise make CLI tests
# read and rewrite the live tasks directory instead of tmp_path: `gptodo expire`
# tests moved 136 real someday tasks to expired before this fixture existed.
WORKSPACE_ENV_VARS = ("GPTODO_TASKS_DIR", "TASKS_REPO_ROOT")


@pytest.fixture(autouse=True)
def _isolate_workspace_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in WORKSPACE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fixture repos from inheriting the host's git configuration.

    A global ``core.hooksPath`` (an agent host installs commit guards there)
    otherwise runs inside every temp repo, so a fixture ``git commit`` exits 1
    and the archive tests fail on that host while passing in CI.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
