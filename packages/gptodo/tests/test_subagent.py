"""Tests for subagent session management."""

import subprocess
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from gptodo.auth import is_transient_auth_death
from gptodo.subagent import (
    AgentSession,
    _setup_coordination,
    list_sessions,
    load_session,
    save_session,
    spawn_agent,
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


@pytest.mark.parametrize(
    "output",
    [
        "401 Invalid authentication credentials",
        '{"error":"authentication_failed"}',
        '{"type":"authentication_error"}',
        "Unauthorized: please run /login",
        "Authentication failed",
        "Authentication error",
    ],
)
def test_transient_auth_death_signatures(output):
    assert is_transient_auth_death(output)


def test_transient_auth_death_requires_small_output():
    assert not is_transient_auth_death("401\n" + "x" * 3000)


def test_transient_auth_death_uses_encoded_byte_size():
    assert not is_transient_auth_death("401 " + "å" * 1000, max_bytes=1500)


@patch("gptodo.subagent.time.sleep")
@patch("gptodo.subagent.subprocess.run")
def test_claude_foreground_retries_tiny_auth_failure_once(run, sleep, sessions_dir):
    run.side_effect = [
        subprocess.CompletedProcess([], 1, "", "Error: 401 Invalid authentication credentials"),
        subprocess.CompletedProcess([], 0, "done", ""),
    ]

    session = spawn_agent("task", "prompt", backend="claude", workspace=sessions_dir)

    assert session.status == "completed"
    assert run.call_count == 2
    sleep.assert_called_once_with(5)
    assert "Invalid authentication credentials" in open(session.output_file).read()
    assert "done" in open(session.output_file).read()


@patch("gptodo.subagent.time.sleep")
@patch("gptodo.subagent.subprocess.run")
def test_claude_foreground_does_not_retry_large_output_mentioning_401(run, sleep, sessions_dir):
    run.return_value = subprocess.CompletedProcess([], 1, "401\n" + "x" * 3000, "")

    session = spawn_agent("task", "prompt", backend="claude", workspace=sessions_dir)

    assert session.status == "failed"
    run.assert_called_once()
    sleep.assert_not_called()


@patch("gptodo.subagent.time.sleep")
@patch("gptodo.subagent.subprocess.run")
def test_claude_foreground_retries_only_once(run, sleep, sessions_dir):
    failure = subprocess.CompletedProcess([], 1, "", "authentication_failed")
    run.side_effect = [failure, failure]

    session = spawn_agent("task", "prompt", backend="claude", workspace=sessions_dir)

    assert session.status == "failed"
    assert run.call_count == 2
    sleep.assert_called_once_with(5)


@patch("gptodo.subagent.subprocess.run")
def test_claude_background_command_retries_auth_failure(run, sessions_dir):
    run.return_value = subprocess.CompletedProcess([], 0, "", "")

    spawn_agent("task", "prompt", backend="claude", background=True, workspace=sessions_dir)

    tmux_command = run.call_args.args[0][-1]
    assert tmux_command.count("claude -p") == 2
    assert "gptodo.auth --classify-file" in tmux_command
    assert ".first-auth-failure" in tmux_command
    assert "sleep 5" in tmux_command
    assert "EXIT_CODE=$EXIT_CODE" in tmux_command


@patch("gptodo.subagent.subprocess.run")
def test_codex_background_command_does_not_retry_auth_failure(run, sessions_dir):
    run.return_value = subprocess.CompletedProcess([], 0, "", "")

    spawn_agent("task", "prompt", backend="codex", background=True, workspace=sessions_dir)

    tmux_command = run.call_args.args[0][-1]
    assert "gptodo.auth --classify-file" not in tmux_command
    assert ".first-auth-failure" not in tmux_command
    assert tmux_command.count("sleep 5") == 0


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
