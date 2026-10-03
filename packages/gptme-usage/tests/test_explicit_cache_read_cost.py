"""Explicit model cache rates override provider heuristics without changing counters."""

from pathlib import Path

import pytest
from gptme_usage.config import merge_with_module_defaults
from gptme_usage.harness_models import (
    HarnessQuotaConfig,
    estimate_session_cost,
    load_quota_config,
)


def test_load_explicit_cache_prices(tmp_path: Path) -> None:
    path = tmp_path / "quota.toml"
    path.write_text(
        '[prices.gptme]\n"glm-5.3-flash" = [0.15, 0.50]\n'
        '[cache_read_prices.gptme]\n"glm-5.3-flash" = 0.03\n'
        'free = 0.0\nnegative = -1.0\nnan = nan\ninfinite = inf\nbad = "oops"\n'
    )
    cfg = load_quota_config(path)
    assert cfg.cache_read_price_table == {
        ("gptme", "glm-5.3-flash"): 0.03,
        ("gptme", "free"): 0.0,
    }
    assert (
        merge_with_module_defaults(cfg).cache_read_price_table
        == cfg.cache_read_price_table
    )


@pytest.mark.parametrize("rate,expected", [(0.03, 0.03), (0.0, 0.0)])
def test_cache_only_explicit_rate(rate: float, expected: float) -> None:
    cfg = HarnessQuotaConfig(
        price_table={("gptme", "glm-5.3-flash"): (0.15, 0.50)},
        cache_read_price_table={("gptme", "glm-5.3-flash"): rate},
        model_routes={"glm-5.3-flash": "openrouter/z-ai/glm-5.3-flash@z-ai"},
    )
    assert (
        estimate_session_cost(
            "gptme",
            "openrouter/z-ai/glm-5.3-flash@z-ai",
            cache_read_tokens=1_000_000,
            config=cfg,
        )
        == expected
    )


def test_mixed_gptme_usage_and_unconfigured_fallback() -> None:
    cfg = HarnessQuotaConfig(
        price_table={("gptme", "glm-5.3-flash"): (0.15, 0.50)},
        cache_read_price_table={("gptme", "glm-5.3-flash"): 0.03},
    )
    args = dict(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )
    assert estimate_session_cost("gptme", "glm-5.3-flash", config=cfg, **args) == 0.83
    cfg.cache_read_price_table.clear()
    assert estimate_session_cost("gptme", "glm-5.3-flash", config=cfg, **args) == 0.95


def test_explicit_codex_rate_preserves_inclusive_input() -> None:
    cfg = HarnessQuotaConfig(
        price_table={("codex", "gpt-5.6-sol"): (5.0, 30.0)},
        cache_read_price_table={("codex", "gpt-5.6-sol"): 0.2},
    )
    assert (
        estimate_session_cost(
            "codex",
            "gpt-5.6-sol",
            input_tokens=1_000_000,
            cache_read_tokens=800_000,
            config=cfg,
        )
        == 1.16
    )
    assert (
        estimate_session_cost(
            "codex",
            "gpt-5.6-sol",
            input_tokens=1_000_000,
            cache_read_tokens=800_000,
            token_count=1_800_000,
            config=cfg,
        )
        == 5.16
    )


def test_unknown_model_stays_unknown() -> None:
    cfg = HarnessQuotaConfig(
        price_table={("gptme", "known"): (0.15, 0.50)},
        cache_read_price_table={("gptme", "unknown"): 0.03},
    )
    assert (
        estimate_session_cost(
            "gptme", "unknown", cache_read_tokens=1_000_000, config=cfg
        )
        is None
    )
