"""Tests for the executor abstraction."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from gptme_runloops.utils.execution import ExecutionResult
from gptme_runloops.utils.executor import (
    ClaudeCodeExecutor,
    Executor,
    GptmeExecutor,
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

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            executor.execute(
                prompt="test",
                workspace=Path("/tmp"),
                timeout=60,
                system_prompt_file=Path(f.name),
            )

            cmd = mock_run.call_args[0][0]
            assert "--append-system-prompt" in cmd


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

        with patch("gptme_runloops.base.git_pull_with_retry", return_value=True):
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
