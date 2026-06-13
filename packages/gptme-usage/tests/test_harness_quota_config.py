"""Tests for HarnessQuotaConfig, load_quota_config, and merge_with_module_defaults."""

from __future__ import annotations

from pathlib import Path

import pytest
from gptme_usage.config import merge_with_module_defaults
from gptme_usage.harness_models import (
    HARNESS_PRICE_USD_PER_1M,
    HarnessQuotaConfig,
    estimate_session_cost,
    estimate_tokens_from_duration,
    load_quota_config,
)

TOML_FIXTURE = """\
claude_plan_tier = "max-5x"

[prices.claude-code]
opus   = [5.0, 25.0]
sonnet = [3.0, 15.0]

[prices.gptme]
"deepseek-v4-pro" = [1.74, 3.48]

[tps.claude-code]
opus   = 10000
sonnet = 8000

[quota_sources]
"gpt-5.4" = "chatgpt"
"deepseek-v4-pro" = "openrouter"

[model_routes]
"deepseek-v4-pro" = "openrouter/deepseek/deepseek-v4-pro@deepseek"

[openrouter_key_contexts]
default = "autonomous"
"deepseek-v4-pro" = "autonomous_deepseek"
"""


@pytest.fixture()
def toml_path(tmp_path: Path) -> Path:
    p = tmp_path / "harness-quota.toml"
    p.write_text(TOML_FIXTURE, encoding="utf-8")
    return p


def test_load_quota_config_basic(toml_path: Path) -> None:
    cfg = load_quota_config(toml_path)
    assert isinstance(cfg, HarnessQuotaConfig)
    assert cfg.claude_plan_tier == "max-5x"
    assert cfg.price_table[("claude-code", "opus")] == (5.0, 25.0)
    assert cfg.price_table[("gptme", "deepseek-v4-pro")] == (1.74, 3.48)
    assert cfg.tps_table[("claude-code", "opus")] == 10000.0
    assert cfg.quota_sources["gpt-5.4"] == "chatgpt"
    assert (
        cfg.model_routes["deepseek-v4-pro"]
        == "openrouter/deepseek/deepseek-v4-pro@deepseek"
    )
    assert cfg.openrouter_key_contexts["default"] == "autonomous"
    assert cfg.openrouter_key_contexts["deepseek-v4-pro"] == "autonomous_deepseek"


def test_load_quota_config_missing_file(tmp_path: Path) -> None:
    cfg = load_quota_config(tmp_path / "nonexistent.toml")
    assert isinstance(cfg, HarnessQuotaConfig)
    assert cfg.price_table == {}
    assert cfg.tps_table == {}


def test_load_quota_config_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "harness-quota.toml"
    p.write_text("", encoding="utf-8")
    cfg = load_quota_config(p)
    assert isinstance(cfg, HarnessQuotaConfig)
    assert cfg.price_table == {}
    assert cfg.claude_plan_tier is None  # unconfigured = unknown, not a Bob default


def test_estimate_session_cost_with_config(toml_path: Path) -> None:
    cfg = load_quota_config(toml_path)
    # opus: input=$5/1M, output=$25/1M — cache_read = 0.1x input = $0.5/1M
    # 1M cache reads * $0.5/1M = $0.50
    cost = estimate_session_cost(
        "claude-code",
        "opus",
        cache_read_tokens=1_000_000,
        config=cfg,
    )
    assert cost is not None
    assert abs(cost - 0.5) < 0.001, f"expected ~0.5, got {cost}"


def test_module_ships_no_agent_data() -> None:
    """The shared module must ship EMPTY tables — no agent's data baked in.

    Per-agent data lives in harness-quota.toml; the package carries none.
    """
    from gptme_usage.harness_models import (
        GPTME_MODEL_ROUTES,
        GPTME_QUOTA_SOURCE,
        HARNESS_PRICE_USD_PER_1M,
        TOKENS_PER_SECOND,
    )

    assert HARNESS_PRICE_USD_PER_1M == {}
    assert TOKENS_PER_SECOND == {}
    assert GPTME_QUOTA_SOURCE == {}
    assert GPTME_MODEL_ROUTES == {}


