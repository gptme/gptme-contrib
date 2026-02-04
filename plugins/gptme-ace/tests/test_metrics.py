"""Tests for ACE Metrics module."""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from gptme_ace.metrics import (
    CurationRun,
    InsightQuality,
    LessonImpact,
    MetricsCalculator,
    MetricsDB,
    get_default_metrics_db,
)


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_metrics.db"
        yield MetricsDB(db_path)


class TestMetricsDB:
    """Tests for MetricsDB class."""

    def test_init_creates_tables(self, temp_db):
        """Test that database initialization creates required tables."""
        import sqlite3

        with sqlite3.connect(temp_db.db_path) as conn:
            # Check tables exist
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [t[0] for t in tables]

            assert "curation_runs" in table_names
            assert "insight_quality" in table_names
            assert "lesson_impact" in table_names

    def test_record_curation_run(self, temp_db):
        """Test recording a curation run."""
        run = CurationRun(
            run_id="test-run-001",
            timestamp=datetime.now(),
            trigger="manual",
            insights_count=10,
            conversions=5,
            duration_seconds=30.5,
            tokens_used=1500,
            success=True,
        )

        temp_db.record_curation_run(run)
        runs = temp_db.get_curation_runs(limit=10)

        assert len(runs) == 1
        assert runs[0].run_id == "test-run-001"
        assert runs[0].insights_count == 10
        assert runs[0].conversions == 5
        assert runs[0].success

    def test_record_insight_quality(self, temp_db):
        """Test recording insight quality metrics."""
        insight = InsightQuality(
            insight_id="insight-001",
            timestamp=datetime.now(),
            quality_score=0.85,
            actionable=True,
            novel=True,
            category="workflow",
            source_session="session-abc",
        )

        temp_db.record_insight_quality(insight)
        insights = temp_db.get_insights(limit=10)

        assert len(insights) == 1
        assert insights[0].insight_id == "insight-001"
        assert insights[0].quality_score == 0.85
        assert insights[0].actionable
        assert insights[0].category == "workflow"

    def test_record_lesson_impact(self, temp_db):
        """Test recording lesson impact metrics."""
        lesson = LessonImpact(
            lesson_id="lesson-001",
            created_timestamp=datetime.now(),
            source_insight_ids=["insight-001", "insight-002"],
            total_uses=15,
            helpful_count=12,
            harmful_count=1,
            last_used=datetime.now(),
        )

        temp_db.record_lesson_impact(lesson)
        lessons = temp_db.get_lessons(limit=10)

        assert len(lessons) == 1
        assert lessons[0].lesson_id == "lesson-001"
        assert lessons[0].total_uses == 15
        assert lessons[0].helpful_count == 12
        assert lessons[0].source_insight_ids == ["insight-001", "insight-002"]

    def test_get_records_with_since_filter(self, temp_db):
        """Test filtering records by timestamp."""
        now = datetime.now()
        yesterday = now - timedelta(days=1)
        week_ago = now - timedelta(days=7)

        # Add old and new runs
        old_run = CurationRun(
            run_id="old-run",
            timestamp=week_ago,
            trigger="scheduled",
            insights_count=5,
            conversions=2,
            duration_seconds=20.0,
            tokens_used=1000,
            success=True,
        )
        new_run = CurationRun(
            run_id="new-run",
            timestamp=now,
            trigger="manual",
            insights_count=10,
            conversions=5,
            duration_seconds=30.0,
            tokens_used=1500,
            success=True,
        )

        temp_db.record_curation_run(old_run)
        temp_db.record_curation_run(new_run)

        # Get only recent runs
        recent = temp_db.get_curation_runs(since=yesterday)
        assert len(recent) == 1
        assert recent[0].run_id == "new-run"

        # Get all runs
        all_runs = temp_db.get_curation_runs()
        assert len(all_runs) == 2


