from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "github"
    / "pr-greptile-trigger.py"
)
spec = importlib.util.spec_from_file_location("pr_greptile_trigger", MODULE_PATH)
if spec is None or spec.loader is None:
    pytest.skip(f"Could not load module from {MODULE_PATH}", allow_module_level=True)
pr_greptile_trigger = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pr_greptile_trigger
spec.loader.exec_module(pr_greptile_trigger)


def test_review_state_for_pr_timeout_returns_error() -> None:
    with (
        patch.object(
            type(pr_greptile_trigger.SAFE_HELPER), "exists", return_value=True
        ),
        patch.object(
            pr_greptile_trigger.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["bash"], timeout=30),
        ),
    ):
        assert (
            pr_greptile_trigger.review_state_for_pr("gptme/gptme-contrib", 504)
            == "error"
        )


def test_trigger_greptile_timeout_returns_helper_timeout() -> None:
    with (
        patch.object(
            type(pr_greptile_trigger.SAFE_HELPER), "exists", return_value=True
        ),
        patch.object(
            pr_greptile_trigger.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["bash"], timeout=30),
        ),
    ):
        ok, output = pr_greptile_trigger.trigger_greptile("gptme/gptme-contrib", 504)

    assert not ok
    assert output == "helper-timeout"
