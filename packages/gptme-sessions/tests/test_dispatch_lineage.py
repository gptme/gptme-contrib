"""Tests for out-of-process dispatch lineage (parent_session_id / dispatch_kind).

A spawned session (worker, fanout child, PM dispatch, gptodo spawn) has to carry
a *directed* link back to the session that spawned it.  Before this, the only
join between a parent and its out-of-process children was timestamp proximity,
which is exactly the mtime-class heuristic these records exist to replace.
"""

from pathlib import Path

from gptme_sessions.post_session import post_session
from gptme_sessions.record import DISPATCH_KINDS, SessionRecord
from gptme_sessions.store import SessionStore


def test_record_keeps_valid_lineage():
    record = SessionRecord(
        session_id="child123", parent_session_id="parent99", dispatch_kind="worker"
    )
    assert record.parent_session_id == "parent99"
    assert record.dispatch_kind == "worker"


def test_record_drops_unknown_dispatch_kind():
    """A typo or a hand-edited row must not reach consumers as a real kind."""
    record = SessionRecord(session_id="child123", dispatch_kind="subprocess")
    assert record.dispatch_kind is None


def test_record_drops_self_parent():
    """A spawner leaking its own id into the child env would create a self-loop."""
    record = SessionRecord(session_id="same", parent_session_id="same")
    assert record.parent_session_id is None


def test_record_drops_blank_parent():
    record = SessionRecord(session_id="child", parent_session_id="   ")
    assert record.parent_session_id is None


def test_lineage_defaults_to_none():
    record = SessionRecord(session_id="child")
    assert record.parent_session_id is None
    assert record.dispatch_kind is None


def test_all_documented_kinds_are_accepted():
    for kind in DISPATCH_KINDS:
        assert SessionRecord(session_id="c", dispatch_kind=kind).dispatch_kind == kind


def test_post_session_persists_lineage(tmp_path: Path):
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="sonnet",
        run_type="worker",
        session_id="child-abc",
        parent_session_id="parent-xyz",
        dispatch_kind="worker",
        duration_seconds=30,
    )
    assert result.record.parent_session_id == "parent-xyz"
    assert result.record.dispatch_kind == "worker"

    reloaded = SessionStore(sessions_dir=tmp_path).load_all()
    assert len(reloaded) == 1
    assert reloaded[0].parent_session_id == "parent-xyz"
    assert reloaded[0].dispatch_kind == "worker"


def test_post_session_reads_lineage_from_environment(tmp_path: Path, monkeypatch):
    """Spawners export the env once; every recorder inherits it for free."""
    monkeypatch.setenv("BOB_PARENT_SESSION_ID", "parent-env")
    monkeypatch.setenv("BOB_DISPATCH_KIND", "fanout")
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="sonnet",
        session_id="child-env",
        duration_seconds=10,
    )
    assert result.record.parent_session_id == "parent-env"
    assert result.record.dispatch_kind == "fanout"


def test_explicit_argument_beats_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BOB_PARENT_SESSION_ID", "stale-env")
    monkeypatch.setenv("BOB_DISPATCH_KIND", "fanout")
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="sonnet",
        session_id="child-explicit",
        parent_session_id="real-parent",
        dispatch_kind="pm-dispatch",
        duration_seconds=10,
    )
    assert result.record.parent_session_id == "real-parent"
    assert result.record.dispatch_kind == "pm-dispatch"


def test_no_lineage_without_env_or_argument(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BOB_PARENT_SESSION_ID", raising=False)
    monkeypatch.delenv("BOB_DISPATCH_KIND", raising=False)
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        model="sonnet",
        session_id="top-level",
        duration_seconds=10,
    )
    assert result.record.parent_session_id is None
    assert result.record.dispatch_kind is None


def test_stale_env_parent_matching_own_id_is_dropped(tmp_path: Path, monkeypatch):
    """A child that reuses the parent's session id is not its own parent."""
    monkeypatch.setenv("BOB_PARENT_SESSION_ID", "same-id")
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="sonnet",
        session_id="same-id",
        duration_seconds=10,
    )
    assert result.record.parent_session_id is None


# --- dispatch_id (PM slot unit name, non-session dispatcher) ----------------


def test_record_keeps_dispatch_id():
    """dispatch_id is stored as-is (no validation, any non-empty string is valid)."""
    record = SessionRecord(session_id="child1", dispatch_id="bob-pm-gptme-gptme-slot-0")
    assert record.dispatch_id == "bob-pm-gptme-gptme-slot-0"


def test_record_dispatch_id_default_none():
    record = SessionRecord(session_id="child1")
    assert record.dispatch_id is None


