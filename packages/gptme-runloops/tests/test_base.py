"""Tests for BaseRunLoop."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from gptme_runloops.base import BACKOFF_SCHEDULE, BaseRunLoop
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


# --- Backoff tests ---


def test_backoff_state_file_path():
    """Test that backoff state file uses run_type in path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        run = TestRunLoop(workspace, "mytype")
        assert run._backoff_state_file == workspace / "state" / "mytype-backoff.json"


def test_backoff_initial_state_no_skip():
    """Test that with no prior state, backoff does not skip."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        run = TestRunLoop(workspace, "test")
        # No state file exists — should not skip
        assert not run._check_backoff("hash1")


def test_backoff_resets_on_new_hash():
    """Test that a new work hash resets the backoff counter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        run = TestRunLoop(workspace, "test")

        # Simulate many failures with hash1
        run._save_backoff_state({"consecutive_failures": 10, "last_hash": "hash1"})

        # New hash — should reset and not skip
        assert not run._check_backoff("hash2")

        # Verify state was reset
        state = run._load_backoff_state()
        assert state["consecutive_failures"] == 0
        assert state["last_hash"] == "hash2"


def test_backoff_record_success_resets():
    """Test that recording success resets failure counter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        run = TestRunLoop(workspace, "test")
        run._save_backoff_state({"consecutive_failures": 5, "last_hash": "hash1"})

        run._record_backoff_success()

        state = run._load_backoff_state()
        assert state["consecutive_failures"] == 0
        assert state["last_hash"] == ""


def test_backoff_record_failure_increments():
    """Test that recording failure increments counter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        run = TestRunLoop(workspace, "test")
        run._save_backoff_state({"consecutive_failures": 2, "last_hash": "hash1"})

        run._record_backoff_failure("hash1")

        state = run._load_backoff_state()
        assert state["consecutive_failures"] == 3
        assert state["last_hash"] == "hash1"


@pytest.mark.parametrize("threshold,skip_n,out_of,_desc", BACKOFF_SCHEDULE)
def test_backoff_schedule_skips_at_threshold(threshold, skip_n, out_of, _desc):
    """Test that backoff skips runs at the specified threshold."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        run = TestRunLoop(workspace, "test")
        run._save_backoff_state(
            {"consecutive_failures": threshold, "last_hash": "hash1"}
        )

        # Run many trials — with high skip probability, at least some should skip
        results = [run._check_backoff("hash1") for _ in range(100)]
        skip_count = sum(results)

        expected_fraction = skip_n / out_of
        # Allow ±20% variance from expected fraction
        assert skip_count >= (expected_fraction - 0.20) * 100
        assert skip_count <= (expected_fraction + 0.20) * 100


def test_backoff_corrupted_state_file():
    """Test that corrupted state file is handled gracefully."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()
        (workspace / "state").mkdir()

        run = TestRunLoop(workspace, "test")
        # Write corrupted JSON
        run._backoff_state_file.write_text("not valid json{{")

        # Should not raise, should return empty state
        state = run._load_backoff_state()
        assert state == {}

        # And check_backoff should work fine (no skip with empty state)
        assert not run._check_backoff("hash1")
