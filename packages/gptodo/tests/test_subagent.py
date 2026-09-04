"""Tests for subagent session management."""

import re
import subprocess
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from gptodo._auth import DEFAULT_MAX_BYTES
from gptodo.cli import cli
from gptodo.subagent import (
    TERMINAL_SESSION_STATUSES,
    AgentSession,
    _setup_coordination,
    check_session,
    cleanup_sessions,
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
    save_session(_make_session("s4", status="auth_failed"), sessions_dir)

    running = list_sessions(sessions_dir, status="running")
    assert len(running) == 2

    completed = list_sessions(sessions_dir, status="completed")
    assert len(completed) == 1

    auth_failed = list_sessions(sessions_dir, status="auth_failed")
    assert len(auth_failed) == 1
    assert auth_failed[0].session_id == "s4"


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


# ── 401 retry tests ───────────────────────────────────────────────────────────


def _make_proc(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_spawn_agent_401_retries_and_succeeds(sessions_dir):
    """Tiny 401 output → one retry → succeeds on second attempt."""
    auth_death = MagicMock(
        returncode=1,
        stdout="Error: 401 unauthorized\n",
        stderr="",
    )
    success = MagicMock(returncode=0, stdout="Task complete\n", stderr="")

    with (
        patch("gptodo.subagent.subprocess.run", side_effect=[auth_death, success]),
        patch("gptodo.subagent.time.sleep") as mock_sleep,
    ):
        session = spawn_agent(
            task_id="t1",
            prompt="do something",
            backend="claude",
            background=False,
            workspace=sessions_dir,
        )

    assert session.status == "completed"
    assert mock_sleep.called
    assert mock_sleep.call_args[0][0] >= 1  # backoff > 0s


def test_spawn_agent_401_retries_still_fails(sessions_dir):
    """Two consecutive 401s → one retry → status auth_failed."""
    auth_death = MagicMock(
        returncode=1,
        stdout="authentication_error\n",
        stderr="",
    )

    with (
        patch("gptodo.subagent.subprocess.run", side_effect=[auth_death, auth_death]),
        patch("gptodo.subagent.time.sleep"),
    ):
        session = spawn_agent(
            task_id="t2",
            prompt="do something",
            backend="claude",
            background=False,
            workspace=sessions_dir,
        )

    assert session.status == "auth_failed"
    assert "retried" in (session.error or "")


def test_spawn_agent_large_401_prose_no_retry(sessions_dir):
    """Large output with '401' in prose must NOT trigger a retry."""
    large_output = "I handled the 401 case in PR #401.\n" + "x" * DEFAULT_MAX_BYTES
    big_fail = MagicMock(returncode=1, stdout=large_output, stderr="")

    with (
        patch("gptodo.subagent.subprocess.run", return_value=big_fail) as mock_run,
        patch("gptodo.subagent.time.sleep") as mock_sleep,
    ):
        session = spawn_agent(
            task_id="t3",
            prompt="do something",
            backend="claude",
            background=False,
            workspace=sessions_dir,
        )

    # Only one subprocess call (no retry)
    assert mock_run.call_count == 1
    assert not mock_sleep.called
    assert session.status == "failed"


def test_spawn_agent_gptme_backend_no_401_retry(sessions_dir):
    """gptme backend does NOT get the 401 retry (it has its own auth layer)."""
    auth_death = MagicMock(returncode=1, stdout="401 unauthorized\n", stderr="")

    with (
        patch("gptodo.subagent.subprocess.run", return_value=auth_death) as mock_run,
        patch("gptodo.subagent.time.sleep") as mock_sleep,
    ):
        session = spawn_agent(
            task_id="t4",
            prompt="do something",
            backend="gptme",
            background=False,
            workspace=sessions_dir,
        )

    assert mock_run.call_count == 1
    assert not mock_sleep.called
    assert session.status == "failed"


def test_spawn_agent_codex_backend_no_401_retry(sessions_dir):
    """codex backend does NOT get the 401 retry (claude-only policy)."""
    auth_death = MagicMock(returncode=1, stdout="401 unauthorized\n", stderr="")

    with (
        patch("gptodo.subagent.subprocess.run", return_value=auth_death) as mock_run,
        patch("gptodo.subagent.time.sleep") as mock_sleep,
    ):
        session = spawn_agent(
            task_id="t4c",
            prompt="do something",
            backend="codex",
            background=False,
            workspace=sessions_dir,
        )

    assert mock_run.call_count == 1
    assert not mock_sleep.called
    assert session.status == "failed"


@pytest.mark.parametrize(
    "stderr",
    ["403 Forbidden\n", "credit balance is too low\n", "disabled subscription\n"],
)
def test_spawn_agent_persistent_403_billing_no_retry(sessions_dir, stderr):
    """Persistent 403 / billing failures must not retry or stamp auth_failed."""
    persistent = MagicMock(returncode=1, stdout="", stderr=stderr)

    with (
        patch("gptodo.subagent.subprocess.run", return_value=persistent) as mock_run,
        patch("gptodo.subagent.time.sleep") as mock_sleep,
    ):
        session = spawn_agent(
            task_id="t4p",
            prompt="do something",
            backend="claude",
            background=False,
            workspace=sessions_dir,
        )

    assert mock_run.call_count == 1
    assert not mock_sleep.called
    assert session.status == "failed"


def test_spawn_agent_401_retry_timeout_persists_first_output(sessions_dir):
    """First-attempt output is persisted even if the retry times out."""
    auth_death = MagicMock(
        returncode=1,
        stdout="Error: 401 unauthorized\n",
        stderr="",
    )

    with (
        patch(
            "gptodo.subagent.subprocess.run",
            side_effect=[
                auth_death,
                subprocess.TimeoutExpired(cmd="claude", timeout=1),
            ],
        ),
        patch("gptodo.subagent.time.sleep"),
    ):
        session = spawn_agent(
            task_id="t4t",
            prompt="do something",
            backend="claude",
            background=False,
            workspace=sessions_dir,
        )

    assert session.status == "failed"
    assert "Timeout" in (session.error or "")
    output_path = sessions_dir / "state" / "sessions" / f"{session.session_id}.output"
    assert output_path.exists()
    assert "401 unauthorized" in output_path.read_text()


def test_check_session_background_auth_death_classified(sessions_dir):
    """Background claude session: EXIT_CODE non-zero + tiny auth output → auth_failed."""
    output_file = sessions_dir / "state" / "sessions" / "agent_authtest.output"
    output_file.write_text("authentication_error: 401 unauthorized\nEXIT_CODE=1\n")

    session = _make_session(
        session_id="agent_authtest",
        status="running",
        backend="claude",
        tmux_session="gptodo_agent_authtest",
        output_file=str(output_file),
    )
    save_session(session, sessions_dir)

    # tmux has-session returns 1 → session ended
    with patch("gptodo.subagent.subprocess.run", return_value=MagicMock(returncode=1)):
        updated = check_session("agent_authtest", sessions_dir)

    assert updated is not None
    assert updated.status == "auth_failed", f"expected auth_failed, got {updated.status!r}"
    assert "auth-death" in (updated.error or "")


def test_check_session_background_normal_failure_not_auth(sessions_dir):
    """Background session: EXIT_CODE non-zero, large output → status failed (not auth_failed)."""
    big_output = (
        "Task ran, hit some issue. Exiting.\n" + "x" * DEFAULT_MAX_BYTES + "\nEXIT_CODE=1\n"
    )
    output_file = sessions_dir / "state" / "sessions" / "agent_bigfail.output"
    output_file.write_text(big_output)

    session = _make_session(
        session_id="agent_bigfail",
        status="running",
        tmux_session="gptodo_agent_bigfail",
        output_file=str(output_file),
    )
    save_session(session, sessions_dir)

    with patch("gptodo.subagent.subprocess.run", return_value=MagicMock(returncode=1)):
        updated = check_session("agent_bigfail", sessions_dir)

    assert updated is not None
    assert updated.status == "failed"


@pytest.mark.parametrize("backend", ["gptme", "codex"])
def test_check_session_non_claude_backend_not_auth_failed(sessions_dir, backend):
    """Background gptme/codex sessions stay failed even on tiny 401 output."""
    output_file = sessions_dir / "state" / "sessions" / f"agent_{backend}.output"
    output_file.write_text("authentication_error: 401 unauthorized\nEXIT_CODE=1\n")

    session = _make_session(
        session_id=f"agent_{backend}",
        status="running",
        backend=backend,
        tmux_session=f"gptodo_agent_{backend}",
        output_file=str(output_file),
    )
    save_session(session, sessions_dir)

    with patch("gptodo.subagent.subprocess.run", return_value=MagicMock(returncode=1)):
        updated = check_session(f"agent_{backend}", sessions_dir)

    assert updated is not None
    assert updated.status == "failed"


def test_cleanup_sessions_removes_old_auth_failed(sessions_dir):
    """auth_failed is terminal: expired session JSON/output/prompt are removed."""
    old_started = "2020-01-01T00:00:00+00:00"
    sessions = sessions_dir / "state" / "sessions"
    output_file = sessions / "oldauth.output"
    prompt_file = sessions / "oldauth.prompt"
    output_file.write_text("authentication_error: 401 unauthorized\n")
    prompt_file.write_text("do the thing\n")

    save_session(
        _make_session(
            "oldauth",
            status="auth_failed",
            started=old_started,
            output_file=str(output_file),
        ),
        sessions_dir,
    )
    save_session(
        _make_session("stillrunning", status="running", started=old_started),
        sessions_dir,
    )

    with (
        patch("gptodo.subagent.check_session"),
        patch("gptodo.subagent.subprocess.run", side_effect=FileNotFoundError),
    ):
        count = cleanup_sessions(sessions_dir, older_than_hours=24)

    assert count == 1
    assert not (sessions / "oldauth.json").exists()
    assert not output_file.exists()
    assert not prompt_file.exists()
    assert (sessions / "stillrunning.json").exists()


def test_cleanup_sessions_keeps_recent_auth_failed(sessions_dir):
    """auth_failed sessions younger than the cutoff stay on disk."""
    save_session(_make_session("freshauth", status="auth_failed"), sessions_dir)

    with (
        patch("gptodo.subagent.check_session"),
        patch("gptodo.subagent.subprocess.run", side_effect=FileNotFoundError),
    ):
        count = cleanup_sessions(sessions_dir, older_than_hours=24)

    assert count == 0
    loaded = load_session("freshauth", sessions_dir)
    assert loaded is not None
    assert loaded.status == "auth_failed"


def test_terminal_statuses_include_auth_failed():
    assert "auth_failed" in TERMINAL_SESSION_STATUSES
    assert "running" not in TERMINAL_SESSION_STATUSES


def test_sessions_cli_accepts_auth_failed_status():
    """gptodo sessions --status auth_failed is a valid Choice, not rejected."""
    from click.testing import CliRunner

    status_opt = next(p for p in cli.commands["sessions"].params if p.name == "status")
    assert "auth_failed" in status_opt.type.choices

    runner = CliRunner()
    result = runner.invoke(cli, ["sessions", "--help"])
    assert result.exit_code == 0
    assert "auth_failed" in result.output


def _write_task(tasks_dir, name, **metadata):
    lines = ["---"]
    for key, value in metadata.items():
        lines.append(f"{key}: {value}")
    lines.extend(["---", f"# {name}"])
    (tasks_dir / f"{name}.md").write_text("\n".join(lines))


@pytest.mark.parametrize("status", ["auth_failed", "failed"])
def test_execute_task_agent_returns_false_on_terminal_failure(sessions_dir, monkeypatch, status):
    """_execute_task_agent must return False on failed/auth_failed (not None)."""
    from gptodo.cli import _execute_task_agent

    tasks_dir = sessions_dir / "tasks"
    tasks_dir.mkdir()
    _write_task(tasks_dir, "my-task", state="todo", created="2026-08-30T00:00:00")
    (sessions_dir / ".git").mkdir()
    monkeypatch.chdir(sessions_dir)

    fake = _make_session(status=status, error="auth-death: transient 401", backend="claude")
    monkeypatch.setattr("gptodo.cli.spawn_agent", lambda **kwargs: fake)

    ok = _execute_task_agent(
        task_id="my-task",
        prompt=None,
        agent_type="general",
        backend="claude",
        background=False,
        model=None,
        timeout=60,
    )
    assert ok is False


def test_execute_task_agent_returns_true_on_completed(sessions_dir, monkeypatch):
    from gptodo.cli import _execute_task_agent

    tasks_dir = sessions_dir / "tasks"
    tasks_dir.mkdir()
    _write_task(tasks_dir, "my-task", state="todo", created="2026-08-30T00:00:00")
    (sessions_dir / ".git").mkdir()
    monkeypatch.chdir(sessions_dir)

    fake = _make_session(status="completed", backend="claude")
    monkeypatch.setattr("gptodo.cli.spawn_agent", lambda **kwargs: fake)

    ok = _execute_task_agent(
        task_id="my-task",
        prompt=None,
        agent_type="general",
        backend="claude",
        background=False,
        model=None,
        timeout=60,
    )
    assert ok is True


def test_loop_counts_auth_failed_as_failure(sessions_dir, monkeypatch):
    """Sequential gptodo loop must not count an auth_failed spawn as success."""
    from click.testing import CliRunner

    tasks_dir = sessions_dir / "tasks"
    tasks_dir.mkdir()
    _write_task(tasks_dir, "fail-task", state="todo", created="2026-08-30T00:00:00")
    _write_task(tasks_dir, "ok-task", state="todo", created="2026-08-30T00:01:00")
    (sessions_dir / ".git").mkdir()
    monkeypatch.chdir(sessions_dir)

    outcomes = {"fail-task": False, "ok-task": True}

    def fake_execute(task_id, **kwargs):
        return outcomes[task_id]

    monkeypatch.setattr("gptodo.cli._execute_task_agent", fake_execute)

    result = CliRunner().invoke(cli, ["loop", "-n", "2"])
    assert result.exit_code == 0, result.output
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "1 succeeded, 1 failed" in plain


# ── Dispatch lineage tests (BOB_PARENT_SESSION_ID / BOB_DISPATCH_KIND) ────────


def test_spawn_agent_background_sets_dispatch_lineage(sessions_dir, monkeypatch):
    """Background tmux commands export dispatch kind and parent session ID."""
    monkeypatch.setenv("CC_SESSION_ID", "cc-parent-abc123")
    monkeypatch.delenv("BOB_SESSION_ID", raising=False)

    with patch("gptodo.subagent.subprocess.run", return_value=_make_proc(returncode=0)) as mock_run:
        spawn_agent(
            task_id="t-bg-lineage",
            prompt="work",
            backend="claude",
            background=True,
            workspace=sessions_dir,
        )

    tmux_cmd = mock_run.call_args.args[0]
    assert tmux_cmd[:4] == ["tmux", "new-session", "-d", "-s"]
    assert tmux_cmd[4].startswith("gptodo_agent_")
    assert "export BOB_SESSION_ID=agent_" in tmux_cmd[-1]
    assert "export BOB_DISPATCH_KIND=gptodo-spawn" in tmux_cmd[-1]
    assert "export BOB_PARENT_SESSION_ID=cc-parent-abc123" in tmux_cmd[-1]


def test_spawn_agent_background_clears_inherited_parent_session_id(sessions_dir, monkeypatch):
    """Background tmux commands do not inherit a stale grandparent ID."""
    monkeypatch.delenv("CC_SESSION_ID", raising=False)
    monkeypatch.delenv("BOB_SESSION_ID", raising=False)
    monkeypatch.setenv("BOB_PARENT_SESSION_ID", "stale-grandparent")

    with patch("gptodo.subagent.subprocess.run", return_value=_make_proc(returncode=0)) as mock_run:
        spawn_agent(
            task_id="t-bg-no-parent",
            prompt="work",
            backend="claude",
            background=True,
            workspace=sessions_dir,
        )

    tmux_cmd = mock_run.call_args.args[0]
    assert "BOB_PARENT_SESSION_ID" in tmux_cmd[-1]
    assert "export BOB_PARENT_SESSION_ID=" not in tmux_cmd[-1]


@pytest.mark.parametrize("backend", ["gptme", "claude", "codex"])
def test_spawn_agent_foreground_sets_child_session_lineage(sessions_dir, backend):
    """Every foreground backend receives its own ID and gptodo-spawn kind."""
    captured_env: dict = {}

    def _fake_run(cmd, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return _make_proc(returncode=0, stdout="done")

    with patch("gptodo.subagent.subprocess.run", side_effect=_fake_run):
        session = spawn_agent(
            task_id="t-dk",
            prompt="work",
            backend=backend,
            background=False,
            workspace=sessions_dir,
        )

    assert captured_env.get("BOB_SESSION_ID") == session.session_id
    assert captured_env.get("BOB_DISPATCH_KIND") == "gptodo-spawn"


@pytest.mark.parametrize("backend", ["gptme", "claude", "codex"])
def test_spawn_agent_foreground_sets_parent_session_id_from_cc_session_id(
    sessions_dir, backend, monkeypatch
):
    """BOB_PARENT_SESSION_ID is set from CC_SESSION_ID when available."""
    monkeypatch.setenv("CC_SESSION_ID", "cc-parent-abc123")
    monkeypatch.delenv("BOB_SESSION_ID", raising=False)

    captured_env: dict = {}

    def _fake_run(cmd, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return _make_proc(returncode=0, stdout="done")

    with patch("gptodo.subagent.subprocess.run", side_effect=_fake_run):
        session = spawn_agent(
            task_id="t-pid-cc",
            prompt="work",
            backend=backend,
            background=False,
            workspace=sessions_dir,
        )

    assert captured_env.get("BOB_SESSION_ID") == session.session_id
    assert captured_env.get("BOB_PARENT_SESSION_ID") == "cc-parent-abc123"


@pytest.mark.parametrize("backend", ["gptme", "claude"])
def test_spawn_agent_foreground_sets_parent_session_id_from_bob_session_id(
    sessions_dir, backend, monkeypatch
):
    """BOB_PARENT_SESSION_ID falls back to BOB_SESSION_ID when CC_SESSION_ID absent."""
    monkeypatch.delenv("CC_SESSION_ID", raising=False)
    monkeypatch.setenv("BOB_SESSION_ID", "bob-parent-xyz789")

    captured_env: dict = {}

    def _fake_run(cmd, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return _make_proc(returncode=0, stdout="done")

    with patch("gptodo.subagent.subprocess.run", side_effect=_fake_run):
        session = spawn_agent(
            task_id="t-pid-bob",
            prompt="work",
            backend=backend,
            background=False,
            workspace=sessions_dir,
        )

    assert captured_env.get("BOB_SESSION_ID") == session.session_id
    assert captured_env.get("BOB_PARENT_SESSION_ID") == "bob-parent-xyz789"


def test_spawn_agent_foreground_no_parent_session_id_when_env_absent(sessions_dir, monkeypatch):
    """No BOB_PARENT_SESSION_ID key when neither CC_SESSION_ID nor BOB_SESSION_ID is set."""
    monkeypatch.delenv("CC_SESSION_ID", raising=False)
    monkeypatch.delenv("BOB_SESSION_ID", raising=False)
    monkeypatch.setenv("BOB_PARENT_SESSION_ID", "stale-grandparent")

    captured_env: dict = {}

    def _fake_run(cmd, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return _make_proc(returncode=0, stdout="done")

    with patch("gptodo.subagent.subprocess.run", side_effect=_fake_run):
        session = spawn_agent(
            task_id="t-nopid",
            prompt="work",
            backend="claude",
            background=False,
            workspace=sessions_dir,
        )

    assert captured_env.get("BOB_SESSION_ID") == session.session_id
    assert "BOB_PARENT_SESSION_ID" not in captured_env
    assert captured_env.get("BOB_DISPATCH_KIND") == "gptodo-spawn"


def test_spawn_agent_foreground_dispatch_kind_overrides_inherited(sessions_dir, monkeypatch):
    """BOB_DISPATCH_KIND=gptodo-spawn overrides any inherited value (e.g. 'worker')."""
    monkeypatch.setenv("BOB_DISPATCH_KIND", "worker")

    captured_env: dict = {}

    def _fake_run(cmd, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return _make_proc(returncode=0, stdout="done")

    with patch("gptodo.subagent.subprocess.run", side_effect=_fake_run):
        spawn_agent(
            task_id="t-override",
            prompt="work",
            backend="gptme",
            background=False,
            workspace=sessions_dir,
        )

    assert captured_env.get("BOB_DISPATCH_KIND") == "gptodo-spawn"
