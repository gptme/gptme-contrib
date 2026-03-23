"""Tests for input_sources module.

Tests the base types, validation logic, and InputSourceManager.
"""

from datetime import datetime
from pathlib import Path
from typing import List

import pytest
from gptme_contrib_lib.input_sources import (
    InputSource,
    InputSourceManager,
    InputSourceType,
    TaskCreationResult,
    TaskRequest,
    ValidationResult,
    ValidationStatus,
)

# ── Data types ──────────────────────────────────────────────────


class TestInputSourceType:
    """Tests for InputSourceType enum."""

    def test_values(self):
        assert InputSourceType.GITHUB.value == "github"
        assert InputSourceType.EMAIL.value == "email"
        assert InputSourceType.WEBHOOK.value == "webhook"
        assert InputSourceType.SCHEDULER.value == "scheduler"


class TestValidationStatus:
    """Tests for ValidationStatus enum."""

    def test_values(self):
        assert ValidationStatus.VALID.value == "valid"
        assert ValidationStatus.INVALID.value == "invalid"
        assert ValidationStatus.SPAM.value == "spam"
        assert ValidationStatus.DUPLICATE.value == "duplicate"


class TestTaskRequest:
    """Tests for TaskRequest dataclass."""

    def test_creation(self):
        req = TaskRequest(
            source_type=InputSourceType.GITHUB,
            source_id="issue-123",
            title="Fix bug",
            description="Something is broken",
            created_at=datetime.now(),
        )
        assert req.title == "Fix bug"
        assert req.source_type == InputSourceType.GITHUB
        assert req.author is None
        assert req.tags == []
        assert req.metadata == {}

    def test_with_optional_fields(self):
        req = TaskRequest(
            source_type=InputSourceType.EMAIL,
            source_id="msg-456",
            title="Feature request",
            description="Please add X",
            created_at=datetime.now(),
            author="user@example.com",
            priority="high",
            tags=["feature", "urgent"],
            metadata={"thread_id": "abc"},
        )
        assert req.author == "user@example.com"
        assert req.priority == "high"
        assert len(req.tags) == 2
        assert req.metadata["thread_id"] == "abc"


class TestValidationResult:
    """Tests for ValidationResult dataclass."""

    def test_valid(self):
        result = ValidationResult(status=ValidationStatus.VALID, message="OK")
        assert result.is_valid is True

    def test_invalid(self):
        result = ValidationResult(
            status=ValidationStatus.INVALID, message="Missing title"
        )
        assert result.is_valid is False

    def test_spam(self):
        result = ValidationResult(status=ValidationStatus.SPAM, message="Detected spam")
        assert result.is_valid is False

    def test_duplicate(self):
        result = ValidationResult(
            status=ValidationStatus.DUPLICATE, message="Already exists"
        )
        assert result.is_valid is False

    def test_with_details(self):
        result = ValidationResult(
            status=ValidationStatus.INVALID,
            message="Bad input",
            details={"field": "title", "reason": "empty"},
        )
        assert result.details["field"] == "title"


class TestTaskCreationResult:
    """Tests for TaskCreationResult dataclass."""

    def test_success(self):
        result = TaskCreationResult(
            success=True,
            task_path=Path("/tasks/new-task.md"),
            task_id="new-task",
        )
        assert result.success is True
        assert result.error is None

    def test_failure(self):
        result = TaskCreationResult(
            success=False,
            error="Failed to write file",
        )
        assert result.success is False
        assert result.task_path is None


# ── InputSource (abstract) ─────────────────────────────────────