def test_post_session_records_dispatch_id_from_argument(tmp_path: Path):
    """Explicit dispatch_id argument is stored in the session record."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        model="sonnet",
        session_id="pm-child",
        dispatch_kind="pm-dispatch",
        dispatch_id="bob-pm-gptme-gptme-slot-3",
        duration_seconds=10,
    )
    assert result.record.dispatch_id == "bob-pm-gptme-gptme-slot-3"


def test_post_session_reads_dispatch_id_from_env(tmp_path: Path, monkeypatch):
    """PM_DISPATCH_ID env var is the fallback when no explicit dispatch_id given."""
    monkeypatch.delenv("BOB_DISPATCH_ID", raising=False)
    monkeypatch.setenv("PM_DISPATCH_ID", "bob-pm-activitywatch-aw-webui-slot-1")
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="sonnet",
        session_id="pm-child2",
        dispatch_kind="pm-dispatch",
        duration_seconds=10,
    )
    assert result.record.dispatch_id == "bob-pm-activitywatch-aw-webui-slot-1"


def test_explicit_dispatch_id_beats_env(tmp_path: Path, monkeypatch):
    """Explicit argument takes priority over the PM_DISPATCH_ID env var."""
    monkeypatch.delenv("BOB_DISPATCH_ID", raising=False)
    monkeypatch.setenv("PM_DISPATCH_ID", "env-value")
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="sonnet",
        session_id="pm-child3",
        dispatch_id="explicit-value",
        duration_seconds=10,
    )
    assert result.record.dispatch_id == "explicit-value"


def test_no_dispatch_id_without_env_or_argument(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PM_DISPATCH_ID", raising=False)
    monkeypatch.delenv("BOB_DISPATCH_ID", raising=False)
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="gptme",
        model="sonnet",
        session_id="top-level",
        duration_seconds=10,
    )
    assert result.record.dispatch_id is None


def test_post_session_reads_dispatch_id_from_bob_env(tmp_path: Path, monkeypatch):
    """BOB_DISPATCH_ID is the harness-neutral fallback for non-PM dispatchers."""
    monkeypatch.delenv("PM_DISPATCH_ID", raising=False)
    monkeypatch.setenv("BOB_DISPATCH_ID", "bob-autonomous-fanout-code-1234")
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="sonnet",
        session_id="fanout-child1",
        dispatch_kind="fanout",
        duration_seconds=10,
    )
    assert result.record.dispatch_id == "bob-autonomous-fanout-code-1234"
    assert result.record.dispatch_kind == "fanout"


def test_bob_dispatch_id_beats_pm_dispatch_id(tmp_path: Path, monkeypatch):
    """A dispatcher's own BOB_DISPATCH_ID wins over an inherited PM_DISPATCH_ID.

    Slot units export PM_DISPATCH_ID and children inherit the whole
    environment, so a nested non-PM dispatcher would otherwise be attributed to
    the outer PM slot run.
    """
    monkeypatch.setenv("PM_DISPATCH_ID", "bob-pm-gptme-gptme-slot-2")
    monkeypatch.setenv("BOB_DISPATCH_ID", "bob-workers-run-99")
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="sonnet",
        session_id="worker-child1",
        dispatch_kind="worker",
        duration_seconds=10,
    )
    assert result.record.dispatch_id == "bob-workers-run-99"


def test_explicit_dispatch_id_beats_bob_env(tmp_path: Path, monkeypatch):
    """Explicit argument still outranks the harness-neutral env var."""
    monkeypatch.setenv("BOB_DISPATCH_ID", "env-value")
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="sonnet",
        session_id="fanout-child2",
        dispatch_id="explicit-value",
        duration_seconds=10,
    )
    assert result.record.dispatch_id == "explicit-value"


def test_blank_bob_dispatch_id_falls_back_to_pm(tmp_path: Path, monkeypatch):
    """An exported-but-empty BOB_DISPATCH_ID must not blank out the PM value."""
    monkeypatch.setenv("BOB_DISPATCH_ID", "")
    monkeypatch.setenv("PM_DISPATCH_ID", "bob-pm-gptme-gptme-slot-5")
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="sonnet",
        session_id="pm-child5",
        duration_seconds=10,
    )
    assert result.record.dispatch_id == "bob-pm-gptme-gptme-slot-5"


def test_dispatch_id_roundtrips_through_store(tmp_path: Path):
    """dispatch_id survives serialization to JSONL and back."""
    store = SessionStore(sessions_dir=tmp_path)
    result = post_session(
        store=store,
        harness="claude-code",
        model="sonnet",
        session_id="pm-child4",
        dispatch_id="bob-pm-gptme-gptme-slot-7",
        duration_seconds=10,
    )
    assert result.record.dispatch_id == "bob-pm-gptme-gptme-slot-7"
    reloaded = store.load_all()
    assert any(r.dispatch_id == "bob-pm-gptme-gptme-slot-7" for r in reloaded)
