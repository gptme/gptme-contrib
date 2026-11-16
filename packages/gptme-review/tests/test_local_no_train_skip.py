"""Local in-session no-train reviews skip the slow 0731 host; nothing else does."""

import json

import pytest
from gptme_review import core

MODEL = "deepseek/deepseek-v4-flash-0731"


def _capture(monkeypatch, *, no_train, ctx):
    sent: list[dict] = []

    def responder(payload, *, api_key, timeout):
        sent.append(json.loads(json.dumps(payload)))
        content = json.dumps({"findings": [], "summary": "ok", "files": []})
        return {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "provider": "DeepInfra",
        }

    monkeypatch.setattr(core, "_post_openrouter", responder)
    monkeypatch.setattr(core, "quarantined_provider_names", lambda model=None: [])
    monkeypatch.setattr(core, "_log_usage_record", lambda *a, **k: None)
    monkeypatch.setattr(core, "_log_failed_attempt", lambda *a, **k: None)
    monkeypatch.setattr(core, "dump_parse_failure", lambda *a, **k: None)
    core.openrouter_review(
        "review this diff",
        model=MODEL,
        api_key="test-key",
        timeout=5,
        max_tokens=4096,
        structured=True,
        no_train=no_train,
        _usage_ctx=ctx,
    )
    return sent[0]["provider"]


def test_local_no_train_review_ignores_inceptron(monkeypatch):
    block = _capture(monkeypatch, no_train=True, ctx={"consumer": "local"})
    assert block["data_collection"] == "deny"
    assert block["allow_fallbacks"] is True
    assert [n.casefold() for n in block["ignore"]] == ["inceptron"]


@pytest.mark.parametrize("ctx", [{"consumer": "sweep"}, {"consumer": "fanout"}, None])
def test_other_no_train_consumers_keep_the_route(monkeypatch, ctx):
    block = _capture(monkeypatch, no_train=True, ctx=ctx)
    assert "ignore" not in block


def test_local_public_review_keeps_the_shared_pin(monkeypatch):
    block = _capture(monkeypatch, no_train=False, ctx={"consumer": "local"})
    assert block["only"] == list(core.DEEPSEEK_V4_FLASH_0731_PROVIDERS)
    assert block["allow_fallbacks"] is False
    assert "ignore" not in block


def test_variant_suffix_does_not_opt_out():
    assert core.local_no_train_skips(
        MODEL + ":nitro", no_train=True, usage_ctx={"consumer": "local"}
    ) == ("inceptron",)
