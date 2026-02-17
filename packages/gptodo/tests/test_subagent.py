"""Tests for subagent session management."""

from datetime import datetime, timezone

import pytest

from gptodo.subagent import (
    AgentSession,
    save_session,
    load_session,
    list_sessions,
)


@pytest.fixture
def sessions_dir(tmp_path):
    """Create a temporary sessions directory."""
    sd = tmp_path / "state" / "sessions"
    sd.mkdir(parents=True)
    return tmp_path


def _make_session(
    session_id: str = "test-abc123",
    task_id: str = "my-task",
    status: str = "running",
    **kwargs,
) -> AgentSession:
    return AgentSession(
        session_id=session_id,
        task_id=task_id,
        agent_type=kwargs.get("agent_type", "general"),
        backend=kwargs.get("backend", "gptme"),
        started=kwargs.get("started", datetime.now(timezone.utc).isoformat()),
        status=status,
        tmux_session=kwargs.get("tmux_session"),
        output_file=kwargs.get("output_file"),
    )


def test_save_and_load_session(sessions_dir):
    session = _make_session()
    save_session(session, sessions_dir)
    loaded = load_session("test-abc123", sessions_dir)
    assert loaded is not None
    assert loaded.session_id == "test-abc123"
    assert loaded.task_id == "my-task"
    assert loaded.status == "running"


def test_list_sessions_all(sessions_dir):
    save_session(_make_session("s1", status="running"), sessions_dir)
    save_session(_make_session("s2", status="completed"), sessions_dir)
    save_session(_make_session("s3", status="running"), sessions_dir)

    all_sessions = list_sessions(sessions_dir)
    assert len(all_sessions) == 3


def test_list_sessions_by_status(sessions_dir):
    save_session(_make_session("s1", status="running"), sessions_dir)
    save_session(_make_session("s2", status="completed"), sessions_dir)
    save_session(_make_session("s3", status="running"), sessions_dir)

    running = list_sessions(sessions_dir, status="running")
    assert len(running) == 2

    completed = list_sessions(sessions_dir, status="completed")
    assert len(completed) == 1


def test_load_nonexistent_session(sessions_dir):
    loaded = load_session("nonexistent", sessions_dir)
    assert loaded is None


def test_load_corrupted_session(sessions_dir):
    sd = sessions_dir / "state" / "sessions"
    (sd / "corrupt.json").write_text("not valid json")
    loaded = load_session("corrupt", sessions_dir)
    assert loaded is None
