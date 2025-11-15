"""Input source framework for automatic task creation from external sources.

This module provides the base interfaces and types for creating tasks from
external sources like GitHub issues, emails, webhooks, and scheduled triggers.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class InputSourceType(Enum):
    """Types of supported input sources."""

    GITHUB = "github"
    EMAIL = "email"
    WEBHOOK = "webhook"
    SCHEDULER = "scheduler"


class ValidationStatus(Enum):
    """Validation result statuses."""

    VALID = "valid"
    INVALID = "invalid"
    SPAM = "spam"
    DUPLICATE = "duplicate"


@dataclass
class TaskRequest:
    """Request to create a task from an external source."""

    # Core fields
    source_type: InputSourceType
    source_id: str  # Unique identifier in source system
    title: str
    description: str

    # Metadata
    created_at: datetime
    author: Optional[str] = None
    priority: Optional[str] = None  # "high", "medium", "low"
    tags: List[str] = None

    # Source-specific data
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.tags is None:
            self.tags = []
        if self.metadata is None:
            self.metadata = {}


@dataclass
class ValidationResult:
    """Result of validating a task request."""

    status: ValidationStatus
    message: str
    details: Optional[Dict[str, Any]] = None

    @property
    def is_valid(self) -> bool:
        """Check if validation passed."""
        return self.status == ValidationStatus.VALID


@dataclass
class TaskCreationResult:
    """Result of creating a task from a request."""

    success: bool
    task_path: Optional[Path] = None
    task_id: Optional[str] = None
    error: Optional[str] = None


class InputSource(ABC):
    """Abstract base class for external input sources."""

    def __init__(
        self,
        config: Dict[str, Any],
        metrics_collector=None,
        rate_limiter=None,
    ):
        """Initialize the input source with configuration.

        Args:
            config: Source-specific configuration dictionary
            metrics_collector: Optional MetricsCollector for tracking metrics
            rate_limiter: Optional RateLimiter for rate limiting
        """
        self.config = config
        self.source_type = self._get_source_type()
        self.metrics_collector = metrics_collector
        self.rate_limiter = rate_limiter
        self.source_name = self.__class__.__name__

    @abstractmethod
    def _get_source_type(self) -> InputSourceType:
        """Return the source type for this implementation."""
        pass

    @abstractmethod
    async def poll_for_inputs(self) -> List[TaskRequest]:
        """Poll the source for new task requests.

        Returns:
            List of task requests from the source

        Raises:
            ConnectionError: If source is unreachable
            AuthenticationError: If authentication fails
        """
        pass

    def validate_input(self, request: TaskRequest) -> ValidationResult:
        """Validate a task request before creation.

        Default implementation does basic validation. Override for
        source-specific validation logic.

        Args:
            request: The task request to validate

        Returns:
            Validation result with status and details
        """
        # Basic validation: check required fields
        if not request.title:
            return ValidationResult(
                status=ValidationStatus.INVALID, message="Missing required field: title"
            )

        if not request.description:
            return ValidationResult(
                status=ValidationStatus.INVALID,
                message="Missing required field: description",
            )

        # Check for duplicates (basic implementation)
        if self._is_duplicate(request):
            return ValidationResult(
                status=ValidationStatus.DUPLICATE,
                message=f"Duplicate request from {request.source_id}",
            )

        return ValidationResult(
            status=ValidationStatus.VALID, message="Request validated successfully"
        )

    def _is_duplicate(self, request: TaskRequest) -> bool:
        """Check if request is a duplicate.

        Default implementation always returns False. Override to implement
        duplicate detection for your source.

        Args:
            request: The task request to check

        Returns:
            True if request is a duplicate
        """
        return False

    @abstractmethod
    async def create_task(self, request: TaskRequest) -> TaskCreationResult:
        """Create a task from a validated request.

        Args:
            request: Validated task request

        Returns:
            Result of task creation with path and ID
        """
        pass

    async def acknowledge_input(self, request: TaskRequest) -> None:
        """Acknowledge that input was processed.

        Optional method for sources that require acknowledgment.
        Default implementation does nothing.

        Args:
            request: The processed task request
        """
        pass

    def _record_metric(self, metric_type: str, **kwargs) -> None:
        """Record a metric if metrics collector is available.

        Args:
            metric_type: Type of metric (poll_success, poll_failure, task_created, etc.)
            **kwargs: Additional metric data
        """
        if not self.metrics_collector:
            return

        metrics = self.metrics_collector.get_or_create_metrics(
            self.source_name, self.source_type.value
        )

        if metric_type == "poll_success":
            metrics.record_poll_success()
        elif metric_type == "poll_failure":
            metrics.record_poll_failure(kwargs.get("error", "Unknown error"))
        elif metric_type == "task_created":
            metrics.record_task_created()
        elif metric_type == "validation_failure":
            metrics.record_validation_failure()
        elif metric_type == "duplicate":
            metrics.record_duplicate()
        elif metric_type == "poll_attempt":
            metrics.record_poll_attempt(kwargs.get("duration", 0.0))

        # Save metrics after recording
        self.metrics_collector.save_metrics(self.source_name)

    def _check_rate_limit(self) -> bool:
        """Check if rate limit allows request.

        Returns:
            True if request allowed, False if rate limited
        """
        if not self.rate_limiter:
            return True

        return self.rate_limiter.check_limit(self.source_name)

    def _consume_rate_limit(self) -> bool:
        """Consume rate limit token if available.

        Returns:
            True if token consumed, False if rate limited
        """
        if not self.rate_limiter:
            return True

        return self.rate_limiter.consume(self.source_name)

    def _get_rate_limit_wait_time(self) -> float:
        """Get time to wait for rate limit.

        Returns:
            Seconds to wait (0 if not rate limited)
        """
        if not self.rate_limiter:
            return 0.0

        return self.rate_limiter.get_wait_time(self.source_name)

    async def process_request(self, request: TaskRequest) -> TaskCreationResult:
        """Process a task request end-to-end.

        This is the main entry point for processing requests. It handles
        validation, task creation, and acknowledgment.

        Args:
            request: The task request to process

        Returns:
            Result of task creation
        """
        # Validate request
        validation = self.validate_input(request)
        if not validation.is_valid:
            return TaskCreationResult(
                success=False, error=f"Validation failed: {validation.message}"
            )

        # Create task
        result = await self.create_task(request)

        # Acknowledge if successful
        if result.success:
            await self.acknowledge_input(request)

        return result


class InputSourceManager:
    """Manager for multiple input sources."""

    def __init__(self):
        """Initialize the input source manager."""
        self.sources: Dict[str, InputSource] = {}

    def register_source(self, name: str, source: InputSource) -> None:
        """Register an input source.

        Args:
            name: Unique name for the source
            source: The input source instance
        """
        self.sources[name] = source

    def unregister_source(self, name: str) -> None:
        """Unregister an input source.

        Args:
            name: Name of the source to unregister
        """
        self.sources.pop(name, None)

    async def poll_all_sources(self) -> Dict[str, List[TaskRequest]]:
        """Poll all registered sources for new requests.

        Returns:
            Dictionary mapping source names to their task requests
        """
        results = {}
        for name, source in self.sources.items():
            try:
                requests = await source.poll_for_inputs()
                results[name] = requests
            except Exception as e:
                # Log error but continue with other sources
                print(f"Error polling source '{name}': {e}")
                results[name] = []
        return results

    async def process_all_sources(self) -> List[TaskCreationResult]:
        """Poll and process all registered sources.

        Returns:
            List of all task creation results
        """
        all_results = []
        requests_by_source = await self.poll_all_sources()

        for name, requests in requests_by_source.items():
            source = self.sources[name]
            for request in requests:
                result = await source.process_request(request)
                all_results.append(result)

        return all_results