class TestMetricsCalculator:
    """Tests for MetricsCalculator class."""

    def test_curation_summary_empty(self, temp_db):
        """Test curation summary with no data."""
        calc = MetricsCalculator(temp_db)
        summary = calc.get_curation_summary(timedelta(days=7))

        assert summary["runs"] == 0
        assert summary["success_rate"] == 0.0
        assert summary["conversion_rate"] == 0.0

    def test_curation_summary_with_data(self, temp_db):
        """Test curation summary calculation."""
        now = datetime.now()

        # Add some runs
        for i in range(5):
            run = CurationRun(
                run_id=f"run-{i}",
                timestamp=now - timedelta(hours=i),
                trigger="scheduled",
                insights_count=10,
                conversions=5 if i < 4 else 0,  # 4/5 have conversions
                duration_seconds=30.0,
                tokens_used=1500,
                success=i < 4,  # 4/5 successful
            )
            temp_db.record_curation_run(run)

        calc = MetricsCalculator(temp_db)
        summary = calc.get_curation_summary(timedelta(days=7))

        assert summary["runs"] == 5
        assert summary["success_rate"] == 0.8  # 4/5
        assert summary["total_insights"] == 50
        assert summary["total_conversions"] == 20

    def test_insight_quality_summary(self, temp_db):
        """Test insight quality summary calculation."""
        now = datetime.now()

        # Add insights with varying quality
        categories = ["workflow", "tool", "workflow", "pattern"]
        for i, cat in enumerate(categories):
            insight = InsightQuality(
                insight_id=f"insight-{i}",
                timestamp=now - timedelta(hours=i),
                quality_score=0.6 + (i * 0.1),  # 0.6, 0.7, 0.8, 0.9
                actionable=i < 3,  # 3/4 actionable
                novel=i % 2 == 0,  # 2/4 novel
                category=cat,
                source_session=f"session-{i}",
            )
            temp_db.record_insight_quality(insight)

        calc = MetricsCalculator(temp_db)
        summary = calc.get_insight_quality_summary(timedelta(days=7))

        assert summary["total"] == 4
        assert summary["avg_quality"] == pytest.approx(0.75)  # (0.6+0.7+0.8+0.9)/4
        assert summary["actionable_rate"] == pytest.approx(0.75)  # 3/4
        assert summary["novel_rate"] == pytest.approx(0.5)  # 2/4
        assert summary["by_category"]["workflow"] == 2

    def test_lesson_impact_summary(self, temp_db):
        """Test lesson impact summary calculation."""
        now = datetime.now()

        # Add lessons with varying impact
        for i in range(3):
            lesson = LessonImpact(
                lesson_id=f"lesson-{i}",
                created_timestamp=now - timedelta(hours=i),
                source_insight_ids=[f"insight-{i}"],
                total_uses=10 + i * 5,  # 10, 15, 20
                helpful_count=8 + i,  # 8, 9, 10
                harmful_count=1,
                last_used=now,
            )
            temp_db.record_lesson_impact(lesson)

        calc = MetricsCalculator(temp_db)
        summary = calc.get_lesson_impact_summary(timedelta(days=7))

        assert summary["created"] == 3
        assert summary["avg_uses"] == 15.0  # (10+15+20)/3

    def test_system_health_healthy(self, temp_db):
        """Test system health with good metrics."""
        now = datetime.now()

        # Add healthy runs (high success rate, good conversions)
        for i in range(5):
            run = CurationRun(
                run_id=f"run-{i}",
                timestamp=now - timedelta(hours=i),
                trigger="scheduled",
                insights_count=10,
                conversions=6,  # 60% conversion
                duration_seconds=30.0,
                tokens_used=1500,
                success=True,
            )
            temp_db.record_curation_run(run)

        # Add high-quality insights
        for i in range(5):
            insight = InsightQuality(
                insight_id=f"insight-{i}",
                timestamp=now - timedelta(hours=i),
                quality_score=0.8,
                actionable=True,
                novel=True,
                category="workflow",
                source_session=f"session-{i}",
            )
            temp_db.record_insight_quality(insight)

        # Add helpful lessons
        for i in range(3):
            lesson = LessonImpact(
                lesson_id=f"lesson-{i}",
                created_timestamp=now - timedelta(hours=i),
                source_insight_ids=[f"insight-{i}"],
                total_uses=10,
                helpful_count=9,
                harmful_count=1,
                last_used=now,
            )
            temp_db.record_lesson_impact(lesson)

        calc = MetricsCalculator(temp_db)
        health = calc.get_system_health()

        assert health["status"] == "healthy"
        assert len(health["alerts"]) == 0


class TestGetDefaultMetricsDB:
    """Tests for get_default_metrics_db function."""

    def test_default_workspace(self):
        """Test default workspace detection."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            db = get_default_metrics_db(workspace)

            assert db.db_path == workspace / "logs" / "ace_curation_metrics.db"
            assert db.db_path.parent.exists()

    def test_none_workspace_uses_cwd(self, monkeypatch):
        """Test that None workspace uses current directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.chdir(tmpdir)
            db = get_default_metrics_db()

            assert db.db_path == Path(tmpdir) / "logs" / "ace_curation_metrics.db"
