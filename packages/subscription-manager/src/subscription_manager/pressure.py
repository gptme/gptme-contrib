"""Subscription pressure scoring and exhaustion detection."""

from __future__ import annotations


def subscription_pressure_from_usage(usage: dict) -> float | None:
    """Return a compact pressure score for a subscription usage snapshot.

    Weekly overall and Sonnet weekly limits are the durable pressure signals.
    The 5h window only counts when it will remain tight for more than two
    hours; a short-reset 5h spike should not drive multi-day slot routing.
    """
    components: list[float] = []
    weekly = usage.get("seven_day", {}).get("utilization")
    if isinstance(weekly, int | float):
        components.append(float(weekly))

    sonnet_weekly = usage.get("seven_day_sonnet", {}).get("utilization")
    if isinstance(sonnet_weekly, int | float):
        components.append(float(sonnet_weekly))

    five_hour = usage.get("five_hour", {}).get("utilization")
    five_hour_resets = usage.get("five_hour", {}).get("resets_in_seconds", 0)
    if (
        isinstance(five_hour, int | float)
        and isinstance(five_hour_resets, int | float)
        and five_hour_resets > 7200
    ):
        components.append(float(five_hour))

    if not components:
        return None
    return max(components)


def is_subscription_blocked(
    usage: dict,
    weekly_exhausted: float = 0.85,
    five_hour_exhausted: float = 0.90,
    sonnet_weekly_exhausted: float = 0.95,
) -> tuple[bool, str]:
    """Check if a subscription's quota is too exhausted to use.

    Three independent limits exist — any one being exhausted blocks the sub:
    - Overall weekly + 5h both exhausted → no Opus capacity left
    - Sonnet weekly exhausted → project-monitoring and Sonnet sessions break
    - Sonnet data missing + high weekly → assume Sonnet exhausted (conservative)

    Returns:
        (blocked, reason_string).
    """
    weekly = usage.get("seven_day", {}).get("utilization", 0)
    five_hour = usage.get("five_hour", {}).get("utilization", 0)
    sonnet_weekly = usage.get("seven_day_sonnet", {}).get("utilization", 0)
    has_sonnet_data = "seven_day_sonnet" in usage

    reasons: list[str] = []

    # Opus blocked: both overall windows high
    if weekly >= weekly_exhausted and five_hour >= five_hour_exhausted:
        reasons.append(f"Opus exhausted ({weekly:.0%}w/{five_hour:.0%}5h)")

    # Sonnet blocked: independent weekly limit (different reset schedule!)
    if has_sonnet_data and sonnet_weekly >= sonnet_weekly_exhausted:
        reasons.append(f"Sonnet exhausted ({sonnet_weekly:.0%})")
    elif not has_sonnet_data and weekly >= weekly_exhausted:
        reasons.append("Sonnet data missing, weekly high — assuming Sonnet exhausted")

    blocked = len(reasons) > 0
    return blocked, "; ".join(reasons) if reasons else "all limits healthy"


def _pressure_from_observation(
    entry: dict,
    now: datetime,  # type: ignore[name-defined]  # noqa: F821
) -> float | None:
    """Compute pressure from a stored observation entry."""
    from subscription_manager.observation import _remaining_until_observed_reset

    if _remaining_until_observed_reset(entry, now) is None:
        return None
    pressure = entry.get("pressure")
    if isinstance(pressure, int | float):
        return float(pressure)
    components = [
        entry.get("weekly_utilization"),
        entry.get("sonnet_weekly_utilization"),
    ]
    five_hour = entry.get("five_hour_utilization")
    five_hour_resets = entry.get("five_hour_resets_in_seconds", 0)
    if isinstance(five_hour_resets, int | float) and five_hour_resets > 7200:
        components.append(five_hour)
    numeric = [float(v) for v in components if isinstance(v, int | float)]
    return max(numeric) if numeric else None
