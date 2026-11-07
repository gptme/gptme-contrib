"""Tests for fleet_vitals' "did a model call succeed?" evidence and heartbeat policy.

The dead-man inference heartbeat (Uptime Kuma) must page on a real inference
outage but not on a quiet night of correct noops. ``productive`` alone can't
tell those apart; ``model_calls_succeeded_in_window`` + ``heartbeat_up`` can.
"""

from __future__ import annotations

import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_vitals as fv  # noqa: E402

NOW = datetime(2026, 11, 7, 13, 0, tzinfo=timezone.utc)
FIXTURE = (
    ROOT / "tests/fixtures/fleet-vitals/alice-2026-10-24_11-07-session-records.jsonl.gz"
)
_403 = {
    "failure_reason": "api_error_403",
    "error": "api_error_status:403; oauth_not_allowed_for_organization",
}


def _r(hours_ago: float, outcome: str, **extra: object) -> dict:
    return {
        "timestamp": (NOW - timedelta(hours=hours_ago)).isoformat(),
        "outcome": outcome,
        **extra,
    }


def _status(recs: list[dict], window_hours: int = 7) -> tuple[str, str, dict]:
    return fv.inference_status_from_lines(
        (json.dumps(r) for r in recs), now=NOW, window_hours=window_hours
    )


# --- model_call_succeeded(): the per-record predicate --------------------------


def test_noop_and_productive_with_tokens_count() -> None:
    assert fv.model_call_succeeded(_r(1, "noop", output_tokens=5046))
    assert fv.model_call_succeeded(_r(1, "productive", output_tokens=1))


def test_missing_zero_or_garbage_tokens_do_not_count() -> None:
    assert not fv.model_call_succeeded(_r(1, "noop"))
    assert not fv.model_call_succeeded(_r(1, "noop", output_tokens=0))
    assert not fv.model_call_succeeded(_r(1, "noop", output_tokens="n/a"))
    assert not fv.model_call_succeeded(_r(1, "noop", output_tokens=None))


def test_failed_skipped_unknown_synthetic_never_count() -> None:
    # Rate-limited run that streamed output before dying: still not evidence.
    assert not fv.model_call_succeeded(
        _r(1, "failed", output_tokens=900, failure_reason="rate_limited")
    )
    assert not fv.model_call_succeeded(_r(1, "skipped", output_tokens=900))
    assert not fv.model_call_succeeded(_r(1, "unknown", output_tokens=900))
    assert not fv.model_call_succeeded(
        _r(1, "noop", output_tokens=10, model="<synthetic>")
    )


# --- metrics field + heartbeat_up(): the push policy ---------------------------


def test_quiet_night_degraded_but_heartbeat_up() -> None:
    # Regression for 2026-11-07 00:06→13:17: zero productive sessions, but
    # noops that really ran the model. Went 7h without a push before this.
    status, detail, m = _status([_r(5, "noop", output_tokens=5046), _r(1, "noop")])
    assert status == "degraded"
    assert m["model_calls_succeeded_in_window"] is True
    assert m["model_call_records_in_window"] == 1
    assert "1 session(s) with model output" in detail
    assert fv.heartbeat_up(status, m)


def test_degraded_without_model_output_does_not_push() -> None:
    status, _, m = _status([_r(3, "noop"), _r(1, "noop", output_tokens=0)])
    assert status == "degraded"
    assert m["model_calls_succeeded_in_window"] is False
    assert not fv.heartbeat_up(status, m)


def test_out_of_window_output_does_not_count() -> None:
    status, _, m = _status([_r(10, "noop", output_tokens=4000), _r(1, "noop")])
    assert status == "degraded" and not fv.heartbeat_up(status, m)


def test_down_never_pushes_even_with_earlier_model_output() -> None:
    # A noop at -6h proves inference worked *then*; the trailing 403 streak
    # says it's broken *now*. The field is True, the heartbeat must not be.
    recs = [_r(6, "noop", output_tokens=4000)] + [
        _r(h, "failed", **_403) for h in (4, 3, 2, 1)
    ]
    status, _, m = _status(recs)
    assert status == "down"
    assert m["model_calls_succeeded_in_window"] is True
    assert not fv.heartbeat_up(status, m)