class MockInputSource(InputSource):
    """Concrete implementation for testing base class behavior."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._requests: List[TaskRequest] = []
        self._acknowledged: List[TaskRequest] = []
        self._duplicates: set = set()

    def _get_source_type(self) -> InputSourceType:
        return InputSourceType.GITHUB

    async def poll_for_inputs(self) -> List[TaskRequest]:
        return self._requests

    async def create_task(self, request: TaskRequest) -> TaskCreationResult:
        return TaskCreationResult(
            success=True,
            task_id=f"task-{request.source_id}",
            task_path=Path(f"/tasks/task-{request.source_id}.md"),
        )

    async def acknowledge_input(self, request: TaskRequest) -> None:
        self._acknowledged.append(request)

    def _is_duplicate(self, request: TaskRequest) -> bool:
        return request.source_id in self._duplicates


class TestInputSourceBase:
    """Tests for InputSource base class behavior."""

    def _make_request(self, title="Test", description="Test desc", source_id="1"):
        return TaskRequest(
            source_type=InputSourceType.GITHUB,
            source_id=source_id,
            title=title,
            description=description,
            created_at=datetime.now(),
        )

    def test_validate_valid_request(self):
        source = MockInputSource(config={})
        req = self._make_request()
        result = source.validate_input(req)
        assert result.is_valid is True

    def test_validate_missing_title(self):
        source = MockInputSource(config={})
        req = self._make_request(title="")
        result = source.validate_input(req)
        assert result.is_valid is False
        assert "title" in result.message

    def test_validate_missing_description(self):
        source = MockInputSource(config={})
        req = self._make_request(description="")
        result = source.validate_input(req)
        assert result.is_valid is False
        assert "description" in result.message

    def test_validate_duplicate_detection(self):
        source = MockInputSource(config={})
        source._duplicates.add("dup-1")
        req = self._make_request(source_id="dup-1")
        result = source.validate_input(req)
        assert result.status == ValidationStatus.DUPLICATE

    def test_source_name_default(self):
        source = MockInputSource(config={})
        assert source.source_name == "MockInputSource"

    def test_source_type(self):
        source = MockInputSource(config={})
        assert source.source_type == InputSourceType.GITHUB

    def test_rate_limit_no_limiter(self):
        source = MockInputSource(config={})
        assert source._check_rate_limit() is True
        assert source._consume_rate_limit() is True
        assert source._get_rate_limit_wait_time() == 0.0

    @pytest.mark.asyncio
    async def test_process_request_valid(self):
        source = MockInputSource(config={})
        req = self._make_request()
        result = await source.process_request(req)
        assert result.success is True
        assert len(source._acknowledged) == 1

    @pytest.mark.asyncio
    async def test_process_request_invalid(self):
        source = MockInputSource(config={})
        req = self._make_request(title="")
        result = await source.process_request(req)
        assert result.success is False
        assert "Validation failed" in result.error
        assert len(source._acknowledged) == 0

    def test_record_metric_no_collector(self):
        source = MockInputSource(config={})
        # Should not raise even without metrics_collector
        source._record_metric("poll_success")

    def test_config_stored(self):
        source = MockInputSource(config={"key": "value"})
        assert source.config["key"] == "value"


# ── InputSourceManager ─────────────────────────────────────────


class TestInputSourceManager:
    """Tests for InputSourceManager."""

    def test_register_source(self):
        manager = InputSourceManager()
        source = MockInputSource(config={})
        manager.register_source("github", source)
        assert "github" in manager.sources

    def test_unregister_source(self):
        manager = InputSourceManager()
        source = MockInputSource(config={})
        manager.register_source("github", source)
        manager.unregister_source("github")
        assert "github" not in manager.sources

    def test_unregister_nonexistent(self):
        manager = InputSourceManager()
        # Should not raise
        manager.unregister_source("nonexistent")

    @pytest.mark.asyncio
    async def test_poll_all_sources(self):
        manager = InputSourceManager()
        source = MockInputSource(config={})
        source._requests = [
            TaskRequest(
                source_type=InputSourceType.GITHUB,
                source_id="1",
                title="Task 1",
                description="Desc 1",
                created_at=datetime.now(),
            )
        ]
        manager.register_source("github", source)
        results = await manager.poll_all_sources()
        assert "github" in results
        assert len(results["github"]) == 1

    @pytest.mark.asyncio
    async def test_poll_all_empty(self):
        manager = InputSourceManager()
        source = MockInputSource(config={})
        manager.register_source("github", source)
        results = await manager.poll_all_sources()
        assert results["github"] == []

    @pytest.mark.asyncio
    async def test_process_all_sources(self):
        manager = InputSourceManager()
        source = MockInputSource(config={})
        source._requests = [
            TaskRequest(
                source_type=InputSourceType.GITHUB,
                source_id="1",
                title="Task 1",
                description="Desc 1",
                created_at=datetime.now(),
            ),
            TaskRequest(
                source_type=InputSourceType.GITHUB,
                source_id="2",
                title="Task 2",
                description="Desc 2",
                created_at=datetime.now(),
            ),
        ]
        manager.register_source("github", source)
        results = await manager.process_all_sources()
        assert len(results) == 2
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_poll_handles_source_error(self):
        """Manager should handle errors from individual sources gracefully."""

        class FailingSource(MockInputSource):
            async def poll_for_inputs(self):
                raise ConnectionError("API down")

        manager = InputSourceManager()
        manager.register_source("failing", FailingSource(config={}))
        manager.register_source("working", MockInputSource(config={}))
        results = await manager.poll_all_sources()
        assert results["failing"] == []
        assert results["working"] == []
