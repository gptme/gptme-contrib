"""Generic subscription-management primitives for multi-agent credential rotation.

Provides quota observation tracking, pressure scoring, window pacing,
capacity-aware fallback routing, and rebalance-state persistence — all
independent of a specific agent's credential-slot wiring.

Agent-specific paths and thresholds are injected by the caller.
"""

from subscription_manager.observation import (
    load_sub_observations,
    record_sub_reset_time,
)
from subscription_manager.pacing import (
    compute_rebalance_hold_seconds,
    compute_window_pacing,
    format_duration,
)
from subscription_manager.pressure import (
    is_subscription_blocked,
    subscription_pressure_from_usage,
)
from subscription_manager.routing import (
    best_lower_pressure_fallback,
    capacity_aware_fallback_order,
    seconds_since_last_switch_to,
    soonest_resetting_fallback,
)
from subscription_manager.state import (
    clear_rebalance_state,
    load_rebalance_state,
    save_rebalance_state,
)

__all__ = [
    # Pacing
    "compute_window_pacing",
    "compute_rebalance_hold_seconds",
    "format_duration",
    # Pressure
    "subscription_pressure_from_usage",
    "is_subscription_blocked",
    # Observation
    "record_sub_reset_time",
    "load_sub_observations",
    # Routing
    "capacity_aware_fallback_order",
    "best_lower_pressure_fallback",
    "soonest_resetting_fallback",
    "seconds_since_last_switch_to",
    # State
    "load_rebalance_state",
    "save_rebalance_state",
    "clear_rebalance_state",
]
