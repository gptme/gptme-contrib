"""Capacity-aware fallback routing and rebalance state management.

Generic helpers for deciding which subscription to use, when to rebalance,
and how to manage persistent rebalance state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from gptme_subscription.observation import (
    DEFAULT_CAPACITY_REBALANCE_MARGIN,
    DEFAULT_CAPACITY_REBALANCE_MIN_PRESSURE,
    DEFAULT_EXPIRING_CAPACITY_CREDIT,
    DEFAULT_REBALANCE_MAX_HOLD,
    DEFAULT_REBALANCE_MIN_HOLD,
    DEFAULT_REBALANCE_TARGET_UTILIZATION,
    DEFAULT_SOON_TO_EXPIRE_THRESHOLD,
    DEFAULT_UNKNOWN_FALLBACK_PRESSURE,
    SubscriptionObservation,
    load_sub_observations,
    pressure_from_observation,
    remaining_until_observed_reset,
    subscription_pressure_from_usage,
)


@dataclass
class RebalanceState:
    """Persistent rebalance decision state."""

    switched_to: str = ""
    """The subscription name we rebalanced to."""

    switched_from: str = ""
    """The subscription name we rebalanced from."""

    switched_at: str = ""
    """ISO-8601 timestamp when the rebalance switch was made."""

    hold_until: str = ""
    """ISO-8601 timestamp until which the rebalance decision is honored."""

    reason: str = ""
    """Human-readable reason for the rebalance."""

    metadata: dict[str, object] = field(default_factory=dict)
    """Extra metadata (pressure scores, cache info, etc.)."""


# --- Window pacing ---


@dataclass(frozen=True)
class PacingSnapshot:
    """Normalized pacing view for a subscription quota window.

    Canonical convention:
    - ``headroom = 1 - utilization``
    - ``pace_gap = utilization - target_utilization``
    - ``pace_gap > 0`` means ahead of the target burn curve / overusing
    - ``pace_gap < 0`` means behind the target burn curve / underusing
    """

    utilization: float
    headroom: float
    elapsed_fraction: float
    target_utilization: float
    pace_gap: float
    status: str

    def as_dict(self) -> dict[str, float | str]:
        return dict(asdict(self))


def _clamp_unit(value: float) -> float:
    return max(0.0, min(1.0, value))


def compute_pacing_snapshot(
    utilization: float,
    *,
    elapsed_fraction: float,
    target_utilization: float = 1.0,
    threshold: float = 0.05,
) -> PacingSnapshot:
    """Return a canonical pacing snapshot for a quota window.

    ``target_utilization`` is a policy knob, not part of the sign convention.
    For a plain "use quota evenly until reset" curve, keep it at ``1.0``.
    Dashboard/rebalance surfaces can pass lower values such as ``0.95`` or
    ``0.90`` while still getting the same ``headroom`` / ``pace_gap`` meaning.
    """

    utilization_clamped = _clamp_unit(utilization)
    elapsed_clamped = _clamp_unit(elapsed_fraction)
    target_fraction = max(0.0, target_utilization)
    target = _clamp_unit(elapsed_clamped * target_fraction)
    gap = utilization_clamped - target
    if gap > threshold:
        status = "overusing"
    elif gap < -threshold:
        status = "underusing"
    else:
        status = "on_track"
    return PacingSnapshot(
        utilization=utilization_clamped,
        headroom=_clamp_unit(1.0 - utilization_clamped),
        elapsed_fraction=elapsed_clamped,
        target_utilization=target,
        pace_gap=gap,
        status=status,
    )


def compute_window_pacing_snapshot(
    utilization: float,
    resets_in_seconds: int,
    window_seconds: int,
    *,
    target_utilization: float = 1.0,
    threshold: float = 0.05,
) -> PacingSnapshot | None:
    """Compute a pacing snapshot from utilization and reset countdown."""

    if window_seconds <= 0 or resets_in_seconds <= 0:
        return None
    remaining_time_frac = _clamp_unit(resets_in_seconds / window_seconds)
    elapsed_frac = 1.0 - remaining_time_frac
    return compute_pacing_snapshot(
        utilization,
        elapsed_fraction=elapsed_frac,
        target_utilization=target_utilization,
        threshold=threshold,
    )


def combine_window_pacing_snapshots(
    windows: Sequence[tuple[float, int, int]],
    *,
    target_utilization: float = 1.0,
    threshold: float = 0.05,
) -> PacingSnapshot | None:
    """Return the pacing snapshot with the highest pace_gap across multiple windows."""

    snapshots: list[PacingSnapshot] = []
    for utilization, resets_in_seconds, window_seconds in windows:
        snapshot = compute_window_pacing_snapshot(
            utilization,
            resets_in_seconds,
            window_seconds,
            target_utilization=target_utilization,
            threshold=threshold,
        )
        if snapshot is not None:
            snapshots.append(snapshot)
    if not snapshots:
        return None
    return max(snapshots, key=lambda snapshot: snapshot.pace_gap)


def compute_window_pacing(
    utilization: float,
    resets_in_seconds: int,
    window_seconds: int,
    *,
    target_utilization: float = 1.0,
    threshold: float = 0.05,
) -> tuple[float, float, str] | None:
    """Compute pacing (target_utilization, gap, status) for a quota window.

    Uses the headroom model: if remaining budget is less than remaining time,
    the gap is positive (overusing). Returns None for invalid inputs.

    Args:
        utilization: Current utilization fraction (0-1).
        resets_in_seconds: Seconds until the window resets.
        window_seconds: Total window duration in seconds.

    Returns:
        ``(target_utilization, gap, status)`` or None.
        gap > 0 = overusing, gap < 0 = underusing, ≈ 0 = on track.
        Status is ``"overusing"``, ``"underusing"``, or ``"on_track"``.
    """
    snapshot = compute_window_pacing_snapshot(
        utilization,
        resets_in_seconds,
        window_seconds,
        target_utilization=target_utilization,
        threshold=threshold,
    )
    if snapshot is None:
        return None
    return snapshot.target_utilization, snapshot.pace_gap, snapshot.status


def compute_rebalance_hold_seconds(
    pace_overage: float,
    min_hold: int = DEFAULT_REBALANCE_MIN_HOLD,
    max_hold: int = DEFAULT_REBALANCE_MAX_HOLD,
    target_utilization: float = DEFAULT_REBALANCE_TARGET_UTILIZATION,
    window_seconds: int = 7 * 24 * 3600,
) -> int:
    """Estimate how long to rest a subscription until pacing catches up.

    Args:
        pace_overage: Positive gap from ``compute_window_pacing``.
        min_hold: Minimum hold time in seconds.
        max_hold: Maximum hold time in seconds.
        target_utilization: Target utilization fraction.
        window_seconds: Duration of the quota window (default 7 days).

    Returns:
        Hold duration in seconds, clamped to ``[min_hold, max_hold]``.
    """
    if pace_overage <= 0:
        return min_hold
    catch_up_seconds = int(pace_overage * window_seconds / target_utilization)
    return max(min_hold, min(max_hold, catch_up_seconds))


# --- Rebalance state persistence ---


def load_rebalance_state(
    state_path: Path,
) -> dict[str, object] | None:
    """Load rebalance state from a JSON file.

    Args:
        state_path: Path to the state JSON file.

    Returns:
        Parsed dict, or None if file doesn't exist or is invalid.
    """
    if not state_path.exists():
        return None
    try:
        raw = state_path.read_text()
        if not raw.strip():
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return {str(key): value for key, value in data.items()}
    except (json.JSONDecodeError, OSError):
        return None


def save_rebalance_state(
    state_path: Path,
    decision: Mapping[str, object],
) -> None:
    """Persist a rebalance decision to a JSON file.

    Args:
        state_path: Path to write the state JSON file.
        decision: Mapping with rebalance decision fields.
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(decision, indent=2) + "\n")


