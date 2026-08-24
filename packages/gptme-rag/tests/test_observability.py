"""Tests for injection logging and index-health reporting."""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from gptme_rag.observability import (
    IndexHealth,
    assess_index_health,
    detect_harness,
    detect_session_id,
    log_injection,
    summarize_injections,
)

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def _hits(*scores: float) -> list[dict]:
    return [
        {"id": f"doc-{i}", "type": "journal", "path": f"j/{i}.md", "similarity": s}
        for i, s in enumerate(scores)
    ]


# --- log_injection ---------------------------------------------------------


def test_log_injection_writes_one_jsonl_record(tmp_path):
    log = tmp_path / "nested" / "injections.jsonl"

    record = log_injection(log, "why is CI red", _hits(0.7, 0.4), backend="tfidf", session_id="s1")

    assert record is not None
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    written = json.loads(lines[0])
    assert written == record
    assert written["session_id"] == "s1"
    assert written["backend"] == "tfidf"
    assert written["num_hits"] == 2
    assert written["num_injected"] == 2
    assert written["top_score"] == pytest.approx(0.7)
    assert written["query_len"] == len("why is CI red")


def test_log_injection_appends(tmp_path):
    log = tmp_path / "injections.jsonl"
    log_injection(log, "a", _hits(0.5), backend="tfidf", session_id="s")
    log_injection(log, "b", [], backend="tfidf", session_id="s")

    assert len(log.read_text().splitlines()) == 2


def test_log_injection_records_only_injected_hits_but_counts_all(tmp_path):
    log = tmp_path / "injections.jsonl"

    record = log_injection(
        log,
        "q",
        _hits(0.9, 0.8, 0.7, 0.6),
        backend="tfidf",
        max_injected=2,
        session_id="s",
    )

    assert record["num_hits"] == 4
    assert record["num_injected"] == 2
    assert len(record["hits"]) == 2
    assert [h["score"] for h in record["hits"]] == [0.9, 0.8]


def test_log_injection_omits_empty_hit_fields(tmp_path):
    log = tmp_path / "injections.jsonl"

    record = log_injection(
        log,
        "q",
        [{"id": "t1", "type": "task", "state": "active", "similarity": 0.3}],
        backend="tfidf",
        session_id="s",
    )

    hit = record["hits"][0]
    assert hit == {"id": "t1", "type": "task", "state": "active", "score": 0.3}
    assert "path" not in hit  # absent key must not become a null column


def test_log_injection_keeps_falsy_but_present_hit_fields(tmp_path):
    """0/False are values; only None and empty string are omitted."""
    log = tmp_path / "injections.jsonl"

    record = log_injection(
        log,
        "q",
        [{"id": "t1", "type": "task", "state": 0, "path": "", "date": None, "similarity": 0.3}],
        backend="tfidf",
        session_id="s",
        harness="test",
    )

    hit = record["hits"][0]
    assert hit["state"] == 0
    assert "path" not in hit
    assert "date" not in hit


def test_log_injection_empty_hits_has_zero_top_score(tmp_path):
    log = tmp_path / "injections.jsonl"

    record = log_injection(log, "q", [], backend="chroma", session_id="s")

    assert record["num_hits"] == 0
    assert record["top_score"] == 0.0
    assert record["hits"] == []


def test_log_injection_merges_extra_fields(tmp_path):
    log = tmp_path / "injections.jsonl"

    record = log_injection(
        log,
        "q",
        _hits(0.5),
        backend="tfidf",
        session_id="s",
        extra={"consumer": "task-retrieval"},
    )

    assert record["consumer"] == "task-retrieval"


def test_log_injection_honours_custom_score_key(tmp_path):
    log = tmp_path / "injections.jsonl"

    record = log_injection(
        log,
        "q",
        [{"id": "d", "bm25": 4.2}],
        backend="bm25",
        session_id="s",
        score_key="bm25",
    )

    assert record["top_score"] == pytest.approx(4.2)


def test_log_injection_treats_unusable_scores_as_zero(tmp_path):
    log = tmp_path / "injections.jsonl"

    record = log_injection(
        log,
        "q",
        [{"id": "d", "similarity": None}, {"id": "e", "similarity": "n/a"}],
        backend="tfidf",
        session_id="s",
        harness="test",
    )

    assert record["top_score"] == 0.0
    assert [h["score"] for h in record["hits"]] == [0.0, 0.0]


