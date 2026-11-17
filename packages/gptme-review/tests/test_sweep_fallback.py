from __future__ import annotations

import subprocess
from collections.abc import Mapping

import pytest
from gptme_review.sweep_fallback import (
    FallbackSkip,
    head_already_reviewed,
    is_too_large,
    run_fallback,
    select_fallback_failures,
)

PROVIDER = {
    "pr": "o/r#1",
    "engine": "llm",
    "reason": "OpenRouter HTTP 503: upstream down",
}
LOCAL = {
    "pr": "o/r#2",
    "engine": "llm",
    "reason": "session produced no valid findings block",
}


def _pr(number: int, **extra: object) -> dict[str, object]:
    return {"repo": "o/r", "number": number, "headRefOid": f"head{number}", **extra}


def _proc(code: int, stderr: str = "", stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["review"], code, stdout, stderr)


class FakeHost:
    def __init__(
        self,
        *,
        models: list[list[str]] | None = None,
        admission: list[str | None] | None = None,
        skip: set[str] | None = None,
        procs: dict[str, subprocess.CompletedProcess] | None = None,
    ) -> None:
        self._models = list(models or [["m1"]])
        self._admission = list(admission or [])
        self._skip = skip or set()
        self._procs = procs or {}
        self.executed: list[tuple[str, str]] = []
        self.recovered: list[str] = []
        self.failed: list[str] = []

    @staticmethod
    def _label(pr: Mapping[str, object]) -> str:
        return f"{pr['repo']}#{pr['number']}"

    def available_models(self) -> list[str]:
        return self._models.pop(0) if len(self._models) > 1 else self._models[0]

    def admission(self, pr: Mapping[str, object]) -> str | None:
        return self._admission.pop(0) if self._admission else None

    def resolve_model(self, pr: Mapping[str, object]) -> str:
        if self._label(pr) in self._skip:
            raise FallbackSkip("not admitted for private repo")
        return "m1"

    def execute(
        self, pr: Mapping[str, object], model: str
    ) -> subprocess.CompletedProcess:
        self.executed.append((self._label(pr), model))
        return self._procs.get(self._label(pr), _proc(0))

    def on_recovered(
        self, pr: Mapping[str, object], proc: subprocess.CompletedProcess
    ) -> None:
        self.recovered.append(self._label(pr))

    def on_failed(
        self, pr: Mapping[str, object], proc: subprocess.CompletedProcess
    ) -> dict[str, object]:
        self.failed.append(self._label(pr))
        return {"stdout_capture": "/tmp/capture"}


def _failures(*numbers: int) -> list[dict[str, object]]:
    return [{"pr": f"o/r#{n}", "reason": "x"} for n in numbers]


def _prs(*numbers: int, **extra: object) -> dict[str, dict[str, object]]:
    return {f"o/r#{n}": _pr(n, **extra) for n in numbers}


@pytest.mark.parametrize("cls", ["rate_limited", "systemic"])
def test_wholesale_failure_retries_everything(cls: str) -> None:
    assert select_fallback_failures(
        failure_class=cls, review_failures=[PROVIDER, LOCAL], too_large_failures=0
    ) == (cls, [PROVIDER, LOCAL])


def test_systemic_with_too_large_pr_retries_nothing() -> None:
    assert select_fallback_failures(
        failure_class="systemic", review_failures=[PROVIDER], too_large_failures=1
    ) == ("systemic", [])


def test_rate_limited_retries_even_with_too_large() -> None:
    # Operator precedence in the original host: rate_limited OR (systemic AND
    # no too-large). A rate-limited run is retried regardless.
    assert select_fallback_failures(
        failure_class="rate_limited", review_failures=[PROVIDER], too_large_failures=1
    ) == ("rate_limited", [PROVIDER])


def test_partial_success_retries_only_provider_failures() -> None:
    assert select_fallback_failures(
        failure_class=None, review_failures=[PROVIDER, LOCAL], too_large_failures=0
    ) == ("provider_outage", [PROVIDER])


def test_partial_success_without_provider_failure_retries_nothing() -> None:
    assert select_fallback_failures(
        failure_class=None, review_failures=[LOCAL], too_large_failures=0
    ) == (None, [])


@pytest.mark.parametrize("cls", ["pr_local", "singleton", "capacity"])
def test_pr_local_classes_stay_fail_closed(cls: str) -> None:
    assert select_fallback_failures(
        failure_class=cls, review_failures=[PROVIDER], too_large_failures=0
    ) == (cls, [])


