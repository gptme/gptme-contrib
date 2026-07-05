"""Tests for the executor abstraction."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from gptme_runloops.utils import execution as execution_mod
from gptme_runloops.utils.execution import ExecutionResult
from gptme_runloops.utils.executor import (
    ClaudeCodeExecutor,
    Executor,
    GptmeExecutor,
    GrokBuildExecutor,
    get_executor,
    list_backends,
)

# --- Executor ABC tests ---


def test_executor_is_abstract():
    """Test that Executor cannot be instantiated directly."""
    with pytest.raises(TypeError):
        Executor()  # type: ignore[abstract]


class MockExecutor(Executor):
    """Concrete executor for testing."""

    name = "mock"

    def execute(self, prompt, workspace, timeout, **kwargs):
        return ExecutionResult(exit_code=0)


def test_mock_executor_instantiation():
    """Test that a concrete executor can be instantiated."""
    executor = MockExecutor()
    assert executor.name == "mock"


def test_mock_executor_execute():
    """Test basic execute on a mock executor."""
    executor = MockExecutor()
    result = executor.execute("test", Path("/tmp"), 60)
    assert result.success
    assert result.exit_code == 0


# --- GptmeExecutor tests ---


def test_gptme_executor_name():
    """Test GptmeExecutor has correct name."""
    executor = GptmeExecutor()
    assert executor.name == "gptme"


def test_gptme_executor_delegates_to_execute_gptme():
    """Test that GptmeExecutor delegates to execute_gptme function."""
    executor = GptmeExecutor()

    with patch("gptme_runloops.utils.executor.execute_gptme") as mock:
        mock.return_value = ExecutionResult(exit_code=0)

        result = executor.execute(
            prompt="test prompt",
            workspace=Path("/tmp"),
            timeout=300,
            model="test-model",
            tool_format="xml",
            tools="save,read",
        )

        assert result.success
        mock.assert_called_once_with(
            prompt="test prompt",
            workspace=Path("/tmp"),
            timeout=300,
            non_interactive=True,
            run_type="run",
            model="test-model",
            tool_format="xml",
            tools="save,read",
            env=None,
        )


def test_gptme_executor_passes_run_type():
    """Test that run_type is passed through to execute_gptme."""
    executor = GptmeExecutor()

    with patch("gptme_runloops.utils.executor.execute_gptme") as mock:
        mock.return_value = ExecutionResult(exit_code=0)

        executor.execute(
            prompt="test",
            workspace=Path("/tmp"),
            timeout=60,
            run_type="autonomous",
        )

        call_kwargs = mock.call_args
        assert call_kwargs[1]["run_type"] == "autonomous"


def test_execute_gptme_persists_trajectory_and_removes_isolated_logs(
    tmp_path, monkeypatch
):
    """execute_gptme copies conversation.jsonl durably and removes GPTME_LOGS_HOME."""
    global_log_dir = tmp_path / "global-logs"
    global_log_dir.mkdir()
    monkeypatch.setattr(execution_mod, "GLOBAL_LOG_DIR", global_log_dir)
    monkeypatch.setattr(execution_mod.shutil, "which", lambda _: "/usr/bin/gptme")

    isolated_log_dirs: list[Path] = []

    def fake_run(cmd, *, env, **kwargs):
        isolated_logs = Path(env["GPTME_LOGS_HOME"])
        isolated_log_dirs.append(isolated_logs)
        session_dir = isolated_logs / "2026-07-02-test-session"
        session_dir.mkdir(parents=True)
        (session_dir / "conversation.jsonl").write_text('{"role": "user"}\n')
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(execution_mod.subprocess, "run", fake_run)

    result = execution_mod.execute_gptme(
        "Test prompt",
        workspace=tmp_path,
        timeout=60,
        run_type="test",
    )

    assert result.success
    assert result.trajectory_path is not None
    assert result.trajectory_path.exists()
    assert result.trajectory_path.parent.parent == global_log_dir
    assert isolated_log_dirs
    assert isolated_log_dirs[0] not in result.trajectory_path.parents
    assert not isolated_log_dirs[0].exists()
    assert not list(tmp_path.glob(".gptme-prompt-*.txt"))


def test_execute_gptme_logs_duplicate_conversation_files(tmp_path, monkeypatch):
    """Unexpected duplicate conversation.jsonl files are visible in the run log."""
    global_log_dir = tmp_path / "global-logs"
    global_log_dir.mkdir()
    monkeypatch.setattr(execution_mod, "GLOBAL_LOG_DIR", global_log_dir)
    monkeypatch.setattr(execution_mod.shutil, "which", lambda _: "/usr/bin/gptme")

    def fake_run(cmd, *, env, **kwargs):
        isolated_logs = Path(env["GPTME_LOGS_HOME"])
        session_a = isolated_logs / "a-session"
        session_b = isolated_logs / "b-session"
        session_a.mkdir(parents=True)
        session_b.mkdir(parents=True)
        (session_a / "conversation.jsonl").write_text('{"session": "a"}\n')
        (session_b / "conversation.jsonl").write_text('{"session": "b"}\n')
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(execution_mod.subprocess, "run", fake_run)

    result = execution_mod.execute_gptme(
        "Test prompt",
        workspace=tmp_path,
        timeout=60,
        run_type="duplicate",
    )

    assert result.trajectory_path is not None
    assert result.trajectory_path.read_text() == '{"session": "a"}\n'
    log_text = "\n".join(path.read_text() for path in global_log_dir.glob("*.log"))
    assert "Multiple conversation.jsonl files found" in log_text
    assert "a-session/conversation.jsonl" in log_text
    assert "b-session/conversation.jsonl" in log_text


def test_gptme_executor_is_available():
    """Test is_available checks for gptme binary."""
    executor = GptmeExecutor()

    with patch("shutil.which", return_value="/usr/bin/gptme"):
        assert executor.is_available()

    with patch("shutil.which", return_value=None):
        assert not executor.is_available()


# --- ClaudeCodeExecutor tests ---


def test_claude_code_executor_name():
    """Test ClaudeCodeExecutor has correct name."""
    executor = ClaudeCodeExecutor()
    assert executor.name == "claude-code"


def test_claude_code_executor_binary_name():
    """Test ClaudeCodeExecutor checks for 'claude' binary."""
    executor = ClaudeCodeExecutor()
    assert executor._binary_name == "claude"


def test_claude_code_executor_is_available():
    """Test is_available checks for claude binary."""
    executor = ClaudeCodeExecutor()

    with patch("shutil.which", return_value="/usr/bin/claude"):
        assert executor.is_available()

    with patch("shutil.which", return_value=None):
        assert not executor.is_available()


def test_claude_code_executor_basic_execution():
    """Test basic Claude Code execution."""
    executor = ClaudeCodeExecutor()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = executor.execute(
            prompt="test prompt",
            workspace=Path("/tmp"),
            timeout=300,
        )

        assert result.success
        assert result.exit_code == 0

        # Verify basic command structure
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert cmd[0] == "claude"
        assert cmd[1] == "-p"
        assert cmd[2] == "test prompt"
        assert "--dangerously-skip-permissions" in cmd


def test_claude_code_executor_strips_claudecode_env():
    """Test that CLAUDECODE env vars are removed for nesting."""
    executor = ClaudeCodeExecutor()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with patch.dict(
            "os.environ",
            {"CLAUDECODE": "true", "CLAUDE_CODE_ENTRYPOINT": "/usr/bin/claude"},
        ):
            executor.execute(
                prompt="test",
                workspace=Path("/tmp"),
                timeout=60,
            )

            call_kwargs = mock_run.call_args[1]
            env = call_kwargs["env"]
            assert "CLAUDECODE" not in env
            assert "CLAUDE_CODE_ENTRYPOINT" not in env


def test_claude_code_executor_model_override():
    """Test model override is passed to claude CLI."""
    executor = ClaudeCodeExecutor()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        executor.execute(
            prompt="test",
            workspace=Path("/tmp"),
            timeout=60,
            model="sonnet",
        )

        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "sonnet"


def test_claude_code_executor_system_prompt():
    """Test system prompt file is passed to claude CLI."""
    executor = ClaudeCodeExecutor()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("You are a helpful assistant.")
        f.flush()
        tmp_path = f.name

    try:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            executor.execute(
                prompt="test",
                workspace=Path("/tmp"),
                timeout=60,
                system_prompt_file=Path(tmp_path),
            )

            cmd = mock_run.call_args[0][0]
            assert "--append-system-prompt" in cmd
    finally:
        os.unlink(tmp_path)


def test_claude_code_executor_timeout():
    """Test timeout handling in Claude Code executor."""
    executor = ClaudeCodeExecutor()

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=60)

        result = executor.execute(
            prompt="test",
            workspace=Path("/tmp"),
            timeout=60,
        )

        assert not result.success
        assert result.exit_code == 124
        assert result.timed_out


def test_claude_code_executor_failure():
    """Test non-zero exit code handling."""
    executor = ClaudeCodeExecutor()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1, stdout="error output", stderr=""
        )

        result = executor.execute(
            prompt="test",
            workspace=Path("/tmp"),
            timeout=60,
        )

        assert not result.success
        assert result.exit_code == 1


def test_claude_code_executor_uses_devnull_stdin():
    """Test that stdin is /dev/null to prevent SIGSTOP in tmux."""
    executor = ClaudeCodeExecutor()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        executor.execute(
            prompt="test",
            workspace=Path("/tmp"),
            timeout=60,
        )

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["stdin"] == subprocess.DEVNULL


# --- Registry tests ---


def test_list_backends():
    """Test listing available backends."""
    backends = list_backends()
    assert "gptme" in backends
    assert "claude-code" in backends
    assert "grok-build" in backends
    assert backends == sorted(backends)  # Should be sorted


def test_get_executor_gptme():
    """Test getting gptme executor by name."""
    with patch("shutil.which", return_value="/usr/bin/gptme"):
        executor = get_executor("gptme")
        assert isinstance(executor, GptmeExecutor)


def test_get_executor_claude_code():
    """Test getting claude-code executor by name."""
    with patch("shutil.which", return_value="/usr/bin/claude"):
        executor = get_executor("claude-code")
        assert isinstance(executor, ClaudeCodeExecutor)


def test_get_executor_grok_build():
    """Test getting grok-build executor by name."""
    with patch("gptme_runloops.utils.executor._resolve_grok_build_binary") as mock_bin:
        mock_bin.return_value = "/usr/bin/grok"
        executor = get_executor("grok-build")
        assert isinstance(executor, GrokBuildExecutor)


def test_get_executor_unknown():
    """Test that unknown backend raises ValueError."""
    with pytest.raises(ValueError, match="Unknown backend"):
        get_executor("nonexistent")


def test_get_executor_unavailable():
    """Test that unavailable backend raises RuntimeError."""
    with patch("shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="not available"):
            get_executor("gptme")


# --- Integration with BaseRunLoop ---


def test_base_run_loop_default_executor():
    """Test that BaseRunLoop defaults to GptmeExecutor."""
    from gptme_runloops.base import BaseRunLoop

    class TestLoop(BaseRunLoop):
        def generate_prompt(self):
            return "test"

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()
        loop = TestLoop(workspace, "test")
        assert isinstance(loop.executor, GptmeExecutor)


def test_base_run_loop_custom_executor():
    """Test that BaseRunLoop accepts a custom executor."""
    from gptme_runloops.base import BaseRunLoop

    class TestLoop(BaseRunLoop):
        def generate_prompt(self):
            return "test"

    mock_executor = MockExecutor()

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()
        loop = TestLoop(workspace, "test", executor=mock_executor)
        assert loop.executor is mock_executor
        assert loop.executor.name == "mock"


def test_base_run_loop_execute_uses_executor():
    """Test that BaseRunLoop.execute() delegates to executor."""
    from gptme_runloops.base import BaseRunLoop

    class TestLoop(BaseRunLoop):
        def generate_prompt(self):
            return "test"

    mock_executor = MockExecutor()

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()
        loop = TestLoop(workspace, "test", executor=mock_executor)

        result = loop.execute("test prompt")
        assert result.success


def test_full_cycle_with_custom_executor():
    """Test full run cycle with a custom executor."""
    from gptme_runloops.base import BaseRunLoop

    class TestLoop(BaseRunLoop):
        def generate_prompt(self):
            return "test prompt"

    mock_executor = MockExecutor()

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()
        loop = TestLoop(workspace, "test", executor=mock_executor)

        with (
            patch("gptme_runloops.base.git_pull_with_retry", return_value=True),
            patch.object(loop, "_record_session"),
        ):
            exit_code = loop.run()
            assert exit_code == 0


def test_subclass_executor_passthrough():
    """Test that subclasses pass executor through to BaseRunLoop."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        mock_executor = MockExecutor()

        # Test AutonomousRun
        from gptme_runloops.autonomous import AutonomousRun

        auto = AutonomousRun(workspace, executor=mock_executor)
        assert auto.executor is mock_executor

        # Test EmailRun
        from gptme_runloops.email import EmailRun

        email_run = EmailRun(workspace, executor=mock_executor)
        assert email_run.executor is mock_executor

        # Test TeamRun
        from gptme_runloops.team import TeamRun

        team = TeamRun(workspace, executor=mock_executor)
        assert team.executor is mock_executor

        # Test ProjectMonitoringRun
        from gptme_runloops.project_monitoring import ProjectMonitoringRun

        ProjectMonitoringRun(workspace, executor=mock_executor)


