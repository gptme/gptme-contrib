"""Tests for monitoring module.

Tests SourceMetrics, MetricsCollector (with persistence), and HealthChecker.
"""

import pytest
from gptme_contrib_lib.monitoring import (
    HealthChecker,
    MetricsCollector,
    SourceMetrics,
)

# ── SourceMetrics ───────────────────────────────────────────────


class TestSourceMetrics:
    """Tests for per-source metrics tracking."""

    def test_initial_state(self):
        m = SourceMetrics(source_name="github", source_type="github")
        assert m.poll_attempts == 0
        assert m.successful_polls == 0
        assert m.failed_polls == 0
        assert m.tasks_created == 0
        assert m.consecutive_failures == 0

    def test_record_poll_attempt(self):
        m = SourceMetrics(source_name="test", source_type="test")
        m.record_poll_attempt(1.5)
        assert m.poll_attempts == 1
        assert m.last_poll_time is not None
        assert m.avg_poll_duration_seconds == 1.5
        assert m.total_poll_duration_seconds == 1.5

    def test_avg_poll_duration(self):
        m = SourceMetrics(source_name="test", source_type="test")
        m.record_poll_attempt(1.0)
        m.record_poll_attempt(3.0)
        assert m.poll_attempts == 2
        assert m.avg_poll_duration_seconds == 2.0

    def test_record_poll_success(self):
        m = SourceMetrics(source_name="test", source_type="test")
        m.record_poll_failure("error1")
        m.record_poll_failure("error2")
        assert m.consecutive_failures == 2
        m.record_poll_success()
        assert m.successful_polls == 1
        assert m.consecutive_failures == 0  # reset on success
        assert m.last_success_time is not None

    def test_record_poll_failure(self):
        m = SourceMetrics(source_name="test", source_type="test")
        m.record_poll_failure("Connection timeout")
        assert m.failed_polls == 1
        assert m.consecutive_failures == 1
        assert m.last_error == "Connection timeout"
        assert m.last_error_time is not None

    def test_consecutive_failures_increment(self):
        m = SourceMetrics(source_name="test", source_type="test")
        for i in range(5):
            m.record_poll_failure(f"error_{i}")
        assert m.consecutive_failures == 5
        assert m.failed_polls == 5

    def test_record_task_created(self):
        m = SourceMetrics(source_name="test", source_type="test")
        m.record_task_created()
        m.record_task_created()
        assert m.tasks_created == 2

    def test_record_validation_failure(self):
        m = SourceMetrics(source_name="test", source_type="test")
        m.record_validation_failure()
        assert m.validation_failures == 1

    def test_record_duplicate(self):
        m = SourceMetrics(source_name="test", source_type="test")
        m.record_duplicate()
        assert m.duplicate_requests == 1

    def test_success_rate_no_polls(self):
        m = SourceMetrics(source_name="test", source_type="test")
        assert m.success_rate == 0.0

    def test_success_rate_calculation(self):
        m = SourceMetrics(source_name="test", source_type="test")
        m.poll_attempts = 10
        m.successful_polls = 7
        assert m.success_rate == 70.0

    def test_is_healthy_default(self):
        m = SourceMetrics(source_name="test", source_type="test")
        assert m.is_healthy is True

    def test_is_healthy_threshold(self):
        m = SourceMetrics(source_name="test", source_type="test")
        m.consecutive_failures = 2
        assert m.is_healthy is True  # threshold is 3
        m.consecutive_failures = 3
        assert m.is_healthy is False

    def test_to_dict(self):
        m = SourceMetrics(source_name="test", source_type="github")
        m.record_poll_attempt(1.0)
        m.record_poll_success()
        m.record_task_created()
        d = m.to_dict()
        assert d["source_name"] == "test"
        assert d["source_type"] == "github"
        assert d["poll_attempts"] == 1
        assert d["successful_polls"] == 1
        assert d["tasks_created"] == 1
        assert d["is_healthy"] is True
        assert d["success_rate"] == 100.0
        assert d["last_poll_time"] is not None
        assert d["last_success_time"] is not None

    def test_to_dict_none_times(self):
        m = SourceMetrics(source_name="test", source_type="test")
        d = m.to_dict()
        assert d["last_poll_time"] is None
        assert d["last_success_time"] is None
        assert d["last_error_time"] is None


# ── MetricsCollector ────────────────────────────────────────────