def test_estimate_session_cost_without_config_returns_none() -> None:
    """With no config and an empty module table, there is no price -> None."""
    cost = estimate_session_cost(
        "claude-code", "opus", cache_read_tokens=1_000_000, config=None
    )
    assert cost is None


def test_estimate_session_cost_config_overrides_price(toml_path: Path) -> None:
    # Compare two configs (the module ships no default to compare against).
    cfg = load_quota_config(toml_path)  # opus = [5, 25]
    cfg_2x = load_quota_config(toml_path)
    cfg_2x.price_table[("claude-code", "opus")] = (10.0, 50.0)  # 2x
    cost_base = estimate_session_cost(
        "claude-code", "opus", cache_read_tokens=1_000_000, config=cfg
    )
    cost_2x = estimate_session_cost(
        "claude-code", "opus", cache_read_tokens=1_000_000, config=cfg_2x
    )
    assert cost_base is not None and cost_2x is not None
    assert (
        abs(cost_2x / cost_base - 2.0) < 0.01
    ), f"expected 2x ratio, got {cost_2x}/{cost_base}"


def test_config_price_table_replaces_not_merges(toml_path: Path) -> None:
    """A non-empty config.price_table is authoritative — no other data leaks in.

    A model absent from the agent's config gets no price (None), even though the
    config does price other models.
    """
    cfg = load_quota_config(toml_path)  # prices opus, sonnet, deepseek-v4-pro...
    # opus IS in this config -> priced.
    assert (
        estimate_session_cost(
            "claude-code", "opus", cache_read_tokens=1_000_000, config=cfg
        )
        is not None
    )
    # A model NOT in this config -> None (no fallback to any other table).
    assert (
        estimate_session_cost(
            "claude-code", "nonexistent-model", cache_read_tokens=1_000_000, config=cfg
        )
        is None
    )


def test_config_tps_table_replaces_not_merges(toml_path: Path) -> None:
    """A non-empty config.tps_table is authoritative; unlisted models -> None."""
    cfg = load_quota_config(toml_path)  # has claude-code/opus TPS
    assert (
        estimate_tokens_from_duration("claude-code", "opus", 10, config=cfg) is not None
    )
    assert (
        estimate_tokens_from_duration(
            "claude-code", "nonexistent-model", 10, config=cfg
        )
        is None
    )


def test_estimate_tokens_from_duration_with_config(toml_path: Path) -> None:
    cfg = load_quota_config(toml_path)
    tokens = estimate_tokens_from_duration("claude-code", "opus", 10, config=cfg)
    assert tokens == 100_000  # 10s * 10000 TPS


def test_estimate_tokens_from_duration_without_config_returns_none() -> None:
    """Empty module table + no config -> no TPS data -> None."""
    assert estimate_tokens_from_duration("claude-code", "opus", 10, config=None) is None


def test_estimate_tokens_from_duration_empty_config_returns_none() -> None:
    """An empty config has no TPS table and the module ships none -> None."""
    cfg = HarnessQuotaConfig()  # empty
    assert estimate_tokens_from_duration("claude-code", "opus", 10, config=cfg) is None


def test_load_quota_config_toml_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "harness-quota.toml"
    p.write_text(TOML_FIXTURE, encoding="utf-8")
    cfg = load_quota_config(p)
    assert ("claude-code", "sonnet") in cfg.price_table
    assert cfg.price_table[("claude-code", "sonnet")] == (3.0, 15.0)


