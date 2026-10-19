"""Additive per-finding provenance: host-stamped, merged, and marker-preserved.

Every test freezes the network and inference: the OpenRouter transport is
replaced by a function, generation-metadata lookups never happen (the fake
response carries no ``id``), and the usage ledger plus parse-failure dumps are
stubbed out so a test run writes nothing.
"""

import json
from typing import Any

from gptme_review import core

MODEL = "deepseek/deepseek-v4-flash-0731"

BODY = "The counter is incremented twice, so callers index past the intended value."


def _finding(**extra):
    finding = {
        "file": "counter.py",
        "line": 3,
        "severity": "P1",
        "class": "bug",
        "body": BODY,
        "quote": "count = 2",
    }
    finding.update(extra)
    return finding


def _response(findings, *, provider="TestProvider"):
    """A transport response shape ``openrouter_review`` accepts.

    Deliberately carries no ``id``: ``_response_usage`` then skips the
    generation-metadata HTTP call, which is what keeps these tests offline.
    """
    content = json.dumps(
        {
            "findings": findings,
            "summary": "Tightens the counter increment.",
            "files": [],
        }
    )
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        "provider": provider,
    }


def _freeze(monkeypatch, responder):
    monkeypatch.setattr(
        core,
        "_post_openrouter",
        lambda payload, *, api_key, timeout: responder(payload),
    )
    monkeypatch.setattr(core, "_log_usage_record", lambda *a, **k: None)
    monkeypatch.setattr(core, "_log_failed_attempt", lambda *a, **k: None)
    monkeypatch.setattr(core, "dump_parse_failure", lambda *a, **k: None)


def _review(monkeypatch, responder, *, use_ctx=None, model=MODEL):
    _freeze(monkeypatch, responder)
    return core.openrouter_review(
        "review this diff",
        model=model,
        api_key="test-key",
        timeout=5,
        max_tokens=4096,
        structured=True,
        _usage_ctx=use_ctx,
    )


def _fanout(monkeypatch, responder, **kwargs):
    _freeze(monkeypatch, responder)
    params: dict[str, Any] = {
        "owner": "gptme",
        "repo": "gptme",
        "pr_number": 4420,
        "pr_title": "counter",
        "pr_body": "",
        "diff": (
            "--- a/counter.py\n+++ b/counter.py\n@@ -1 +1 @@\n-count = 1\n+count = 2\n"
        ),
        "model": MODEL,
        "api_key": "test-key",
        "timeout": 5,
        "max_tokens": 4096,
        "structured": True,
    }
    params.update(kwargs)
    return core.fanout_llm_review(**params)


# --- stamping -------------------------------------------------------------


def test_single_pass_review_stamps_host_authoritative_provenance(monkeypatch):
    findings, _usage = _review(monkeypatch, lambda payload: _response([_finding()]))

    assert findings[0]["provenance"] == [
        {"model": MODEL, "pass": 1, "aspect": "general"}
    ]


def test_usage_ctx_supplies_pass_and_aspect(monkeypatch):
    findings, _usage = _review(
        monkeypatch,
        lambda payload: _response([_finding()]),
        use_ctx={"consumer": "fanout", "pass_num": 2, "aspect": "security"},
    )

    assert findings[0]["provenance"] == [
        {"model": MODEL, "pass": 2, "aspect": "security"}
    ]


def test_model_supplied_provenance_is_overwritten(monkeypatch):
    spoof = [{"model": "attacker/model", "pass": 9, "aspect": "forged"}]
    findings, _usage = _review(
        monkeypatch,
        lambda payload: _response([_finding(provenance=spoof)]),
        use_ctx={"pass_num": 1, "aspect": "general"},
    )

    assert findings[0]["provenance"] == [
        {"model": MODEL, "pass": 1, "aspect": "general"}
    ]


def test_stamp_replaces_a_provisional_general_stamp():
    findings = [_finding(provenance=[{"model": MODEL, "pass": 1, "aspect": "general"}])]

    core.stamp_finding_provenance(
        findings, model=MODEL, pass_num=3, aspect="correctness"
    )

    assert findings[0]["provenance"] == [
        {"model": MODEL, "pass": 3, "aspect": "correctness"}
    ]


