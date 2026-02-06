"""Tests for the ACE Reviewer module."""

import json
from pathlib import Path

import pytest

from gptme_ace.reviewer import (
    DeltaReviewer,
    ReviewCriterion,
    ReviewDecision,
    ReviewResult,
    DEFAULT_CRITERIA,
    ReviewerError,
)


@pytest.fixture
def temp_workspace(tmp_path):
    """Create a temporary workspace with delta directories"""
    delta_dir = tmp_path / "deltas"
    delta_dir.mkdir()
    (delta_dir / "pending").mkdir()
    (delta_dir / "approved").mkdir()
    (delta_dir / "rejected").mkdir()
    (delta_dir / "reviews").mkdir()

    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "workflow").mkdir()

    return tmp_path


@pytest.fixture
def sample_delta():
    """Create a sample delta dict"""
    return {
        "delta_id": "test-delta-001",
        "created": "2026-02-05T08:00:00Z",
        "source": "test-trajectory",
        "source_insights": ["insight-1", "insight-2"],
        "lesson_id": "workflow/git-workflow",
        "operations": [
            {
                "type": "ADD",
                "section": "Pattern",
                "content": "Always use explicit paths when navigating directories.",
                "position": "after:## Detection",
            },
            {
                "type": "MODIFY",
                "section": "Outcome",
                "content": "Following this pattern prevents navigation errors.",
                "target": "Following this leads to",
            },
        ],
        "rationale": "This pattern was observed in multiple trajectories where agents navigated incorrectly.",
        "review_status": "pending",
    }


@pytest.fixture
def sample_lesson_content():
    """Create sample lesson content"""
    return """---
match:
  keywords:
    - git workflow
status: active
---

# Git Workflow

## Rule
Always use explicit git commands.

## Detection
Observable signals:
- Failed git commands
- Wrong branch issues

## Pattern
Standard git workflow.

## Outcome
Following this leads to better git hygiene.
"""