class TestMetricsCollector:
    """Tests for metrics collection and persistence."""

    def test_creates_metrics_dir(self, tmp_path):
        metrics_dir = tmp_path / "metrics"
        MetricsCollector(metrics_dir)
        assert metrics_dir.exists()

    def test_get_or_create_new(self, tmp_path):
        collector = MetricsCollector(tmp_path / "metrics")
        m = collector.get_or_create_metrics("github", "github")
        assert m.source_name == "github"
        assert m.source_type == "github"

    def test_get_or_create_returns_same(self, tmp_path):
        collector = MetricsCollector(tmp_path / "metrics")
        m1 = collector.get_or_create_metrics("github", "github")
        m1.record_task_created()
        m2 = collector.get_or_create_metrics("github", "github")
        assert m2.tasks_created == 1  # same instance

    def test_save_and_load_metrics(self, tmp_path):
        metrics_dir = tmp_path / "metrics"
        collector = MetricsCollector(metrics_dir)
        m = collector.get_or_create_metrics("github", "github")
        m.record_poll_attempt(2.5)
        m.record_poll_success()
        m.record_task_created()
        m.record_poll_failure("test error")
        collector.save_metrics("github")

        # Verify file exists
        metrics_file = metrics_dir / "github.json"
        assert metrics_file.exists()

        # Load into new collector
        collector2 = MetricsCollector(metrics_dir)
        m2 = collector2.get_or_create_metrics("github", "github")
        assert m2.poll_attempts == 1
        assert m2.successful_polls == 1
        assert m2.tasks_created == 1
        assert m2.failed_polls == 1
        assert m2.last_error == "test error"

    def test_save_all_metrics(self, tmp_path):
        collector = MetricsCollector(tmp_path / "metrics")
        collector.get_or_create_metrics("a", "github")
        collector.get_or_create_metrics("b", "email")
        collector.save_all_metrics()
        assert (tmp_path / "metrics" / "a.json").exists()
        assert (tmp_path / "metrics" / "b.json").exists()

    def test_save_nonexistent_source(self, tmp_path):
        collector = MetricsCollector(tmp_path / "metrics")
        # Should not raise
        collector.save_metrics("nonexistent")

    def test_get_all_metrics(self, tmp_path):
        collector = MetricsCollector(tmp_path / "metrics")
        collector.get_or_create_metrics("a", "github")
        collector.get_or_create_metrics("b", "email")
        all_metrics = collector.get_all_metrics()
        assert "a" in all_metrics
        assert "b" in all_metrics

    def test_generate_summary(self, tmp_path):
        collector = MetricsCollector(tmp_path / "metrics")
        m1 = collector.get_or_create_metrics("github", "github")
        m1.poll_attempts = 10
        m1.successful_polls = 8
        m1.failed_polls = 2
        m1.tasks_created = 5

        m2 = collector.get_or_create_metrics("email", "email")
        m2.poll_attempts = 5
        m2.successful_polls = 5
        m2.tasks_created = 3

        summary = collector.generate_summary()
        assert summary["total_sources"] == 2
        assert summary["healthy_sources"] == 2
        assert summary["total_poll_attempts"] == 15
        assert summary["total_successful_polls"] == 13
        assert summary["total_failed_polls"] == 2
        assert summary["total_tasks_created"] == 8
        assert summary["overall_success_rate"] == pytest.approx(86.67, abs=0.1)
        assert "github" in summary["sources"]
        assert "email" in summary["sources"]

    def test_generate_summary_empty(self, tmp_path):
        collector = MetricsCollector(tmp_path / "metrics")
        summary = collector.generate_summary()
        assert summary["total_sources"] == 0
        assert summary["overall_success_rate"] == 0.0

    def test_load_corrupted_file(self, tmp_path):
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        (metrics_dir / "bad.json").write_text("not valid json")
        collector = MetricsCollector(metrics_dir)
        m = collector.get_or_create_metrics("bad", "test")
        # Should fall back to fresh metrics
        assert m.poll_attempts == 0


# ── HealthChecker ───────────────────────────────────────────────


class TestHealthChecker:
    """Tests for health checking system."""

    def test_unknown_source(self, tmp_path):
        collector = MetricsCollector(tmp_path / "metrics")
        checker = HealthChecker(collector)
        result = checker.check_source_health("unknown")
        assert result["status"] == "unknown"

    def test_healthy_source(self, tmp_path):
        collector = MetricsCollector(tmp_path / "metrics")
        m = collector.get_or_create_metrics("github", "github")
        m.record_poll_success()
        checker = HealthChecker(collector)
        result = checker.check_source_health("github")
        assert result["status"] == "healthy"
        assert result["consecutive_failures"] == 0

    def test_unhealthy_source(self, tmp_path):
        collector = MetricsCollector(tmp_path / "metrics")
        m = collector.get_or_create_metrics("github", "github")
        for i in range(5):
            m.record_poll_failure(f"error_{i}")
        checker = HealthChecker(collector, max_consecutive_failures=3)
        result = checker.check_source_health("github")
        assert result["status"] == "unhealthy"
        assert result["consecutive_failures"] == 5
        assert result["last_error"] == "error_4"

    def test_custom_failure_threshold(self, tmp_path):
        collector = MetricsCollector(tmp_path / "metrics")
        m = collector.get_or_create_metrics("test", "test")
        m.consecutive_failures = 4
        checker = HealthChecker(collector, max_consecutive_failures=5)
        result = checker.check_source_health("test")
        assert result["status"] == "healthy"  # 4 < 5

    def test_check_all_sources(self, tmp_path):
        collector = MetricsCollector(tmp_path / "metrics")
        m1 = collector.get_or_create_metrics("good", "github")
        m1.record_poll_success()
        m2 = collector.get_or_create_metrics("bad", "email")
        for _ in range(5):
            m2.record_poll_failure("down")

        checker = HealthChecker(collector, max_consecutive_failures=3)
        result = checker.check_all_sources()
        assert result["overall_status"] == "degraded"
        assert result["healthy_sources"] == 1
        assert result["total_sources"] == 2
        assert result["sources"]["good"]["status"] == "healthy"
        assert result["sources"]["bad"]["status"] == "unhealthy"

    def test_all_healthy(self, tmp_path):
        collector = MetricsCollector(tmp_path / "metrics")
        collector.get_or_create_metrics("a", "github")
        collector.get_or_create_metrics("b", "email")
        checker = HealthChecker(collector)
        result = checker.check_all_sources()
        assert result["overall_status"] == "healthy"

    def test_empty_sources(self, tmp_path):
        collector = MetricsCollector(tmp_path / "metrics")
        checker = HealthChecker(collector)
        result = checker.check_all_sources()
        assert result["overall_status"] == "healthy"
        assert result["total_sources"] == 0
