"""Tests for AutonomousRun and autonomous dispatch helpers."""

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from gptme_runloops.autonomous import (
    AutonomousRun,
    is_capable_backend,
    self_review_cooldown_active,
    self_review_hours_since_last,
)
from gptme_runloops.utils.execution import ExecutionResult

# --- is_capable_backend ---


def test_capable_backend_claude_code():
    assert is_capable_backend("claude-code") is True
    assert is_capable_backend("claude-code", "claude-sonnet-4-6") is True


def test_capable_backend_glm5():
    assert is_capable_backend("gptme", "glm-5.2") is True
    assert is_capable_backend("gptme", "glm-5-pro") is True


def test_incapable_backend_gptme_non_glm():
    assert is_capable_backend("gptme", "deepseek-v4-pro") is False
    assert is_capable_backend("gptme") is False


def test_incapable_backend_unknown():
    assert is_capable_backend("codex") is False
    assert is_capable_backend("") is False


# --- self_review_hours_since_last ---


def _write_self_review_state(path: Path, age_hours: float) -> None:
    ts = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    path.write_text(json.dumps({"timestamp": ts.isoformat()}))


def test_hours_since_last_recent(tmp_path):
    state = tmp_path / "self-review-last.json"
    _write_self_review_state(state, age_hours=2.0)
    hours = self_review_hours_since_last(state)
    assert 1.9 < hours < 2.1


def test_hours_since_last_missing_file(tmp_path):
    assert self_review_hours_since_last(tmp_path / "no-such.json") == 999.0


def test_hours_since_last_corrupt_file(tmp_path):
    state = tmp_path / "bad.json"
    state.write_text("not json at all")
    assert self_review_hours_since_last(state) == 999.0


def test_hours_since_last_missing_timestamp(tmp_path):
    state = tmp_path / "empty.json"
    state.write_text(json.dumps({}))
    assert self_review_hours_since_last(state) == 999.0


# --- self_review_cooldown_active ---


def test_cooldown_active_when_recent(tmp_path):
    state = tmp_path / "self-review-last.json"
    _write_self_review_state(state, age_hours=3.0)
    assert self_review_cooldown_active(state, cooldown_hours=6.0) is True


def test_cooldown_inactive_when_old(tmp_path):
    state = tmp_path / "self-review-last.json"
    _write_self_review_state(state, age_hours=8.0)
    assert self_review_cooldown_active(state, cooldown_hours=6.0) is False


def test_cooldown_inactive_when_missing(tmp_path):
    assert self_review_cooldown_active(tmp_path / "no-such.json") is False


def test_cooldown_boundary(tmp_path):
    state = tmp_path / "self-review-last.json"
    _write_self_review_state(state, age_hours=6.0)
    # Exactly at boundary → NOT active (>= 6h means cooldown cleared)
    assert self_review_cooldown_active(state, cooldown_hours=6.0) is False


# --- CLI exit codes ---


def test_cli_is_capable_backend_exit0():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gptme_runloops.autonomous",
            "is-capable-backend",
            "claude-code",
        ],
        capture_output=True,
    )
    assert result.returncode == 0


def test_cli_is_capable_backend_exit1():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gptme_runloops.autonomous",
            "is-capable-backend",
            "codex",
        ],
        capture_output=True,
    )
    assert result.returncode == 1


def test_cli_self_review_cooldown_active(tmp_path):
    state = tmp_path / "self-review-last.json"
    _write_self_review_state(state, age_hours=2.0)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gptme_runloops.autonomous",
            "self-review-cooldown",
            str(state),
        ],
        capture_output=True,
    )
    assert result.returncode == 0  # exit 0 = on cooldown


def test_cli_self_review_cooldown_inactive(tmp_path):
    state = tmp_path / "self-review-last.json"
    _write_self_review_state(state, age_hours=10.0)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gptme_runloops.autonomous",
            "self-review-cooldown",
            str(state),
        ],
        capture_output=True,
    )
    assert result.returncode == 1  # exit 1 = not on cooldown


def test_cli_self_review_hours(tmp_path):
    state = tmp_path / "self-review-last.json"
    _write_self_review_state(state, age_hours=5.0)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gptme_runloops.autonomous",
            "self-review-hours",
            str(state),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert 4.9 < float(result.stdout.strip()) < 5.1


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

        # Mock external calls (including _record_session to avoid live store writes)
        with (
            patch("gptme_runloops.base.git_pull_with_retry") as mock_pull,
            patch("gptme_runloops.utils.executor.execute_gptme") as mock_execute,
            patch.object(run, "_record_session"),
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