class TestDeltaReviewer:
    """Tests for DeltaReviewer class"""

    def test_init_default(self):
        """Test default initialization"""
        reviewer = DeltaReviewer()
        assert reviewer.delta_dir == Path("deltas")
        assert reviewer.lessons_dir == Path("lessons")
        assert reviewer.approve_threshold == 0.7
        assert reviewer.reject_threshold == 0.4
        assert not reviewer.auto_approve

    def test_init_custom(self, temp_workspace):
        """Test custom initialization"""
        reviewer = DeltaReviewer(
            delta_dir=temp_workspace / "deltas",
            lessons_dir=temp_workspace / "lessons",
            approve_threshold=0.8,
            reject_threshold=0.3,
            auto_approve=True,
        )
        assert reviewer.delta_dir == temp_workspace / "deltas"
        assert reviewer.approve_threshold == 0.8
        assert reviewer.auto_approve

    def test_list_pending_deltas_empty(self, temp_workspace):
        """Test listing pending deltas when empty"""
        reviewer = DeltaReviewer(delta_dir=temp_workspace / "deltas")
        pending = reviewer.list_pending_deltas()
        assert pending == []

    def test_list_pending_deltas(self, temp_workspace, sample_delta):
        """Test listing pending deltas"""
        pending_dir = temp_workspace / "deltas" / "pending"
        (pending_dir / "delta-001.json").write_text(json.dumps(sample_delta))
        (pending_dir / "delta-002.json").write_text(
            json.dumps({**sample_delta, "delta_id": "delta-002"})
        )

        reviewer = DeltaReviewer(delta_dir=temp_workspace / "deltas")
        pending = reviewer.list_pending_deltas()
        assert len(pending) == 2
        assert "delta-001" in pending
        assert "delta-002" in pending

    def test_load_pending_delta(self, temp_workspace, sample_delta):
        """Test loading a pending delta"""
        pending_dir = temp_workspace / "deltas" / "pending"
        (pending_dir / "test-delta-001.json").write_text(json.dumps(sample_delta))

        reviewer = DeltaReviewer(delta_dir=temp_workspace / "deltas")
        loaded = reviewer.load_pending_delta("test-delta-001")
        assert loaded["delta_id"] == "test-delta-001"
        assert loaded["lesson_id"] == "workflow/git-workflow"

    def test_load_pending_delta_not_found(self, temp_workspace):
        """Test loading a non-existent delta"""
        reviewer = DeltaReviewer(delta_dir=temp_workspace / "deltas")
        with pytest.raises(ReviewerError, match="not found"):
            reviewer.load_pending_delta("nonexistent")

    def test_load_pending_delta_already_approved(self, temp_workspace, sample_delta):
        """Test loading a delta that's already approved"""
        approved_dir = temp_workspace / "deltas" / "approved"
        (approved_dir / "test-delta-001.json").write_text(json.dumps(sample_delta))

        reviewer = DeltaReviewer(delta_dir=temp_workspace / "deltas")
        with pytest.raises(ReviewerError, match="already approved"):
            reviewer.load_pending_delta("test-delta-001")

    def test_load_lesson_context(self, temp_workspace, sample_lesson_content):
        """Test loading lesson context"""
        lesson_path = temp_workspace / "lessons" / "workflow" / "git-workflow.md"
        lesson_path.write_text(sample_lesson_content)

        reviewer = DeltaReviewer(
            delta_dir=temp_workspace / "deltas",
            lessons_dir=temp_workspace / "lessons",
        )
        content = reviewer.load_lesson_context("workflow/git-workflow")
        assert content is not None
        assert "Git Workflow" in content

    def test_evaluate_criterion_relevance(self, sample_delta):
        """Test evaluating relevance criterion"""
        reviewer = DeltaReviewer()
        criterion = ReviewCriterion(
            name="relevance",
            description="Test",
            score=0.0,
            weight=1.0,
        )
        result = reviewer.evaluate_criterion(criterion, sample_delta, None)
        assert result.score >= 0.0
        assert result.score <= 1.0
        assert result.feedback != ""

    def test_evaluate_criterion_format_compliance(self, sample_delta):
        """Test evaluating format compliance criterion"""
        reviewer = DeltaReviewer()
        criterion = ReviewCriterion(
            name="format_compliance",
            description="Test",
            score=0.0,
            weight=1.0,
        )
        result = reviewer.evaluate_criterion(criterion, sample_delta, None)
        # Sample delta has valid operations
        assert result.score >= 0.6
        assert (
            "Operations follow format" in result.feedback or "Valid" in result.feedback
        )

    def test_review_delta(self, temp_workspace, sample_delta, sample_lesson_content):
        """Test full delta review"""
        pending_dir = temp_workspace / "deltas" / "pending"
        (pending_dir / "test-delta-001.json").write_text(json.dumps(sample_delta))

        lesson_path = temp_workspace / "lessons" / "workflow" / "git-workflow.md"
        lesson_path.write_text(sample_lesson_content)

        reviewer = DeltaReviewer(
            delta_dir=temp_workspace / "deltas",
            lessons_dir=temp_workspace / "lessons",
        )
        result = reviewer.review_delta("test-delta-001")

        assert isinstance(result, ReviewResult)
        assert result.delta_id == "test-delta-001"
        assert result.overall_score >= 0.0
        assert result.overall_score <= 1.0
        assert result.decision in list(ReviewDecision)
        assert len(result.criteria) > 0
        assert result.summary != ""

    def test_save_review(self, temp_workspace, sample_delta):
        """Test saving review result"""
        pending_dir = temp_workspace / "deltas" / "pending"
        (pending_dir / "test-delta-001.json").write_text(json.dumps(sample_delta))

        reviewer = DeltaReviewer(delta_dir=temp_workspace / "deltas")
        result = reviewer.review_delta("test-delta-001")
        saved_path = reviewer.save_review(result)

        assert saved_path.exists()
        saved_data = json.loads(saved_path.read_text())
        assert saved_data["delta_id"] == "test-delta-001"
        assert "overall_score" in saved_data
        assert "criteria" in saved_data

    def test_move_to_approved(self, temp_workspace, sample_delta):
        """Test moving delta to approved"""
        pending_dir = temp_workspace / "deltas" / "pending"
        (pending_dir / "test-delta-001.json").write_text(json.dumps(sample_delta))

        reviewer = DeltaReviewer(delta_dir=temp_workspace / "deltas")
        approved_path = reviewer.move_to_approved("test-delta-001")

        assert approved_path.exists()
        assert not (pending_dir / "test-delta-001.json").exists()
        approved_data = json.loads(approved_path.read_text())
        assert approved_data["review_status"] == "approved"

    def test_move_to_rejected(self, temp_workspace, sample_delta):
        """Test moving delta to rejected"""
        pending_dir = temp_workspace / "deltas" / "pending"
        (pending_dir / "test-delta-001.json").write_text(json.dumps(sample_delta))

        reviewer = DeltaReviewer(delta_dir=temp_workspace / "deltas")
        rejected_path = reviewer.move_to_rejected("test-delta-001", "Quality too low")

        assert rejected_path.exists()
        assert not (pending_dir / "test-delta-001.json").exists()
        rejected_data = json.loads(rejected_path.read_text())
        assert rejected_data["review_status"] == "rejected"
        assert rejected_data["rejection_reason"] == "Quality too low"

    def test_review_and_process_auto_approve(
        self, temp_workspace, sample_delta, sample_lesson_content
    ):
        """Test review and process with auto-approve"""
        pending_dir = temp_workspace / "deltas" / "pending"
        (pending_dir / "test-delta-001.json").write_text(json.dumps(sample_delta))

        lesson_path = temp_workspace / "lessons" / "workflow" / "git-workflow.md"
        lesson_path.write_text(sample_lesson_content)

        # Set very low approve threshold to ensure approval
        reviewer = DeltaReviewer(
            delta_dir=temp_workspace / "deltas",
            lessons_dir=temp_workspace / "lessons",
            approve_threshold=0.3,
            auto_approve=True,
        )
        result = reviewer.review_and_process("test-delta-001")

        # Should be auto-approved if score >= 0.3
        if result.overall_score >= 0.3:
            assert result.auto_approved
            assert (
                temp_workspace / "deltas" / "approved" / "test-delta-001.json"
            ).exists()

    def test_batch_review(self, temp_workspace, sample_delta):
        """Test batch review of multiple deltas"""
        pending_dir = temp_workspace / "deltas" / "pending"
        for i in range(3):
            delta = {**sample_delta, "delta_id": f"delta-{i:03d}"}
            (pending_dir / f"delta-{i:03d}.json").write_text(json.dumps(delta))

        reviewer = DeltaReviewer(delta_dir=temp_workspace / "deltas")
        results = reviewer.batch_review()

        assert len(results) == 3
        for result in results:
            assert isinstance(result, ReviewResult)

    def test_get_status(self, temp_workspace, sample_delta):
        """Test getting status summary"""
        pending_dir = temp_workspace / "deltas" / "pending"
        approved_dir = temp_workspace / "deltas" / "approved"
        rejected_dir = temp_workspace / "deltas" / "rejected"

        # Create some test deltas
        (pending_dir / "pending-001.json").write_text(json.dumps(sample_delta))
        (pending_dir / "pending-002.json").write_text(json.dumps(sample_delta))
        (approved_dir / "approved-001.json").write_text(json.dumps(sample_delta))
        (rejected_dir / "rejected-001.json").write_text(json.dumps(sample_delta))

        reviewer = DeltaReviewer(delta_dir=temp_workspace / "deltas")
        status = reviewer.get_status()

        assert status["pending"] == 2
        assert status["approved"] == 1
        assert status["rejected"] == 1


