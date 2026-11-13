"""Host-provided panel execution stays local to the requesting call."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import MappingProxyType

import pytest
from gptme_review import panel
from gptme_review.core import ReviewDeadline, ReviewDeadlineExceeded, ReviewError

FIRST = panel.PanelReviewer(
    "host:first", "first", "host", "first", "host", "first", "pool"
)
SECOND = panel.PanelReviewer(
    "host:second", "second", "host", "second", "host", "second", "pool"
)
PRICES = {"host": {"first": (2.0, 10.0), "second": (2.0, 10.0)}}


def review_text():
    return json.dumps({"summary": "Reviewed.", "files": [], "findings": []})


def finding():
    return {
        "body": "`load()` returns None for an empty file, crashing the caller.",
        "file": "src/load.py",
        "line": 10,
        "quote": "value = load()",
        "severity": "P1",
        "class": "bug",
        "security": False,
        "verification": "",
        "suggestion": "",
    }


@pytest.fixture(autouse=True)
def no_live_transports(monkeypatch):
    # A missing injection must fail locally, never launch a subscription CLI.
    monkeypatch.setattr(panel, "TRANSPORTS", {})


def test_transport_mapping_is_call_local_and_default_remains_dynamic(monkeypatch):
    calls = []

    def custom(reviewer, prompt, timeout):
        calls.append((reviewer, prompt, timeout))
        return review_text(), {"input_tokens": 1000, "output_tokens": 100}, None

    def fallback(reviewer, prompt, timeout):
        return review_text(), {}, 0.25

    defaults = {"host": fallback}
    monkeypatch.setattr(panel, "TRANSPORTS", defaults)
    supplied = MappingProxyType({"host": custom})
    result = panel.run_reviewer(
        FIRST, "PROMPT", timeout=7, prices=PRICES, transports=supplied
    )
    assert result.ok
    assert calls == [(FIRST, "PROMPT", 7)]
    assert result.list_price_usd == pytest.approx(0.003)
    assert panel.TRANSPORTS is defaults
    assert dict(supplied) == {"host": custom}
    assert panel.run_reviewer(FIRST, "second").list_price_usd == 0.25
    monkeypatch.setattr(panel, "TRANSPORTS", {"host": custom})
    assert panel.run_reviewer(
        FIRST, "third", prices=PRICES
    ).list_price_usd == pytest.approx(0.003)


def test_empty_transport_mapping_does_not_fall_back(monkeypatch):
    called = []

    def fallback(*args):
        called.append(args)
        return review_text(), {}, None

    monkeypatch.setattr(panel, "TRANSPORTS", {"host": fallback})
    result = panel.run_reviewer(FIRST, "PROMPT", transports={})
    assert not result.ok
    assert "host" in result.error
    assert not called


def test_injected_transport_failure_is_a_reviewer_result():
    def unavailable(*args):
        raise ReviewError("host policy refused this request")

    result = panel.run_reviewer(FIRST, "PROMPT", transports={"host": unavailable})
    assert not result.ok
    assert result.reviewer is FIRST
    assert "host policy refused" in result.error
    assert result.row()["outcome"] == "failed"


def test_runner_forwards_inputs_and_preserves_consensus_accounting(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        raise AssertionError("custom runner must bypass the global runner")

    monkeypatch.setattr(panel, "run_reviewer", forbidden)

    def runner(reviewer, prompt, *, timeout, prices):
        calls.append((reviewer, prompt, timeout, prices))
        return panel.ReviewerResult(
            reviewer,
            True,
            findings=[finding()],
            tokens={"input_tokens": 1000, "output_tokens": 100},
            list_price_usd=0.003,
        )

    findings, usage = panel.panel_review(
        "PROMPT",
        [FIRST, SECOND],
        timeout=7,
        prices=PRICES,
        min_agreement=2,
        runner=runner,
    )
    assert {call[0].key for call in calls} == {FIRST.key, SECOND.key}
    assert all(
        call[1] == "PROMPT" and call[2] == 7 and call[3] is PRICES for call in calls
    )
    assert len(findings) == 1
    assert findings[0]["agreement"] == 2
    assert set(findings[0]["panel_models"]) == {FIRST.key, SECOND.key}
    assert usage["consensus_passes"] == usage["consensus_min_agreement"] == 2
    assert usage["completion_tokens"] == 200
    assert usage["panel"]["list_price_usd"] == pytest.approx(0.006)
    assert panel.run_reviewer is forbidden


def test_default_runner_is_resolved_at_call_time_after_custom_call(monkeypatch):
    calls = []

    def custom(reviewer, prompt, **kwargs):
        calls.append("custom")
        return panel.ReviewerResult(reviewer, True, list_price_usd=0.1)

    def first_default(reviewer, prompt, **kwargs):
        calls.append("first default")
        return panel.ReviewerResult(reviewer, True, list_price_usd=0.2)

    def second_default(reviewer, prompt, **kwargs):
        calls.append("second default")
        return panel.ReviewerResult(reviewer, True, list_price_usd=0.3)

    monkeypatch.setattr(panel, "run_reviewer", first_default)
    panel.panel_review("PROMPT", [FIRST], runner=custom)
    _, usage = panel.panel_review("PROMPT", [FIRST])
    assert usage["panel"]["list_price_usd"] == 0.2
    monkeypatch.setattr(panel, "run_reviewer", second_default)
    _, usage = panel.panel_review("PROMPT", [FIRST])
    assert usage["panel"]["list_price_usd"] == 0.3
    assert calls == ["custom", "first default", "second default"]


def test_custom_runner_receives_shared_deadline_cap():
    seen = []

    def runner(reviewer, prompt, *, timeout, prices):
        seen.append(timeout)
        return panel.ReviewerResult(reviewer, True)

    panel.panel_review(
        "PROMPT", [FIRST], timeout=420, deadline=ReviewDeadline(2), runner=runner
    )
    assert len(seen) == 1
    assert 0 < seen[0] <= 2


def test_custom_runner_failure_keeps_degraded_panel_accounting():
    def runner(reviewer, prompt, **kwargs):
        if reviewer is SECOND:
            return panel.ReviewerResult(reviewer, False, error="host policy refused")
        return panel.ReviewerResult(reviewer, True, findings=[finding()])

    findings, usage = panel.panel_review(
        "PROMPT", [FIRST, SECOND], min_agreement=2, runner=runner
    )
    assert len(findings) == 1
    assert usage["consensus_requested_passes"] == 2
    assert usage["consensus_passes"] == usage["consensus_min_agreement"] == 1
    assert usage["panel"]["reviewers"][1]["outcome"] == "failed"
    assert "host policy refused" in usage["pass_failures"][0]

    with pytest.raises(
        panel.PanelReviewError, match="all subscription panel reviewers failed"
    ) as exc:
        panel.panel_review("PROMPT", [SECOND], runner=runner)
    assert exc.value.reviewer_rows[0]["error"] == "host policy refused"


def test_overlapping_hosts_keep_their_runner_and_transport_routes():
    entered = Barrier(2)
    original_runner = panel.run_reviewer
    original_transports = panel.TRANSPORTS

    def review_for_host(host, cost):
        def transport(reviewer, prompt, timeout):
            entered.wait(timeout=2)
            assert prompt == host
            return review_text(), {"output_tokens": len(host)}, cost

        transports = MappingProxyType({"host": transport})

        def runner(reviewer, prompt, *, timeout, prices):
            return panel.run_reviewer(
                reviewer, prompt, timeout=timeout, prices=prices, transports=transports
            )

        return panel.panel_review(host, [FIRST], runner=runner, prices=PRICES)

    with ThreadPoolExecutor(max_workers=2) as pool:
        bob = pool.submit(review_for_host, "bob", 0.1)
        alice = pool.submit(review_for_host, "alice", 0.2)
        _, bob_usage = bob.result(timeout=5)
        _, alice_usage = alice.result(timeout=5)

    assert bob_usage["panel"]["list_price_usd"] == 0.1
    assert alice_usage["panel"]["list_price_usd"] == 0.2
    assert bob_usage["completion_tokens"] == 3
    assert alice_usage["completion_tokens"] == 5
    assert panel.run_reviewer is original_runner
    assert panel.TRANSPORTS is original_transports
    assert panel.TRANSPORTS == {}


def test_expired_deadline_does_not_invoke_custom_runner():
    called = []
    now = [0.0]
    deadline = ReviewDeadline(1, clock=lambda: now[0])
    now[0] = 2.0

    def runner(reviewer, prompt, **kwargs):
        called.append(reviewer)
        return panel.ReviewerResult(reviewer, True)

    with pytest.raises(ReviewDeadlineExceeded):
        panel.panel_review("PROMPT", [FIRST], runner=runner, deadline=deadline)
    assert not called
