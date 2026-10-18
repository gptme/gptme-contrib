"""Execution evidence needed by hosted and local review adapters."""

from dataclasses import replace
from typing import Any

import pytest
from gptme_review import core


@pytest.fixture
def target():
    return core.ReviewTarget(
        repo="owner/project",
        number=42,
        title="Fix behavior",
        body="Expected behavior",
        diff="diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
        file_contents={"a.py": "new\n"},
        extra_context="Exact-head coverage disclosure",
    )


@pytest.fixture(autouse=True)
def public_repo_policy(monkeypatch):
    # Tests exercise dispatch and forwarding, not authenticated forge discovery.
    monkeypatch.setattr(core, "repo_is_sensitive", lambda repo: False)


@pytest.fixture
def config():
    return core.ReviewConfig(passes=1, min_agreement=1, until_dry=False, route=None)


def test_single_pass_preserves_transport_and_usage_contract(
    monkeypatch, target, config
):
    usage = {
        "cost": 0.012,
        "provider": "provider-a",
        "raw_output": "raw output",
        "change_summary": {"summary": "Fix behavior"},
    }
    seen: dict[str, Any] = {}
    deadline = core.ReviewDeadline(30)

    def transport(prompt, **kwargs):
        seen.update(prompt=prompt, **kwargs)
        return [], usage

    monkeypatch.setattr(core, "openrouter_review", transport)
    monkeypatch.setattr(
        core, "fanout_llm_review", lambda **kw: pytest.fail("unexpected fan-out")
    )
    result = core.review(
        target,
        config,
        api_key="test-key",
        fanout=False,
        deadline=deadline,
        usage_context={"files_preloaded": 1},
    )

    assert seen["prompt"] == target.build_prompt()
    assert seen["deadline"] is deadline
    assert seen["no_train"] is False
    assert seen["_usage_ctx"] == {
        "consumer": "sweep",
        "repo_pr": "owner/project#42",
        "mode": "review",
        "files_preloaded": 1,
    }
    assert result.usage == usage
    assert result.raw_output == "raw output"
    assert result.change_summary == usage["change_summary"]
    assert result.passes == 1


@pytest.mark.parametrize("until_dry,passes", [(False, 3), (True, 1)])
def test_general_consensus_and_until_dry_keep_full_evidence(
    monkeypatch, target, config, until_dry, passes
):
    usage = {
        "consensus_requested_passes": 3,
        "consensus_passes": 1,
        "consensus_requested_jobs": 3,
        "consensus_answered_jobs": 1,
        "consensus_min_agreement_requested": 2,
        "consensus_min_agreement": 1,
        "pass_failures": ["provider timed out"],
        "until_dry_stopped": "error",
        "cost": 0.01,
    }
    deadline = core.ReviewDeadline(30)
    seen: dict[str, Any] = {}

    def transport(**kwargs):
        seen.update(kwargs)
        return [], usage

    monkeypatch.setattr(core, "fanout_llm_review", transport)
    monkeypatch.setattr(
        core,
        "openrouter_review",
        lambda *a, **kw: pytest.fail("unexpected single pass"),
    )
    monkeypatch.setattr(
        core,
        "select_aspects",
        lambda *a, **kw: pytest.fail("unexpected aspect expansion"),
    )
    result = core.review(
        target,
        replace(config, passes=passes, until_dry=until_dry),
        api_key="test-key",
        fanout=False,
        deadline=deadline,
        usage_context={"files_preloaded": 1},
    )

    assert seen["aspects"] == {"general": ""}
    assert seen["deadline"] is deadline
    assert seen["usage_context"] == {"files_preloaded": 1}
    assert seen["until_dry"] is until_dry
    assert seen["passes"] == passes
    assert result.usage == usage  # Do not turn degraded consensus into a clean verdict.
    assert result.passes == 1


def test_summary_only_findings_remain_available_to_artifact_adapter(
    monkeypatch, target, config
):
    advisory = {
        "title": "Update docs",
        "file": "a.py",
        "severity": "P2",
        "class": "docs",
    }
    monkeypatch.setattr(core, "fanout_llm_review", lambda **kw: ([advisory], {}))
    result = core.review(target, config, api_key="test-key")
    assert result.findings == []
    assert result.summary_only_findings == [advisory]
    assert result.blocking is False
    assert result.shortfalls == []


def test_result_evidence_defaults_are_not_shared():
    def result():
        return core.ReviewResult([], {}, None, False, [], "llm", "model", 1)

    first, second = result(), result()
    first.usage["cost"] = 1
    first.summary_only_findings.append({"title": "advisory"})
    assert second.usage == {}
    assert second.summary_only_findings == []


@pytest.mark.parametrize("fanout", [False, True])
def test_usage_context_can_preserve_local_attribution(
    monkeypatch, target, config, fanout
):
    calls = []

    def transport(prompt, **kwargs):
        calls.append(kwargs)
        return [], {}

    monkeypatch.setattr(core, "openrouter_review", transport)
    monkeypatch.setattr(core, "select_aspects", lambda *a, **kw: {"general": ""})
    core.review(
        replace(target, number=0),
        config,
        api_key="test-key",
        fanout=fanout,
        usage_context={"consumer": "local", "repo_pr": target.repo},
    )
    assert len(calls) == 1
    assert calls[0]["_usage_ctx"]["consumer"] == "local"
    assert calls[0]["_usage_ctx"]["repo_pr"] == target.repo


@pytest.mark.parametrize("fanout", [False, True])
def test_expired_job_deadline_prevents_any_wire_request(
    monkeypatch, target, config, fanout
):
    now = [0.0]
    deadline = core.ReviewDeadline(1, clock=lambda: now[0])
    now[0] = 2.0
    monkeypatch.setattr(core, "select_aspects", lambda *a, **kw: {"general": ""})
    monkeypatch.setattr(
        core,
        "_post_openrouter",
        lambda *a, **kw: pytest.fail("expired deadline reached the wire"),
    )
    with pytest.raises(core.ReviewError, match="deadline"):
        core.review(
            target, config, api_key="test-key", fanout=fanout, deadline=deadline
        )


def test_direct_request_preserves_sensitive_repo_routing(monkeypatch, target, config):
    seen = []
    monkeypatch.setattr(core, "repo_is_sensitive", lambda repo: True)

    def transport(prompt, **kwargs):
        seen.append(kwargs["no_train"])
        return [], {}

    monkeypatch.setattr(core, "openrouter_review", transport)
    core.review(target, config, api_key="test-key", fanout=False)
    assert seen == [True]