class TestReviewCriteria:
    """Tests for review criteria"""

    def test_default_criteria_count(self):
        """Test that default criteria are defined"""
        assert len(DEFAULT_CRITERIA) >= 5

    def test_criteria_have_weights(self):
        """Test that all criteria have weights"""
        for criterion in DEFAULT_CRITERIA:
            assert criterion.weight > 0


class TestReviewDecision:
    """Tests for ReviewDecision enum"""

    def test_decision_values(self):
        """Test decision enum values"""
        assert ReviewDecision.APPROVE.value == "approve"
        assert ReviewDecision.REJECT.value == "reject"
        assert ReviewDecision.NEEDS_REVISION.value == "needs_revision"
        assert ReviewDecision.PENDING.value == "pending"


class TestReviewResult:
    """Tests for ReviewResult dataclass"""

    def test_create_result(self):
        """Test creating a review result"""
        result = ReviewResult(
            delta_id="test-001",
            reviewed_at="2026-02-05T08:00:00Z",
            reviewer="test_reviewer",
            decision=ReviewDecision.APPROVE,
            overall_score=0.85,
            criteria=[],
            summary="Test summary",
            suggestions=["Suggestion 1"],
            auto_approved=True,
        )
        assert result.delta_id == "test-001"
        assert result.decision == ReviewDecision.APPROVE
        assert result.auto_approved
