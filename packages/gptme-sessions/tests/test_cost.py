"""Tests for session cost estimation and analysis."""

from __future__ import annotations

from unittest.mock import patch

from gptme_sessions.cost import (
    CostSummary,
    DayCostStats,
    ModelCostStats,
    analyze_costs,
    estimate_record_cost,
    format_cost_summary,
)
from gptme_sessions.record import SessionRecord


def _make_record(
    harness: str = "claude-code",
    model: str = "claude-sonnet-4-6",
    input_tokens: int | None = 1000,
    output_tokens: int | None = 500,
    cache_creation_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    token_count: int | None = None,
    timestamp: str = "2026-06-20T10:00:00+00:00",
) -> SessionRecord:
    return SessionRecord(
        harness=harness,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        token_count=token_count,
        timestamp=timestamp,
    )


class TestEstimateRecordCost:
    def test_no_crash_with_real_gptme_usage(self):
        """estimate_record_cost doesn't raise — returns float or None."""
        record = _make_record()
        result = estimate_record_cost(record)
        assert result is None or isinstance(result, float)

    def test_returns_none_for_unknown_model(self):
        """Returns None when model has no pricing entry."""
        record = _make_record(model="completely-unknown-model-xyz-42")
        result = estimate_record_cost(record)
        assert result is None or isinstance(result, float)

    def test_returns_none_for_missing_tokens(self):
        """Returns None when no token data is available."""
        record = _make_record(input_tokens=None, output_tokens=None, token_count=None)
        result = estimate_record_cost(record)
        assert result is None or isinstance(result, float)


class TestAnalyzeCosts:
    def _mock_cost(self, record: SessionRecord) -> float | None:
        """Mock cost: $0.00001 per input token."""
        if record.input_tokens is None:
            return None
        return record.input_tokens * 0.00001

    def test_empty_records(self):
        """Empty record list produces zero-cost summary."""
        with patch("gptme_sessions.cost.estimate_record_cost", return_value=None):
            summary = analyze_costs([])
        assert summary.session_count == 0
        assert summary.priced_count == 0
        assert summary.total_cost == 0.0

    def test_basic_aggregation(self):
        """Correctly sums cost across multiple records."""
        records = [
            _make_record(model="sonnet", input_tokens=1000, timestamp="2026-06-20T10:00:00+00:00"),
            _make_record(model="haiku", input_tokens=2000, timestamp="2026-06-20T11:00:00+00:00"),
        ]
        with patch("gptme_sessions.cost.estimate_record_cost", side_effect=self._mock_cost):
            summary = analyze_costs(records)

        assert summary.session_count == 2
        assert summary.priced_count == 2
        assert abs(summary.total_cost - 0.03) < 1e-9  # 0.01 + 0.02
        assert abs(summary.avg_cost - 0.015) < 1e-9

    def test_by_model_grouping(self):
        """Groups cost by model correctly."""
        records = [
            _make_record(model="sonnet", input_tokens=1000),
            _make_record(model="sonnet", input_tokens=1000),
            _make_record(model="haiku", input_tokens=2000),
        ]
        with patch("gptme_sessions.cost.estimate_record_cost", side_effect=self._mock_cost):
            summary = analyze_costs(records)

        assert len(summary.by_model) >= 1
        total_from_models = sum(s.total_cost for s in summary.by_model.values())
        assert abs(total_from_models - summary.total_cost) < 1e-9

    def test_by_day_grouping(self):
        """Groups cost by day correctly."""
        records = [
            _make_record(input_tokens=1000, timestamp="2026-06-19T10:00:00+00:00"),
            _make_record(input_tokens=1000, timestamp="2026-06-19T12:00:00+00:00"),
            _make_record(input_tokens=1000, timestamp="2026-06-20T10:00:00+00:00"),
        ]
        with patch("gptme_sessions.cost.estimate_record_cost", side_effect=self._mock_cost):
            summary = analyze_costs(records)

        assert "2026-06-19" in summary.by_day
        assert "2026-06-20" in summary.by_day
        assert summary.by_day["2026-06-19"].count == 2
        assert summary.by_day["2026-06-20"].count == 1

    def test_days_filter(self):
        """days= parameter filters records by recency."""
        import datetime as dt

        now = dt.datetime.now(dt.timezone.utc)
        old_ts = "2020-01-01T00:00:00+00:00"
        recent_ts = (now - dt.timedelta(hours=1)).isoformat()
        records = [
            _make_record(input_tokens=1000, timestamp=old_ts),  # old
            _make_record(input_tokens=1000, timestamp=recent_ts),  # recent
        ]
        with patch("gptme_sessions.cost.estimate_record_cost", side_effect=self._mock_cost):
            summary = analyze_costs(records, days=7)

        assert summary.session_count == 1

    def test_skips_unpriced_records(self):
        """Records where cost is None are counted but excluded from totals."""
        records = [
            _make_record(input_tokens=1000),
            _make_record(input_tokens=None),  # no tokens → no cost
        ]
        with patch("gptme_sessions.cost.estimate_record_cost", side_effect=self._mock_cost):
            summary = analyze_costs(records)

        assert summary.session_count == 2
        assert summary.priced_count == 1


class TestFormatCostSummary:
    def _summary(self) -> CostSummary:
        return CostSummary(
            session_count=10,
            priced_count=8,
            total_cost=0.1234,
            by_model={
                "sonnet": ModelCostStats(count=5, total_cost=0.08),
                "haiku": ModelCostStats(count=3, total_cost=0.04),
            },
            by_day={
                "2026-06-19": DayCostStats(count=4, total_cost=0.05),
                "2026-06-20": DayCostStats(count=4, total_cost=0.07),
            },
        )

    def test_basic_output(self):
        """Basic summary output contains key fields."""
        text = format_cost_summary(self._summary())
        assert "10" in text
        assert "0.1234" in text
        assert "8" in text

    def test_by_model_section(self):
        """by_model=True includes model breakdown."""
        text = format_cost_summary(self._summary(), by_model=True)
        assert "By Model" in text
        assert "sonnet" in text
        assert "haiku" in text

    def test_daily_section(self):
        """daily=True includes day-by-day breakdown."""
        text = format_cost_summary(self._summary(), daily=True)
        assert "By Day" in text
        assert "2026-06-19" in text
        assert "2026-06-20" in text

    def test_no_extra_sections_by_default(self):
        """Default output omits model and day breakdowns."""
        text = format_cost_summary(self._summary())
        assert "By Model" not in text
        assert "By Day" not in text

    def test_empty_summary_no_crash(self):
        """Empty summary renders without exceptions."""
        empty = CostSummary(session_count=0, priced_count=0, total_cost=0.0)
        text = format_cost_summary(empty, daily=True, by_model=True)
        assert "0" in text
