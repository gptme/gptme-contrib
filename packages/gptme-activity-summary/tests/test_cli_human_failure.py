"""Human CLI failures propagate the real summarizer's backend failure marker."""

import json
from datetime import date
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

import gptme_activity_summary.cc_backend as backend
import gptme_activity_summary.cli as cli_module
from gptme_activity_summary.aw_data import AppUsage, AWActivity
from gptme_activity_summary.github_data import GitHubActivity, RepoActivity

DAY = date(2026, 10, 21)


@pytest.fixture(params=["daily", "weekly", "monthly"])
def human_command(request):
    period_args = {
        "daily": ["--date", str(DAY)],
        "weekly": ["--week", "2026-W43"],
        "monthly": ["--month", "2026-10"],
    }
    return [request.param, "--mode", "human", *period_args[request.param]]


@pytest.fixture(params=[False, True])
def args(human_command, request):
    return human_command + (["--github-user", "alice"] if request.param else [])


@pytest.fixture
def cc_call(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "fetch_aw_activity",
        Mock(
            return_value=AWActivity(
                start_date=DAY,
                end_date=DAY,
                available=True,
                hostname="laptop",
                top_apps=[AppUsage(app="Code", duration=3600)],
                total_active_seconds=3600,
                afk_filtered=True,
            )
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "fetch_user_activity",
        Mock(
            return_value=GitHubActivity(
                start_date=DAY,
                end_date=DAY,
                repos=[RepoActivity(repo="alice/project", commits=1)],
            )
        ),
    )
    call = Mock(return_value="")
    monkeypatch.setattr(backend, "call_claude_code", call)
    return call


def test_empty_backend_response_exits_nonzero_before_summary_heading(args, cc_call):
    result = CliRunner().invoke(cli_module.cli, args)

    cc_call.assert_called_once()
    assert result.exit_code != 0, result.output
    assert "Error:" in result.output
    assert "--raw" in result.output
    assert "## Daily Summary" not in result.output
    assert "## Activity Summary" not in result.output
    assert "### Stats" not in result.output


def test_valid_backend_response_still_renders_summary(args, cc_call):
    cc_call.return_value = json.dumps(
        {"narrative": "Finished the parser repair.", "highlights": ["Fixed interval clipping."]}
    )
    result = CliRunner().invoke(cli_module.cli, args)

    assert result.exit_code == 0, result.output
    cc_call.assert_called_once()
    assert "Finished the parser repair." in result.output
    assert "Fixed interval clipping." in result.output
    assert "## Activity Summary" in result.output or "## Daily Summary" in result.output
    assert "Error:" not in result.output


def test_raw_report_succeeds_without_calling_backend(args, cc_call):
    result = CliRunner().invoke(cli_module.cli, [*args, "--raw"])

    assert result.exit_code == 0, result.output
    cc_call.assert_not_called()
    assert "Host: laptop" in result.output
    assert "Code" in result.output
    assert "Generating summary with Claude Code" not in result.output


def test_empty_activity_succeeds_without_calling_backend(args, cc_call, monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "fetch_aw_activity",
        Mock(return_value=AWActivity(start_date=DAY, end_date=DAY, available=True)),
    )
    monkeypatch.setattr(
        cli_module,
        "fetch_user_activity",
        Mock(return_value=GitHubActivity(start_date=DAY, end_date=DAY)),
    )
    result = CliRunner().invoke(cli_module.cli, args)

    assert result.exit_code == 0, result.output
    cc_call.assert_not_called()
    assert "No activity data found" in result.output
    assert "Generating summary with Claude Code" not in result.output
