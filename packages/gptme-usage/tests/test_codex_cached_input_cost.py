"""Codex input_tokens include cached input; cache reads must not be billed twice."""

import pytest
from gptme_usage.harness_models import HarnessQuotaConfig, estimate_session_cost


def _config(harness: str, model: str) -> HarnessQuotaConfig:
    return HarnessQuotaConfig(price_table={(harness, model): (5.0, 30.0)})


def test_codex_real_record_not_double_counted() -> None:
    # 2026-09-29 codex gpt-5.6-sol record: token_count == input + output, so
    # input already contains the 2,228,352 cached tokens. Recorded cost was
    # 18.02 (cached billed at full price AND at 0.5x); correct is ~6.88.
    cost = estimate_session_cost(
        "codex",
        "gpt-5.6-sol",
        input_tokens=2_433_444,
        cache_read_tokens=2_228_352,
        output_tokens=9_459,
        token_count=2_442_903,
        config=_config("codex", "gpt-5.6-sol"),
    )
    expected = (
        (2_433_444 - 2_228_352) * 5.0 + 9_459 * 30.0 + 2_228_352 * 5.0 * 0.5
    ) / 1e6
    assert cost == pytest.approx(expected)
    assert cost == pytest.approx(6.88, abs=0.01)


def test_gptme_input_excludes_cache_reads_unchanged() -> None:
    # gptme records exclude cache reads from input (token_count == i + o + cr).
    cost = estimate_session_cost(
        "gptme",
        "gpt-5.6-sol",
        input_tokens=1_000_000,
        cache_read_tokens=2_000_000,
        output_tokens=0,
        config=_config("gptme", "gpt-5.6-sol"),
    )
    assert cost == pytest.approx((1_000_000 * 5.0 + 2_000_000 * 2.5) / 1e6)


def test_codex_unknown_cache_provider_bills_cached_at_input_rate_once() -> None:
    # gpt-6-astra has no verified cache multiplier: cached input is charged at
    # the full input rate exactly once (no double count, no invented discount).
    cost = estimate_session_cost(
        "codex",
        "gpt-6-astra",
        input_tokens=1_000_000,
        cache_read_tokens=800_000,
        output_tokens=0,
        config=_config("codex", "gpt-6-astra"),
    )
    assert cost == pytest.approx(5.0)
