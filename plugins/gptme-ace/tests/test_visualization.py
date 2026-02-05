"""Tests for ACE Visualization CLI"""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gptme_ace.metrics import CurationRun, InsightQuality, LessonImpact
from gptme_ace.visualization import (
    _format_datetime,
    _load_deltas,
    cli,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def cli_runner():
    """Click CLI test runner"""
    return CliRunner()


@pytest.fixture
def temp_dir():
    """Temporary directory for tests"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_delta_file(temp_dir):
    """Create a sample delta JSON file"""
    delta_dir = temp_dir / "deltas"
    delta_dir.mkdir()

    delta_data = {
        "delta_id": "delta_123abc456def",
        "created": "2026-02-05T08:00:00Z",
        "source": "ace_curator",
        "source_insights": ["insight_001", "insight_002"],
        "lesson_id": "lessons/workflow/test-lesson.md",
        "operations": [
            {
                "type": "add",
                "section": "## Pattern",
                "content": "New pattern content here",
                "position": "append",
            },
            {
                "type": "modify",
                "section": "## Rule",
                "content": "Updated rule text",
                "target": {"hash": "abc123"},
            },
        ],
        "rationale": "Adding new pattern based on recent insights",
        "review_status": "pending",
    }

    delta_file = delta_dir / "delta_123abc456def.json"
    with open(delta_file, "w") as f:
        json.dump(delta_data, f)

    return delta_dir


@pytest.fixture
def multiple_deltas(temp_dir):
    """Create multiple delta files with different statuses"""
    delta_dir = temp_dir / "deltas"
    delta_dir.mkdir()

    deltas_data = [
        {
            "delta_id": "delta_pending_001",
            "created": "2026-02-05T10:00:00Z",
            "source": "ace_curator",
            "source_insights": ["insight_001"],
            "lesson_id": "lessons/workflow/lesson-a.md",
            "operations": [{"type": "add", "section": "## Test", "content": "Test"}],
            "rationale": "Test rationale",
            "review_status": "pending",
        },
        {
            "delta_id": "delta_approved_002",
            "created": "2026-02-05T09:00:00Z",
            "source": "ace_curator",
            "source_insights": ["insight_002"],
            "lesson_id": "lessons/workflow/lesson-b.md",
            "operations": [{"type": "modify", "section": "## Rule", "content": "Mod"}],
            "rationale": "Approved change",
            "review_status": "approved",
            "applied_at": "2026-02-05T11:00:00Z",
            "applied_by": "auto",
        },
        {
            "delta_id": "delta_rejected_003",
            "created": "2026-02-05T08:00:00Z",
            "source": "ace_curator",
            "source_insights": ["insight_003"],
            "lesson_id": "lessons/workflow/lesson-c.md",
            "operations": [{"type": "remove", "section": "## Old"}],
            "rationale": "Rejected change",
            "review_status": "rejected",
        },
    ]

    for delta_data in deltas_data:
        delta_file = delta_dir / f"{delta_data['delta_id']}.json"
        with open(delta_file, "w") as f:
            json.dump(delta_data, f)

    return delta_dir


@pytest.fixture
def mock_metrics_db():
    """Mock MetricsDB with sample data"""
    db = MagicMock()

    # Sample curation runs
    runs = [
        CurationRun(
            run_id="run_001",
            timestamp=datetime.now() - timedelta(days=1),
            trigger="autonomous_hook",
            insights_count=10,
            conversions=3,
            duration_seconds=45.5,
            tokens_used=15000,
            success=True,
        ),
        CurationRun(
            run_id="run_002",
            timestamp=datetime.now() - timedelta(days=2),
            trigger="manual",
            insights_count=5,
            conversions=2,
            duration_seconds=30.0,
            tokens_used=8000,
            success=True,
        ),
        CurationRun(
            run_id="run_003",
            timestamp=datetime.now() - timedelta(days=3),
            trigger="scheduled",
            insights_count=8,
            conversions=0,
            duration_seconds=20.0,
            tokens_used=5000,
            success=False,
            error_message="API rate limit exceeded",
        ),
    ]

    # Sample insight quality records
    insights = [
        InsightQuality(
            insight_id="insight_001",
            timestamp=datetime.now() - timedelta(days=1),
            quality_score=0.85,
            actionable=True,
            novel=True,
            category="workflow",
            source_session="session_001",
        ),
        InsightQuality(
            insight_id="insight_002",
            timestamp=datetime.now() - timedelta(days=2),
            quality_score=0.72,
            actionable=True,
            novel=False,
            category="tools",
            source_session="session_002",
        ),
        InsightQuality(
            insight_id="insight_003",
            timestamp=datetime.now() - timedelta(days=3),
            quality_score=0.45,
            actionable=False,
            novel=False,
            category="workflow",
            source_session="session_003",
        ),
    ]

    # Sample lesson impact
    lesson_impacts = [
        LessonImpact(
            lesson_id="lessons/workflow/good-lesson.md",
            created_timestamp=datetime.now() - timedelta(days=30),
            source_insight_ids=["insight_001"],
            total_uses=50,
            helpful_count=45,
            harmful_count=2,
        ),
        LessonImpact(
            lesson_id="lessons/tools/ok-lesson.md",
            created_timestamp=datetime.now() - timedelta(days=20),
            source_insight_ids=["insight_002"],
            total_uses=20,
            helpful_count=12,
            harmful_count=3,
        ),
    ]

    db.get_curation_runs.return_value = runs
    db.get_insights.return_value = insights
    db.get_lessons.return_value = lesson_impacts

    return db


# ============================================================================
# Unit Tests
# ============================================================================


class TestFormatDatetime:
    """Tests for _format_datetime helper"""

    def test_none_returns_dash(self):
        assert _format_datetime(None) == "-"

    def test_datetime_object(self):
        dt = datetime(2026, 2, 5, 10, 30, 45)
        result = _format_datetime(dt)
        assert result == "2026-02-05 10:30"

    def test_string_truncated(self):
        result = _format_datetime("2026-02-05T10:30:45.123456Z")
        assert result == "2026-02-05T10:30:45"


class TestLoadDeltas:
    """Tests for _load_deltas helper"""

    def test_empty_dir(self, temp_dir):
        delta_dir = temp_dir / "deltas"
        delta_dir.mkdir()
        deltas = _load_deltas(delta_dir)
        assert deltas == []

    def test_nonexistent_dir(self, temp_dir):
        delta_dir = temp_dir / "nonexistent"
        deltas = _load_deltas(delta_dir)
        assert deltas == []

    def test_loads_single_delta(self, sample_delta_file):
        deltas = _load_deltas(sample_delta_file)
        assert len(deltas) == 1
        assert deltas[0].delta_id == "delta_123abc456def"
        assert deltas[0].review_status == "pending"
        assert len(deltas[0].operations) == 2

    def test_loads_multiple_deltas_sorted(self, multiple_deltas):
        deltas = _load_deltas(multiple_deltas)
        assert len(deltas) == 3
        # Should be sorted by created date, most recent first
        assert deltas[0].delta_id == "delta_pending_001"
        assert deltas[1].delta_id == "delta_approved_002"
        assert deltas[2].delta_id == "delta_rejected_003"

    def test_handles_invalid_json(self, temp_dir):
        """Should skip invalid JSON files gracefully"""
        delta_dir = temp_dir / "deltas"
        delta_dir.mkdir()

        # Create invalid JSON
        (delta_dir / "invalid.json").write_text("not valid json")

        # Create valid delta
        valid_delta = {
            "delta_id": "valid_delta",
            "created": "2026-02-05T00:00:00Z",
            "source": "test",
            "lesson_id": "test.md",
            "operations": [],
            "rationale": "test",
            "review_status": "pending",
        }
        with open(delta_dir / "valid.json", "w") as f:
            json.dump(valid_delta, f)

        deltas = _load_deltas(delta_dir)
        assert len(deltas) == 1
        assert deltas[0].delta_id == "valid_delta"


# ============================================================================
# CLI Command Tests
# ============================================================================


class TestDeltasListCommand:
    """Tests for 'deltas list' command"""

    def test_list_all_deltas(self, cli_runner, multiple_deltas):
        result = cli_runner.invoke(
            cli, ["--data-dir", str(multiple_deltas.parent), "deltas", "list"]
        )
        assert result.exit_code == 0
        assert "delta_pendin" in result.output
        assert "delta_approv" in result.output
        assert "delta_reject" in result.output

    def test_list_pending_only(self, cli_runner, multiple_deltas):
        result = cli_runner.invoke(
            cli,
            [
                "--data-dir",
                str(multiple_deltas.parent),
                "deltas",
                "list",
                "--status",
                "pending",
            ],
        )
        assert result.exit_code == 0
        assert "delta_pendin" in result.output
        assert "delta_approv" not in result.output

    def test_list_json_output(self, cli_runner, multiple_deltas):
        result = cli_runner.invoke(
            cli, ["--data-dir", str(multiple_deltas.parent), "deltas", "list", "-j"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 3
        assert all("delta_id" in d for d in data)

    def test_list_with_limit(self, cli_runner, multiple_deltas):
        result = cli_runner.invoke(
            cli,
            ["--data-dir", str(multiple_deltas.parent), "deltas", "list", "-n", "1"],
        )
        assert result.exit_code == 0
        # Should only show 1 delta
        assert result.output.count("delta_") >= 1


class TestDeltasShowCommand:
    """Tests for 'deltas show' command"""

    def test_show_by_full_id(self, cli_runner, sample_delta_file):
        result = cli_runner.invoke(
            cli,
            [
                "--data-dir",
                str(sample_delta_file.parent),
                "deltas",
                "show",
                "delta_123abc456def",
            ],
        )
        assert result.exit_code == 0
        assert "delta_123abc456def" in result.output
        assert "lessons/workflow/test-lesson.md" in result.output
        assert "Adding new pattern" in result.output

    def test_show_by_partial_id(self, cli_runner, sample_delta_file):
        result = cli_runner.invoke(
            cli,
            [
                "--data-dir",
                str(sample_delta_file.parent),
                "deltas",
                "show",
                "delta_123",
            ],
        )
        assert result.exit_code == 0
        assert "delta_123abc456def" in result.output

    def test_show_nonexistent(self, cli_runner, sample_delta_file):
        result = cli_runner.invoke(
            cli,
            [
                "--data-dir",
                str(sample_delta_file.parent),
                "deltas",
                "show",
                "nonexistent",
            ],
        )
        assert result.exit_code == 1
        assert "No delta found" in result.output

    def test_show_json_output(self, cli_runner, sample_delta_file):
        result = cli_runner.invoke(
            cli,
            [
                "--data-dir",
                str(sample_delta_file.parent),
                "deltas",
                "show",
                "delta_123",
                "-j",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["delta_id"] == "delta_123abc456def"
        assert len(data["operations"]) == 2


class TestDeltasSummaryCommand:
    """Tests for 'deltas summary' command"""

    def test_summary_basic(self, cli_runner, multiple_deltas):
        result = cli_runner.invoke(
            cli, ["--data-dir", str(multiple_deltas.parent), "deltas", "summary"]
        )
        assert result.exit_code == 0
        assert "Total Deltas: 3" in result.output
        assert "Pending:" in result.output
        assert "Approved:" in result.output
        assert "Rejected:" in result.output

    def test_summary_json_output(self, cli_runner, multiple_deltas):
        result = cli_runner.invoke(
            cli, ["--data-dir", str(multiple_deltas.parent), "deltas", "summary", "-j"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total"] == 3
        assert data["by_status"]["pending"] == 1
        assert data["by_status"]["approved"] == 1
        assert data["by_status"]["rejected"] == 1


class TestMetricsRunsCommand:
    """Tests for 'metrics runs' command"""

    def test_runs_with_mock_db(self, cli_runner, temp_dir, mock_metrics_db):
        with patch(
            "gptme_ace.visualization.get_default_metrics_db",
            return_value=mock_metrics_db,
        ):
            result = cli_runner.invoke(
                cli, ["--data-dir", str(temp_dir), "metrics", "runs"]
            )
            assert result.exit_code == 0
            assert "Curation Runs" in result.output
            assert "Total Runs:" in result.output

    def test_runs_json_output(self, cli_runner, temp_dir, mock_metrics_db):
        with patch(
            "gptme_ace.visualization.get_default_metrics_db",
            return_value=mock_metrics_db,
        ):
            result = cli_runner.invoke(
                cli, ["--data-dir", str(temp_dir), "metrics", "runs", "-j"]
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert len(data) == 3
            assert all("run_id" in r for r in data)


class TestMetricsQualityCommand:
    """Tests for 'metrics quality' command"""

    def test_quality_basic(self, cli_runner, temp_dir, mock_metrics_db):
        with patch(
            "gptme_ace.visualization.get_default_metrics_db",
            return_value=mock_metrics_db,
        ):
            result = cli_runner.invoke(
                cli, ["--data-dir", str(temp_dir), "metrics", "quality"]
            )
            assert result.exit_code == 0
            assert "Insight Quality" in result.output
            assert "Average Quality:" in result.output

    def test_quality_json_output(self, cli_runner, temp_dir, mock_metrics_db):
        with patch(
            "gptme_ace.visualization.get_default_metrics_db",
            return_value=mock_metrics_db,
        ):
            result = cli_runner.invoke(
                cli, ["--data-dir", str(temp_dir), "metrics", "quality", "-j"]
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "total_insights" in data
            assert "avg_quality" in data


class TestMetricsImpactCommand:
    """Tests for 'metrics impact' command"""

    def test_impact_basic(self, cli_runner, temp_dir, mock_metrics_db):
        with patch(
            "gptme_ace.visualization.get_default_metrics_db",
            return_value=mock_metrics_db,
        ):
            result = cli_runner.invoke(
                cli, ["--data-dir", str(temp_dir), "metrics", "impact"]
            )
            assert result.exit_code == 0
            assert "Lesson Impact" in result.output

    def test_impact_json_output(self, cli_runner, temp_dir, mock_metrics_db):
        with patch(
            "gptme_ace.visualization.get_default_metrics_db",
            return_value=mock_metrics_db,
        ):
            result = cli_runner.invoke(
                cli, ["--data-dir", str(temp_dir), "metrics", "impact", "-j"]
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert len(data) == 2
            assert all("lesson_id" in item for item in data)


class TestMetricsTrendsCommand:
    """Tests for 'metrics trends' command"""

    def test_trends_basic(self, cli_runner, temp_dir, mock_metrics_db):
        with patch(
            "gptme_ace.visualization.get_default_metrics_db",
            return_value=mock_metrics_db,
        ):
            result = cli_runner.invoke(
                cli, ["--data-dir", str(temp_dir), "metrics", "trends"]
            )
            assert result.exit_code == 0
            assert "Trends" in result.output


class TestDashboardCommand:
    """Tests for 'dashboard' command"""

    def test_dashboard_basic(self, cli_runner, multiple_deltas, mock_metrics_db):
        with patch(
            "gptme_ace.visualization.get_default_metrics_db",
            return_value=mock_metrics_db,
        ):
            result = cli_runner.invoke(
                cli, ["--data-dir", str(multiple_deltas.parent), "dashboard"]
            )
            assert result.exit_code == 0
            assert "ACE Dashboard" in result.output
            assert "DELTAS" in result.output
            assert "CURATION RUNS" in result.output
            assert "INSIGHTS" in result.output

    def test_dashboard_json_output(self, cli_runner, multiple_deltas, mock_metrics_db):
        with patch(
            "gptme_ace.visualization.get_default_metrics_db",
            return_value=mock_metrics_db,
        ):
            result = cli_runner.invoke(
                cli, ["--data-dir", str(multiple_deltas.parent), "dashboard", "-j"]
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "deltas" in data
            assert "runs_7d" in data
            assert "insights_7d" in data


# ============================================================================
# Integration Tests
# ============================================================================


class TestCLIIntegration:
    """Integration tests for CLI"""

    def test_help_available(self, cli_runner):
        result = cli_runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "ACE Visualization CLI" in result.output

    def test_deltas_help(self, cli_runner):
        result = cli_runner.invoke(cli, ["deltas", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "show" in result.output
        assert "summary" in result.output

    def test_metrics_help(self, cli_runner):
        result = cli_runner.invoke(cli, ["metrics", "--help"])
        assert result.exit_code == 0
        assert "runs" in result.output
        assert "quality" in result.output
        assert "impact" in result.output
        assert "trends" in result.output

    def test_empty_data_dir(self, cli_runner, temp_dir):
        """CLI should handle empty data directories gracefully"""
        result = cli_runner.invoke(cli, ["--data-dir", str(temp_dir), "deltas", "list"])
        assert result.exit_code == 0
        assert "No deltas found" in result.output
