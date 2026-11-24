"""load_quota_config is meant to degrade gracefully, but malformed top-level
tables crash it and non-finite tps/prices load and later crash/poison math."""

from pathlib import Path

import pytest
from gptme_usage.harness_models import (
    HarnessQuotaConfig,
    estimate_tokens_from_duration,
    load_quota_config,
)


@pytest.mark.parametrize(
    "body",
    [
        "prices = 5\n",
        'quota_sources = "chatgpt"\n',
        "tps = [1, 2]\n",
        "model_routes = 3\n",
    ],
)
def test_non_table_section_does_not_crash(tmp_path: Path, body: str) -> None:
    p = tmp_path / "q.toml"
    p.write_text(body)
    assert isinstance(load_quota_config(p), HarnessQuotaConfig)


@pytest.mark.parametrize("value", ["inf", "nan", "-100"])
def test_non_finite_or_negative_tps_rejected(tmp_path: Path, value: str) -> None:
    p = tmp_path / "q.toml"
    p.write_text(f"[tps.claude-code]\nopus = {value}\n")
    cfg = load_quota_config(p)
    assert ("claude-code", "opus") not in cfg.tps_table
    # Must never raise or return a negative token count.
    est = estimate_tokens_from_duration("claude-code", "opus", 10, config=cfg)
    assert est is None or est >= 0


@pytest.mark.parametrize("pair", ["[nan, 1.0]", "[-5.0, 1.0]", "[1.0, inf]"])
def test_non_finite_or_negative_prices_rejected(tmp_path: Path, pair: str) -> None:
    p = tmp_path / "q.toml"
    p.write_text(f"[prices.gptme]\nm = {pair}\n")
    assert ("gptme", "m") not in load_quota_config(p).price_table
