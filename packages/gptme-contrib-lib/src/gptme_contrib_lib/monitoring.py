"""Monitoring and metrics collection for input sources.

Provides metrics tracking, health checks, and error monitoring for all input sources.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional


@dataclass
class SourceMetrics:
    """Metrics for a single input source."""

    source_name: str
    source_type: str

    # Poll metrics
    poll_attempts: int = 0
    successful_polls: int = 0
    failed_polls: int = 0

    # Task metrics
    tasks_created: int = 0
    validation_failures: int = 0
    duplicate_requests: int = 0

    # Error tracking
    last_poll_time: Optional[datetime] = None
    last_success_time: Optional[datetime] = None
    last_error: Optional[str] = None
    last_error_time: Optional[datetime] = None
    consecutive_failures: int = 0

    # Performance
    avg_poll_duration_seconds: float = 0.0
    total_poll_duration_seconds: float = 0.0

    def record_poll_attempt(self, duration_seconds: float) -> None:
        """Record a poll attempt.

        Args:
            duration_seconds: How long the poll took
        """
        self.poll_attempts += 1
        self.last_poll_time = datetime.now()
        self.total_poll_duration_seconds += duration_seconds
        self.avg_poll_duration_seconds = (
            self.total_poll_duration_seconds / self.poll_attempts
        )

    def record_poll_success(self) -> None:
        """Record a successful poll."""
        self.successful_polls += 1
        self.last_success_time = datetime.now()
        self.consecutive_failures = 0

    def record_poll_failure(self, error: str) -> None:
        """Record a failed poll.

        Args:
            error: Error message
        """
        self.failed_polls += 1
        self.consecutive_failures += 1
        self.last_error = error
        self.last_error_time = datetime.now()

    def record_task_created(self) -> None:
        """Record a task creation."""
        self.tasks_created += 1

    def record_validation_failure(self) -> None:
        """Record a validation failure."""
        self.validation_failures += 1

    def record_duplicate(self) -> None:
        """Record a duplicate request."""
        self.duplicate_requests += 1

    @property
    def success_rate(self) -> float:
        """Calculate success rate.

        Returns:
            Success rate as percentage (0-100)
        """
        if self.poll_attempts == 0:
            return 0.0
        return (self.successful_polls / self.poll_attempts) * 100

    @property
    def is_healthy(self) -> bool:
        """Check if source is healthy.

        Returns:
            True if source is healthy (consecutive failures < threshold)
        """
        return self.consecutive_failures < 3

    def to_dict(self) -> Dict:
        """Export metrics to dictionary.

        Returns:
            Metrics as dictionary
        """
        return {
            "source_name": self.source_name,
            "source_type": self.source_type,
            "poll_attempts": self.poll_attempts,
            "successful_polls": self.successful_polls,
            "failed_polls": self.failed_polls,
            "tasks_created": self.tasks_created,
            "validation_failures": self.validation_failures,
            "duplicate_requests": self.duplicate_requests,
            "last_poll_time": self.last_poll_time.isoformat()
            if self.last_poll_time
            else None,
            "last_success_time": self.last_success_time.isoformat()
            if self.last_success_time
            else None,
            "last_error": self.last_error,
            "last_error_time": self.last_error_time.isoformat()
            if self.last_error_time
            else None,
            "consecutive_failures": self.consecutive_failures,
            "avg_poll_duration_seconds": self.avg_poll_duration_seconds,
            "success_rate": self.success_rate,
            "is_healthy": self.is_healthy,
        }


class MetricsCollector:
    """Collects and persists metrics for all input sources."""

    def __init__(self, metrics_dir: Path):
        """Initialize metrics collector.

        Args:
            metrics_dir: Directory to store metrics files
        """
        self.metrics_dir = Path(metrics_dir)
        self.metrics_dir.mkdir(parents=True, exist_ok=True)
        self.sources: Dict[str, SourceMetrics] = {}

    def get_or_create_metrics(
        self, source_name: str, source_type: str
    ) -> SourceMetrics:
        """Get or create metrics for a source.

        Args:
            source_name: Name of the source
            source_type: Type of the source

        Returns:
            SourceMetrics instance
        """
        if source_name not in self.sources:
            # Try to load from disk
            metrics_file = self.metrics_dir / f"{source_name}.json"
            if metrics_file.exists():
                metrics = self._load_metrics(metrics_file, source_name, source_type)
            else:
                metrics = SourceMetrics(
                    source_name=source_name, source_type=source_type
                )
            self.sources[source_name] = metrics

        return self.sources[source_name]

    def _load_metrics(
        self, metrics_file: Path, source_name: str, source_type: str
    ) -> SourceMetrics:
        """Load metrics from file.

        Args:
            metrics_file: Path to metrics file
            source_name: Name of the source
            source_type: Type of the source

        Returns:
            SourceMetrics instance
        """
        try:
            with open(metrics_file) as f:
                data = json.load(f)

            # Parse datetime fields
            for field_name in [
                "last_poll_time",
                "last_success_time",
                "last_error_time",
            ]:
                if data.get(field_name):
                    data[field_name] = datetime.fromisoformat(data[field_name])
                else:
                    data[field_name] = None

            # Remove calculated fields
            data.pop("success_rate", None)
            data.pop("is_healthy", None)

            return SourceMetrics(**data)
        except Exception as e:
            print(f"Error loading metrics for {source_name}: {e}")
            return SourceMetrics(source_name=source_name, source_type=source_type)

    def save_metrics(self, source_name: str) -> None:
        """Save metrics to disk.

        Args:
            source_name: Name of the source
        """
        if source_name not in self.sources:
            return

        metrics = self.sources[source_name]
        metrics_file = self.metrics_dir / f"{source_name}.json"

        try:
            with open(metrics_file, "w") as f:
                json.dump(metrics.to_dict(), f, indent=2)
        except Exception as e:
            print(f"Error saving metrics for {source_name}: {e}")

    def save_all_metrics(self) -> None:
        """Save all metrics to disk."""
        for source_name in self.sources:
            self.save_metrics(source_name)

    def get_all_metrics(self) -> Dict[str, SourceMetrics]:
        """Get metrics for all sources.

        Returns:
            Dictionary mapping source names to metrics
        """
        return self.sources.copy()

    def generate_summary(self) -> Dict:
        """Generate summary of all metrics.

        Returns:
            Summary dictionary with aggregate metrics
        """
        total_polls = sum(m.poll_attempts for m in self.sources.values())
        total_successes = sum(m.successful_polls for m in self.sources.values())
        total_failures = sum(m.failed_polls for m in self.sources.values())
        total_tasks = sum(m.tasks_created for m in self.sources.values())

        healthy_sources = sum(1 for m in self.sources.values() if m.is_healthy)
        total_sources = len(self.sources)

        return {
            "total_sources": total_sources,
            "healthy_sources": healthy_sources,
            "unhealthy_sources": total_sources - healthy_sources,
            "total_poll_attempts": total_polls,
            "total_successful_polls": total_successes,
            "total_failed_polls": total_failures,
            "total_tasks_created": total_tasks,
            "overall_success_rate": (
                (total_successes / total_polls * 100) if total_polls > 0 else 0.0
            ),
            "sources": {
                name: metrics.to_dict() for name, metrics in self.sources.items()
            },
        }


class HealthChecker:
    """Health check system for input sources."""

    def __init__(
        self, metrics_collector: MetricsCollector, max_consecutive_failures: int = 3
    ):
        """Initialize health checker.

        Args:
            metrics_collector: Metrics collector instance
            max_consecutive_failures: Max failures before marking unhealthy
        """
        self.metrics_collector = metrics_collector
        self.max_consecutive_failures = max_consecutive_failures

    def check_source_health(self, source_name: str) -> Dict:
        """Check health of a specific source.

        Args:
            source_name: Name of the source

        Returns:
            Health status dictionary
        """
        metrics = self.metrics_collector.sources.get(source_name)
        if not metrics:
            return {
                "source_name": source_name,
                "status": "unknown",
                "message": "No metrics available",
            }

        is_healthy = metrics.consecutive_failures < self.max_consecutive_failures

        return {
            "source_name": source_name,
            "status": "healthy" if is_healthy else "unhealthy",
            "consecutive_failures": metrics.consecutive_failures,
            "last_error": metrics.last_error,
            "last_error_time": metrics.last_error_time.isoformat()
            if metrics.last_error_time
            else None,
            "last_success_time": metrics.last_success_time.isoformat()
            if metrics.last_success_time
            else None,
            "success_rate": metrics.success_rate,
        }

    def check_all_sources(self) -> Dict:
        """Check health of all sources.

        Returns:
            Dictionary with health status for all sources
        """
        results = {}
        for source_name in self.metrics_collector.sources:
            results[source_name] = self.check_source_health(source_name)

        # Add summary
        healthy_count = sum(
            1 for status in results.values() if status["status"] == "healthy"
        )
        total_count = len(results)

        return {
            "overall_status": "healthy" if healthy_count == total_count else "degraded",
            "healthy_sources": healthy_count,
            "total_sources": total_count,
            "sources": results,
        }
