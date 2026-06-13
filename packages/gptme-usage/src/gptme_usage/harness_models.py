"""Harness model registry and per-agent quota configuration.

Provides:
- Generic cost math (estimate_session_cost, estimate_tokens_from_duration)
- HarnessQuotaConfig dataclass + load_quota_config() for per-agent model config
- Agent SDK credit facts (dates, plan table)
- Cache pricing multipliers

Agent-specific data (price tables, TPS, model routes, quota sources) lives in
~/.config/gptme/harness-quota.toml and is loaded via load_quota_config(). The
module-level tables (HARNESS_PRICE_USD_PER_1M etc.) ship EMPTY — this package
carries no single agent's config. Callers pass a loaded config; without one the
cost/lookup helpers return None/empty. See harness-quota.example.toml.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

# Approximate agent-session token mix used to blend input/output list prices into
# a single $/1M-token-equivalent floor. Agent runs are usually prompt-heavy due
# to large context and tool transcripts, so we keep output share conservative.
DEFAULT_OUTPUT_TOKEN_SHARE = 0.05

# --- Agent SDK credit change (June 15, 2026) ---
# Starting on this date, `claude -p` / Agent SDK usage no longer counts toward
# Claude Max subscription usage limits. Instead, a separate monthly credit applies:
#   Max 5x:  $100/month
#   Max 20x: $200/month
# Source: https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
CLAUDE_AGENT_SDK_CREDIT_CHANGE_DATE = datetime(2026, 6, 15, tzinfo=UTC)

# Monthly Agent SDK credit by subscription plan (USD).
# After CLAUDE_AGENT_SDK_CREDIT_CHANGE_DATE, agent sessions draw from this
# credit pool instead of the subscription usage limits.
CLAUDE_AGENT_SDK_MONTHLY_CREDIT_USD: dict[str, float] = {
    "pro": 20.0,
    "max-5x": 100.0,
    "max-20x": 200.0,
    "team-standard": 20.0,
    "team-premium": 100.0,
    "enterprise-usage": 20.0,
    "enterprise-premium": 200.0,
}
# Default: assume Max 20x (highest credit) for Bob/Alice operations
CLAUDE_AGENT_SDK_ASSUMED_MONTHLY_CREDIT_USD = CLAUDE_AGENT_SDK_MONTHLY_CREDIT_USD[
    "max-20x"
]


def is_post_agent_sdk_credit_change(now: datetime | None = None) -> bool:
    """Return True if the Anthropic Agent SDK credit change has taken effect."""
    if now is None:
        now = datetime.now(UTC)
    return now >= CLAUDE_AGENT_SDK_CREDIT_CHANGE_DATE


# ---------------------------------------------------------------------------
# Per-agent quota configuration
# ---------------------------------------------------------------------------


@dataclass
class HarnessQuotaConfig:
    """Per-agent quota configuration loaded from harness-quota.toml.

    Holds the agent-specific model registry: prices, TPS estimates,
    quota source map, model routing, and OpenRouter key contexts.
    Cost functions (estimate_session_cost, estimate_tokens_from_duration)
    accept an optional HarnessQuotaConfig and fall back to the module-level
    defaults when None is passed.

    Load via load_quota_config(); do not construct directly in most cases.
    """

    price_table: dict[tuple[str, str], tuple[float, float]] = field(
        default_factory=dict
    )
    tps_table: dict[tuple[str, str], float] = field(default_factory=dict)
    quota_sources: dict[str, str] = field(default_factory=dict)
    model_routes: dict[str, str] = field(default_factory=dict)
    openrouter_key_contexts: dict[str, str] = field(default_factory=dict)
    # Agent's Claude plan tier (e.g. "max-5x", "max-20x"). None = unconfigured;
    # callers must not assume a specific agent's plan as a generic default.
    claude_plan_tier: str | None = None


def _resolve_config_path(path: Path | None) -> Path:
    """Return the harness-quota.toml path, consulting gptme config dir when path is None."""
    if path is not None:
        return path
    config_dir: Path | None = None
    try:
        from gptme.config import get_config

        raw = getattr(get_config(), "config_dir", None)
        if raw is not None:
            config_dir = Path(str(raw))
    except Exception:
        pass
    if config_dir is None:
        config_dir = Path.home() / ".config" / "gptme"
    return config_dir / "harness-quota.toml"


def load_quota_config(path: Path | None = None) -> HarnessQuotaConfig:
    """Load per-agent quota configuration from a TOML file.

    Looks for ``~/.config/gptme/harness-quota.toml`` by default (or the path
    returned by ``gptme.config.get_config().config_dir``).  Returns an empty
    HarnessQuotaConfig when the file is absent, unreadable, or Python < 3.11
    (no stdlib tomllib) and tomli is not installed.

    TOML schema::

        claude_plan_tier = "max-20x"  # optional; omit when unknown (default None)

        [prices.claude-code]
        opus    = [5.0, 25.0]   # [input_$/1M, output_$/1M]
        sonnet  = [3.0, 15.0]

        [prices.gptme]
        "deepseek-v4-pro" = [1.74, 3.48]

        [tps.claude-code]
        opus   = 18899          # tokens per second (empirical)
        sonnet = 12804

        [quota_sources]
        "gpt-5.4"          = "chatgpt"
        "deepseek-v4-pro"  = "openrouter"
        "qwen3.6"          = "local"

        [model_routes]
        "deepseek-v4-pro" = "openrouter/deepseek/deepseek-v4-pro@deepseek"

        [openrouter_key_contexts]
        default           = "autonomous"
        "deepseek-v4-pro" = "autonomous_deepseek"
    """
    toml_path = _resolve_config_path(path)

    if not toml_path.exists():
        return HarnessQuotaConfig()

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomli as tomllib  # type: ignore[import-not-found,no-redef]
        except ModuleNotFoundError:
            return HarnessQuotaConfig()

    try:
        with open(toml_path, "rb") as fh:
            raw = tomllib.load(fh)
    except Exception:
        return HarnessQuotaConfig()

    price_table: dict[tuple[str, str], tuple[float, float]] = {}
    for backend, models in (raw.get("prices") or {}).items():
        if not isinstance(models, dict):
            continue
        for model, pair in models.items():
            if isinstance(pair, list) and len(pair) == 2:
                try:
                    price_table[(backend, model)] = (float(pair[0]), float(pair[1]))
                except (TypeError, ValueError):
                    pass

    tps_table: dict[tuple[str, str], float] = {}
    for backend, models in (raw.get("tps") or {}).items():
        if not isinstance(models, dict):
            continue
        for model, tps in models.items():
            try:
                tps_table[(backend, model)] = float(tps)
            except (TypeError, ValueError):
                pass

    quota_sources: dict[str, str] = {}
    for model, src in (raw.get("quota_sources") or {}).items():
        if isinstance(src, str):
            quota_sources[model] = src

    model_routes: dict[str, str] = {}
    for model, route in (raw.get("model_routes") or {}).items():
        if isinstance(route, str):
            model_routes[model] = route

    openrouter_key_contexts: dict[str, str] = {}
    for key, ctx in (raw.get("openrouter_key_contexts") or {}).items():
        if isinstance(ctx, str):
            openrouter_key_contexts[key] = ctx

    raw_tier = raw.get("claude_plan_tier")
    claude_plan_tier = str(raw_tier) if isinstance(raw_tier, str) and raw_tier else None

    return HarnessQuotaConfig(
        price_table=price_table,
        tps_table=tps_table,
        quota_sources=quota_sources,
        model_routes=model_routes,
        openrouter_key_contexts=openrouter_key_contexts,
        claude_plan_tier=claude_plan_tier,
    )


# --- Agent-specific configuration ---
# HARNESS_TIERS (capability judgments), NON_AGENTIC_MODELS, SUPERSEDED_ARMS,
# tier_for_model, tier_grade_data, tier_adjusted_bandit_score, and _default_db_path
# are agent-specific (empirical bandit data, model selection decisions, session DB paths).
# These live in the agent's own brain repo (e.g. ErikBjare/bob) and should be layered on
# top of this generic pricing infrastructure in gptme-contrib.
# See: ErikBjare/bob#1088

# Sources checked 2026-04-24:
# - Anthropic docs: Opus 4.5+ = $5/$25, Opus 4.0-4.1 = $15/$75
#   (https://docs.anthropic.com/en/docs/about-claude/pricing)
# - OpenAI API pricing for GPT-5.4: $2.50/$15; GPT-5.5: $5/$30 (API not yet live, sub only)
#   (https://openai.com/index/introducing-gpt-5-5/)
# - OpenRouter: DeepSeek V4 Pro ($1.74/$3.48), DeepSeek V4 Flash ($0.14/$0.28),
#   GLM-5 ($0.72/$2.30), Grok 4.20 ($2/$6), MiniMax M2.7 ($0.30/$1.20),
#   Kimi K2.6 ($0.7448/$4.655) (https://openrouter.ai/deepseek/deepseek-v4-pro,
#   https://openrouter.ai/deepseek/deepseek-v4-flash, https://openrouter.ai/moonshotai/kimi-k2.6)
# Copilot-backed models use the same underlying provider price floors as their
# API equivalents so selector ordering stays anchored to real model cost.
# Per-agent data — supplied via ~/.config/gptme/harness-quota.toml ([prices.*])
# and loaded into HarnessQuotaConfig.price_table. The module ships EMPTY so it
# carries no single agent's config (see harness-quota.example.toml for the shape).
# estimate_session_cost() falls back to this empty default only when called
# without a config; production callers pass load_quota_config().
HARNESS_PRICE_USD_PER_1M: dict[tuple[str, str], tuple[float, float]] = {}


def blended_token_price(
    input_price: float,
    output_price: float,
    *,
    output_share: float = DEFAULT_OUTPUT_TOKEN_SHARE,
) -> float:
    """Blend input/output token prices into one comparable floor.

    This intentionally approximates current agent traffic instead of claiming a
    universal truth. The selector normalizes this value downstream, so the main
    requirement is a stable, real-price ordering across models.
    """
    output_share = max(0.0, min(1.0, output_share))
    input_share = 1.0 - output_share
    return round(input_price * input_share + output_price * output_share, 3)


# Real blended list-price floor used by selector scoring and productivity
# metrics. Subscription-backed backends can exceed this floor when quota
# pressure rises, but they should never appear cheaper than their underlying
# model list price.
HARNESS_COST: dict[tuple[str, str], float] = {
    key: blended_token_price(input_price, output_price)
    for key, (input_price, output_price) in HARNESS_PRICE_USD_PER_1M.items()
}


def quota_pool_label(harness: str, model: str) -> str:
    """Return the quota/billing pool label for a harness/model pair.

    Note: After CLAUDE_AGENT_SDK_CREDIT_CHANGE_DATE (June 15, 2026), claude-code
    sessions draw from a $200/month Agent SDK credit pool instead of the
    subscription usage limits. The label remains "claude-max" for continuity
    but the underlying budget model changes. See is_post_agent_sdk_credit_change().
    """
    if harness == "claude-code":
        return "claude-max"
    if harness == "grok-build":
        return "supergrok-heavy"
    if harness == "copilot-cli":
        return "copilot"
    if harness == "codex":
        return "chatgpt-sub (shared)"
    if harness == "gptme":
        source = GPTME_QUOTA_SOURCE.get(model)
        if source == "chatgpt":
            return "chatgpt-sub (shared)"
        if source == "openrouter":
            return "openrouter"
        if source == "local":
            return "local"
    return "unknown"


def pricing_key_for_model(
    harness: str,
    model: str,
    config: HarnessQuotaConfig | None = None,
) -> tuple[str, str]:
    """Normalize harness/model identifiers to the canonical pricing table key."""
    normalized_model = model
    if harness == "claude-code":
        resolved = resolve_cc_version(model)
        for family in ("opus", "sonnet", "haiku"):
            if resolved == family or resolved.startswith(f"{family}-"):
                normalized_model = family
                break
        else:
            normalized_model = resolved
    elif harness == "gptme":
        # Config replaces the module-level routes entirely (consistent with
        # price_table and tps_table: all three use replace-not-merge) so a
        # configured agent never silently inherits stale routes from the defaults.
        routes = (
            config.model_routes
            if (config is not None and config.model_routes)
            else GPTME_MODEL_ROUTES
        )
        for short_name, provider_model in routes.items():
            if model == provider_model:
                normalized_model = short_name
                break
    return harness, normalized_model


class HarnessCostRow(TypedDict):
    """One row of canonical harness pricing for reporting/inspection."""

    backend: str
    model: str
    input_usd_per_1m: float
    output_usd_per_1m: float
    blended_usd_per_1m: float
    quota_pool: str


def harness_cost_rows() -> list[HarnessCostRow]:
    """Return canonical harness pricing rows for reporting/inspection."""
    rows: list[HarnessCostRow] = []
    for (harness, model), (
        input_price,
        output_price,
    ) in HARNESS_PRICE_USD_PER_1M.items():
        rows.append(
            {
                "backend": harness,
                "model": model,
                "input_usd_per_1m": input_price,
                "output_usd_per_1m": output_price,
                "blended_usd_per_1m": HARNESS_COST[(harness, model)],
                "quota_pool": quota_pool_label(harness, model),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            -row["blended_usd_per_1m"],
            row["backend"],
            row["model"],
        ),
    )


# --- Cache pricing multipliers ---
# Anthropic: cache reads = 0.1x input, cache creation = 1.25x input.
#   Source: https://docs.anthropic.com/en/docs/about-claude/pricing
# OpenAI: cached input = 0.5x input, no separate creation cost.
#   Source: https://platform.openai.com/docs/pricing
# OpenRouter/others: no cache pricing exposed; treat cache tokens as regular input.
CACHE_READ_MULTIPLIER: dict[str, float] = {
    "anthropic": 0.1,  # Opus/Sonnet cache reads
    "openai": 0.5,  # GPT cached input
}
CACHE_CREATION_MULTIPLIER: dict[str, float] = {
    "anthropic": 1.25,  # Opus/Sonnet cache writes
    "openai": 1.0,  # OpenAI has no separate cache creation cost
}

# Map harness keys to cache pricing provider.
_CACHE_PRICING_PROVIDER: dict[tuple[str, str], str] = {
    ("claude-code", "opus"): "anthropic",
    ("claude-code", "sonnet"): "anthropic",
    ("copilot-cli", "claude-opus-4.6"): "anthropic",
    ("copilot-cli", "claude-sonnet-4.6"): "anthropic",
    ("codex", "gpt-5.4"): "openai",
    ("codex", "gpt-5.5"): "openai",
    ("copilot-cli", "gpt-5.4"): "openai",
    ("gptme", "gpt-5.4"): "openai",
    ("gptme", "gpt-5.5"): "openai",
    # kimi-k2.6: no cache pricing exposed on OpenRouter
}


def estimate_session_cost(
    harness: str,
    model: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    token_count: int | None = None,
    config: HarnessQuotaConfig | None = None,
) -> float | None:
    """Estimate real USD cost from observed token breakdown.

    Returns None if pricing is unknown or all token counts are None.
    Uses cache-aware pricing when available; falls back to treating
    cache tokens as regular input for providers without cache pricing.

    When only ``token_count`` is available (no i/o breakdown), applies
    a heuristic for Claude Code subscription-backed sessions: assumes
    all tokens are cache reads (validated at 99.9%+ for 1,104 CC
    sessions with full breakdowns).  See knowledge/research/
    2026-05-01-token-coverage-backfill-analysis.md.

    Args:
        config: Optional per-agent quota config.  When provided and its
            ``price_table`` is non-empty, that table is used for pricing
            instead of the module-level ``HARNESS_PRICE_USD_PER_1M``.
    """
    # A non-empty config.price_table replaces the module-level table outright
    # (not a merge) so a configured agent never inherits Bob's prices underneath
    # its own. Consistent with tps_table and model_routes.
    price_table = (
        config.price_table
        if (config is not None and config.price_table)
        else HARNESS_PRICE_USD_PER_1M
    )
    key = pricing_key_for_model(harness, model, config)
    prices = price_table.get(key)
    if prices is None:
        return None

    input_price, output_price = prices

    inp = input_tokens or 0
    out = output_tokens or 0
    cache_create = cache_creation_tokens or 0
    cache_read = cache_read_tokens or 0

    if inp == 0 and out == 0 and cache_create == 0 and cache_read == 0:
        # Fallback: CC sessions where only token_count is available.
        # Empirical validation: 1,104 CC sessions with full breakdowns
        # show 99.9%+ of billed tokens are cache reads, so token_count
        # ≈ cache_read_tokens for these sessions.
        if token_count and token_count > 0 and key in SUBSCRIPTION_BACKED_MODELS:
            provider = _CACHE_PRICING_PROVIDER.get(key)
            if provider:
                cache_read_rate = CACHE_READ_MULTIPLIER.get(provider, 1.0)
                cost_usd = (token_count * input_price * cache_read_rate) / 1_000_000
                return round(cost_usd, 6)
        return None

    provider = _CACHE_PRICING_PROVIDER.get(key)
    cache_read_rate = CACHE_READ_MULTIPLIER.get(provider or "", 1.0)
    cache_create_rate = CACHE_CREATION_MULTIPLIER.get(provider or "", 1.0)

    cost_usd = (
        inp * input_price
        + out * output_price
        + cache_create * input_price * cache_create_rate
        + cache_read * input_price * cache_read_rate
    ) / 1_000_000

    return round(cost_usd, 6)


# Backends where quota pressure should be translated into effective cost. API
# models already pay real marginal dollars, but subscription-backed models need
# a synthetic pressure term once quota gets tight.
SUBSCRIPTION_BACKED_MODELS: set[tuple[str, str]] = {
    ("claude-code", "opus"),
    ("claude-code", "sonnet"),
    ("grok-build", "grok-build"),
    ("codex", "gpt-5.4"),
    ("codex", "gpt-5.5"),
    ("copilot-cli", "claude-opus-4.6"),
    ("copilot-cli", "claude-sonnet-4.6"),
    ("copilot-cli", "gpt-5.4"),
    ("gptme", "gpt-5.4"),
    ("gptme", "gpt-5.5"),
}

# Tier ordering for scoring
TIER_RANK = {"high": 3, "medium": 2, "low": 1, "retired": 0}

# --- Duration-based token estimation ---
# Median tokens per second from sessions where both duration and token_count
# are available. Used as a fallback when token data is missing, enabling
# near-100% cost estimation coverage (up from ~35%).
# Source: analysis of 2,253 sessions across all (harness, model) pairs
# with >= 10 data points. See knowledge/research/2026-05-01-token-coverage-backfill-analysis.md
# Per-agent data — supplied via harness-quota.toml ([tps.*]) into
# HarnessQuotaConfig.tps_table. Ships EMPTY (no agent's config in the package).
TOKENS_PER_SECOND: dict[tuple[str, str], float] = {}


def estimate_tokens_from_duration(
    harness: str,
    model: str,
    duration_seconds: int,
    *,
    config: HarnessQuotaConfig | None = None,
) -> int | None:
    """Estimate token count from session duration.

    Returns None when no TPS data is available for this (harness, model) pair
    or when duration is zero/negative.

    Args:
        config: Optional per-agent quota config.  When provided and its
            ``tps_table`` is non-empty, that table is used instead of the
            module-level ``TOKENS_PER_SECOND``.
    """
    if duration_seconds <= 0:
        return None
    # Replace (not merge) the module-level table when config provides one — see
    # estimate_session_cost for the rationale (no Bob-data leak into agents).
    tps_table = (
        config.tps_table
        if (config is not None and config.tps_table)
        else TOKENS_PER_SECOND
    )
    key = pricing_key_for_model(harness, model, config)
    tps = tps_table.get(key)
    if tps is None:
        tps = tps_table.get((harness, model))
    if tps is None:
        return None
    return int(duration_seconds * tps)


# --- Quota source classification ---
# Which quota pool each gptme model draws from.
# Used by check-quota.py to know how to check availability.
# "openrouter" = OpenRouter API key with daily $ limit
# "chatgpt" = ChatGPT subscription (separate pool)
# Claude Code models don't appear here (they use CC subscription checks).
# Per-agent data — supplied via harness-quota.toml ([quota_sources]) into
# HarnessQuotaConfig.quota_sources. Ships EMPTY. openrouter_models() /
# local_models() read the passed config; this default is the unconfigured case.
GPTME_QUOTA_SOURCE: dict[str, str] = {}


# --- Provider-qualified model strings ---
# Maps short model names to full provider-prefixed strings for gptme.
# Per-agent data — supplied via harness-quota.toml ([model_routes]) into
# HarnessQuotaConfig.model_routes. Ships EMPTY; resolve_gptme_model() and
# pricing_key_for_model() read the passed config.
GPTME_MODEL_ROUTES: dict[str, str] = {}


# --- Claude Code version aliases ---
# Short aliases (opus/sonnet/haiku) map to the currently-shipping version.
# Bump these when Anthropic releases a new model so the bandit tracks per-version
# performance instead of flattening all versions into one arm.
# See: ErikBjare/bob#612
CC_MODEL_VERSIONS: dict[str, str] = {
    # Short alias → currently-shipping CC version. Bump these when Anthropic
    # ships a new model. Historical gotcha (2026-04-17, ErikBjare/bob#614):
    # don't couple the bump to a blanket migrate-cc-versioned-arms.py
    # with weight=1.0 — that copies OLD-version data into the NEW-version
    # arm, contaminating posteriors. Prefer letting the new arm accumulate
    # signal organically from new sessions (trajectory detection handles
    # attribution correctly). The 573 pre-existing "opus" posteriors that
    # got mis-seeded into opus-4-7 via the migration were relabeled back to
    # opus-4-6 (the actual historical majority) and opus-4-7 restarted from
    # priors.
    "opus": "opus-4-8",
    "sonnet": "sonnet-4-6",
    "haiku": "haiku-4-5",
    # Fable 5 — Mythos-class frontier model, released 2026-06-09. ~10x Opus
    # cost, free on subscription through June 22. Added as low-n exploration
    # arm; not wired into routing until/unless cost-economics change.
    "fable": "fable-5",
    "fable-5": "fable-5",
    "claude-fable-5": "fable-5",
}


def openrouter_models(config: HarnessQuotaConfig | None = None) -> list[str]:
    """Return list of gptme models that use OpenRouter quota.

    Reads ``config.quota_sources`` when provided (per-agent), else falls back to
    the module-level ``GPTME_QUOTA_SOURCE``.
    """
    sources = (
        config.quota_sources
        if (config is not None and config.quota_sources)
        else GPTME_QUOTA_SOURCE
    )
    return [m for m, src in sources.items() if src == "openrouter"]


def gptme_openrouter_context(
    model: str, config: HarnessQuotaConfig | None = None
) -> str:
    """Return the OpenRouter key context an autonomous gptme call uses for model.

    When ``config.openrouter_key_contexts`` is provided, look up the model there,
    falling back to its ``"default"`` entry (or ``"autonomous"``). Without config,
    use the legacy rule: deepseek models swap to the dedicated
    ``autonomous_deepseek`` key (separate $/day budget) so their quota is isolated
    from the shared ``autonomous`` key.
    """
    if config is not None and config.openrouter_key_contexts:
        ctx = config.openrouter_key_contexts
        return ctx.get(model, ctx.get("default", "autonomous"))
    if "deepseek" in model:
        return "autonomous_deepseek"
    return "autonomous"


def local_models(config: HarnessQuotaConfig | None = None) -> list[str]:
    """Return list of gptme models served by local inference (LM Studio).

    Reads ``config.quota_sources`` when provided, else ``GPTME_QUOTA_SOURCE``.
    """
    sources = (
        config.quota_sources
        if (config is not None and config.quota_sources)
        else GPTME_QUOTA_SOURCE
    )
    return [m for m, src in sources.items() if src == "local"]


def resolve_gptme_model(
    short_name: str, config: HarnessQuotaConfig | None = None
) -> str:
    """Resolve short model name to provider-qualified string for gptme."""
    routes = (
        config.model_routes
        if (config is not None and config.model_routes)
        else GPTME_MODEL_ROUTES
    )
    return routes.get(short_name, short_name)


COPILOT_MODEL_ALIASES: dict[str, str] = {
    "claude-opus-4": "opus",
    "claude-opus-4.6": "opus",
    "claude-sonnet-4": "sonnet",
    "claude-sonnet-4.6": "sonnet",
}


def resolve_copilot_version(model: str) -> str:
    """Normalize copilot-cli model name to its canonical short form.

    Copilot uses Claude model names (claude-opus-4.6) but the bandit state
    tracks them as short aliases (opus) to prevent signal fragmentation.
    Idempotent for already-short inputs and non-Claude models.

    Examples:
        resolve_copilot_version("claude-opus-4.6")    -> "opus"
        resolve_copilot_version("claude-sonnet-4.6")  -> "sonnet"
        resolve_copilot_version("gpt-5.4")            -> "gpt-5.4"
        resolve_copilot_version("opus")               -> "opus"
    """
    return COPILOT_MODEL_ALIASES.get(model.lower().strip(), model.lower().strip())


def resolve_cc_version(model: str) -> str:
    """Resolve a Claude Code model name to its versioned arm suffix.

    Handles short aliases, the ``claude-`` prefix, and concrete dated model
    IDs (``claude-opus-4-7-20251014``). Idempotent for already-versioned inputs.
    Unknown inputs pass through unchanged.

    Examples:
        resolve_cc_version("opus")                      -> "opus-4-7"
        resolve_cc_version("opus-4-7")                  -> "opus-4-7"
        resolve_cc_version("claude-opus-4-7")           -> "opus-4-7"
        resolve_cc_version("claude-opus-4-7-20251014")  -> "opus-4-7"
    """

    m = model.lower().strip()
    if m.startswith("claude-"):
        m = m[len("claude-") :]
    # Strip trailing YYYYMMDD date suffix from concrete model IDs
    m = re.sub(r"-\d{8}$", "", m)
    if m in CC_MODEL_VERSIONS.values():
        return m
    return CC_MODEL_VERSIONS.get(m, m)


# --- Tier classification for cross-harness grade comparison ---
# Maps model short-name to capability tier, aligned with
# knowledge/strategic/2026-05-10-judge-grade-bias-analysis.md

_MODEL_TIER_MAP: dict[str, str] = {
    # opus-tier: frontier reasoning
    "opus": "opus",
    "claude-opus-4-6": "opus",
    "claude-opus-4.6": "opus",
    "gpt-5.5": "opus",
    # sonnet-tier: solid coding
    "sonnet": "sonnet",
    "claude-sonnet-4.6": "sonnet",
    "deepseek-v4-pro": "sonnet",
    "kimi-k2.6": "sonnet",
    "gpt-5.4": "sonnet",
    "gpt-5": "sonnet",
    "gemini-3.5-flash": "sonnet",
    "grok-build": "sonnet",
}
_MODEL_TIER_HIGH_SUBSTRINGS: list[str] = ["opus", "gpt-5.5"]
_MODEL_TIER_MEDIUM_SUBSTRINGS: list[str] = [
    "sonnet",
    "deepseek-v4-pro",
    "kimi",
    "gpt-5.4",
    "gpt-5",
    "grok-build",
]

# Known prefixes to strip before tier lookup
_TIER_STRIP_PREFIXES: list[str] = [
    "openai-subscription/",
    "openrouter/anthropic/",
    "openrouter/deepseek/",
    "openrouter/google/",
    "openrouter/x-ai/",
    "openrouter/z-ai/",
    "openrouter/",
    "google/",
    "claude-",
]
