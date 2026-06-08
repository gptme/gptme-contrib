"""gptme-subscription: subscription observation, pressure scoring, capacity-aware routing, and CLI.

Extracted from Bob's manage-subscription.py to be reusable by any agent.
See https://github.com/gptme/gptme-contrib/issues/831
"""

from gptme_subscription.auth import (
    CredentialInfo,
    check_credential_file,
    format_reauth_instructions,
    probe_credential,
)
from gptme_subscription.config import Config
from gptme_subscription.manager import (
    Decision,
    SubscriptionManager,
)
from gptme_subscription.observation import (
    SubscriptionObservation,
    format_duration,
    is_subscription_blocked,
    load_sub_observations,
    pressure_from_observation,
    record_sub_reset_time,
    remaining_until_observed_reset,
    subscription_pressure_from_usage,
)
from gptme_subscription.routing import (
    PacingSnapshot,
    RebalanceState,
    best_lower_pressure_fallback,
    capacity_aware_fallback_order,
    clear_rebalance_state,
    combine_window_pacing_snapshots,
    compute_pacing_snapshot,
    compute_rebalance_hold_seconds,
    compute_window_pacing,
    compute_window_pacing_snapshot,
    load_rebalance_state,
    save_rebalance_state,
    soonest_resetting_fallback,
)

__all__ = [
    # Observation
    "subscription_pressure_from_usage",
    "is_subscription_blocked",
    "format_duration",
    "record_sub_reset_time",
    "load_sub_observations",
    "remaining_until_observed_reset",
    "pressure_from_observation",
    "SubscriptionObservation",
    # Routing
    "PacingSnapshot",
    "compute_pacing_snapshot",
    "compute_window_pacing_snapshot",
    "combine_window_pacing_snapshots",
    "compute_window_pacing",
    "compute_rebalance_hold_seconds",
    "capacity_aware_fallback_order",
    "best_lower_pressure_fallback",
    "soonest_resetting_fallback",
    "load_rebalance_state",
    "save_rebalance_state",
    "clear_rebalance_state",
    "RebalanceState",
    # Config / manager / CLI
    "Config",
    "SubscriptionManager",
    "Decision",
    # Auth
    "CredentialInfo",
    "check_credential_file",
    "probe_credential",
    "format_reauth_instructions",
]
