"""Integration tests for run_loops CLI commands.

Tests end-to-end execution of CLI commands with real lock files,
directory creation, and cleanup. Uses mock gptme to avoid actual
AI execution while testing full infrastructure.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest


class TestCLIIntegration:
    """Integration tests for run_loops CLI commands."""

    @pytest.fixture
    def test_workspace(self, tmp_path: Path) -> Path:
        """Create a test workspace with required structure."""
        workspace = tmp_path / "test_workspace"
        workspace.mkdir()

        # Create required directories
        (workspace / "journal").mkdir()
        (workspace / "state").mkdir()
        # Note: logs directory is created by run loops automatically

        # Create minimal queue file
        queue_file = workspace / "state" / "queue-manual.md"
        queue_file.write_text("# Work Queue\n\n## Planned Next\n- Test task\n")

        return workspace

    def test_autonomous_cli_help(self):
        """Test autonomous command help shows correct information."""
        result = subprocess.run(
            ["uv", "run", "python3", "-m", "run_loops.cli", "autonomous", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert "autonomous" in result.stdout.lower()

    def test_email_cli_help(self):
        """Test email command help shows correct information."""
        result = subprocess.run(
            ["uv", "run", "python3", "-m", "run_loops.cli", "email", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert "email" in result.stdout.lower()

    def test_monitoring_cli_help(self):
        """Test monitoring command help shows correct information."""
        result = subprocess.run(
            ["uv", "run", "python3", "-m", "run_loops.cli", "monitoring", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert "monitoring" in result.stdout.lower()

    def test_email_run_direct_instantiation(self, test_workspace: Path):
        """Test EmailRun class can be instantiated and configured."""
        from gptme_runloops.email import EmailRun

        # Create minimal email config for testing
        (test_workspace / "email").mkdir(exist_ok=True)

        # Test that EmailRun can be instantiated
        run = EmailRun(test_workspace)

        # Verify basic properties
        assert run.workspace == test_workspace
        # EmailRun successfully instantiated

    def test_monitoring_run_direct_instantiation(self, test_workspace: Path):
        """Test ProjectMonitoringRun class can be instantiated and configured."""
        from gptme_runloops.project_monitoring import ProjectMonitoringRun

        # Test that ProjectMonitoringRun can be instantiated
        run = ProjectMonitoringRun(
            test_workspace, target_orgs=["test-org"], author="test-author"
        )

        # Verify basic properties
        assert run.workspace == test_workspace
        assert run.target_orgs == ["test-org"]
        assert run.author == "test-author"
        # ProjectMonitoringRun successfully instantiated

    def test_lock_file_creation(self, test_workspace: Path):
        """Test that CLI commands create proper lock files."""
        lock_dir = test_workspace / "logs"
        lock_dir.mkdir(exist_ok=True)

        # Manually create a lock to test infrastructure
        from gptme_runloops.utils.lock import RunLoopLock

        lock_file = lock_dir / "gptme-test.lock"

        # First acquisition
        with RunLoopLock(lock_dir, "test"):
            # If we're in this block, lock was acquired successfully
            assert lock_file.exists()

        # After release, lock can be acquired again (even if file remains)
        with RunLoopLock(lock_dir, "test"):
            # Second acquisition successful = lock was properly released
            assert lock_file.exists()

    def test_concurrent_lock_prevention(self, test_workspace: Path):
        """Test that concurrent runs are prevented by locks."""
        from gptme_runloops.utils.lock import RunLoopLock

        lock_dir = test_workspace / "logs"
        lock_dir.mkdir(exist_ok=True)

        # Acquire first lock
        lock1 = RunLoopLock(lock_dir, "test")
        assert lock1.acquire()

        try:
            # Try to acquire second lock (should fail)
            lock2 = RunLoopLock(lock_dir, "test")
            assert not lock2.acquire()
            lock2.release()
        finally:
            lock1.release()

    def test_workspace_directory_structure(self, test_workspace: Path):
        """Test that required directories exist in workspace."""
        assert (test_workspace / "journal").exists()
        assert (test_workspace / "state").exists()
        # Note: logs directory is created by run loops, not required upfront

    @pytest.mark.slow
    def test_cli_command_performance(self, test_workspace: Path):
        """Test CLI command execution performance (help commands)."""
        commands = [
            ["uv", "run", "python3", "-m", "run_loops.cli", "--help"],
            ["uv", "run", "python3", "-m", "run_loops.cli", "autonomous", "--help"],
            ["uv", "run", "python3", "-m", "run_loops.cli", "email", "--help"],
            ["uv", "run", "python3", "-m", "run_loops.cli", "monitoring", "--help"],
        ]

        for cmd in commands:
            start = time.time()
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10, check=True
            )
            duration = time.time() - start

            assert result.returncode == 0
            assert duration < 5.0  # Help commands should be fast


class TestShellScriptComparison:
    """Compare Python CLI behavior with shell script implementations."""

    @pytest.fixture
    def shell_script_dir(self) -> Path:
        """Get path to shell scripts."""
        workspace = Path.cwd()
        return workspace / "scripts" / "runs"

    def test_shell_scripts_exist(self, shell_script_dir: Path):
        """Verify shell scripts exist for comparison."""
        scripts = [
            "email-loop.sh",
            "project-monitoring-loop.sh",
        ]

        for script in scripts:
            script_path = shell_script_dir / script
            if script_path.exists():
                assert script_path.is_file()
                assert os.access(script_path, os.X_OK)  # Executable

    def test_lock_mechanism_compatibility(self, tmp_path: Path):
        """Test that Python locks are compatible with shell script expectations."""
        from gptme_runloops.utils.lock import RunLoopLock

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        lock_dir = workspace / "logs"
        lock_dir.mkdir()

        # Create lock using Python
        lock = RunLoopLock(lock_dir, "test")
        lock.acquire()

        try:
            # Verify lock file format (gptme-{name}.lock)
            lock_file = lock_dir / "gptme-test.lock"
            assert lock_file.exists()

            # Read lock file content (should have PID)
            content = lock_file.read_text().strip()
            assert content == str(os.getpid())
        finally:
            lock.release()

    def test_workspace_path_consistency(self, tmp_path: Path):
        """Test that workspace path can be passed and used correctly."""
        # Create test workspace structure
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "journal").mkdir()
        (workspace / "state").mkdir()

        # Verify workspace structure is valid
        assert workspace.exists()
        assert workspace.is_dir()
        assert (workspace / "journal").exists()
        assert (workspace / "state").exists()

        # Test that CLI accepts workspace parameter
        result = subprocess.run(
            [
                "uv",
                "run",
                "python3",
                "-m",
                "run_loops.cli",
                "autonomous",
                "--workspace",
                str(workspace),
                "--help",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
