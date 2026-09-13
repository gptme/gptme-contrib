"""Launch causes survive recording independently of the dispatch mechanism."""

import json
from pathlib import Path

import pytest

from gptme_sessions.post_session import post_session
from gptme_sessions.record import SessionRecord
from gptme_sessions.store import SessionStore


CAUSE = {
    "kind": "pr-review",
    "id": "review-42",
    "parent_session_id": "parent-123",
    "task": "review-change",
    "pr": "gptme/gptme#42",
    "unit": "review-slot-0",
    "release": "v1",
}


def test_dispatch_cause_roundtrip_preserves_lineage(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BOB_DISPATCH_CAUSE", '{"kind": "stale-env"}')
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        session_id="child-123",
        parent_session_id="parent-123",
        dispatch_kind="worker",
        dispatch_id="worker-run-7",
        dispatch_cause=CAUSE,
    )
    assert result.record.dispatch_cause == CAUSE
    [reloaded] = store.load_all()
    assert reloaded.dispatch_cause == CAUSE
    assert reloaded.parent_session_id == "parent-123"
    assert reloaded.dispatch_kind == "worker"
    assert reloaded.dispatch_id == "worker-run-7"
    assert json.loads(reloaded.to_json())["dispatch_cause"] == CAUSE


def test_dispatch_cause_environment_fallback(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BOB_DISPATCH_CAUSE", json.dumps(CAUSE))
    store = SessionStore(sessions_dir=tmp_path)
    post_session(store=store, harness="gptme", session_id="env-child")
    [record] = store.load_all()
    assert record.dispatch_cause == CAUSE


def test_explicit_empty_cause_overrides_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BOB_DISPATCH_CAUSE", json.dumps(CAUSE))
    result = post_session(
        store=SessionStore(sessions_dir=tmp_path), harness="gptme", dispatch_cause={}
    )
    assert result.record.dispatch_cause == {}


@pytest.mark.parametrize("raw", ["{broken", "[]", '"timer"', "42", "true", "null", ""])
def test_invalid_environment_cause_does_not_prevent_recording(tmp_path: Path, monkeypatch, raw):
    monkeypatch.setenv("BOB_DISPATCH_CAUSE", raw)
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(store=store, harness="gptme", session_id="invalid-env")
    assert result.record.dispatch_cause is None
    [reloaded] = store.load_all()
    assert reloaded.session_id == "invalid-env"
    assert reloaded.dispatch_cause is None


@pytest.mark.parametrize("value", [[], "timer", 42, True])
def test_invalid_record_cause_is_dropped(value):
    record = SessionRecord.from_dict({"session_id": "invalid-row", "dispatch_cause": value})
    assert record.dispatch_cause is None
    assert record.to_dict()["dispatch_cause"] is None


def test_invalid_argument_does_not_fall_back_to_stale_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BOB_DISPATCH_CAUSE", json.dumps(CAUSE))
    result = post_session(
        store=SessionStore(sessions_dir=tmp_path),
        harness="gptme",
        dispatch_cause="invalid",  # type: ignore[arg-type]
    )
    assert result.record.dispatch_cause is None


def test_missing_cause_remains_unrecorded(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BOB_DISPATCH_CAUSE", raising=False)
    result = post_session(store=SessionStore(sessions_dir=tmp_path), harness="gptme")
    assert result.record.dispatch_cause is None
    legacy = SessionRecord.from_dict({"session_id": "old", "dispatch_kind": "worker"})
    assert legacy.dispatch_cause is None
    assert legacy.dispatch_kind == "worker"
