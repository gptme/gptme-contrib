"""Session cost estimation and analysis.

Requires ``gptme-usage`` (optional dependency):
    pip install gptme-sessions[cost]
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .record import SessionRecord


@dataclass
class ModelCostStats:
    count: int = 0
    total_cost: float = 0.0

    @property
    def avg_cost(self) -> float:
        return self.total_cost / self.count if self.count else 0.0


@dataclass
class DayCostStats:
    count: int = 0
    total_cost: float = 0.0


@dataclass
class CostSummary:
    """Aggregated cost statistics over a set of session records."""

    session_count: int
    priced_count: int  # records where cost could be estimated
    total_cost: float
    by_model: dict[str, ModelCostStats] = field(default_factory=dict)
    by_day: dict[str, DayCostStats] = field(default_factory=dict)

    @property
    def avg_cost(self) -> float:
        return self.total_cost / self.priced_count if self.priced_count else 0.0


def _load_pricing_config():
    """Load the pricing config once, returning the HarnessQuotaConfig or None."""
    try:
        from gptme_usage.harness_models import load_quota_config

        return load_quota_config()
    except Exception:
        return None


_UNSET = object()
_PRICING_CONFIG: object = _UNSET


def estimate_record_cost(record: "SessionRecord") -> float | None:
    """Estimate USD cost for a single session record.

    Returns None if gptme-usage is not installed, pricing is unknown,
    or the record lacks token data.
    """
    try:
        from gptme_usage.harness_models import estimate_session_cost
    except ImportError:
        return None

    global _PRICING_CONFIG
    if _PRICING_CONFIG is _UNSET:
        _PRICING_CONFIG = _load_pricing_config()

    harness = record.harness or "unknown"
    model = record.model or "unknown"

    result = estimate_session_cost(
        harness,
        model,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        cache_creation_tokens=record.cache_creation_tokens,
        cache_read_tokens=record.cache_read_tokens,
        token_count=record.token_count,
        config=_PRICING_CONFIG,
    )
    return float(result) if result is not None else None


def analyze_costs(
    records: list["SessionRecord"],
    days: int | None = None,
) -> CostSummary:
    """Analyze costs across a list of session records.

    Args:
        records: Session records to analyze.
        days: If set, only include records from the last N days.

    Returns:
        CostSummary with total cost, by-model breakdown, and by-day breakdown.
    """
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        filtered = []
        for r in records:
            if not r.timestamp:
                continue
            try:
                ts = datetime.fromisoformat(r.timestamp.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    filtered.append(r)
            except ValueError:
                pass
        records = filtered

    by_model: dict[str, ModelCostStats] = defaultdict(ModelCostStats)
    by_day: dict[str, DayCostStats] = defaultdict(DayCostStats)
    total_cost = 0.0
    priced_count = 0

    for record in records:
        cost = estimate_record_cost(record)
        model_key = record.model_normalized or record.model or "unknown"
        day_key = record.timestamp[:10] if record.timestamp else "unknown"

        if cost is not None:
            total_cost += cost
            priced_count += 1
            by_model[model_key].count += 1
            by_model[model_key].total_cost += cost
            by_day[day_key].count += 1
            by_day[day_key].total_cost += cost

    return CostSummary(
        session_count=len(records),
        priced_count=priced_count,
        total_cost=total_cost,
        by_model=dict(by_model),
        by_day=dict(sorted(by_day.items())),
    )


def format_cost_summary(summary: CostSummary, daily: bool = False, by_model: bool = False) -> str:
    """Format a CostSummary for human-readable output."""
    lines: list[str] = []

    lines.append("# Session Cost Summary")
    lines.append("")
    lines.append(f"Sessions: {summary.session_count} total, {summary.priced_count} with cost data")
    lines.append(f"Total cost: ${summary.total_cost:.4f}")
    if summary.priced_count:
        lines.append(f"Avg cost:   ${summary.avg_cost:.4f}")

    if by_model and summary.by_model:
        lines.append("")
        lines.append("## By Model")
        lines.append("")
        lines.append(f"{'Model':<30} {'Sessions':>8} {'Total':>10} {'Avg':>10}")
        lines.append("-" * 62)
        for model, mstats in sorted(
            summary.by_model.items(), key=lambda x: x[1].total_cost, reverse=True
        ):
            lines.append(
                f"{model:<30} {mstats.count:>8} ${mstats.total_cost:>9.4f} ${mstats.avg_cost:>9.4f}"
            )

    if daily and summary.by_day:
        lines.append("")
        lines.append("## By Day")
        lines.append("")
        lines.append(f"{'Date':<12} {'Sessions':>8} {'Total':>10}")
        lines.append("-" * 32)
        for day, dstats in summary.by_day.items():
            lines.append(f"{day:<12} {dstats.count:>8} ${dstats.total_cost:>9.4f}")

    return "\n".join(lines)
