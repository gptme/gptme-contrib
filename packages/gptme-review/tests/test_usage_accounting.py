"""Billing evidence must survive a failed generation-metadata lookup."""

import json

import pytest
from gptme_review import core


@pytest.mark.parametrize("fetch_cost", [True, False])
def test_response_preserves_inline_cost_when_metadata_unavailable(
    monkeypatch, fetch_cost
):
    monkeypatch.setattr(core, "_generation_metadata", lambda *a, **k: {})
    response: dict = {
        "id": "gen-inline",
        "provider": "Example",
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "cost": 0.0042},
    }
    usage = core._response_usage(
        response, model="m", api_key="k", fetch_cost=fetch_cost
    )
    assert usage["cost"] == 0.0042
    assert usage["cost_method"] == "openrouter_response"
    assert usage["generation_id"] == "gen-inline"
    assert response["usage"]["cost"] == 0.0042


def test_generation_metadata_takes_precedence_including_zero(monkeypatch):
    monkeypatch.setattr(
        core, "_generation_metadata", lambda *a, **k: {"cost": 0.0, "provider": "B"}
    )
    usage = core._response_usage(
        {"id": "gen-zero", "usage": {"cost": 0.04}}, model="m", api_key="k"
    )
    assert usage["cost"] == 0.0
    assert usage["cost_method"] == "openrouter_generation"
    assert usage["provider"] == "B"


@pytest.mark.parametrize("invalid", [None, True, "0.2", -1, float("nan"), float("inf")])
def test_invalid_inline_cost_is_not_billing_evidence(monkeypatch, invalid):
    monkeypatch.setattr(core, "_generation_metadata", lambda *a, **k: {})
    usage = core._response_usage({"usage": {"cost": invalid}}, model="m", api_key="k")
    assert usage["cost"] is None
    assert usage.get("cost_method") is None


def test_usage_ledger_retains_billing_source_and_generation_id(monkeypatch, tmp_path):
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setattr(core, "USAGE_LEDGER", ledger)
    monkeypatch.setattr(core, "resolve_review_route", lambda: None)
    core._log_usage_record(
        {
            "cost": 0.0042,
            "cost_method": "openrouter_response",
            "generation_id": "gen-inline",
            "prompt_tokens": 12,
        },
        consumer="test",
    )
    row = json.loads(ledger.read_text())
    assert row["cost"] == 0.0042
    assert row["cost_method"] == "openrouter_response"
    assert row["generation_id"] == "gen-inline"


def test_missing_cost_stays_estimate_not_billed_zero(monkeypatch, tmp_path):
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setattr(core, "USAGE_LEDGER", ledger)
    monkeypatch.setattr(core, "resolve_review_route", lambda: None)
    core._log_usage_record({"prompt_tokens": 100}, consumer="test", outcome="fast_fail")
    row = json.loads(ledger.read_text())
    assert row["cost"] > 0
    assert row["cost_method"] == "prompt_estimate"
