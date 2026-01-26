"""Tests for BaseRunLoop."""

import tempfile
from pathlib import Path
from unittest.mock import patch


from gptme_runloops.base import BaseRunLoop
from gptme_runloops.utils.execution import ExecutionResult


class TestRunLoop(BaseRunLoop):
    """Concrete test implementation of BaseRunLoop."""

    def generate_prompt(self) -> str:
        return "Test prompt"


def test_base_setup():
    """Test basic setup process."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        run = TestRunLoop(workspace, "test")

        # Setup should succeed
        assert run.setup()
        assert run.lock.lock_fd is not None

        # Cleanup
        run.cleanup()
        assert run.lock.lock_fd is None


def test_base_setup_lock_failure():
    """Test setup when lock acquisition fails."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        # First run acquires lock
        run1 = TestRunLoop(workspace, "test")
        assert run1.setup()

        # Second run fails to acquire lock
        run2 = TestRunLoop(workspace, "test")
        assert not run2.setup()

        # Cleanup
        run1.cleanup()


def test_base_pre_run():
    """Test pre_run git pull."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        run = TestRunLoop(workspace, "test")

        # Mock git pull
        with patch("gptme_runloops.base.git_pull_with_retry") as mock_pull:
            mock_pull.return_value = True
            assert run.pre_run()
            mock_pull.assert_called_once()


def test_base_execute():
    """Test execute with mock gptme."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        run = TestRunLoop(workspace, "test")

        # Mock gptme execution
        with patch("gptme_runloops.base.execute_gptme") as mock_execute:
            mock_execute.return_value = ExecutionResult(exit_code=0)

            result = run.execute("Test prompt")
            assert result.success
            assert result.exit_code == 0


def test_base_run_full_cycle():
    """Test complete run cycle."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        run = TestRunLoop(workspace, "test")

        # Mock all external calls
        with (
            patch("gptme_runloops.base.git_pull_with_retry") as mock_pull,
            patch("gptme_runloops.base.execute_gptme") as mock_execute,
        ):
            mock_pull.return_value = True
            mock_execute.return_value = ExecutionResult(exit_code=0)

            # Run full cycle
            exit_code = run.run()

            assert exit_code == 0
            mock_pull.assert_called_once()
            mock_execute.assert_called_once()


def test_base_run_exception_handling():
    """Test exception handling in run cycle."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        run = TestRunLoop(workspace, "test")

        # Mock setup to raise exception
        with patch.object(run, "setup", side_effect=Exception("Test error")):
            exit_code = run.run()

            # Should return error code
            assert exit_code == 1
            # Lock should be released in cleanup
            assert run.lock.lock_fd is None
