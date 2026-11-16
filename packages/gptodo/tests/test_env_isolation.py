"""The suite must never inherit a real workspace from the caller's env."""

import os


def test_workspace_env_is_isolated() -> None:
    # Mirrors conftest.WORKSPACE_ENV_VARS; the autouse fixture must clear both.
    for name in ("GPTODO_TASKS_DIR", "TASKS_REPO_ROOT"):
        assert name not in os.environ, f"{name} leaked into the gptodo tests"
