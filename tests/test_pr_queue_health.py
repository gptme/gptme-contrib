from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "github" / "pr-queue-health.py"
)
spec = importlib.util.spec_from_file_location("pr_queue_health", MODULE_PATH)
if spec is None or spec.loader is None:
    pytest.skip(f"Could not load module from {MODULE_PATH}", allow_module_level=True)
pr_queue_health = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pr_queue_health
spec.loader.exec_module(pr_queue_health)


def test_get_per_repo_limits_warns_on_invalid_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = {k: v for k, v in os.environ.items() if k != "GPTME_PR_LIMITS"}
    with patch.dict(os.environ, {**env, "GPTME_PR_LIMITS": "{invalid"}, clear=True):
        limits = pr_queue_health.get_per_repo_limits()

    assert limits == pr_queue_health.DEFAULT_PER_REPO_LIMITS
    captured = capsys.readouterr()
    assert "invalid gptme_pr_limits json" in captured.err.lower()


def test_fetch_prs_for_repo_requests_limit_100() -> None:
    with patch.object(pr_queue_health, "run_gh", return_value="[]") as mock_run_gh:
        result = pr_queue_health.fetch_prs_for_repo(
            "gptme/gptme-contrib", "TimeToBuildBob"
        )

    assert result == []
    args = mock_run_gh.call_args.args[0]
    assert "--limit" in args
    assert args[args.index("--limit") + 1] == str(pr_queue_health.PR_LIST_LIMIT)


def test_parse_datetime_invalid_returns_none() -> None:
    assert pr_queue_health.parse_datetime("") is None
    assert pr_queue_health.parse_datetime("not-a-timestamp") is None


def test_run_gh_warns_on_failure(capsys: pytest.CaptureFixture[str]) -> None:
    completed = subprocess.CompletedProcess(
        args=["gh", "pr", "list"],
        returncode=1,
        stdout="",
        stderr="boom",
    )
    with patch.object(pr_queue_health.subprocess, "run", return_value=completed):
        result = pr_queue_health.run_gh(["pr", "list"])

    assert result is None
    captured = capsys.readouterr()
    assert "gh pr list failed: boom" in captured.err


def test_main_returns_2_when_all_repo_fetches_fail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(pr_queue_health, "get_gh_user", lambda: "TimeToBuildBob")
    monkeypatch.setattr(
        pr_queue_health,
        "get_tracked_repos",
        lambda: ["gptme/gptme", "gptme/gptme-contrib"],
    )
    monkeypatch.setattr(
        pr_queue_health, "fetch_prs_for_repo", lambda repo, author: None
    )

    with patch.object(sys, "argv", ["pr-queue-health.py"]):
        code = pr_queue_health.main()

    assert code == 2
    captured = capsys.readouterr()
    assert "failed to fetch prs for all tracked repositories" in captured.err.lower()
