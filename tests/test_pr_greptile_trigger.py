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


def test_fetch_prs_json_decode_error_returns_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed JSON is a hard failure — returns None and emits a warning."""
    with patch.object(pr_greptile_trigger, "run_gh", return_value="not-json{{{"):
        result = pr_greptile_trigger.fetch_prs("gptme/gptme", "TimeToBuildBob")
    assert result is None
    captured = capsys.readouterr()
    assert "warn" in captured.err.lower()


def test_fetch_prs_empty_response_returns_none() -> None:
    """Empty response (gh auth/timeout failure) returns None, not empty list."""
    with patch.object(pr_greptile_trigger, "run_gh", return_value=""):
        result = pr_greptile_trigger.fetch_prs("gptme/gptme", "TimeToBuildBob")
    assert result is None


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


def test_main_execute_partial_failure_returns_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returns 1 (partial failure) when some triggers fail."""
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
    assert pr_greptile_trigger._run(args) == 1


def test_main_execute_all_fail_returns_2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Returns 2 when all triggers fail."""
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
    assert pr_greptile_trigger._run(args) == 2


def test_main_all_fetch_prs_fail_returns_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Returns 2 when ALL fetch_prs calls return None (auth/network failure), not 0."""
    monkeypatch.setattr(pr_greptile_trigger, "get_gh_user", lambda: "bot")
    monkeypatch.setattr(
        pr_greptile_trigger,
        "fetch_prs",
        lambda repo, author: None,  # hard failure for every repo
    )
    monkeypatch.setattr(
        pr_greptile_trigger, "resolve_repos", lambda arg: ["gptme/gptme"]
    )
    args = pr_greptile_trigger._parse_args([])
    assert pr_greptile_trigger._run(args) == 2
    captured = capsys.readouterr()
    assert (
        "auth failure" in captured.err.lower()
        or "could not fetch" in captured.err.lower()
    )


def test_main_partial_fetch_fail_returns_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Returns 2 (not 0) when some repos fail and the successful repos have no open PRs."""
    monkeypatch.setattr(pr_greptile_trigger, "get_gh_user", lambda: "bot")

    def _fetch(repo: str, author: str) -> list[dict[str, object]] | None:
        # First repo fails, second succeeds but is empty
        return None if repo == "gptme/gptme" else []

    monkeypatch.setattr(pr_greptile_trigger, "fetch_prs", _fetch)
    monkeypatch.setattr(
        pr_greptile_trigger,
        "resolve_repos",
        lambda arg: ["gptme/gptme", "gptme/gptme-contrib"],
    )
    args = pr_greptile_trigger._parse_args([])
    assert pr_greptile_trigger._run(args) == 2
    captured = capsys.readouterr()
    assert "1/2" in captured.err or "missed" in captured.err.lower()


def test_main_partial_fetch_with_prs_warns_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Partial fetch failure warns on stderr even when other repos returned PRs."""
    monkeypatch.setattr(pr_greptile_trigger, "get_gh_user", lambda: "bot")

    def _fetch(repo: str, author: str) -> list[dict[str, object]] | None:
        # First repo fails, second returns a PR
        if repo == "gptme/gptme":
            return None
        return [
            {
                "number": 1,
                "title": "test PR",
                "url": "https://github.com/gptme/gptme-contrib/pull/1",
            }
        ]

    monkeypatch.setattr(pr_greptile_trigger, "fetch_prs", _fetch)
    monkeypatch.setattr(
        pr_greptile_trigger,
        "resolve_repos",
        lambda arg: ["gptme/gptme", "gptme/gptme-contrib"],
    )
    monkeypatch.setattr(
        pr_greptile_trigger,
        "review_state_for_pr",
        lambda repo, num: "already-reviewed",
    )
    args = pr_greptile_trigger._parse_args([])
    rc = pr_greptile_trigger._run(args)
    captured = capsys.readouterr()
    # Should NOT return 2 (PRs were found), but must warn about the missed repo
    assert rc == 0
    assert "warn" in captured.err.lower() or "missed" in captured.err.lower()
    assert "1/2" in captured.err


# --- initial-review fallback ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 45),
        ("", 45),
        ("abc", 45),
        ("45.5", 45),
        ("-1", 45),
        ("+1", 45),
        (" 1 ", 45),
        ("٤٥", 45),
        ("0", 0),
        ("60", 60),
        ("045", 45),
    ],
)
def test_grace_setting_matches_helper(value: str | None, expected: int) -> None:
    """Import must survive invalid settings and accept only ASCII digit strings."""
    env = {k: v for k, v in os.environ.items() if k != "GREPTILE_INITIAL_GRACE_MINS"}
    if value is not None:
        env["GREPTILE_INITIAL_GRACE_MINS"] = value
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; "
            "print(runpy.run_path(sys.argv[1])['INITIAL_REVIEW_GRACE_MINUTES'])",
            str(MODULE_PATH),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected)


