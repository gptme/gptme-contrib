"""Capacity-aware fallback routing for multi-subscription credential rotation.

Selects fallback subscriptions based on pressure, urgency (soonest reset),
and capacity headroom, with stale-observation handling.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

# Default thresholds — callers can override via keyword arguments.
UNKNOWN_FALLBACK_PRESSURE: float = 0.50
SOON_TO_EXPIRE_THRESHOLD: int = 12 * 3600  # 12 hours
EXPIRING_CAPACITY_CREDIT: float = 0.25
CAPACITY_REBALANCE_MIN_PRESSURE: float = 0.70
CAPACITY_REBALANCE_MARGIN: float = 0.25


def capacity_aware_fallback_order(
    fallback_order: list[str],
    observations: dict[str, dict],
    now: datetime | None = None,
    unknown_pressure: float = UNKNOWN_FALLBACK_PRESSURE,
    soon_to_expire_threshold: int = SOON_TO_EXPIRE_THRESHOLD,
    expiring_capacity_credit: float = EXPIRING_CAPACITY_CREDIT,
) -> list[str]:
    """Return fallback_order sorted by pressure first, expiring capacity second.

    Unknown usage gets a neutral pressure score so stale/missing observations do
    not look better than a known low-pressure slot. Soon-to-expire capacity gets
    a bounded credit, enough to prefer moderately used expiring quota but not
    enough to keep hammering an 80% slot when another sits around 20%.
    """
    from subscription_manager.observation import _remaining_until_observed_reset
    from subscription_manager.pressure import _pressure_from_observation

    current_time = now or datetime.now(timezone.utc)

    def score(sub: str) -> tuple[float, float]:
        entry = observations.get(sub, {})
        pressure = _pressure_from_observation(entry, current_time)
        if pressure is None:
            pressure = unknown_pressure
        remaining = _remaining_until_observed_reset(entry, current_time)
        if remaining is None:
            return pressure, float("inf")
        expiry_credit = 0.0
        if remaining < soon_to_expire_threshold:
            expiry_credit = expiring_capacity_credit * (
                1.0 - (remaining / soon_to_expire_threshold)
            )
        rounded_remaining = round(remaining / 3600) * 3600
        return pressure - expiry_credit, rounded_remaining

    return sorted(fallback_order, key=score)


def best_lower_pressure_fallback(
    active: str,
    active_usage: dict,
    fallback_order: list[str],
    observations: dict[str, dict],
    now: datetime | None = None,
    min_pressure: float = CAPACITY_REBALANCE_MIN_PRESSURE,
    margin: float = CAPACITY_REBALANCE_MARGIN,
) -> tuple[str, float, float] | None:
    """Return another fallback with materially lower pressure than active.

    Returns:
        (fallback_name, active_pressure, fallback_pressure) or None.
    """
    from subscription_manager.pressure import (
        _pressure_from_observation,  # noqa: F811
        subscription_pressure_from_usage,
    )

    current_time = now or datetime.now(timezone.utc)
    active_pressure = subscription_pressure_from_usage(active_usage)
    if active_pressure is None or active_pressure < min_pressure:
        return None

    best: tuple[str, float] | None = None
    for sub in capacity_aware_fallback_order(
        fallback_order, observations, now=current_time
    ):
        if sub == active:
            continue
        pressure = _pressure_from_observation(observations.get(sub, {}), current_time)
        if pressure is None:
            continue
        if active_pressure - pressure < margin:
            continue
        if best is None or pressure < best[1]:
            best = (sub, pressure)

    if best is None:
        return None
    return best[0], active_pressure, best[1]


def soonest_resetting_fallback(
    fallback_order: list[str],
    reset_times_path: Path,
    now: datetime | None = None,
) -> list[str]:
    """Return fallback_order re-sorted by urgency: soonest reset first.

    Reads cached observations from ``reset_times_path``. Subs with no known
    reset time are placed last (assume their reset is far away). This ensures
    we prefer consuming soon-to-expire capacity over fresh capacity when the
    primary sub is exhausted.
    """

    from subscription_manager.observation import load_sub_observations

    current_time = now or datetime.now(timezone.utc)
    observations = load_sub_observations(reset_times_path)

    def urgency_key(sub: str) -> float:
        entry = observations.get(sub)
        if not entry:
            return float("inf")
        try:
            observed_at = datetime.fromisoformat(str(entry["observed_at"]))
            resets_in = float(entry["resets_in_seconds"])
            reset_at = observed_at + timedelta(seconds=resets_in)
            remaining = (reset_at - current_time).total_seconds()
            if remaining <= 0:
                return float("inf")
            return round(remaining / 3600) * 3600
        except (KeyError, ValueError, TypeError):
            return float("inf")

    return sorted(fallback_order, key=urgency_key)


def seconds_since_last_switch_to(
    sub: str,
    switch_log_path: Path,
    now: datetime | None = None,
) -> int | None:
    """How many seconds since the last switch TO a named sub.

    Returns None if no switch log exists or no matching entry is found.
    """
    current_time = now or datetime.now(timezone.utc)
    if not switch_log_path.exists():
        return None
    try:
        lines = switch_log_path.read_text().strip().split("\n")
        for line in reversed(lines):
            if not line.strip():
                continue
            if f"switched to {sub}" not in line:
                continue
            ts_str = line.split(" ")[0]
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return int((current_time - ts).total_seconds())
    except (ValueError, IndexError):
        pass
    return None