# --- ClaudeCodeExecutor unsupported parameter warnings ---


def test_claude_code_executor_warns_on_tools():
    """Test that ClaudeCodeExecutor logs a warning when 'tools' is passed."""
    executor = ClaudeCodeExecutor()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with patch("gptme_runloops.utils.executor.logger") as mock_logger:
            executor.execute(
                prompt="test",
                workspace=Path("/tmp"),
                timeout=60,
                tools="gptodo,save",
            )
            # Should warn that tools param is not supported
            mock_logger.warning.assert_called()
            warning_msg = mock_logger.warning.call_args_list[0][0][0]
            assert "tools" in warning_msg
            assert "not supported" in warning_msg


def test_claude_code_executor_warns_on_tool_format():
    """Test that ClaudeCodeExecutor logs a warning when 'tool_format' is passed."""
    executor = ClaudeCodeExecutor()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with patch("gptme_runloops.utils.executor.logger") as mock_logger:
            executor.execute(
                prompt="test",
                workspace=Path("/tmp"),
                timeout=60,
                tool_format="xml",
            )
            mock_logger.warning.assert_called()
            warning_msg = mock_logger.warning.call_args_list[0][0][0]
            assert "tool_format" in warning_msg
            assert "not supported" in warning_msg


def test_claude_code_executor_no_warn_without_tools():
    """Test that ClaudeCodeExecutor does NOT warn when tools/tool_format not passed."""
    executor = ClaudeCodeExecutor()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with patch("gptme_runloops.utils.executor.logger") as mock_logger:
            executor.execute(
                prompt="test",
                workspace=Path("/tmp"),
                timeout=60,
            )
            mock_logger.warning.assert_not_called()


