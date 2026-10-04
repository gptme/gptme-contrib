"""Canonical run IDs remain independent from reusable display labels."""

import json
from pathlib import Path
from unittest.mock import patch

from gptme_sessions.post_session import post_session
from gptme_sessions.record import SessionRecord
from gptme_sessions.store import SessionStore

RUN_A = "11111111-1111-4111-8111-111111111111"
RUN_B = "22222222-2222-4222-8222-222222222222"


def test_same_label_runs_remain_independently_mutable(tmp_path: Path):
    store = SessionStore(sessions_dir=tmp_path)
    for run_id in (RUN_A, RUN_B):
        result = post_session(
            store=store,
            harness="gptme",
            session_id=run_id,
            session_label="dead",
        )
        assert result.record.session_id == run_id
        assert result.record.session_label == "dead"

    assert store.stamp_attempt_kind(RUN_A, "infra_retry")
    rows = {row.session_id: row for row in store.load_all()}
    assert set(rows) == {RUN_A, RUN_B}
    assert rows[RUN_A].attempt_kind == "infra_retry"
    assert rows[RUN_B].attempt_kind is None
    assert rows[RUN_A].session_label == rows[RUN_B].session_label == "dead"

    rows[RUN_B].category = "research"
    store.rewrite([rows[RUN_B]])
    reloaded = {row.session_id: row for row in store.load_all()}
    assert reloaded[RUN_A].attempt_kind == "infra_retry"
    assert reloaded[RUN_B].category == "research"


def test_run_id_with_label_uses_canonical_commit_trailer_ownership(tmp_path: Path):
    trajectory = tmp_path / "trajectory.jsonl"
    trajectory.write_text("{}\n")
    sha = "a" * 40
    signals = {
        "deliverables": ["src/fix.py"],
        "productive": True,
        "tool_calls": {"patch": 1},
    }
    with patch("gptme_sessions.post_session.extract_from_path", return_value=signals):
        result = post_session(
            store=SessionStore(sessions_dir=tmp_path / "store"),
            harness="gptme",
            session_id=RUN_A,
            session_label="dead",
            trajectory_path=trajectory,
            deliverables=[sha],
            commit_trailers={sha: [RUN_A]},
        )
    assert sha in result.record.deliverables
    assert result.record.session_id == RUN_A


def test_legacy_record_keeps_its_identity_without_inventing_a_label():
    legacy = {"session_id": "dead", "timestamp": "2026-09-01T00:00:00Z"}
    record = SessionRecord.from_dict(legacy)
    assert record.session_id == "dead"
    assert record.session_label is None
    assert json.loads(record.to_json())["session_id"] == "dead"


def test_attempt_stamp_refuses_distinct_legacy_runs_with_same_id(tmp_path: Path):
    import pytest

    store = SessionStore(sessions_dir=tmp_path)
    for day in (1, 2):
        store.append(SessionRecord(session_id="dead", timestamp=f"2026-09-0{day}T00:00:00Z"))
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="ambiguous"):
        store.stamp_attempt_kind("dead", "infra_retry")
    assert store.path.read_bytes() == before