def test_head_already_reviewed() -> None:
    assert head_already_reviewed(_pr(1, headRefOid="abcdef12", reviewed_sha="abcdef"))
    assert not head_already_reviewed(_pr(1, headRefOid="abcdef12", reviewed_sha=""))
    assert not head_already_reviewed(_pr(1, headRefOid="ffff", reviewed_sha="abcdef"))
    # A human request is the exception: it always gets a fallback.
    assert not head_already_reviewed(
        _pr(1, headRefOid="abcdef12", reviewed_sha="abcdef", requested_by="erik")
    )


def test_is_too_large() -> None:
    assert is_too_large("HTTP 406: diff exceeded TOO_LARGE")
    assert not is_too_large("")


def test_recovers_and_reports() -> None:
    host = FakeHost()
    out: list[str] = []
    result = run_fallback(
        _failures(1, 2), _prs(1, 2), host, engine="subscription", emit=out.append
    )
    assert host.executed == [("o/r#1", "m1"), ("o/r#2", "m1")]
    assert host.recovered == ["o/r#1", "o/r#2"]
    assert (result.attempted, result.recovered, result.skipped) == (2, 2, 0)
    assert [f["pr"] for f in result.replaced] == ["o/r#1", "o/r#2"]
    assert result.added == []
    assert result.metadata() == {
        "engine": "subscription",
        "model": "m1",
        "models": ["m1"],
        "attempted": 2,
        "recovered": 2,
        "skipped": 0,
    }
    assert out[-1] == (
        "  fallback engine=subscription: recovered 2/2 PR(s); attempted 2, skipped 0"
    )


def test_failed_attempt_replaces_row_with_classified_failure() -> None:
    host = FakeHost(procs={"o/r#1": _proc(1, stderr="OpenRouter HTTP 429 rate limit")})
    result = run_fallback(_failures(1), _prs(1), host, engine="subscription")
    assert result.recovered == 0
    assert result.replaced == _failures(1)
    (row,) = result.added
    assert row["pr"] == "o/r#1"
    assert row["engine"] == "subscription"
    assert row["rate_limited"] is True
    assert row["too_large"] is False
    assert row["stdout_capture"] == "/tmp/capture"
    assert row["reason"] == "OpenRouter HTTP 429 rate limit"
    assert host.failed == ["o/r#1"]


def test_failed_attempt_without_output_keeps_exit_status() -> None:
    host = FakeHost(procs={"o/r#1": _proc(-9)})
    result = run_fallback(_failures(1), _prs(1), host, engine="subscription")
    assert result.added[0]["reason"] == "killed by SIGKILL (exit -9) with no output"


def test_empty_pool_stops_and_leaves_rest_skipped() -> None:
    host = FakeHost(models=[["m1"], ["m1"], []])
    result = run_fallback(
        _failures(1, 2, 3), _prs(1, 2, 3), host, engine="subscription"
    )
    assert host.executed == [("o/r#1", "m1")]
    assert result.stop_reason == "subscription quota unavailable"
    assert (result.attempted, result.skipped) == (1, 2)
    assert result.metadata()["stop_reason"] == "subscription quota unavailable"


def test_admission_refusal_stops_loop() -> None:
    host = FakeHost(admission=[None, "sweep deadline reached"])
    result = run_fallback(
        _failures(1, 2, 3), _prs(1, 2, 3), host, engine="subscription"
    )
    assert host.executed == [("o/r#1", "m1")]
    assert result.stop_reason == "sweep deadline reached"
    assert result.skipped == 2


def test_route_skip_continues_with_next_pr() -> None:
    host = FakeHost(skip={"o/r#1"})
    result = run_fallback(_failures(1, 2), _prs(1, 2), host, engine="subscription")
    assert host.executed == [("o/r#2", "m1")]
    assert result.replaced == [{"pr": "o/r#2", "reason": "x"}]
    assert (result.attempted, result.skipped, result.stop_reason) == (1, 1, None)


def test_same_head_upgrade_is_not_retried() -> None:
    prs = _prs(1, 2)
    prs["o/r#1"].update(headRefOid="abc123", reviewed_sha="abc")
    out: list[str] = []
    host = FakeHost()
    result = run_fallback(
        _failures(1, 2), prs, host, engine="subscription", emit=out.append
    )
    assert host.executed == [("o/r#2", "m1")]
    assert "  fallback skipped o/r#1: head already reviewed" in out
    assert result.skipped == 1


def test_no_failures_is_a_noop() -> None:
    host = FakeHost()
    result = run_fallback([], {}, host, engine="subscription", emit=lambda _: None)
    assert host.executed == []
    assert (result.attempted, result.recovered, result.skipped) == (0, 0, 0)