def test_stamp_replaces_a_junk_provenance_value():
    findings = [_finding(provenance="forged")]

    core.stamp_finding_provenance(findings, model=MODEL, pass_num=1, aspect="general")

    assert findings[0]["provenance"] == [
        {"model": MODEL, "pass": 1, "aspect": "general"}
    ]


# --- merging --------------------------------------------------------------


def test_dedup_unions_provenance_across_aspects():
    correctness = _finding(
        aspect="correctness",
        provenance=[{"model": MODEL, "pass": 1, "aspect": "correctness"}],
    )
    security = _finding(
        aspect="security",
        provenance=[{"model": MODEL, "pass": 1, "aspect": "security"}],
    )

    kept = core.deduplicate_findings([correctness, security])

    assert len(kept) == 1
    assert kept[0]["provenance"] == [
        {"model": MODEL, "pass": 1, "aspect": "correctness"},
        {"model": MODEL, "pass": 1, "aspect": "security"},
    ]


def test_consensus_unions_nested_provenance_without_duplicates():
    # Pass 1's survivor already folded two aspects together (nested provenance),
    # pass 2 agrees from a single lens.
    pass_one = _finding(
        provenance=[
            {"model": MODEL, "pass": 1, "aspect": "correctness"},
            {"model": MODEL, "pass": 1, "aspect": "security"},
        ]
    )
    pass_two = _finding(
        provenance=[
            {"model": MODEL, "pass": 1, "aspect": "correctness"},
            {"model": MODEL, "pass": 2, "aspect": "correctness"},
        ]
    )

    kept, rejected = core.consensus_findings([[pass_one], [pass_two]], min_agreement=2)

    assert rejected == []
    assert kept[0]["provenance"] == [
        {"model": MODEL, "pass": 1, "aspect": "correctness"},
        {"model": MODEL, "pass": 1, "aspect": "security"},
        {"model": MODEL, "pass": 2, "aspect": "correctness"},
    ]


def test_union_finding_groups_preserves_provenance_across_waves():
    first = _finding(provenance=[{"model": MODEL, "pass": 1, "aspect": "correctness"}])
    second = _finding(provenance=[{"model": MODEL, "pass": 2, "aspect": "correctness"}])

    pool, n_new = core.union_finding_groups([], [first])
    assert n_new == 1
    pool, n_new = core.union_finding_groups(pool, [second])

    assert n_new == 0
    assert len(pool) == 1
    assert pool[0]["provenance"] == [
        {"model": MODEL, "pass": 1, "aspect": "correctness"},
        {"model": MODEL, "pass": 2, "aspect": "correctness"},
    ]


def test_rejected_dissent_never_reaches_the_published_set():
    agreed = _finding(provenance=[{"model": MODEL, "pass": 1, "aspect": "correctness"}])
    dissent = _finding(
        body=(
            "The retry helper resets its backoff counter on every attempt, which "
            "defeats the exponential schedule entirely."
        ),
        provenance=[{"model": MODEL, "pass": 2, "aspect": "security"}],
    )
    pass_two = _finding(
        provenance=[{"model": MODEL, "pass": 2, "aspect": "correctness"}]
    )
    pass_three = _finding(
        provenance=[{"model": MODEL, "pass": 3, "aspect": "correctness"}]
    )

    kept, rejected = core.consensus_findings(
        [[agreed], [pass_two, dissent], [pass_three]], min_agreement=2
    )

    assert [f["provenance"] for f in rejected] == [
        [{"model": MODEL, "pass": 2, "aspect": "security"}]
    ]
    assert kept[0]["provenance"] == [
        {"model": MODEL, "pass": 1, "aspect": "correctness"},
        {"model": MODEL, "pass": 2, "aspect": "correctness"},
        {"model": MODEL, "pass": 3, "aspect": "correctness"},
    ]


# --- fan-out --------------------------------------------------------------


