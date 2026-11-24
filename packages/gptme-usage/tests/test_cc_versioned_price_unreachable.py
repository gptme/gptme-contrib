"""Versioned claude-code price rows are unreachable: pricing_key_for_model
collapses every opus-*/sonnet-*/haiku-* id to the bare family key."""

import pytest
from gptme_usage.harness_models import (
    HarnessQuotaConfig,
    estimate_session_cost,
    estimate_tokens_from_duration,
)

# Mirrors the real harness-quota.toml shape: family row + versioned rows.
CFG = HarnessQuotaConfig(
    price_table={
        ("claude-code", "opus"): (5.0, 25.0),
        ("claude-code", "opus-5-5"): (4.0, 20.0),
        ("claude-code", "sonnet"): (3.0, 15.0),
        ("claude-code", "sonnet-5-5"): (2.0, 10.0),
    }
)


@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-opus-5-5", 4.0 + 20.0),
        ("opus-5-5", 4.0 + 20.0),
        ("claude-sonnet-5-5", 2.0 + 10.0),
    ],
)
def test_versioned_row_is_used(model: str, expected: float) -> None:
    cost = estimate_session_cost(
        "claude-code",
        model,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        config=CFG,
    )
    assert cost == pytest.approx(expected)


def test_family_row_still_used_when_no_versioned_row() -> None:
    # Regression guard for the fix: opus -> opus-5 (pin) has no row, so the
    # family row must still price it.
    cost = estimate_session_cost(
        "claude-code", "opus", input_tokens=1_000_000, config=CFG
    )
    assert cost == pytest.approx(5.0)


def test_duration_estimate_falls_back_to_family_tps_row() -> None:
    # A versioned price row must not hide the family TPS row.
    cfg = HarnessQuotaConfig(
        price_table=dict(CFG.price_table),
        tps_table={("claude-code", "opus"): 100.0},
    )
    assert (
        estimate_tokens_from_duration("claude-code", "claude-opus-5-5", 10, config=cfg)
        == 1000
    )


def test_total_only_estimate_survives_a_versioned_row_missing_from_subscription_set() -> (
    None
):
    # ``opus`` resolves to ``opus-5``; a configured ``opus-5`` row must not turn
    # a token_count-only estimate into None just because the subscription set
    # lists only other versions.
    cfg = HarnessQuotaConfig(
        price_table={
            ("claude-code", "opus"): (5.0, 25.0),
            ("claude-code", "opus-5"): (4.0, 20.0),
        }
    )
    cost = estimate_session_cost(
        "claude-code", "opus", token_count=1_000_000, config=cfg
    )
    assert cost is not None and cost > 0