def test_non_auth_failures_after_success_do_not_push() -> None:
    # A noop at -6h, then rate-limit/5xx failures: no 403 streak, so status is
    # degraded and the field is True, but the newest evidence is failure.
    recs = [_r(6, "noop", output_tokens=4000)] + [
        _r(h, "failed", failure_reason=fr)
        for h, fr in ((4, "rate_limit"), (3, "api_error_529"), (2, "timeout"))
    ]
    status, _, m = _status(recs)
    assert status == "degraded"
    assert m["model_calls_succeeded_in_window"] is True
    assert m["failures_since_last_model_call"] == 3
    assert not fv.heartbeat_up(status, m)
    # Recovery: a fresh model-call success clears it.
    status, _, m = _status(recs + [_r(1, "noop", output_tokens=10)])
    assert m["failures_since_last_model_call"] == 0
    assert fv.heartbeat_up(status, m)


def test_healthy_pushes_and_stale_does_not() -> None:
    status, _, m = _status([_r(2, "productive")])
    assert status == "healthy" and fv.heartbeat_up(status, m)
    status, _, m = _status([])
    assert status == "stale" and m["model_calls_succeeded_in_window"] is False
    assert not fv.heartbeat_up(status, m)


def test_field_flows_through_cli_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = tmp_path / "session-records.jsonl"
    # The CLI uses the real clock (no injectable now), so the record must be
    # relative to it, not to the fixed NOW, or the test expires within hours.
    rec = {
        "timestamp": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
        "outcome": "noop",
        "output_tokens": 12,
    }
    p.write_text(json.dumps(rec) + "\n")
    fv._inference_cli(["--records", str(p), "--window-hours", "7", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["model_calls_succeeded_in_window"] is True


# --- replay: Alice's real Oct 26 → Nov 6 2026 billing/OAuth outage -------------
# Sanitized session records (timestamp, outcome, failure_reason, a canonical
# 403 error string, <synthetic> marker, output_tokens — nothing else), replayed
# hourly with the 7h window the Kuma heartbeat uses.


def _replay() -> list[tuple[datetime, str, bool, dict]]:
    with gzip.open(FIXTURE, "rt") as f:
        records = fv.parse_jsonl_lines(f)
    out = []
    t = datetime(2026, 10, 25, tzinfo=timezone.utc)
    end = datetime(2026, 11, 7, 16, tzinfo=timezone.utc)
    while t <= end:
        status, _, m = fv.inference_status_from_records(records, t, 7)
        out.append((t, status, fv.heartbeat_up(status, m), m))
        t += timedelta(hours=1)
    return out


def test_replay_outage_never_pushes() -> None:
    rows = _replay()
    down = [r for r in rows if r[1] == "down"]
    first, last = down[0][0], down[-1][0]
    span = [r for r in rows if first <= r[0] <= last]
    # 273 consecutive hours down, uninterrupted, and not one heartbeat push.
    assert len(span) == len(down) == 273
    assert not any(up for _, _, up, _ in span)
    # No auth/billing failure or synthetic twin in the whole outage carried
    # output tokens, so the evidence field never lit up while down.
    assert not any(m["model_calls_succeeded_in_window"] for *_, m in down)


def test_replay_pushes_before_and_after_outage() -> None:
    rows = _replay()
    down = [r for r in rows if r[1] == "down"]
    first, last = down[0][0], down[-1][0]
    assert any(up for t, _, up, _ in rows if t < first)
    assert any(up for t, _, up, _ in rows if t > last)


def test_replay_quiet_night_after_recovery_pushes_on_model_output() -> None:
    # The night of 2026-11-07: degraded (0 productive) yet the heartbeat stays
    # up because noops really called the model — the false-page this fixes.
    rows = _replay()
    quiet_up = [
        t for t, status, up, _ in rows if status == "degraded" and up and t.day == 7
    ]
    assert quiet_up, "expected degraded-but-alive hours on 2026-11-07"


def test_degraded_without_recency_field_fails_closed() -> None:
    # Metrics from an older producer lack failures_since_last_model_call;
    # recency is unknown, so a dead-man heartbeat must not push.
    m = {"model_calls_succeeded_in_window": True}
    assert not fv.heartbeat_up("degraded", m)
    assert fv.heartbeat_up("degraded", {**m, "failures_since_last_model_call": 0})


def test_healthy_with_later_failures_does_not_push() -> None:
    # Regression (contrib#2058 review): a productive session at -6h keeps
    # status healthy, but the timeout/403/timeout run after it is the newest
    # evidence — the healthy shortcut must not bypass the recency check.
    recs = [
        _r(6, "productive", output_tokens=5000),
        _r(3, "failed", failure_reason="timeout"),
        _r(2, "failed", **_403),
        _r(1, "failed", failure_reason="timeout"),
    ]
    status, _, m = _status(recs)
    assert status == "healthy"  # status contract unchanged
    assert m["failures_since_last_model_call"] == 3
    assert not fv.heartbeat_up(status, m)


def test_productive_without_tokens_ends_failure_scan() -> None:
    # A thin productive record (no tokens) after failures is still success
    # evidence: it must reset the recency count rather than page.
    recs = [
        _r(4, "failed", failure_reason="timeout"),
        _r(3, "failed", failure_reason="timeout"),
        _r(1, "productive"),
    ]
    status, _, m = _status(recs)
    assert status == "healthy"
    assert m["failures_since_last_model_call"] == 0
    assert fv.heartbeat_up(status, m)


def test_recency_scan_is_order_independent() -> None:
    # Regression (contrib#2058 review): direct callers of
    # inference_status_from_records() may pass unsorted records; the result
    # must match the sorted case.
    recs = [_r(6, "noop", output_tokens=4000)] + [
        _r(h, "failed", failure_reason="timeout") for h in (3, 2, 1)
    ]
    pairs = [(datetime.fromisoformat(r["timestamp"]), r) for r in recs]
    fwd = fv.inference_status_from_records(pairs, now=NOW, window_hours=7)
    rev = fv.inference_status_from_records(pairs[::-1], now=NOW, window_hours=7)
    assert fwd[2]["failures_since_last_model_call"] == 3
    assert rev[2] == fwd[2]
    assert not fv.heartbeat_up(rev[0], rev[2])


def test_equal_timestamp_tie_fails_closed_in_any_order() -> None:
    # Regression (contrib#2058 review): a success and a failure sharing a
    # timestamp must give the same answer for either input order, and the
    # unordered tie must not let the success hide the failure.
    noop = _r(1, "noop", output_tokens=4000)
    fail = _r(1, "failed", failure_reason="timeout")
    results = []
    for recs in ([fail, noop], [noop, fail]):
        pairs = [(datetime.fromisoformat(r["timestamp"]), r) for r in recs]
        results.append(fv.inference_status_from_records(pairs, now=NOW, window_hours=7))
    assert results[0][2] == results[1][2]
    assert results[0][2]["failures_since_last_model_call"] == 1
    assert not fv.heartbeat_up(results[0][0], results[0][2])


def test_unhashable_outcome_does_not_abort_report() -> None:
    # Regression (contrib#2058 review): parse_jsonl_lines() accepts any
    # timestamped object, so a malformed outcome must be skipped, not raise.
    bad = _r(2, "noop", output_tokens=4000)
    bad["outcome"] = []
    assert not fv.model_call_succeeded(bad)
    good = _r(1, "noop", output_tokens=4000)
    pairs = [(datetime.fromisoformat(r["timestamp"]), r) for r in (bad, good)]
    status, _, metrics = fv.inference_status_from_records(
        pairs, now=NOW, window_hours=7
    )
    assert metrics["model_call_records_in_window"] == 1


def test_infinite_output_tokens_does_not_abort_report() -> None:
    # Regression (contrib#2058 review): json.loads accepts 1e309 as inf, and
    # int(inf) raises OverflowError. Reject that record's evidence, don't abort.
    bad = (
        json.dumps(
            {"timestamp": (NOW - timedelta(hours=2)).isoformat(), "outcome": "noop"}
        )[:-1]
        + ', "output_tokens": 1e309}'
    )
    good = json.dumps(_r(1, "noop", output_tokens=4000))
    pairs = fv.parse_jsonl_lines([bad, good])
    assert len(pairs) == 2
    assert not fv.model_call_succeeded(pairs[0][1])
    _, _, metrics = fv.inference_status_from_records(pairs, now=NOW, window_hours=7)
    assert metrics["model_call_records_in_window"] == 1


def test_healthy_without_recency_field_fails_closed() -> None:
    assert not fv.heartbeat_up("healthy", {"productive_in_window": 1})
