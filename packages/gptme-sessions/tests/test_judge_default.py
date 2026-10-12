"""Provider selection is deployment configuration, not a shared credential change."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (None, "claude-haiku-4-5-20251001"),
        ("", "claude-haiku-4-5-20251001"),
        ("openrouter/anthropic/claude-haiku-5.5", "openrouter/anthropic/claude-haiku-5.5"),
    ],
)
def test_default_judge_model_environment(override, expected):
    env = os.environ.copy()
    env.pop("GPTME_SESSIONS_JUDGE_MODEL", None)
    if override is not None:
        env["GPTME_SESSIONS_JUDGE_MODEL"] = override
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from gptme_sessions.judge import DEFAULT_JUDGE_MODEL; print(DEFAULT_JUDGE_MODEL)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == expected
