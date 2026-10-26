"""Human reports preserve host selection and coverage through both output paths."""

from datetime import date
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

import gptme_activity_summary.cc_backend as backend
import gptme_activity_summary.cli as cli_module
from gptme_activity_summary.aw_data import AppUsage, AWActivity
from gptme_activity_summary.github_data import GitHubActivity, RepoActivity


@pytest.fixture
def models(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock]:
    daily = Mock(return_value={"narrative": "Daily report"})
    github = Mock(return_value={"narrative": "GitHub report"})
    monkeypatch.setattr(backend, "summarize_human_day_with_cc", daily)
    monkeypatch.setattr(backend, "summarize_github_activity_with_cc", github)
    return daily, github


@pytest.mark.parametrize(
    ("command", "period_args", "start", "end"),
    [
        ("daily", ["--date", "2026-10-20"], date(2026, 10, 20), date(2026, 10, 20)),
        ("weekly", ["--week", "2026-W43"], date(2026, 10, 19), date(2026, 10, 25)),
        ("monthly", ["--month", "2026-10"], date(2026, 10, 1), date(2026, 10, 31)),
    ],
)
@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("github_user", [None, "alice"])
def test_human_commands_preserve_host_and_coverage(
    monkeypatch: pytest.MonkeyPatch,
    models: tuple[Mock, Mock],
    command: str,
    period_args: list[str],
    start: date,
    end: date,
    raw: bool,
    github_user: str | None,
) -> None:
    warning = "No AFK bucket for laptop; window time is unfiltered."
    activity = AWActivity(
        start_date=start,
        end_date=end,
        available=True,
        hostname="laptop",
        warnings=[warning],
        afk_filtered=False,
        total_active_seconds=3600,
        top_apps=[AppUsage(app="Terminal", duration=3600)],
    )
    fetch = Mock(return_value=activity)
    monkeypatch.setattr(cli_module, "fetch_aw_activity", fetch)
    fetch_github = Mock(return_value=GitHubActivity(start_date=start, end_date=end))
    monkeypatch.setattr(cli_module, "fetch_user_activity", fetch_github)
    args = ["--verbose", command, "--mode", "human", "--aw-host", "laptop", *period_args]
    if raw:
        args.append("--raw")
    if github_user:
        args.extend(["--github-user", github_user])

    result = CliRunner().invoke(cli_module.cli, args)

    assert result.exit_code == 0, result.output
    fetch.assert_called_once_with(start, end, hostname="laptop")
    if github_user:
        fetch_github.assert_called_once_with(start, end, github_user)
    else:
        fetch_github.assert_not_called()
    assert "1.0h unfiltered window time" in result.output
    assert "1.0h active" not in result.output
    assert warning in result.output
    daily_model, github_model = models
    if raw:
        daily_model.assert_not_called()
        github_model.assert_not_called()
        context = result.output
    else:
        selected = github_model if github_user and command != "daily" else daily_model
        other = daily_model if selected is github_model else github_model
        selected.assert_called_once()
        other.assert_not_called()
        context = selected.call_args.args[0]
    assert "laptop" in context
    assert warning in context
    assert "Terminal" in context


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize(
    ("host", "warning"),
    [
        (None, "Multiple window hosts: desktop, laptop. Select one with --aw-host."),
        ("unknown", "No window bucket for unknown. Available hosts: desktop, laptop."),
    ],
)
def test_host_diagnostics_without_activity_skip_model(
    monkeypatch: pytest.MonkeyPatch,
    models: tuple[Mock, Mock],
    raw: bool,
    host: str | None,
    warning: str,
) -> None:
    start = date(2026, 10, 20)
    fetch = Mock(
        return_value=AWActivity(
            start_date=start,
            end_date=start,
            available=True,
            hostname=host,
            warnings=[warning],
        )
    )
    monkeypatch.setattr(cli_module, "fetch_aw_activity", fetch)
    monkeypatch.setattr(
        cli_module,
        "fetch_user_activity",
        Mock(return_value=GitHubActivity(start_date=start, end_date=start)),
    )
    args = ["daily", "--mode", "human", "--date", str(start), "--github-user", "alice"]
    if host:
        args.extend(["--aw-host", host])
    if raw:
        args.append("--raw")

    result = CliRunner().invoke(cli_module.cli, args)

    assert result.exit_code == 0, result.output
    fetch.assert_called_once_with(start, start, hostname=host)
    assert warning in result.output
    assert "No activity data found" in result.output
    assert "server not reachable" not in result.output
    for model in models:
        model.assert_not_called()


def test_github_report_keeps_aw_diagnostics_without_aw_apps(
    monkeypatch: pytest.MonkeyPatch,
    models: tuple[Mock, Mock],
) -> None:
    start = date(2026, 10, 20)
    warning = "Multiple window buckets for laptop; window data skipped."
    monkeypatch.setattr(
        cli_module,
        "fetch_aw_activity",
        Mock(
            return_value=AWActivity(
                start_date=start,
                end_date=start,
                hostname="laptop",
                available=True,
                warnings=[warning],
            )
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "fetch_user_activity",
        Mock(
            return_value=GitHubActivity(
                start_date=start,
                end_date=start,
                repos=[RepoActivity(repo="alice/project", commits=1)],
            )
        ),
    )

    result = CliRunner().invoke(
        cli_module.cli,
        ["daily", "--mode", "human", "--date", str(start), "--github-user", "alice"],
    )

    assert result.exit_code == 0, result.output
    assert warning in result.output
    models[0].assert_called_once()
    context = models[0].call_args.args[0]
    assert "laptop" in context
    assert warning in context
    assert "alice/project" in context
    models[1].assert_not_called()
