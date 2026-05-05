# subscription-manager

Generic subscription quota observation, pressure scoring, and capacity-aware
routing for multi-agent credential rotation.

Extracted from Bob's `scripts/manage-subscription.py` so Alice and future
agents can share the same credential-slot, quota-observation, and
capacity-routing policy without copying Bob-specific wiring.

## Usage

```python
from subscription_manager import (
    compute_window_pacing,
    compute_rebalance_hold_seconds,
    subscription_pressure_from_usage,
    is_subscription_blocked,
    record_sub_reset_time,
    load_sub_observations,
    capacity_aware_fallback_order,
    best_lower_pressure_fallback,
    load_rebalance_state,
    save_rebalance_state,
)
```

All functions accept paths and thresholds as parameters, making them
independent of any specific agent's file layout.

## License

MIT — see [gptme-contrib](https://github.com/gptme/gptme-contrib)
