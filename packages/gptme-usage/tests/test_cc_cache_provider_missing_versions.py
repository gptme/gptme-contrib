"""Anthropic models whose pricing key is not listed in _CACHE_PRICING_PROVIDER
get cache reads billed at the full input rate (10x) and lose the
token_count-only subscription estimate."""

import pytest
from gptme_usage.harness_models import HarnessQuotaConfig, estimate_session_cost

CFG = HarnessQuotaConfig(
    price_table={
        ("claude-code", "fable-5"): (15.0, 75.0),
        ("claude-code", "fable-5-1"): (15.0, 75.0),
        ("claude-code", "fable-5-2"): (15.0, 75.0),
        ("claude-code", "haiku"): (0.10, 0.50),
    }
)


@pytest.mark.parametrize(
    "model,expected",
    [
        ("fable-5", 1.5),  # control: passes today
        ("fable", 1.5),  # floating alias -> pinned fable-5-1
        ("claude-fable-5-1", 1.5),
        ("claude-fable-5-2", 1.5),
        ("claude-haiku-5-5", 0.01),
    ],
)
def test_cache_reads_use_anthropic_multiplier(model: str, expected: float) -> None:
    cost = estimate_session_cost(
        "claude-code", model, cache_read_tokens=1_000_000, config=CFG
    )
    assert cost == pytest.approx(expected)


@pytest.mark.parametrize("model", ["fable", "claude-fable-5-1", "claude-fable-5-2"])
def test_token_count_only_fable_is_estimated(model: str) -> None:
    # fable-5 gets $1.50 here (tested upstream); its successors return None.
    cost = estimate_session_cost(
        "claude-code", model, token_count=1_000_000, config=CFG
    )
    assert cost == pytest.approx(1.5)