def clear_rebalance_state(state_path: Path) -> None:
    """Remove a persisted rebalance state file.

    Args:
        state_path: Path to the state JSON file to remove.
    """
    if state_path.exists():
        state_path.unlink()


# --- Fallback routing ---


def capacity_aware_fallback_order(
    fallback_order: list[str],
    obs_dir: Path,
    now: datetime | None = None,
    unknown_pressure: float = DEFAULT_UNKNOWN_FALLBACK_PRESSURE,
    soon_to_expire_threshold: float = DEFAULT_SOON_TO_EXPIRE_THRESHOLD,
    expiring_capacity_credit: float = DEFAULT_EXPIRING_CAPACITY_CREDIT,
    observations: Mapping[str, SubscriptionObservation] | None = None,
) -> list[str]:
    """Return ``fallback_order`` sorted by pressure, then expiring capacity.

    Unknown usage gets ``unknown_pressure`` so stale/missing observations
    don't look better than a known low-pressure slot. Soon-to-expire capacity
    gets a bounded credit, enough to prefer moderately used expiring quota
    but not enough to keep hammering an 80% slot when another sits around 20%.

    Args:
        fallback_order: Initial ordered list of subscription names.
        obs_dir: Observation directory (passed to ``load_sub_observations``).
        now: Current time. Defaults to UTC now.
        unknown_pressure: Pressure score assigned when no observation exists.
        soon_to_expire_threshold: Seconds remaining for expiry credit to apply.
        expiring_capacity_credit: Max credit applied to expiring capacity.
        observations: Pre-loaded observations dict. If None, loads from obs_dir.

    Returns:
        Reordered fallback list (ascending pressure, then expiring capacity).
    """
    current_time = now or datetime.now(timezone.utc)
    if observations is None:
        observations = load_sub_observations(obs_dir)

    def score(sub: str) -> tuple[float, float]:
        entry = observations.get(sub)
        if entry is None:
            return unknown_pressure, float("inf")
        pressure = pressure_from_observation(
            entry, "seven_day", 7 * 24 * 3600, now=current_time
        )
        if pressure is None:
            return unknown_pressure, float("inf")
        remaining = remaining_until_observed_reset(
            entry, "seven_day", 7 * 24 * 3600, now=current_time
        )
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
    obs_dir: Path,
    now: datetime | None = None,
    min_pressure: float = DEFAULT_CAPACITY_REBALANCE_MIN_PRESSURE,
    margin: float = DEFAULT_CAPACITY_REBALANCE_MARGIN,
) -> tuple[str, float, float] | None:
    """Return a fallback with materially lower pressure than the active sub.

    Args:
        active: Current active subscription name.
        active_usage: Usage snapshot for the active subscription.
        fallback_order: Ordered list of fallback subscription names.
        obs_dir: Observation directory for pressure lookups.
        now: Current time.
        min_pressure: Active must have pressure above this to consider swap.
        margin: Minimum pressure difference required.

    Returns:
        ``(fallback_name, active_pressure, fallback_pressure)`` or None.
    """
    current_time = now or datetime.now(timezone.utc)
    active_pressure = subscription_pressure_from_usage(active_usage)
    if active_pressure is None or active_pressure < min_pressure:
        return None

    observations = load_sub_observations(obs_dir)
    best: tuple[str, float] | None = None
    for sub in capacity_aware_fallback_order(
        fallback_order, obs_dir, now=current_time, observations=observations
    ):
        if sub == active:
            continue
        entry = observations.get(sub)
        if entry is None:
            continue
        pressure = pressure_from_observation(
            entry, "seven_day", 7 * 24 * 3600, now=current_time
        )
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
    active: str,
    fallback_order: list[str],
    obs_dir: Path,
    window_seconds: int = 7 * 24 * 3600,
    metric_key: str = "seven_day",
    now: datetime | None = None,
) -> str | None:
    """Find the fallback with the soonest scheduled reset.

    Useful for forward routing: proactively spread quota across subscriptions
    by picking the one that will reset first.

    Args:
        active: Skip this subscription (current active).
        fallback_order: Ordered list to search.
        obs_dir: Observation directory.
        window_seconds: Window duration for reset calculation. Must match the
            window duration implied by ``metric_key`` (e.g. 7*24*3600 for
            ``"seven_day"``).
        metric_key: Which metric key to look up in observation state.
            Must correspond to the same window as ``window_seconds``.
        now: Current time.

    Returns:
        Subscription name with the soonest reset, or None if no data.
    """
    current_time = now or datetime.now(timezone.utc)
    observations = load_sub_observations(obs_dir)
    best: tuple[str, float] | None = None
    for sub in fallback_order:
        if sub == active:
            continue
        entry = observations.get(sub)
        if entry is None:
            continue
        remaining = remaining_until_observed_reset(
            entry, metric_key, window_seconds, now=current_time
        )
        if remaining is None:
            continue
        if best is None or remaining < best[1]:
            best = (sub, remaining)
    return best[0] if best else None