def test_status_distinguishes_initial_review_due_from_waiting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recent = pr_greptile_trigger.datetime.now(
        pr_greptile_trigger.timezone.utc
    ).isoformat()
    monkeypatch.setattr(pr_greptile_trigger, "INITIAL_REVIEW_GRACE_MINUTES", 45)
    monkeypatch.setattr(
        pr_greptile_trigger,
        "fetch_prs",
        lambda repo, author: [
            {"number": i, "title": "t", "url": "u", "createdAt": created}
            for i, created in enumerate(["2020-01-01T00:00:00Z", recent, None], 1)
        ],
    )
    monkeypatch.setattr(
        pr_greptile_trigger,
        "review_state_for_pr",
        lambda repo, num: "awaiting-initial-review",
    )
    trigger = MagicMock()
    monkeypatch.setattr(pr_greptile_trigger, "trigger_greptile", trigger)
    args = pr_greptile_trigger._parse_args(
        ["--status", "--author", "bot", "--repo", "o/r"]
    )
    assert pr_greptile_trigger._run(args) == 0
    output = capsys.readouterr().out
    assert "Actionable: 1" in output
    assert "Awaiting initial: 2" in output
    assert "o/r#1: 🆕 initial review due" in output
    for number in (2, 3):
        assert f"o/r#{number}: ⏳ awaiting Greptile auto-review" in output
    trigger.assert_not_called()


def _pr(state: str, created_at: str | None) -> object:
    return pr_greptile_trigger.PRInfo(
        repo="gptme/gptme-contrib",
        number=1,
        title="t",
        url="u",
        review_state=state,
        created_at=created_at,
    )


def test_re_review_states_are_always_actionable() -> None:
    for state in ("stale", "needs-re-review"):
        assert pr_greptile_trigger.is_actionable(_pr(state, None)) is True


def test_awaiting_initial_within_grace_is_not_actionable() -> None:
    recent = "2026-11-22T00:30:00Z"
    now = pr_greptile_trigger.datetime(
        2026, 11, 22, 0, 40, tzinfo=pr_greptile_trigger.timezone.utc
    )
    assert (
        pr_greptile_trigger.is_actionable(_pr("awaiting-initial-review", recent), now)
        is False
    )


def test_awaiting_initial_past_grace_is_actionable() -> None:
    old = "2026-11-21T08:04:44Z"
    now = pr_greptile_trigger.datetime(
        2026, 11, 22, 0, 53, tzinfo=pr_greptile_trigger.timezone.utc
    )
    assert (
        pr_greptile_trigger.is_actionable(_pr("awaiting-initial-review", old), now)
        is True
    )


def test_awaiting_initial_without_timestamp_is_not_actionable() -> None:
    """Fail safe against spam when the PR age is unknown."""
    assert (
        pr_greptile_trigger.is_actionable(_pr("awaiting-initial-review", None)) is False
    )


def test_none_state_is_never_actionable() -> None:
    old = "2026-11-21T08:04:44Z"
    now = pr_greptile_trigger.datetime(
        2026, 11, 22, 0, 53, tzinfo=pr_greptile_trigger.timezone.utc
    )
    assert pr_greptile_trigger.is_actionable(_pr("none", old), now) is False


def test_main_triggers_stale_initial_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreviewed PR past the grace window is triggered by the batch caller."""
    triggered: list[int] = []

    monkeypatch.setattr(pr_greptile_trigger, "get_gh_user", lambda: "bot")
    monkeypatch.setattr(
        pr_greptile_trigger,
        "fetch_prs",
        lambda repo, author: [
            {
                "number": 2262,
                "title": "old unreviewed",
                "url": "u",
                "repo": repo,
                "createdAt": "2020-01-01T00:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        pr_greptile_trigger,
        "review_state_for_pr",
        lambda repo, num: "awaiting-initial-review",
    )

    def _trigger(repo: str, num: int) -> tuple[bool, str]:
        triggered.append(num)
        return True, "ok"

    monkeypatch.setattr(pr_greptile_trigger, "trigger_greptile", _trigger)
    monkeypatch.setattr(
        pr_greptile_trigger, "resolve_repos", lambda arg: ["gptme/gptme-contrib"]
    )

    args = pr_greptile_trigger._parse_args(["--execute"])
    assert pr_greptile_trigger._run(args) == 0
    assert triggered == [2262]
