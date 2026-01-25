"""Tests for AutonomousRun."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from gptme_runloops.autonomous import AutonomousRun
from gptme_runloops.utils.execution import ExecutionResult


def test_autonomous_generate_prompt():
    """Test prompt generation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        run = AutonomousRun(workspace)
        prompt = run.generate_prompt()

        # Should contain key sections
        assert "autonomous" in prompt.lower()
        assert "Step 1" in prompt
        assert "Step 2" in prompt
        assert "Step 3" in prompt


def test_autonomous_run_cycle():
    """Test full autonomous run cycle."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "logs").mkdir()

        run = AutonomousRun(workspace)

        # Mock external calls
        with (
            patch("run_loops.base.git_pull_with_retry") as mock_pull,
            patch("run_loops.base.execute_gptme") as mock_execute,
        ):
            mock_pull.return_value = True
            mock_execute.return_value = ExecutionResult(exit_code=0)

            exit_code = run.run()

            assert exit_code == 0
            mock_execute.assert_called_once()


def test_autonomous_timeout():
    """Test timeout configuration."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)

        run = AutonomousRun(workspace)

        # Should have 50-minute timeout
        assert run.timeout == 3000
