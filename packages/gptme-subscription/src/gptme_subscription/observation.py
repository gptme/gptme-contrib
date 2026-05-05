"""Subscription observation and pressure scoring.

Generic helpers for tracking subscription quota usage, computing pressure
scores, and determining whether a subscription is blocked.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class SubscriptionObservation:
    """Persistent observation state for a single subscription."""

    track_resets: dict[str, str] = field(default_factory=dict)
    """Mapping of metric keys (e.g. 'seven_day', 'seven_day_sonnet') to ISO-8601
    timestamps of when they were last observed at a reset boundary."""

    metadata: dict[str, object] = field(default_factory=dict)
    """Arbitrary metadata (agent name, record version, etc.)."""


# --- Thresholds (configurable) ---

# Default exhaustion thresholds
DEFAULT_WEEKLY_EXHAUSTED = 0.95
DEFAULT_FIVE_HOUR_EXHAUSTED = 0.95
DEFAULT_SONNET_WEEKLY_EXHAUSTED = 0.85

# Default fallback routing constants
DEFAULT_UNKNOWN_FALLBACK_PRESSURE = 0.5
DEFAULT_SOON_TO_EXPIRE_THRESHOLD = 3600 * 4  # 4 hours
DEFAULT_EXPIRING_CAPACITY_CREDIT = 0.15
DEFAULT_CAPACITY_REBALANCE_MIN_PRESSURE = 0.3
DEFAULT_CAPACITY_REBALANCE_MARGIN = 0.1
DEFAULT_REBALANCE_MIN_HOLD = 600  # 10 minutes
DEFAULT_REBALANCE_MAX_HOLD = 7200  # 2 hours
DEFAULT_REBALANCE_TARGET_UTILIZATION = 0.85


# --- Utility ---


def format_duration(seconds: int) -> str:
    """Format a compact human-readable duration.

    >>> format_duration(0)
    '0m'
    >>> format_duration(3661)
    '1h01m'
    >>> format_duration(7200)
    '2h'
    >>> format_duration(1800)
    '30m'
    """
    if seconds <= 0:
        return "0m"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h{minutes:02d}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


# --- Subscription observation state ---


def record_sub_reset_time(
    obs_dir: Path,
    sub: str,
    metric_key: str,
    timestamp: str | None = None,
) -> None:
    """Record that a subscription's metric was just observed at a reset boundary.

    The observation directory is consumer-managed; different agents may use
    different paths (e.g. ``/home/bob/bob/state/backend-quota/``).

    Args:
        obs_dir: Directory where observation JSON files are stored.
        sub: Subscription name (e.g. ``bob``, ``alice``, ``erik``).
        metric_key: Which metric reset was observed (e.g. ``seven_day``).
        timestamp: ISO-8601 timestamp. Defaults to current UTC time.
    """
    obs_dir.mkdir(parents=True, exist_ok=True)
    obs_file = obs_dir / f"{sub}.json"
    entry: dict[str, dict[str, str]] = {}
    if obs_file.exists():
        try:
            raw = obs_file.read_text()
            if raw.strip():
                entry = json.loads(raw)
        except (json.JSONDecodeError, OSError):
            pass  # corrupt file — start fresh
    entry.setdefault("track_resets", {})[metric_key] = (
        timestamp or datetime.now(timezone.utc).isoformat()
    )
    obs_file.write_text(json.dumps(entry, indent=2) + "\n")


def load_sub_observations(
    obs_dir: Path,
) -> dict[str, SubscriptionObservation]:
    """Load all subscription observations from a directory.

    Args:
        obs_dir: Directory containing ``<sub>.json`` observation files.

    Returns:
        Dict mapping subscription name to its observation state.
    """
    observations: dict[str, SubscriptionObservation] = {}
    if not obs_dir.exists():
        return observations
    for f in sorted(obs_dir.iterdir()):
        if f.suffix != ".json":
            continue
        try:
            raw = f.read_text()
            if not raw.strip():
                continue
            data = json.loads(raw)
            sub = f.stem
            observations[sub] = SubscriptionObservation(
                track_resets=data.get("track_resets", {}),
                metadata=data.get("metadata", {}),
            )
        except (json.JSONDecodeError, OSError):
            continue
    return observations


def remaining_until_observed_reset(
    observation: SubscriptionObservation,
    metric_key: str,
    window_seconds: int,
    now: datetime | None = None,
) -> float | None:
    """Compute seconds until the next reset, given the last observed reset time.

    Args:
        observation: Observation state for a subscription.
        metric_key: Which metric's reset time to check.
        window_seconds: Duration of the rolling window (e.g. 7*24*3600 for weekly).
        now: Current time. Defaults to UTC now.

    Returns:
        Seconds until reset, or None if no observation exists.
    """
    current_time = now or datetime.now(timezone.utc)
    reset_str = observation.track_resets.get(metric_key)
    if not reset_str:
        return None
    try:
        reset_time = datetime.fromisoformat(reset_str)
        # Normalize naive timestamps to UTC to avoid TypeError when
        # subtracting from timezone-aware current_time
        if reset_time.tzinfo is None:
            reset_time = reset_time.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    next_reset = reset_time + timedelta(seconds=window_seconds)
    remaining = (next_reset - current_time).total_seconds()
    return max(0.0, remaining)


def pressure_from_observation(
    observation: SubscriptionObservation,
    metric_key: str,
    window_seconds: int,
    max_pressure_window: float = 0.85,
    now: datetime | None = None,
) -> float | None:
    """Compute pressure from when a subscription was last observed at a reset.

    Uses the fraction of the window that has elapsed since the last reset.
    A subscription that was reset recently (e.g. 1h ago in a 7d window) has
    low pressure because most of its quota remains. One observed long ago
    has higher pressure (more quota consumed).

    Args:
        observation: Observation state for a subscription.
        metric_key: Which metric to check.
        window_seconds: Duration of the rolling window.
        max_pressure_window: Cap the pressure-scoring window to this fraction
            of the full window (prevents stale observations from saturating).
        now: Current time.

    Returns:
        Pressure score 0-1, or None if no observation exists.
    """
    remaining = remaining_until_observed_reset(
        observation, metric_key, window_seconds, now=now
    )
    if remaining is None:
        return None
    # Elapsed fraction of the window since observation
    elapsed = max(0.0, window_seconds - remaining)
    elapsed_frac = elapsed / window_seconds
    # Cap so old observations don't saturate
    return min(max_pressure_window, elapsed_frac)


# --- Pressure scoring ---


def _pressure_component(utilization: float, exhausted_threshold: float) -> float:
    """Return a 0-1 pressure component, treating threshold-crossing as exhausted."""
    value = max(0.0, min(1.0, utilization))
    threshold = max(0.0, min(1.0, exhausted_threshold))
    if threshold <= 0 or value >= threshold:
        return 1.0
    return value


def subscription_pressure_from_usage(
    usage: dict,
    weekly_exhausted: float = DEFAULT_WEEKLY_EXHAUSTED,
    five_hour_threshold: float = DEFAULT_FIVE_HOUR_EXHAUSTED,
    sonnet_weekly_exhausted: float = DEFAULT_SONNET_WEEKLY_EXHAUSTED,
    short_reset_window: int = 7200,
) -> float | None:
    """Return a compact pressure score for a subscription usage snapshot.

    Weekly overall and Sonnet weekly limits are the durable pressure signals.
    The 5h window only counts when it will remain tight for more than
    ``short_reset_window`` seconds; a short-reset 5h spike should not drive
    multi-day slot routing.

    Args:
        usage: Usage snapshot dict, typically from ``check-quota`` API.
        weekly_exhausted: Utilization level treated as exhausted (0-1).
        five_hour_threshold: 5h utilization level treated as exhausted (0-1).
        sonnet_weekly_exhausted: Sonnet-weekly utilization level treated as
            exhausted (0-1). Separate from ``weekly_exhausted`` because Sonnet
            has a lower per-project cap (default 0.85 vs 0.95).
        short_reset_window: Min resets_in_seconds for 5h to count (avoids
            short spikes driving multi-day routing).

    Returns:
        Pressure score 0-1, or None if no useful data.
    """
    components: list[float] = []
    weekly = usage.get("seven_day", {}).get("utilization")
    if isinstance(weekly, int | float):
        components.append(_pressure_component(float(weekly), weekly_exhausted))

    sonnet_weekly = usage.get("seven_day_sonnet", {}).get("utilization")
    if isinstance(sonnet_weekly, int | float):
        components.append(
            _pressure_component(float(sonnet_weekly), sonnet_weekly_exhausted)
        )

    five_hour = usage.get("five_hour", {}).get("utilization")
    five_hour_resets = usage.get("five_hour", {}).get("resets_in_seconds", 0)
    if (
        isinstance(five_hour, int | float)
        and isinstance(five_hour_resets, int | float)
        and five_hour_resets > short_reset_window
    ):
        components.append(_pressure_component(float(five_hour), five_hour_threshold))

    if not components:
        return None
    return max(components)


def is_subscription_blocked(
    usage: dict,
    weekly_exhausted: float = DEFAULT_WEEKLY_EXHAUSTED,
    five_hour_exhausted: float = DEFAULT_FIVE_HOUR_EXHAUSTED,
    sonnet_weekly_exhausted: float = DEFAULT_SONNET_WEEKLY_EXHAUSTED,
) -> tuple[bool, str]:
    """Check if a subscription's quota is too exhausted to use.

    Three independent limits exist — any one being exhausted blocks the sub:
    - Overall weekly + 5h both exhausted -> no Opus capacity left
    - Sonnet weekly exhausted -> project-monitoring and Sonnet sessions break
    - Sonnet data missing + high weekly -> assume Sonnet exhausted (conservative)

    Args:
        usage: Usage snapshot dict.
        weekly_exhausted: Threshold for overall weekly (0-1).
        five_hour_exhausted: Threshold for 5h window (0-1).
        sonnet_weekly_exhausted: Threshold for Sonnet weekly (0-1).

    Returns:
        (blocked: bool, reason: str).
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
        # Can't confirm Sonnet is OK — be conservative when overall is already high
        reasons.append("Sonnet data missing, weekly high — assuming Sonnet exhausted")

    blocked = len(reasons) > 0
    return blocked, "; ".join(reasons) if reasons else "all limits healthy"
