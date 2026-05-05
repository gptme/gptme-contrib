"""Quota window pacing and rebalance timing utilities."""

from __future__ import annotations


def format_duration(seconds: int) -> str:
    """Format a duration in seconds into a compact human-readable string."""
    if seconds <= 0:
        return "0m"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h{minutes:02d}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def compute_window_pacing(
    utilization: float, resets_in_seconds: int, window_seconds: int
) -> tuple[float, float, str] | None:
    """Compute pacing (target, gap, status) for a quota window.

    Uses the headroom model: if remaining budget is less than remaining time,
    we're overusing. Returns None if inputs are insufficient.

    Returns:
        (elapsed_frac, gap, status) where gap > 0 = overusing, gap < 0 = underusing.
    """
    if window_seconds <= 0 or resets_in_seconds <= 0:
        return None
    remaining_time_frac = min(1.0, resets_in_seconds / window_seconds)
    elapsed_frac = 1.0 - remaining_time_frac
    gap = utilization - elapsed_frac
    if gap > 0.05:
        status = "overusing"
    elif gap < -0.05:
        status = "underusing"
    else:
        status = "on_track"
    return elapsed_frac, gap, status


def compute_rebalance_hold_seconds(
    pace_overage: float,
    target_utilization: float = 0.90,
    min_hold: int = 6 * 3600,
    max_hold: int = 48 * 3600,
) -> int:
    """Estimate how long to rest a sub until its pacing target catches up.

    Args:
        pace_overage: How far ahead of target pace the sub is (positive = overusing).
        target_utilization: Target fraction of the quota window to use.
        min_hold: Minimum hold time in seconds.
        max_hold: Maximum hold time in seconds.
    """
    if pace_overage <= 0:
        return min_hold
    weekly_window_seconds = 7 * 24 * 3600
    catch_up_seconds = int(pace_overage * weekly_window_seconds / target_utilization)
    return max(min_hold, min(max_hold, catch_up_seconds))
