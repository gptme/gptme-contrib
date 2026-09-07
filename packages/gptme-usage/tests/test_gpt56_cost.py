"""GPT-5.6 routes retain the existing subscription/cache cost heuristics."""

import pytest
from gptme_usage.harness_models import (
    HarnessQuotaConfig,
    estimate_session_cost,
    estimate_tokens_from_duration,
)

pytestmark = [
    pytest.mark.parametrize("harness", ["codex", "gptme"]),
    pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]),
]


@pytest.fixture()
def config(harness: str, model: str) -> HarnessQuotaConfig:
    # Deliberately synthetic prices and throughput: test the cost contract,
    # independent of any agent's config or current vendor rates.
    return HarnessQuotaConfig(
        price_table={(harness, model): (4.0, 20.0)},
        tps_table={(harness, model): 1000.0},
        model_routes={model: f"openai-subscription/{model}"},
    )


def test_total_only_uses_subscription_heuristic(
    harness: str, model: str, config: HarnessQuotaConfig
) -> None:
    assert estimate_session_cost(
        harness, model, token_count=1_000_000, config=config
    ) == pytest.approx(2.0)


def test_duration_tokens_use_subscription_heuristic(
    harness: str, model: str, config: HarnessQuotaConfig
) -> None:
    tokens = estimate_tokens_from_duration(harness, model, 60, config=config)
    assert tokens == 60_000
    assert estimate_session_cost(
        harness, model, token_count=tokens, config=config
    ) == pytest.approx(0.12)


def test_observed_breakdown_discounts_only_cache_reads(
    harness: str, model: str, config: HarnessQuotaConfig
) -> None:
    # $4 input + $20 output + $4 cache creation + $2 cache reads.
    # An inconsistent total must not override the observed breakdown.
    assert estimate_session_cost(
        harness,
        model,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        token_count=99_000_000,
        config=config,
    ) == pytest.approx(30.0)


def test_provider_route_uses_same_cost(
    harness: str, model: str, config: HarnessQuotaConfig
) -> None:
    routed_model = f"openai-subscription/{model}" if harness == "gptme" else model
    assert estimate_session_cost(
        harness, routed_model, cache_read_tokens=1_000_000, config=config
    ) == pytest.approx(2.0)