def test_log_injection_never_raises_on_unwritable_path(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")

    # Best-effort by design: a bad path must not break the consumer.
    assert log_injection(blocker / "x.jsonl", "q", [], backend="tfidf") is None


def test_detect_session_id_prefers_first_set_var(monkeypatch):
    monkeypatch.delenv("A", raising=False)
    monkeypatch.setenv("B", "session-b")
    assert detect_session_id(["A", "B"]) == "session-b"
    monkeypatch.delenv("B", raising=False)
    assert detect_session_id(["A", "B"]) == "unknown"


# --- summarize_injections --------------------------------------------------


def test_summarize_missing_log_is_empty(tmp_path):
    stats = summarize_injections(tmp_path / "absent.jsonl")
    assert stats.total == 0
    assert stats.fire_rate == 0.0


def test_summarize_aggregates_fire_rate_and_harnesses(tmp_path):
    log = tmp_path / "injections.jsonl"
    log_injection(log, "a", _hits(0.8, 0.4), backend="tfidf", session_id="s1", harness="gptme")
    log_injection(log, "b", [], backend="tfidf", session_id="s1", harness="gptme")
    log_injection(log, "c", _hits(0.6), backend="tfidf", session_id="s2", harness="claude-code")

    stats = summarize_injections(log)

    assert stats.total == 3
    assert stats.nonzero == 2
    assert stats.fire_rate == pytest.approx(2 / 3)
    assert stats.avg_hits == pytest.approx(3 / 3)
    assert stats.avg_top_score == pytest.approx((0.8 + 0.6) / 2)
    assert stats.unique_sessions == 2
    assert stats.by_harness == {"gptme": 2, "claude-code": 1}


def test_summarize_skips_partial_trailing_line(tmp_path):
    """A concurrent append leaves a half-written line — skip it, don't raise."""
    log = tmp_path / "injections.jsonl"
    log_injection(log, "a", _hits(0.5), backend="tfidf", session_id="s")
    with open(log, "a") as f:
        f.write('{"ts": "2026-08-24T12:00:00Z", "num_hi')

    stats = summarize_injections(log)

    assert stats.total == 1
    assert stats.malformed == 1


def test_summarize_ignores_blank_and_non_object_lines(tmp_path):
    log = tmp_path / "injections.jsonl"
    log.write_text('\n[1, 2]\n{"num_hits": 1, "top_score": 0.5}\n\n')

    stats = summarize_injections(log)

    assert stats.total == 1
    assert stats.malformed == 1


def test_summarize_skips_non_numeric_fields(tmp_path):
    """A bad typed field must not crash the aggregator — same skip contract."""
    log = tmp_path / "injections.jsonl"
    log.write_text(
        '{"num_hits": "nope", "top_score": 0.5}\n'
        '{"num_hits": 2, "top_score": "bad"}\n'
        '{"num_hits": 1, "top_score": 0.5}\n'
    )

    stats = summarize_injections(log)

    assert stats.total == 1
    assert stats.malformed == 2
    assert stats.avg_top_score == pytest.approx(0.5)


# --- assess_index_health ---------------------------------------------------


def test_health_missing_when_never_built():
    health = assess_index_health(built_at=None, indexed_count=0)
    assert health.status == "missing"
    assert not health.ok
    assert health.reasons == ["no index"]


def test_health_missing_when_index_is_empty():
    health = assess_index_health(built_at=NOW, indexed_count=0, now=NOW)
    assert health.status == "missing"
    assert "index is empty" in health.reasons


def test_health_ok_when_fresh_and_complete():
    health = assess_index_health(
        built_at=NOW - timedelta(minutes=10),
        indexed_count=500,
        on_disk_count=500,
        now=NOW,
    )
    assert health.ok
    assert health.reasons == []
    assert health.age_hours == pytest.approx(0.17, abs=0.01)
    assert health.delta == 0


def test_health_stale_when_too_old():
    health = assess_index_health(
        built_at=NOW - timedelta(hours=51 * 24),
        indexed_count=500,
        on_disk_count=500,
        max_age_hours=2.0,
        now=NOW,
    )
    assert health.status == "stale"
    assert any("age" in r for r in health.reasons)


def test_health_stale_when_disk_drifted_past_tolerance():
    health = assess_index_health(built_at=NOW, indexed_count=500, on_disk_count=540, now=NOW)
    assert health.status == "stale"
    assert health.delta == 40

    tolerant = assess_index_health(
        built_at=NOW,
        indexed_count=500,
        on_disk_count=540,
        count_delta_tolerance=50,
        now=NOW,
    )
    assert tolerant.ok


def test_health_skips_completeness_check_without_disk_count():
    health = assess_index_health(built_at=NOW, indexed_count=500, now=NOW)
    assert health.ok
    assert health.delta is None


def test_missing_slice_dominates_stale_global():
    """A vanished document type is worse than mere age — don't report stale."""
    health = assess_index_health(
        built_at=NOW - timedelta(hours=5),
        indexed_count=100,
        slices={"task": (0, 10)},
        max_age_hours=2.0,
        now=NOW,
    )
    assert health.slices["task"].status == "missing"
    assert health.status == "missing"
    assert any("empty" in r for r in health.reasons)
    assert any("age" in r for r in health.reasons)


def test_missing_slice_dominates_even_after_a_stale_slice():
    health = assess_index_health(
        built_at=NOW,
        indexed_count=100,
        slices={"journal": (100, 200), "task": (0, 10)},
        slice_delta_tolerance=25,
        now=NOW,
    )
    assert health.slices["journal"].status == "stale"
    assert health.slices["task"].status == "missing"
    assert health.status == "missing"


def test_empty_slice_is_flagged_even_when_index_looks_healthy():
    """The rot that matters: one document type silently gone from a big index."""
    health = assess_index_health(
        built_at=NOW,
        indexed_count=84_000,
        on_disk_count=84_000,
        slices={"task": (0, 1079), "journal": (60_000, 60_010)},
        now=NOW,
    )

    assert health.slices["task"].status == "missing"
    assert health.slices["journal"].status == "ok"
    assert "slice 'task' is empty" in health.reasons
    # A per-slice failure must not be masked by a healthy global count.
    assert not health.ok


def test_slice_stale_on_delta_and_on_global_age():
    drifted = assess_index_health(
        built_at=NOW,
        indexed_count=100,
        slices={"task": (100, 200)},
        slice_delta_tolerance=25,
        now=NOW,
    )
    assert drifted.slices["task"].status == "stale"
    assert drifted.status == "stale"

    aged = assess_index_health(
        built_at=NOW - timedelta(hours=5),
        indexed_count=100,
        slices={"task": (100, 100)},
        max_age_hours=2.0,
        now=NOW,
    )
    assert aged.slices["task"].status == "stale"


def test_naive_built_at_is_treated_as_utc():
    health = assess_index_health(built_at=datetime(2026, 8, 24, 11, 30), indexed_count=10, now=NOW)
    assert health.ok
    assert health.age_hours == pytest.approx(0.5)


def test_naive_now_is_treated_as_utc():
    health = assess_index_health(
        built_at=NOW,
        indexed_count=10,
        now=datetime(2026, 8, 24, 13, 0),
    )
    assert health.ok
    assert health.age_hours == pytest.approx(1.0)


def test_future_built_at_clamps_age_to_zero():
    health = assess_index_health(built_at=NOW + timedelta(hours=3), indexed_count=10, now=NOW)
    assert health.age_hours == 0.0
    assert health.ok


def test_to_dict_is_json_serializable_and_includes_slices():
    health = assess_index_health(
        built_at=NOW,
        indexed_count=10,
        on_disk_count=12,
        count_delta_tolerance=5,
        slices={"task": (4, 4)},
        now=NOW,
    )

    data = health.to_dict()
    assert json.loads(json.dumps(data)) == data
    assert data["delta"] == 2
    assert data["slices"]["task"] == {
        "status": "ok",
        "indexed": 4,
        "on_disk": 4,
        "delta": 0,
    }


def test_index_health_dataclass_defaults_are_conservative():
    assert IndexHealth(status="missing").ok is False


# --- detect_harness --------------------------------------------------------


def _patch_proc(monkeypatch, parent_pid: int, tree: dict[int, tuple[str, int | str]]) -> None:
    """Install a fake /proc tree: pid -> (comm, ppid-or-sentinel)."""
    monkeypatch.setattr(os, "getppid", lambda: parent_pid)

    class FakePath:
        def __init__(self, path: str) -> None:
            self._path = str(path)

        def __truediv__(self, other: object) -> "FakePath":
            return FakePath(self._path.rstrip("/") + "/" + str(other).lstrip("/"))

        def read_text(self, encoding: str | None = None) -> str:
            parts = self._path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "proc":
                raise OSError(self._path)
            try:
                pid = int(parts[1])
            except ValueError as exc:
                raise OSError(self._path) from exc
            if pid not in tree:
                raise OSError("no such pid")
            comm, ppid = tree[pid]
            kind = parts[2]
            if kind == "comm":
                if comm == "OSERROR":
                    raise OSError("comm vanished")
                return f"{comm}\n"
            if kind == "status":
                if ppid == "OSERROR":
                    raise OSError("status vanished")
                if ppid == "MALFORMED":
                    return "PPid:\n"
                return f"Name:\t{comm}\nPPid:\t{ppid}\n"
            raise OSError(kind)

    monkeypatch.setattr("gptme_rag.observability.Path", FakePath)


def test_detect_harness_returns_matching_ancestor(monkeypatch):
    _patch_proc(monkeypatch, 100, {100: ("python", 50), 50: ("gptme", 1)})
    assert detect_harness() == "gptme"


def test_detect_harness_maps_claude_comm(monkeypatch):
    _patch_proc(monkeypatch, 7, {7: ("claude", 1)})
    assert detect_harness() == "claude-code"


def test_detect_harness_unknown_when_nothing_matches(monkeypatch):
    _patch_proc(monkeypatch, 3, {3: ("bash", 1)})
    assert detect_harness() == "unknown"


def test_detect_harness_breaks_on_malformed_ppid(monkeypatch):
    _patch_proc(monkeypatch, 9, {9: ("python", "MALFORMED")})
    assert detect_harness() == "unknown"


def test_detect_harness_breaks_on_unreadable_proc(monkeypatch):
    _patch_proc(monkeypatch, 4, {4: ("OSERROR", 1)})
    assert detect_harness() == "unknown"


def test_detect_harness_breaks_on_pid_cycle(monkeypatch):
    _patch_proc(monkeypatch, 11, {11: ("python", 11)})
    assert detect_harness() == "unknown"
