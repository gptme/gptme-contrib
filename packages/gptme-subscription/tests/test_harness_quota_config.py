"""Tests for HarnessQuotaConfig and load_quota_config."""

from __future__ import annotations

from pathlib import Path

import pytest
from gptme_subscription.harness_models import (
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


def test_estimate_session_cost_falls_back_without_config() -> None:
    cost = estimate_session_cost(
        "claude-code",
        "opus",
        cache_read_tokens=1_000_000,
        config=None,
    )
    # Should still work using module-level HARNESS_PRICE_USD_PER_1M
    assert cost is not None
    assert cost > 0


def test_estimate_session_cost_config_overrides_price(toml_path: Path) -> None:
    # Modify the config with a custom price and verify it's used
    cfg = load_quota_config(toml_path)
    # Patch price_table with a custom price for testing
    cfg.price_table[("claude-code", "opus")] = (10.0, 50.0)  # 2x normal
    cost_custom = estimate_session_cost(
        "claude-code",
        "opus",
        cache_read_tokens=1_000_000,
        config=cfg,
    )
    cost_default = estimate_session_cost(
        "claude-code",
        "opus",
        cache_read_tokens=1_000_000,
        config=None,
    )
    assert cost_custom is not None
    assert cost_default is not None
    # Custom price is 2x, so cost should be ~2x default
    assert (
        abs(cost_custom / cost_default - 2.0) < 0.01
    ), f"expected 2x ratio, got {cost_custom}/{cost_default}"


def test_estimate_tokens_from_duration_with_config(toml_path: Path) -> None:
    cfg = load_quota_config(toml_path)
    tokens = estimate_tokens_from_duration("claude-code", "opus", 10, config=cfg)
    assert tokens == 100_000  # 10s * 10000 TPS


def test_estimate_tokens_from_duration_falls_back_without_config() -> None:
    tokens = estimate_tokens_from_duration("claude-code", "opus", 10, config=None)
    assert tokens is not None
    assert tokens > 0


def test_estimate_tokens_from_duration_empty_config() -> None:
    cfg = HarnessQuotaConfig()  # empty
    # Should fall back to module-level TOKENS_PER_SECOND
    tokens = estimate_tokens_from_duration("claude-code", "opus", 10, config=cfg)
    assert tokens is not None
    assert tokens > 0


def test_load_quota_config_toml_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "harness-quota.toml"
    p.write_text(TOML_FIXTURE, encoding="utf-8")
    cfg = load_quota_config(p)
    assert ("claude-code", "sonnet") in cfg.price_table
    assert cfg.price_table[("claude-code", "sonnet")] == (3.0, 15.0)


def test_config_model_routes_replace_not_merge() -> None:
    """A non-empty config.model_routes must fully replace GPTME_MODEL_ROUTES.

    Regression guard: earlier code merged the two ({**globals, **config}), so a
    configured agent silently inherited Bob's routes. An agent's config should be
    authoritative — provider models only in Bob's globals must not resolve.
    """
    from gptme_subscription.harness_models import (
        GPTME_MODEL_ROUTES,
        pricing_key_for_model,
    )

    if not GPTME_MODEL_ROUTES:
        pytest.skip("no module-level GPTME_MODEL_ROUTES to test replacement against")

    bob_short, bob_provider = next(iter(GPTME_MODEL_ROUTES.items()))
    # Config with a disjoint route set (does not contain bob_provider).
    cfg = HarnessQuotaConfig(model_routes={"someagent-model": "openrouter/x/y@z"})

    # With replace semantics, Bob's provider model is unknown to this config, so
    # it stays unnormalized instead of resolving to Bob's short name.
    key = pricing_key_for_model("gptme", bob_provider, config=cfg)
    assert key == ("gptme", bob_provider)
    assert key != ("gptme", bob_short)

    # Without config, Bob's globals still resolve normally (unchanged behavior).
    assert pricing_key_for_model("gptme", bob_provider) == ("gptme", bob_short)
