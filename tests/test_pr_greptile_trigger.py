from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    mock_helper = MagicMock(spec=Path)
    mock_helper.exists.return_value = True
    with (
        patch.object(pr_greptile_trigger, "SAFE_HELPER", mock_helper),
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
    mock_helper = MagicMock(spec=Path)
    mock_helper.exists.return_value = True
    with (
        patch.object(pr_greptile_trigger, "SAFE_HELPER", mock_helper),
        patch.object(
            pr_greptile_trigger.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["bash"], timeout=30),
        ),
    ):
        ok, output = pr_greptile_trigger.trigger_greptile("gptme/gptme-contrib", 504)

    assert not ok
    assert output == "helper-timeout"


# --- resolve_repos ---


def test_resolve_repos_cli_flag_takes_priority() -> None:
    """--repo flag returns exactly that repo regardless of env var."""
    with patch.dict(os.environ, {"GREPTILE_REPOS": "gptme/gptme-cloud"}):
        result = pr_greptile_trigger.resolve_repos("gptme/gptme")
    assert result == ["gptme/gptme"]


def test_resolve_repos_env_var_overrides_defaults() -> None:
    """GREPTILE_REPOS env var overrides built-in defaults."""
    with patch.dict(
        os.environ, {"GREPTILE_REPOS": "org/a, org/b , org/c"}, clear=False
    ):
        result = pr_greptile_trigger.resolve_repos(None)
    assert result == ["org/a", "org/b", "org/c"]


def test_resolve_repos_defaults_when_no_flag_or_env() -> None:
    """Returns built-in DEFAULT_GREPTILE_REPOS when no flag and env var unset."""
    env = {k: v for k, v in os.environ.items() if k != "GREPTILE_REPOS"}
    with patch.dict(os.environ, env, clear=True):
        result = pr_greptile_trigger.resolve_repos(None)
    assert result == list(pr_greptile_trigger.DEFAULT_GREPTILE_REPOS)


# --- fetch_prs ---


def test_fetch_prs_valid_json_returns_prs_with_repo() -> None:
    """Valid JSON response is parsed and repo field is injected."""
    sample = [
        {"number": 1, "title": "feat: foo", "url": "https://github.com/o/r/pull/1"}
    ]
    with patch.object(pr_greptile_trigger, "run_gh", return_value=json.dumps(sample)):
        result = pr_greptile_trigger.fetch_prs("gptme/gptme", "TimeToBuildBob")
    assert len(result) == 1
    assert result[0]["repo"] == "gptme/gptme"
    assert result[0]["number"] == 1


def test_fetch_prs_json_decode_error_returns_empty_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed JSON is handled gracefully — returns [] and emits a warning."""
    with patch.object(pr_greptile_trigger, "run_gh", return_value="not-json{{{"):
        result = pr_greptile_trigger.fetch_prs("gptme/gptme", "TimeToBuildBob")
    assert result == []
    captured = capsys.readouterr()
    assert "warn" in captured.err.lower()


def test_fetch_prs_empty_response_returns_empty_list() -> None:
    """Empty/None response returns [] without crashing."""
    with patch.object(pr_greptile_trigger, "run_gh", return_value=""):
        result = pr_greptile_trigger.fetch_prs("gptme/gptme", "TimeToBuildBob")
    assert result == []


def test_main_returns_2_when_all_pr_status_checks_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing helper / status errors for every PR must not look like success."""
    monkeypatch.setattr(pr_greptile_trigger, "get_gh_user", lambda: "bot")
    monkeypatch.setattr(
        pr_greptile_trigger,
        "fetch_prs",
        lambda repo, author: [{"number": 1, "title": "t", "url": "u", "repo": repo}],
    )
    monkeypatch.setattr(
        pr_greptile_trigger, "review_state_for_pr", lambda repo, num: "error"
    )
    monkeypatch.setattr(
        pr_greptile_trigger, "resolve_repos", lambda arg: ["gptme/gptme"]
    )

    args = pr_greptile_trigger._parse_args([])
    assert pr_greptile_trigger._run(args) == 2
    captured = capsys.readouterr()
    assert "could not determine greptile status" in captured.err.lower()


# --- exit code semantics ---


def test_main_execute_all_success_returns_0(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns 0 when all triggers succeed."""
    monkeypatch.setattr(pr_greptile_trigger, "get_gh_user", lambda: "bot")
    monkeypatch.setattr(
        pr_greptile_trigger,
        "fetch_prs",
        lambda repo, author: [{"number": 1, "title": "t", "url": "u", "repo": repo}],
    )
    monkeypatch.setattr(
        pr_greptile_trigger, "review_state_for_pr", lambda repo, num: "needs-re-review"
    )
    monkeypatch.setattr(
        pr_greptile_trigger, "trigger_greptile", lambda repo, num: (True, "ok")
    )
    monkeypatch.setattr(
        pr_greptile_trigger, "resolve_repos", lambda arg: ["gptme/gptme"]
    )
    args = pr_greptile_trigger._parse_args(["--execute"])
    assert pr_greptile_trigger._run(args) == 0


def test_main_execute_partial_failure_returns_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returns 2 (partial failure) when some triggers fail."""
    call_count = {"n": 0}

    def _trigger(repo: str, num: int) -> tuple[bool, str]:
        call_count["n"] += 1
        return (call_count["n"] == 1, "ok" if call_count["n"] == 1 else "err")

    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(pr_greptile_trigger, "get_gh_user", lambda: "bot")
    monkeypatch.setattr(
        pr_greptile_trigger,
        "fetch_prs",
        lambda repo, author: [
            {"number": 1, "title": "a", "url": "u1", "repo": repo},
            {"number": 2, "title": "b", "url": "u2", "repo": repo},
        ],
    )
    monkeypatch.setattr(
        pr_greptile_trigger, "review_state_for_pr", lambda repo, num: "needs-re-review"
    )
    monkeypatch.setattr(pr_greptile_trigger, "trigger_greptile", _trigger)
    monkeypatch.setattr(
        pr_greptile_trigger, "resolve_repos", lambda arg: ["gptme/gptme"]
    )
    args = pr_greptile_trigger._parse_args(["--execute"])
    assert pr_greptile_trigger._run(args) == 2


def test_main_execute_all_fail_returns_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns 1 when all triggers fail."""
    monkeypatch.setattr(pr_greptile_trigger, "get_gh_user", lambda: "bot")
    monkeypatch.setattr(
        pr_greptile_trigger,
        "fetch_prs",
        lambda repo, author: [{"number": 1, "title": "t", "url": "u", "repo": repo}],
    )
    monkeypatch.setattr(
        pr_greptile_trigger, "review_state_for_pr", lambda repo, num: "needs-re-review"
    )
    monkeypatch.setattr(
        pr_greptile_trigger, "trigger_greptile", lambda repo, num: (False, "err")
    )
    monkeypatch.setattr(
        pr_greptile_trigger, "resolve_repos", lambda arg: ["gptme/gptme"]
    )
    args = pr_greptile_trigger._parse_args(["--execute"])
    assert pr_greptile_trigger._run(args) == 1