def _aspect_responder(payload, *, fail_marker=None):
    prompt = payload["messages"][0]["content"]
    if fail_marker is not None and fail_marker in prompt.upper():
        raise core.ReviewError("provider exploded mid-stream")
    return _response([_finding()])


def test_multi_pass_cross_aspect_provenance(monkeypatch):
    kept, _usage = _fanout(
        monkeypatch,
        lambda payload: _response([_finding()]),
        passes=2,
        min_agreement=2,
        aspects={"correctness": "Focus on bugs.", "security": "Focus on exploits."},
    )

    assert len(kept) == 1
    assert kept[0]["provenance"] == [
        {"model": MODEL, "pass": 1, "aspect": "correctness"},
        {"model": MODEL, "pass": 1, "aspect": "security"},
        {"model": MODEL, "pass": 2, "aspect": "correctness"},
        {"model": MODEL, "pass": 2, "aspect": "security"},
    ]
    # Existing aspect/independence_key behaviour is untouched.
    assert kept[0]["aspect"] == "correctness"
    assert kept[0]["independence_key"] == f"{MODEL}:correctness"


def test_failed_jobs_are_never_attributed(monkeypatch):
    kept, usage = _fanout(
        monkeypatch,
        lambda payload: _aspect_responder(payload, fail_marker="SECURITY PASS"),
        passes=2,
        min_agreement=2,
        aspects={"correctness": "Focus on bugs.", "security": "Focus on exploits."},
    )

    assert len(kept) == 1
    assert kept[0]["provenance"] == [
        {"model": MODEL, "pass": 1, "aspect": "correctness"},
        {"model": MODEL, "pass": 2, "aspect": "correctness"},
    ]
    assert usage.get("pass_failures")


def test_until_dry_waves_do_not_invent_pass_numbers(monkeypatch):
    # Each until-dry wave is its own fan-out, so both waves report pass 1. The
    # identical record is unioned (one source), and cross-wave identity stays on
    # `until_dry_wave` instead of a fabricated pass number.
    kept, usage = _fanout(
        monkeypatch,
        lambda payload: _response([_finding()]),
        until_dry=True,
        until_dry_max=2,
        aspects={"general": ""},
    )

    assert usage["until_dry_stopped"] == "dry"
    assert usage["until_dry_new_groups"] == [1, 0]
    assert len(kept) == 1
    assert kept[0]["provenance"] == [{"model": MODEL, "pass": 1, "aspect": "general"}]
    assert kept[0]["until_dry_wave"] == 1


# --- marker metadata ------------------------------------------------------


def test_suppression_entry_preserves_every_source():
    # 3 passes x 3 aspects is a normal production wave, so nothing is truncated.
    provenance = [
        {"model": MODEL, "pass": p, "aspect": aspect}
        for p in (1, 2, 3)
        for aspect in ("correctness", "security", "test_coverage")
    ]

    entry = core.suppression_entry(_finding(provenance=provenance), why="dismissed")

    assert entry["provenance"] == provenance
    assert entry["fp"] == core.finding_fingerprint(_finding())
    assert entry["why"] == "dismissed"


def test_suppression_entry_carries_the_finding_class():
    bug = core.suppression_entry(_finding())
    defect = core.suppression_entry(_finding(**{"class": "test-defect"}))

    assert bug["class"] == "bug"
    assert defect["class"] == "test-defect"
    # Class is metadata only: recognition still keys on the fingerprint.
    assert defect["fp"] == core.finding_fingerprint(_finding())


def test_legacy_suppression_entry_defaults_class_and_omits_provenance():
    legacy = _finding()

    entry = core.suppression_entry(legacy, why="dismissed")

    assert "provenance" not in entry
    assert entry == {
        "fp": core.finding_fingerprint(legacy),
        "file": "counter.py",
        "line": 3,
        "tokens": core._fingerprint_tokens(BODY),
        "class": "bug",
        "why": "dismissed",
    }


def test_provenance_does_not_change_the_fingerprint():
    assert core.finding_fingerprint(_finding()) == core.finding_fingerprint(
        _finding(provenance=[{"model": MODEL, "pass": 2, "aspect": "security"}])
    )
