"""Tests for subagent session management."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from gptodo.subagent import (
    AgentSession,
    _setup_coordination,
    list_sessions,
    load_session,
    save_session,
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


# --- Coordination tests ---


@pytest.fixture
def coord_workspace(tmp_path):
    """Create a workspace with coordination system prompt."""
    prompt_dir = tmp_path / "packages" / "coordination"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "agent-system-prompt.md").write_text(
        "# Coordination Protocol\nFollow this protocol.\n"
    )
    return tmp_path


def test_setup_coordination_auto_detect_db(coord_workspace):
    """Test coordination setup auto-detects DB path."""
    with patch("gptodo.subagent.subprocess.run"):
        agent_id, db_path, prompt_path = _setup_coordination(coord_workspace)

    assert agent_id.startswith("agent_")
    assert "state/coordination/coord.db" in db_path
    assert "agent-system-prompt.md" in prompt_path
    # DB dir should be created
    assert (coord_workspace / "state" / "coordination").exists()


def test_setup_coordination_explicit_db(coord_workspace):
    """Test coordination with explicit DB path."""
    custom_db = str(coord_workspace / "custom" / "my.db")
    with patch("gptodo.subagent.subprocess.run"):
        agent_id, db_path, prompt_path = _setup_coordination(
            coord_workspace, coordination_db=custom_db
        )

    assert db_path == custom_db
    assert (coord_workspace / "custom").exists()


def test_setup_coordination_missing_prompt(tmp_path):
    """Test coordination fails gracefully without system prompt."""
    with pytest.raises(FileNotFoundError, match="Coordination system prompt"):
        _setup_coordination(tmp_path)


def test_setup_coordination_announce_failure(coord_workspace):
    """Test coordination continues when announce subprocess fails."""
    with patch(
        "gptodo.subagent.subprocess.run",
        side_effect=FileNotFoundError("coordination not found"),
    ):
        agent_id, db_path, prompt_path = _setup_coordination(coord_workspace)

    # Should still return valid results despite announce failure
    assert agent_id.startswith("agent_")
    assert db_path.endswith("coord.db")
