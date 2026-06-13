"""gptme-usage: cross-backend usage, cost, and quota surface for gptme agents.

This package owns the *usage / capacity* concern — the model registry, cost
math, and per-agent quota configuration that inform both harness/model selection
for autonomous runs and (downstream) subscription pressure scoring. It is a leaf
package: it must not depend on ``gptme-subscription`` (slot rotation is a
separate, higher-level concern). See
``ErikBjare/bob knowledge/technical-designs/gptme-usage-package-split.md``.

Extracted from ``gptme_subscription.harness_models`` (gptme/gptme-contrib#1088,
relocated here so quota/usage is not mis-homed in the subscription package).

Primary API::

    from gptme_usage import (
        HarnessQuotaConfig,
        load_quota_config,
        estimate_session_cost,
        estimate_tokens_from_duration,
    )
"""

from __future__ import annotations

from gptme_usage.harness_models import (
    HarnessQuotaConfig,
    estimate_session_cost,
    estimate_tokens_from_duration,
    load_quota_config,
    pricing_key_for_model,
)

__all__ = [
    "HarnessQuotaConfig",
    "load_quota_config",
    "estimate_session_cost",
    "estimate_tokens_from_duration",
    "pricing_key_for_model",
]

__version__ = "0.1.0"
