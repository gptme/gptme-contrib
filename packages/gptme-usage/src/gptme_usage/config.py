"""Config merging utilities for HarnessQuotaConfig.

Provides helpers to merge a caller-supplied partial config with module-level
defaults, so agents that supply a subset of models still see defaults for
anything not in their config.
"""

from __future__ import annotations

from gptme_usage.harness_models import (
    GPTME_MODEL_ROUTES,
    GPTME_QUOTA_SOURCE,
    HARNESS_PRICE_USD_PER_1M,
    TOKENS_PER_SECOND,
    HarnessQuotaConfig,
)


def merge_with_module_defaults(config: HarnessQuotaConfig) -> HarnessQuotaConfig:
    """Merge a caller-supplied config with module-level defaults.

    Returns a new ``HarnessQuotaConfig`` where entries from *config* take
    priority, but any model/route/source present in the module-level defaults
    (``HARNESS_PRICE_USD_PER_1M``, ``TOKENS_PER_SECOND``,
    ``GPTME_QUOTA_SOURCE``, ``GPTME_MODEL_ROUTES``) that is *not* in the
    caller's config is also included.

    This gives callers with a **partial** config access to module defaults
    for unlisted models, while still letting a fully-specified config
    override anything.

    For ``openrouter_key_contexts`` and ``claude_plan_tier`` (which have no
    module-level defaults) the caller's values are used as-is.

    **Replace semantics**: if you need *strict* replace (agent isolation,
    no fallback to module defaults), pass the original config directly to
    helpers instead of the merged result. This helper is opt-in — it never
    changes the default behavior.
    """
    # Start with the module defaults.
    merged_price = dict(HARNESS_PRICE_USD_PER_1M)
    merged_tps = dict(TOKENS_PER_SECOND)
    merged_quota = dict(GPTME_QUOTA_SOURCE)
    merged_routes = dict(GPTME_MODEL_ROUTES)
    merged_key_ctxs = dict(config.openrouter_key_contexts)

    # Overlay with the caller's config — caller entries take priority.
    merged_price.update(config.price_table)
    merged_tps.update(config.tps_table)
    merged_quota.update(config.quota_sources)
    merged_routes.update(config.model_routes)

    return HarnessQuotaConfig(
        price_table=merged_price,
        tps_table=merged_tps,
        quota_sources=merged_quota,
        model_routes=merged_routes,
        openrouter_key_contexts=merged_key_ctxs,
        claude_plan_tier=config.claude_plan_tier,
    )
