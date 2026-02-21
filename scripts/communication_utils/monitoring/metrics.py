"""
Metrics collection for performance monitoring.

Tracks operation timing, success rates, and error rates
across communication platforms.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class OperationMetrics:
    """Metrics for a single operation."""

    operation: str
    platform: str
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    success: bool | None = None
    error: str | None = None

    @property
    def duration(self) -> float | None:
        """Get operation duration in seconds."""
        if self.end_time is None:
            return None
        return self.end_time - self.start_time

    def complete(self, success: bool = True, error: str | None = None) -> None:
        """Mark operation as complete."""
        self.end_time = time.time()
        self.success = success
        self.error = error


class MetricsCollector:
    """
    Collect and aggregate operation metrics.

    Tracks timing, success rates, and errors across operations
    for performance monitoring and optimization.
    """

    def __init__(self):
        """Initialize metrics collector."""
        self.operations: list[OperationMetrics] = []

    def start_operation(self, operation: str, platform: str) -> OperationMetrics:
        """
        Start tracking an operation.

        Args:
            operation: Operation name (send_email, post_tweet, etc.)
            platform: Platform identifier

        Returns:
            OperationMetrics object to track this operation
        """
        metrics = OperationMetrics(operation=operation, platform=platform)
        self.operations.append(metrics)
        return metrics

    def get_stats(self, platform: str | None = None) -> dict:
        """
        Get aggregated statistics.

        Args:
            platform: Optional platform filter

        Returns:
            Dictionary with aggregated metrics
        """
        ops = self.operations
        if platform:
            ops = [op for op in ops if op.platform == platform]

        if not ops:
            return {
                "total_operations": 0,
                "success_rate": 0.0,
                "avg_duration": 0.0,
                "error_count": 0,
            }

        completed = [op for op in ops if op.end_time is not None]
        successful = [op for op in completed if op.success]
        failed = [op for op in completed if not op.success]

        durations = [op.duration for op in completed if op.duration is not None]
        avg_duration = sum(durations) / len(durations) if durations else 0.0

        success_rate = (len(successful) / len(completed) if completed else 0.0) * 100

        return {
            "total_operations": len(ops),
            "completed_operations": len(completed),
            "successful_operations": len(successful),
            "failed_operations": len(failed),
            "success_rate": round(success_rate, 2),
            "avg_duration": round(avg_duration, 3),
            "error_count": len(failed),
        }

    def get_recent_errors(
        self, limit: int = 10, platform: str | None = None
    ) -> list[dict]:
        """
        Get recent error details.

        Args:
            limit: Maximum number of errors to return
            platform: Optional platform filter

        Returns:
            List of error details
        """
        ops = self.operations
        if platform:
            ops = [op for op in ops if op.platform == platform]

        errors = [
            {
                "operation": op.operation,
                "platform": op.platform,
                "error": op.error,
                "timestamp": datetime.fromtimestamp(op.start_time).isoformat(),
            }
            for op in ops
            if not op.success and op.error
        ]

        return sorted(errors, key=lambda x: x["timestamp"], reverse=True)[:limit]

    def clear(self) -> None:
        """Clear all collected metrics."""
        self.operations.clear()

    def get_operation_breakdown(self) -> dict[str, dict]:
        """
        Get metrics broken down by operation type.

        Returns:
            Dictionary mapping operation names to their stats
        """
        breakdown: dict[str, list] = {}

        for op in self.operations:
            if op.operation not in breakdown:
                breakdown[op.operation] = []
            breakdown[op.operation].append(op)

        result = {}
        for operation, ops in breakdown.items():
            completed = [o for o in ops if o.end_time is not None]
            successful = [o for o in completed if o.success]
            durations = [o.duration for o in completed if o.duration is not None]

            result[operation] = {
                "total": len(ops),
                "successful": len(successful),
                "failed": len(completed) - len(successful),
                "avg_duration": (
                    round(sum(durations) / len(durations), 3) if durations else 0.0
                ),
            }

        return result
