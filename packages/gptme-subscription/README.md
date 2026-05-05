# gptme-subscription

Generic subscription observation, pressure scoring, and capacity-aware routing for agent credential slots.

## Purpose

This package provides the policy layer above [credential-slots](https://github.com/gptme/gptme-contrib/tree/main/packages/credential-slots):

- **Observation state**: Track per-subscription quota reset timestamps
- **Pressure scoring**: Compute compact pressure scores from usage snapshots
- **Capacity-aware routing**: Sort fallbacks by pressure, find lower-pressure alternatives, forward-route to soonest-resetting subscriptions
- **Rebalance state machine**: Persist and query rebalance decisions (rest durations, hold windows)

## API

```python
from gptme_subscription import (
    subscription_pressure_from_usage,
    is_subscription_blocked,
    format_duration,
    capacity_aware_fallback_order,
    best_lower_pressure_fallback,
    compute_window_pacing,
)
```

See `src/gptme_subscription/__init__.py` for the full public API.

## Status

Alpha — extracted from Bob's `manage-subscription.py`. See [gptme/gptme-contrib#831](https://github.com/gptme/gptme-contrib/issues/831).
