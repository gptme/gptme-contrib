"""Offline tests for the run-reservation admission gate.

All tests use fake prices, synthetic authorization, and no-op worker commands.
No paid API calls are made. The tests prove that:
  - missing, invalid, exhausted, model/runtime/attempts-changed reservations
    refuse before the provider subprocess is spawned
  - concurrent sessions are serialised correctly at the session cap
  - failed/timed-out worker sends count against the experiment budget
  - labeling admission accounting works independently of selection accounting
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from gptme_lessons_extras.run_reservation import (
    check_labeling_admission,
    compute_reservation_hash,
    load_reservation,
    validate_reservation,
)
from gptme_lessons_extras.selector import select

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_MODEL = "test-provider/offline-synthetic-no-train-v0"
_FAKE_ROUTE = "https://fake.example.com/v1"
_FAKE_SPEND_AUTH = "ErikBjare/bob#SYNTHETIC-TEST-ONLY"


def _make_reservation(
    *,
    experiment_id: str = "test-experiment",
    model: str = _FAKE_MODEL,
    route: str = _FAKE_ROUTE,
    runtimes: list[str] | None = None,
    arms: list[str] | None = None,
    max_sessions: int = 2,
    max_attempts: int = 2,
    max_labeling_items: int = 5,
) -> dict[str, Any]:
    runtimes = runtimes or ["gptme", "claude-code"]
    arms = arms or ["control", "treatment"]
    base: dict[str, Any] = {
        "version": 1,
        "experiment_id": experiment_id,
        "route": route,
        "model": model,
        "runtimes": runtimes,
        "arms": arms,
        "max_sessions_per_arm_per_runtime": max_sessions,
        "max_attempts_per_session": max_attempts,
        "labeling": {
            "max_items_per_cell": max_labeling_items,
            "max_labels_per_item": 2,
            "adjudication_possible": True,
        },
        "spend_authority": _FAKE_SPEND_AUTH,
        "created_at": "2026-10-16T00:00:00Z",
    }
    base["reservation_hash"] = compute_reservation_hash(base)
    return base


def _write_reservation(path: Path, data: dict[str, Any]) -> None:
    path.write_bytes(json.dumps(data).encode())


def _request(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "version": 1,
        "runtime": "gptme",
        "session_id": "session-a",
        "event_kind": "user_prompt",
        "context_fingerprint": "ctx-a",
        "policy_version": "pol-a",
        "query": "safe commit workflow",
        "candidates": [
            {"id": "commit", "description": "Commit only owned paths."},
            {"id": "test", "description": "Run focused tests."},
        ],
        "fallback_ids": ["test"],
        "max_results": 1,
        "model": _FAKE_MODEL,
        "timeout_seconds": 2.0,
        "max_calls_per_session": 2,
    }
    value.update(overrides)
    return value


def _worker(tmp_path: Path, response: dict[str, Any]) -> list[str]:
    """Fake worker subprocess — no network calls."""
    script = tmp_path / "worker.py"
    script.write_text(f"import json, sys\nprint({json.dumps(response)!r})\n")
    return [sys.executable, str(script)]


def _failing_worker(tmp_path: Path) -> list[str]:
    script = tmp_path / "failing_worker.py"
    script.write_text("import sys\nsys.exit(1)\n")
    return [sys.executable, str(script)]


def _timeout_worker(tmp_path: Path, delay: float = 10.0) -> list[str]:
    script = tmp_path / "timeout_worker.py"
    script.write_text(f"import time\ntime.sleep({delay})\n")
    return [sys.executable, str(script)]


def _selected(*ids: str) -> dict[str, Any]:
    return {"version": 1, "status": "selected", "ids": list(ids)}


# ---------------------------------------------------------------------------
# validate_reservation / compute_reservation_hash
# ---------------------------------------------------------------------------


def test_validate_reservation_accepts_well_formed() -> None:
    data = _make_reservation()
    result = validate_reservation(data)
    assert result["experiment_id"] == "test-experiment"
    assert result["max_sessions_per_arm_per_runtime"] == 2
    assert result["labeling"]["max_items_per_cell"] == 5


def test_validate_reservation_rejects_wrong_version() -> None:
    data = _make_reservation()
    data["version"] = 99
    data["reservation_hash"] = compute_reservation_hash(data)
    with pytest.raises(ValueError, match="unsupported reservation version"):
        validate_reservation(data)


def test_validate_reservation_rejects_hash_mismatch() -> None:
    data = _make_reservation()
    data["reservation_hash"] = "0" * 64  # wrong hash
    with pytest.raises(ValueError, match="reservation_hash mismatch"):
        validate_reservation(data)


def test_validate_reservation_rejects_missing_field() -> None:
    data = _make_reservation()
    del data["spend_authority"]
    with pytest.raises(ValueError, match="spend_authority"):
        validate_reservation(data)


def test_compute_reservation_hash_changes_on_model_change() -> None:
    base = _make_reservation()
    modified = {**base, "model": "different-model"}
    h1 = compute_reservation_hash(base)
    h2 = compute_reservation_hash(modified)
    assert h1 != h2


# ---------------------------------------------------------------------------
# load_reservation
# ---------------------------------------------------------------------------


def test_load_reservation_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert load_reservation(tmp_path / "nonexistent.json") is None


def test_load_reservation_returns_none_for_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "res.json"
    p.write_bytes(b"not json")
    assert load_reservation(p) is None


def test_load_reservation_returns_none_for_hash_mismatch(tmp_path: Path) -> None:
    data = _make_reservation()
    data["reservation_hash"] = "0" * 64
    p = tmp_path / "res.json"
    _write_reservation(p, data)
    assert load_reservation(p) is None


def test_load_reservation_succeeds_for_valid_file(tmp_path: Path) -> None:
    data = _make_reservation()
    p = tmp_path / "res.json"
    _write_reservation(p, data)
    result = load_reservation(p)
    assert result is not None
    assert result["experiment_id"] == "test-experiment"


# ---------------------------------------------------------------------------
# select() — reservation gate integration
# ---------------------------------------------------------------------------


def test_missing_reservation_file_refuses_before_send(tmp_path: Path) -> None:
    res_path = tmp_path / "reservation.json"  # does not exist
    result = select(
        _request(),
        state_dir=tmp_path / "state",
        worker_command=_worker(tmp_path, _selected("commit")),
        reservation_path=res_path,
    )
    assert result["status"] == "fallback"
    assert result["reason"] == "reservation_missing"
    assert result["calls_used"] == 0


def test_model_mismatch_refuses_before_send(tmp_path: Path) -> None:
    res_path = tmp_path / "res.json"
    _write_reservation(res_path, _make_reservation(model=_FAKE_MODEL))
    result = select(
        _request(model="different-provider/different-model"),
        state_dir=tmp_path / "state",
        worker_command=_worker(tmp_path, _selected("commit")),
        reservation_path=res_path,
    )
    assert result["status"] == "fallback"
    assert result["reason"] == "reservation_model_changed"
    assert result["calls_used"] == 0


def test_runtime_outside_reservation_refuses_before_send(tmp_path: Path) -> None:
    res_path = tmp_path / "res.json"
    _write_reservation(res_path, _make_reservation(runtimes=["gptme"]))
    result = select(
        _request(runtime="claude-code"),
        state_dir=tmp_path / "state",
        worker_command=_worker(tmp_path, _selected("commit")),
        reservation_path=res_path,
    )
    assert result["status"] == "fallback"
    assert result["reason"] == "reservation_runtime_changed"
    assert result["calls_used"] == 0


def test_attempts_exceeding_reservation_refuses_before_send(tmp_path: Path) -> None:
    res_path = tmp_path / "res.json"
    _write_reservation(res_path, _make_reservation(max_attempts=2))
    result = select(
        _request(max_calls_per_session=3),
        state_dir=tmp_path / "state",
        worker_command=_worker(tmp_path, _selected("commit")),
        reservation_path=res_path,
    )
    assert result["status"] == "fallback"
    assert result["reason"] == "reservation_attempts_changed"
    assert result["calls_used"] == 0


def test_valid_reservation_permits_selection(tmp_path: Path) -> None:
    res_path = tmp_path / "res.json"
    _write_reservation(res_path, _make_reservation())
    result = select(
        _request(),
        state_dir=tmp_path / "state",
        worker_command=_worker(tmp_path, _selected("commit")),
        reservation_path=res_path,
    )
    assert result["status"] == "selected"
    assert result["ids"] == ["commit"]
    assert result["calls_used"] == 1


def test_exhausted_reservation_refuses(tmp_path: Path) -> None:
    # max_sessions=1, arms=2 → total cap=2 per runtime; use 2, then refuse
    res_path = tmp_path / "res.json"
    _write_reservation(res_path, _make_reservation(max_sessions=1))
    state = tmp_path / "state"
    cmd = _worker(tmp_path, _selected("commit"))
    for session in ("s1", "s2"):
        r = select(
            _request(session_id=session),
            state_dir=state,
            worker_command=cmd,
            reservation_path=res_path,
        )
        assert r["status"] == "selected", f"session {session} should be admitted"
    # Third distinct session should be refused
    r3 = select(
        _request(session_id="s3"),
        state_dir=state,
        worker_command=cmd,
        reservation_path=res_path,
    )
    assert r3["status"] == "fallback"
    assert r3["reason"] == "reservation_exhausted"
    assert r3["calls_used"] == 0


def test_same_session_reuse_does_not_exhaust_budget(tmp_path: Path) -> None:
    # max_sessions=1, arms=2 → cap=2; same session_id across multiple calls
    res_path = tmp_path / "res.json"
    _write_reservation(res_path, _make_reservation(max_sessions=1))
    state = tmp_path / "state"
    cmd = _worker(tmp_path, _selected("commit"))
    for _ in range(4):
        r = select(
            _request(session_id="same-session", context_fingerprint=f"ctx-{_}"),
            state_dir=state,
            worker_command=cmd,
            reservation_path=res_path,
        )
        # Might hit the per-session call cap, but never the session cap
        assert r["reason"] != "reservation_exhausted"


def test_failed_worker_counts_against_budget(tmp_path: Path) -> None:
    """A failed worker send still occupies a slot — the attempt was reserved."""
    res_path = tmp_path / "res.json"
    _write_reservation(res_path, _make_reservation(max_sessions=10))
    state = tmp_path / "state"
    result = select(
        _request(),
        state_dir=state,
        worker_command=_failing_worker(tmp_path),
        reservation_path=res_path,
    )
    assert result["status"] == "fallback"
    assert result["reason"] == "worker_failed"
    assert result["calls_used"] == 1  # call was reserved before the worker ran


def test_timeout_worker_counts_against_budget(tmp_path: Path) -> None:
    res_path = tmp_path / "res.json"
    _write_reservation(res_path, _make_reservation(max_sessions=10))
    state = tmp_path / "state"
    result = select(
        # The budget is measured from the call's start, so it also covers
        # first-call setup; importing gptme.hooks alone is ~45 ms cold. A
        # 50 ms budget therefore expires before the call is reserved, which is
        # a different path than the one under test. One second leaves setup
        # ample room and still times out on the 10 s worker below.
        _request(timeout_seconds=1.0),
        state_dir=state,
        worker_command=_timeout_worker(tmp_path),
        reservation_path=res_path,
    )
    assert result["status"] == "fallback"
    assert result["reason"] == "timeout"
    assert result["calls_used"] == 1


def test_no_reservation_path_bypasses_gate(tmp_path: Path) -> None:
    """Backward compatibility: no reservation_path → no experiment gate."""
    result = select(
        _request(),
        state_dir=tmp_path / "state",
        worker_command=_worker(tmp_path, _selected("commit")),
        reservation_path=None,
    )
    assert result["status"] == "selected"


# ---------------------------------------------------------------------------
# Concurrent session admission
# ---------------------------------------------------------------------------


def test_concurrent_sessions_respect_cap(tmp_path: Path) -> None:
    """Two threads racing to be admitted; only the cap allows."""
    cap = 3  # 3 sessions per arm × 2 arms = 6 total
    res_path = tmp_path / "res.json"
    _write_reservation(res_path, _make_reservation(max_sessions=cap))
    state = tmp_path / "state"
    cmd = _worker(tmp_path, _selected("commit"))

    def run_session(session_id: str) -> str:
        r = select(
            _request(session_id=session_id),
            state_dir=state,
            worker_command=cmd,
            reservation_path=res_path,
        )
        return r["reason"] if r["status"] == "fallback" else "ok"

    sessions = [f"concurrent-{i}" for i in range(cap * 2 + 2)]  # 8 unique sessions
    with ThreadPoolExecutor(max_workers=6) as ex:
        outcomes = list(ex.map(run_session, sessions))
    admitted = outcomes.count("ok")
    exhausted = outcomes.count("reservation_exhausted")
    # Exactly cap * len(arms) = 6 sessions admitted; rest refused
    assert (
        admitted == cap * 2
    ), f"expected {cap * 2} admitted, got {admitted}; outcomes={outcomes}"
    assert admitted + exhausted == len(sessions)


# ---------------------------------------------------------------------------
# check_labeling_admission
# ---------------------------------------------------------------------------


def test_labeling_admission_allows_under_cap(tmp_path: Path) -> None:
    res = _make_reservation(max_labeling_items=10)
    ok, reason = check_labeling_admission(
        res, tmp_path, runtime="gptme", arm="treatment"
    )
    assert ok is True
    assert reason == "admitted"


def test_labeling_admission_refuses_at_cap(tmp_path: Path) -> None:
    res = _make_reservation(max_labeling_items=3)
    for _ in range(3):
        ok, _ = check_labeling_admission(
            res, tmp_path, runtime="gptme", arm="treatment"
        )
        assert ok is True
    # Fourth item should be refused
    ok, reason = check_labeling_admission(
        res, tmp_path, runtime="gptme", arm="treatment"
    )
    assert ok is False
    assert reason == "labeling_exhausted"


def test_labeling_admits_independent_arms(tmp_path: Path) -> None:
    res = _make_reservation(max_labeling_items=2)
    # Fill up treatment arm
    for _ in range(2):
        ok, _ = check_labeling_admission(
            res, tmp_path, runtime="gptme", arm="treatment"
        )
        assert ok is True
    # treatment exhausted
    ok, reason = check_labeling_admission(
        res, tmp_path, runtime="gptme", arm="treatment"
    )
    assert ok is False
    assert reason == "labeling_exhausted"
    # control arm unaffected
    ok, reason = check_labeling_admission(res, tmp_path, runtime="gptme", arm="control")
    assert ok is True


def test_labeling_refuses_unknown_arm(tmp_path: Path) -> None:
    res = _make_reservation()
    ok, reason = check_labeling_admission(
        res, tmp_path, runtime="gptme", arm="unknown-arm"
    )
    assert ok is False
    assert reason == "labeling_unknown_arm"


def test_labeling_refuses_unknown_runtime(tmp_path: Path) -> None:
    res = _make_reservation()
    ok, reason = check_labeling_admission(
        res, tmp_path, runtime="unknown-rt", arm="treatment"
    )
    assert ok is False
    assert reason == "labeling_unknown_runtime"