def test_config_model_routes_drive_pricing_key() -> None:
    """config.model_routes resolves a provider string back to its short name.

    The module ships no routes, so resolution is fully config-driven; a provider
    string absent from the config stays unnormalized.
    """
    from gptme_usage.harness_models import pricing_key_for_model

    cfg = HarnessQuotaConfig(
        model_routes={"deepseek-v4-pro": "openrouter/deepseek/deepseek-v4-pro@deepseek"}
    )
    # The configured provider string normalizes to the short name.
    assert pricing_key_for_model(
        "gptme", "openrouter/deepseek/deepseek-v4-pro@deepseek", config=cfg
    ) == ("gptme", "deepseek-v4-pro")
    # A provider string not in the config stays unnormalized.
    assert pricing_key_for_model("gptme", "openrouter/other/model", config=cfg) == (
        "gptme",
        "openrouter/other/model",
    )


def test_config_aware_model_source_helpers() -> None:
    """openrouter_models / local_models / gptme_openrouter_context read config."""
    from gptme_usage.harness_models import (
        gptme_openrouter_context,
        local_models,
        openrouter_models,
    )

    cfg = HarnessQuotaConfig(
        quota_sources={
            "deepseek-v4-pro": "openrouter",
            "kimi-k2.6": "openrouter",
            "qwen3.6": "local",
        },
        openrouter_key_contexts={
            "default": "autonomous",
            "deepseek-v4-pro": "autonomous_deepseek",
        },
    )
    assert set(openrouter_models(cfg)) == {"deepseek-v4-pro", "kimi-k2.6"}
    assert local_models(cfg) == ["qwen3.6"]
    assert gptme_openrouter_context("deepseek-v4-pro", cfg) == "autonomous_deepseek"
    assert gptme_openrouter_context("kimi-k2.6", cfg) == "autonomous"  # default
    # Empty/no config: module ships no sources -> empty lists.
    assert openrouter_models() == []
    assert local_models() == []
    # No config -> legacy deepseek heuristic still applies.
    assert gptme_openrouter_context("deepseek-v4-pro") == "autonomous_deepseek"
    assert gptme_openrouter_context("kimi-k2.6") == "autonomous"


# ---------------------------------------------------------------------------
# merge_with_module_defaults
# ---------------------------------------------------------------------------


def test_merge_with_module_defaults_caller_wins() -> None:
    """Caller-supplied entries take priority over module defaults."""
    custom_price: dict[tuple[str, str], tuple[float, float]] = {
        ("claude-code", "opus"): (99.0, 99.0),
    }
    cfg = HarnessQuotaConfig(price_table=custom_price)
    merged = merge_with_module_defaults(cfg)

    # Caller's override must survive.
    assert merged.price_table[("claude-code", "opus")] == (99.0, 99.0)
    # Module defaults for other models must also be present.
    assert len(merged.price_table) >= len(HARNESS_PRICE_USD_PER_1M)


def test_merge_with_module_defaults_fallback_for_unlisted() -> None:
    """Module defaults appear for models NOT in the caller's config."""
    cfg = HarnessQuotaConfig()  # empty custom config
    merged = merge_with_module_defaults(cfg)

    # The merged table should contain everything from the module defaults.
    for key, val in HARNESS_PRICE_USD_PER_1M.items():
        assert merged.price_table[key] == val, f"missing/wrong default for {key}"


def test_merge_with_module_defaults_preserves_caller_metadata() -> None:
    """Non-table fields (openrouter_key_contexts, claude_plan_tier) pass through."""
    cfg = HarnessQuotaConfig(
        openrouter_key_contexts={"default": "my-ctx", "special": "other-ctx"},
        claude_plan_tier="max-5x",
    )
    merged = merge_with_module_defaults(cfg)

    assert merged.claude_plan_tier == "max-5x"
    assert merged.openrouter_key_contexts == {
        "default": "my-ctx",
        "special": "other-ctx",
    }


def test_merge_with_module_defaults_does_not_mutate_input() -> None:
    """merge_with_module_defaults returns a new object; it must not mutate *config*."""
    cfg = HarnessQuotaConfig()
    original_len = len(cfg.price_table)
    merge_with_module_defaults(cfg)
    assert len(cfg.price_table) == original_len, "input config was mutated"
