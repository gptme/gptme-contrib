"""Tests for the activity summary CLI."""

from typing import NoReturn

import pytest
from click.testing import CliRunner, Result

import gptme_activity_summary.cli as cli_module


def _invoke_smart(date_str: str) -> Result:
    return CliRunner().invoke(
        cli_module.cli,
        ["--dry-run", "smart", "--date", date_str],
    )


def test_smart_daily_failure_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_journal_entries_for_date", lambda _date: ["entry"])

    def fail_daily(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("daily boom")

    monkeypatch.setattr(cli_module, "generate_daily_with_cc", fail_daily)

    result = _invoke_smart("2026-08-18")

    assert result.exit_code == 1
    assert "Daily: Failed - daily boom" in result.output
    assert "=== Summary ===" in result.output
    assert "  daily: FAILED" in result.output


@pytest.mark.parametrize("failing_period", ["weekly", "monthly"])
def test_smart_due_period_failure_runs_all_due_periods(
    monkeypatch: pytest.MonkeyPatch,
    failing_period: str,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli_module, "get_journal_entries_for_date", lambda _date: [])

    def generate_weekly(*_args: object, **_kwargs: object) -> object:
        calls.append("weekly")
        if failing_period == "weekly":
            raise RuntimeError("weekly boom")
        return object()

    def generate_monthly(*_args: object, **_kwargs: object) -> object:
        calls.append("monthly")
        if failing_period == "monthly":
            raise RuntimeError("monthly boom")
        return object()

    monkeypatch.setattr(cli_module, "generate_weekly_summary_cc", generate_weekly)
    monkeypatch.setattr(cli_module, "generate_monthly_summary_cc", generate_monthly)

    result = _invoke_smart("2026-06-01")

    successful_period = "monthly" if failing_period == "weekly" else "weekly"
    assert result.exit_code == 1
    assert calls == ["weekly", "monthly"]
    assert f"{failing_period.capitalize()}: Failed - {failing_period} boom" in result.output
    assert f"  {failing_period}: FAILED" in result.output
    assert f"  {successful_period}: OK" in result.output


def test_smart_skipped_and_not_due_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_journal_entries_for_date", lambda _date: [])

    def unexpected_generation(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("no generator should run")

    monkeypatch.setattr(cli_module, "generate_daily_with_cc", unexpected_generation)
    monkeypatch.setattr(cli_module, "generate_weekly_summary_cc", unexpected_generation)
    monkeypatch.setattr(cli_module, "generate_monthly_summary_cc", unexpected_generation)

    result = _invoke_smart("2026-06-02")

    assert result.exit_code == 0
    assert "Daily: No entries for 2026-06-02" in result.output
    assert "Weekly: Not due (not Monday)" in result.output
    assert "Monthly: Not due (not 1st of month)" in result.output
    assert "  daily: skipped" in result.output
    assert "FAILED" not in result.output
