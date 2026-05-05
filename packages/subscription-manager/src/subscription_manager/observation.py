"""Quota observation persistence for fallback subscription pressure tracking.

Stores per-subscription reset-time and utilization snapshots so the
routing layer can make urgency-aware decisions without live API probes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _remaining_until_observed_reset(entry: dict, now: datetime) -> float | None:
    """Seconds until the observed quota period resets, or None if unknown/past."""
    try:
        observed_at = datetime.fromisoformat(str(entry["observed_at"]))
        resets_in = float(entry["resets_in_seconds"])
    except (KeyError, ValueError, TypeError):
        return None
    reset_at = observed_at + timedelta(seconds=resets_in)
    remaining = (reset_at - now).total_seconds()
    return remaining if remaining > 0 else None


def load_sub_observations(
    reset_times_path: Path,
) -> dict[str, dict]:
    """Load cached per-subscription observation data from disk."""
    try:
        if reset_times_path.exists():
            data = json.loads(reset_times_path.read_text())
            if isinstance(data, dict):
                return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def record_sub_reset_time(
    sub: str,
    resets_in_seconds: float,
    reset_times_path: Path,
    usage: dict | None = None,
    now: datetime | None = None,
) -> None:
    """Persist the observed weekly reset time and usage for a subscription.

    Called whenever we have live usage data for a non-primary sub so that
    future calls — potentially while on a different sub — can estimate when
    each fallback's period ends and how pressured it was without probing live.

    Args:
        sub: Subscription name (e.g. "alice", "erik").
        resets_in_seconds: Seconds until the weekly quota period resets.
        reset_times_path: Path to the JSON observation state file.
        usage: Optional full usage snapshot for pressure scoring.
        now: Current time for the observation timestamp.
    """
    current_time = now or datetime.now(timezone.utc)
    try:
        data: dict[str, object] = {}
        if reset_times_path.exists():
            data = json.loads(reset_times_path.read_text())
        entry: dict[str, object] = {
            "observed_at": current_time.isoformat(),
            "resets_in_seconds": int(resets_in_seconds),
        }
        if usage is not None:
            # Import here to avoid circular dependency
            from subscription_manager.pressure import (
                subscription_pressure_from_usage,
            )

            weekly = usage.get("seven_day", {})
            five_hour = usage.get("five_hour", {})
            sonnet = usage.get("seven_day_sonnet", {})
            for key, source_key, source in (
                ("weekly_utilization", "utilization", weekly),
                ("five_hour_utilization", "utilization", five_hour),
                ("sonnet_weekly_utilization", "utilization", sonnet),
                ("five_hour_resets_in_seconds", "resets_in_seconds", five_hour),
                ("sonnet_resets_in_seconds", "resets_in_seconds", sonnet),
            ):
                value = source.get(source_key)
                if isinstance(value, int | float):
                    entry[key] = float(value)
            pressure = subscription_pressure_from_usage(usage)
            if pressure is not None:
                entry["pressure"] = round(pressure, 3)
        data[sub] = entry
        reset_times_path.parent.mkdir(parents=True, exist_ok=True)
        reset_times_path.write_text(json.dumps(data, indent=2) + "\n")
    except (OSError, json.JSONDecodeError):
        pass
