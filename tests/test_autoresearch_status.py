"""Tests for autoresearch check-autoresearch-status.py."""

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

# Add scripts dir to path so we can import the module
SCRIPT_DIR = Path(__file__).parent.parent / "scripts" / "autoresearch"
sys.path.insert(0, str(SCRIPT_DIR))

spec = importlib.util.spec_from_file_location(
    "check_autoresearch_status",
    SCRIPT_DIR / "check-autoresearch-status.py",
)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)


@pytest.fixture(autouse=True)
def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up env vars and reload module for each test."""
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "artifact"))
    monkeypatch.setenv("AUTORESEARCH_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "artifact").mkdir()
    (tmp_path / "state").mkdir()
    # Re-execute module to pick up new env vars
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)


class TestBudgetSummary:
    """Tests for budget summary reporting."""

    def test_no_budget_dir(self, tmp_path: Path):
        """No budget directory returns 'no budget data'."""
        result = mod.get_budget_summary()
        assert "no budget data" in result or "no data" in result

    def test_empty_budget_dir(self, tmp_path: Path):
        """Budget dir exists but no files for today."""
        budget_dir = tmp_path / "state" / "budget"
        budget_dir.mkdir(parents=True)
        result = mod.get_budget_summary()
        assert "no budget data for today" in result or "no data" in result

    def test_daily_budget_consumption(self, tmp_path: Path):
        """Daily budget file shows iterations consumed."""
        budget_dir = tmp_path / "state" / "budget"
        budget_dir.mkdir(parents=True)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        budget_file = budget_dir / f"my-experiment-{today}.json"
        budget_file.write_text(json.dumps({"iterations": 7}))

        result = mod.get_budget_summary()
        assert "my-experiment" in result
        assert "7" in result

    def test_global_budget_display(self, tmp_path: Path):
        """Global budget file shows total/limit."""
        budget_dir = tmp_path / "state" / "budget"
        budget_dir.mkdir(parents=True)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        # Need at least one experiment file for global to show
        (budget_dir / f"exp1-{today}.json").write_text(json.dumps({"iterations": 3}))
        global_file = budget_dir / f"global-{today}.json"
        global_file.write_text(json.dumps({"iterations": 10, "limit": 30}))

        result = mod.get_budget_summary()
        assert "GLOBAL" in result
        assert "10/30" in result

    def test_alltime_budget_complete(self, tmp_path: Path):
        """All-time budget shows completion status."""
        budget_dir = tmp_path / "state" / "budget"
        budget_dir.mkdir(parents=True)
        alltime_file = budget_dir / "finished-exp-all-time.json"
        alltime_file.write_text(json.dumps({"total_iterations": 50, "limit": 50}))

        result = mod.get_budget_summary()
        assert "finished-exp" in result
        assert "50/50" in result
        assert "COMPLETE" in result

    def test_alltime_budget_in_progress(self, tmp_path: Path):
        """All-time budget shows percentage when in progress."""
        budget_dir = tmp_path / "state" / "budget"
        budget_dir.mkdir(parents=True)
        alltime_file = budget_dir / "running-exp-all-time.json"
        alltime_file.write_text(json.dumps({"total_iterations": 25, "limit": 100}))

        result = mod.get_budget_summary()
        assert "running-exp" in result
        assert "25/100" in result
        assert "25%" in result


class TestReviewTimestamp:
    """Tests for operator review timestamp tracking."""

    def test_no_review_file(self, tmp_path: Path):
        """No review file returns epoch."""
        result = mod.get_last_review_time()
        assert result == "1970-01-01T00:00:00+00:00"

    def test_update_and_read_review(self, tmp_path: Path):
        """Update then read review timestamp."""
        mod.update_review_timestamp()
        result = mod.get_last_review_time()
        assert result.startswith("2026-") or result.startswith("202")
        assert "T" in result


class TestServiceStatus:
    """Tests for systemd service status detection."""

    def test_no_services(self):
        """Returns appropriate message when no services running."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type(
                "Result", (), {"stdout": "", "returncode": 0}
            )()
            result = mod.get_service_status()
            assert "no autoresearch services running" in result

    def test_services_running(self):
        """Parses running service names from systemctl output."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type(
                "Result",
                (),
                {
                    "stdout": "bob-autoresearch@gptme.service loaded active running\n",
                    "returncode": 0,
                },
            )()
            result = mod.get_service_status()
            assert "1 service(s) running" in result


class TestBranchDetection:
    """Tests for autoresearch branch detection."""

    def test_no_artifact_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Non-existent artifact dir returns empty list."""
        monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "nonexistent"))
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(mod)
        result = mod.get_autoresearch_branches()
        assert result == []

    def test_no_branches(self, tmp_path: Path):
        """Artifact dir with no autoresearch branches."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type(
                "Result", (), {"stdout": "", "returncode": 0}
            )()
            result = mod.get_autoresearch_branches()
            assert result == []

    def test_parses_branches(self, tmp_path: Path):
        """Correctly parses branch names from git output."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type(
                "Result",
                (),
                {
                    "stdout": "  origin/autoresearch/eval-improvement-20260325\n  origin/autoresearch/eval-improvement-20260324\n",
                    "returncode": 0,
                },
            )()
            result = mod.get_autoresearch_branches()
            assert len(result) == 2
            assert "autoresearch/eval-improvement-20260325" in result
            assert not any(b.startswith("origin/") for b in result)


class TestScoreDelta:
    """Tests for score extraction from logs."""

    def test_no_logs(self, tmp_path: Path):
        """No log files returns appropriate message."""
        result = mod.get_score_delta_from_log()
        assert "no logs found" in result

    def test_extracts_score_lines(self, tmp_path: Path):
        """Extracts score-related lines from log file."""
        state_dir = tmp_path / "state"
        log_file = state_dir / "session_20260101_120000_iter_1.log"
        log_file.write_text(
            "Starting iteration 1\n"
            "Baseline: 0.667\n"
            "Running eval...\n"
            "Score: 0.750\n"
            "✅ ACCEPTED (delta: +0.083)\n"
            "Iteration complete\n"
        )
        result = mod.get_score_delta_from_log()
        assert "Baseline: 0.667" in result
        assert "Score: 0.750" in result
        assert "ACCEPTED" in result