# --- GrokBuildExecutor tests ---


def test_grok_build_executor_name():
    executor = GrokBuildExecutor()
    assert executor.name == "grok-build"


def test_grok_build_executor_binary_name():
    executor = GrokBuildExecutor()
    assert executor._binary_name == "grok"


def test_grok_build_executor_is_available_from_path():
    executor = GrokBuildExecutor()

    with patch("shutil.which", return_value="/usr/bin/grok"):
        assert executor.is_available()


def test_grok_build_executor_is_available_from_home_fallback(monkeypatch, tmp_path):
    executor = GrokBuildExecutor()
    grok_home = tmp_path / ".grok" / "bin"
    grok_home.mkdir(parents=True)
    grok_bin = grok_home / "grok"
    grok_bin.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    monkeypatch.setattr("shutil.which", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert executor.is_available()


def test_grok_build_executor_basic_execution():
    executor = GrokBuildExecutor()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with patch(
            "gptme_runloops.utils.executor._resolve_grok_build_binary"
        ) as mock_bin:
            mock_bin.return_value = "/usr/bin/grok"

            result = executor.execute(
                prompt="test prompt",
                workspace=Path("/tmp"),
                timeout=300,
            )

        assert result.success
        assert result.exit_code == 0

        cmd = mock_run.call_args[0][0]
        # stdbuf is optional (unavailable on stock macOS); check by basename
        if shutil.which("stdbuf"):
            assert os.path.basename(cmd[0]) == "stdbuf"
            assert cmd[1:4] == ["-oL", "-eL", "/usr/bin/grok"]
        else:
            assert cmd[0] == "/usr/bin/grok"
        assert "--single" in cmd
        assert "--output-format" in cmd
        assert "streaming-json" in cmd
        assert "--permission-mode" in cmd
        assert "bypassPermissions" in cmd


def test_grok_build_executor_system_prompt():
    executor = GrokBuildExecutor()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("You are a helpful assistant.")
        f.flush()
        tmp_path = f.name

    try:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            with patch(
                "gptme_runloops.utils.executor._resolve_grok_build_binary"
            ) as mock_bin:
                mock_bin.return_value = "/usr/bin/grok"
                executor.execute(
                    prompt="test",
                    workspace=Path("/tmp"),
                    timeout=60,
                    system_prompt_file=Path(tmp_path),
                )

            cmd = mock_run.call_args[0][0]
            assert "--system-prompt-override" in cmd
    finally:
        os.unlink(tmp_path)


def test_grok_build_executor_timeout():
    executor = GrokBuildExecutor()

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="grok", timeout=60)

        with patch(
            "gptme_runloops.utils.executor._resolve_grok_build_binary"
        ) as mock_bin:
            mock_bin.return_value = "/usr/bin/grok"
            result = executor.execute(
                prompt="test",
                workspace=Path("/tmp"),
                timeout=60,
            )

        assert not result.success
        assert result.exit_code == 124
        assert result.timed_out


def test_grok_build_executor_uses_devnull_stdin():
    executor = GrokBuildExecutor()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with patch(
            "gptme_runloops.utils.executor._resolve_grok_build_binary"
        ) as mock_bin:
            mock_bin.return_value = "/usr/bin/grok"
            executor.execute(
                prompt="test",
                workspace=Path("/tmp"),
                timeout=60,
            )

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["stdin"] == subprocess.DEVNULL
