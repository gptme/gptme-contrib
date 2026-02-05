#!/usr/bin/env python3
"""Tests for ACE Applier module"""

import json
import tempfile
from pathlib import Path

import pytest

from gptme_ace.applier import DeltaApplier, ApplierError
from gptme_ace.curator import DeltaOperation


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace with lessons and deltas"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create lessons directory
        lessons_dir = tmpdir / "lessons" / "workflow"
        lessons_dir.mkdir(parents=True)

        # Create a test lesson
        lesson_content = """---
match:
  keywords:
    - test keyword
status: active
---

# Test Lesson

## Rule
This is the rule section.

## Context
This is the context section.

## Pattern
```bash
echo "hello"
```

## Outcome
This is the outcome section.
"""
        (lessons_dir / "test-lesson.md").write_text(lesson_content)

        # Create deltas directory
        delta_dir = tmpdir / "deltas"
        for status in ["pending", "approved", "applied"]:
            (delta_dir / status).mkdir(parents=True)

        yield {
            "root": tmpdir,
            "lessons_dir": tmpdir / "lessons",
            "delta_dir": delta_dir,
            "lesson_path": lessons_dir / "test-lesson.md",
        }


class TestDeltaApplier:
    """Tests for DeltaApplier class"""

    def test_init(self, temp_workspace):
        """Test applier initialization"""
        applier = DeltaApplier(
            lessons_dir=temp_workspace["lessons_dir"],
            delta_dir=temp_workspace["delta_dir"],
        )
        assert applier.lessons_dir == temp_workspace["lessons_dir"]
        assert applier.delta_dir == temp_workspace["delta_dir"]
        assert not applier.dry_run

    def test_init_dry_run(self, temp_workspace):
        """Test applier with dry run mode"""
        applier = DeltaApplier(
            lessons_dir=temp_workspace["lessons_dir"],
            delta_dir=temp_workspace["delta_dir"],
            dry_run=True,
        )
        assert applier.dry_run

    def test_find_lesson_file(self, temp_workspace):
        """Test finding lesson files"""
        applier = DeltaApplier(
            lessons_dir=temp_workspace["lessons_dir"],
            delta_dir=temp_workspace["delta_dir"],
        )

        # Find by full path
        found = applier.find_lesson_file("workflow/test-lesson")
        assert found == temp_workspace["lesson_path"]

        # Find by name only
        found = applier.find_lesson_file("test-lesson")
        assert found == temp_workspace["lesson_path"]

        # Not found
        found = applier.find_lesson_file("nonexistent")
        assert found is None

    def test_load_delta_from_approved(self, temp_workspace):
        """Test loading delta from approved directory"""
        # Create an approved delta
        delta_dict = {
            "delta_id": "test-delta-123",
            "created": "2026-02-05T07:00:00Z",
            "source": "ace_curator",
            "source_insights": ["insight-1"],
            "lesson_id": "workflow/test-lesson",
            "operations": [
                {
                    "type": "add",
                    "section": "Outcome",
                    "content": "Additional outcome text.",
                    "position": "append",
                    "target": None,
                }
            ],
            "rationale": "Test rationale",
            "review_status": "approved",
        }

        approved_dir = temp_workspace["delta_dir"] / "approved"
        (approved_dir / "test-delta-123.json").write_text(json.dumps(delta_dict))

        applier = DeltaApplier(
            lessons_dir=temp_workspace["lessons_dir"],
            delta_dir=temp_workspace["delta_dir"],
        )

        delta = applier.load_delta("test-delta-123")
        assert delta.delta_id == "test-delta-123"
        assert delta.lesson_id == "workflow/test-lesson"
        assert len(delta.operations) == 1
        assert delta.operations[0].type == "add"

    def test_load_delta_pending_raises_error(self, temp_workspace):
        """Test that loading pending delta raises helpful error"""
        # Create a pending delta
        delta_dict = {
            "delta_id": "pending-delta",
            "created": "2026-02-05T07:00:00Z",
            "source": "ace_curator",
            "source_insights": [],
            "lesson_id": "test",
            "operations": [],
            "rationale": "Test",
            "review_status": "pending",
        }

        pending_dir = temp_workspace["delta_dir"] / "pending"
        (pending_dir / "pending-delta.json").write_text(json.dumps(delta_dict))

        applier = DeltaApplier(
            lessons_dir=temp_workspace["lessons_dir"],
            delta_dir=temp_workspace["delta_dir"],
        )

        with pytest.raises(ApplierError, match="still pending approval"):
            applier.load_delta("pending-delta")

    def test_apply_add_operation(self, temp_workspace):
        """Test applying ADD operation to lesson"""
        # Create approved delta with ADD operation
        delta_dict = {
            "delta_id": "add-delta",
            "created": "2026-02-05T07:00:00Z",
            "source": "ace_curator",
            "source_insights": ["insight-1"],
            "lesson_id": "workflow/test-lesson",
            "operations": [
                {
                    "type": "add",
                    "section": "Outcome",
                    "content": "\nThis is new outcome content.",
                    "position": "append",
                    "target": None,
                }
            ],
            "rationale": "Adding outcome content",
            "review_status": "approved",
        }

        (temp_workspace["delta_dir"] / "approved" / "add-delta.json").write_text(
            json.dumps(delta_dict)
        )

        applier = DeltaApplier(
            lessons_dir=temp_workspace["lessons_dir"],
            delta_dir=temp_workspace["delta_dir"],
        )

        delta = applier.load_delta("add-delta")
        result = applier.apply_delta(delta)

        assert result["operations_applied"] == 1
        assert result["operations_failed"] == 0
        assert not result["errors"]
        assert result["file_modified"]

        # Verify content was added
        content = temp_workspace["lesson_path"].read_text()
        assert "This is new outcome content." in content

        # Verify delta was archived
        assert not (
            temp_workspace["delta_dir"] / "approved" / "add-delta.json"
        ).exists()
        assert (temp_workspace["delta_dir"] / "applied" / "add-delta.json").exists()

    def test_apply_dry_run(self, temp_workspace):
        """Test dry run mode doesn't modify files"""
        original_content = temp_workspace["lesson_path"].read_text()

        # Create approved delta
        delta_dict = {
            "delta_id": "dry-run-delta",
            "created": "2026-02-05T07:00:00Z",
            "source": "ace_curator",
            "source_insights": [],
            "lesson_id": "workflow/test-lesson",
            "operations": [
                {
                    "type": "add",
                    "section": "Outcome",
                    "content": "\nDry run content.",
                    "position": "append",
                    "target": None,
                }
            ],
            "rationale": "Test",
            "review_status": "approved",
        }

        (temp_workspace["delta_dir"] / "approved" / "dry-run-delta.json").write_text(
            json.dumps(delta_dict)
        )

        applier = DeltaApplier(
            lessons_dir=temp_workspace["lessons_dir"],
            delta_dir=temp_workspace["delta_dir"],
            dry_run=True,
        )

        delta = applier.load_delta("dry-run-delta")
        result = applier.apply_delta(delta)

        assert result["dry_run"] is True
        assert result["would_modify"] is True
        assert "diff_preview" in result

        # Verify file was NOT modified
        assert temp_workspace["lesson_path"].read_text() == original_content

        # Verify delta was NOT archived (still in approved)
        assert (
            temp_workspace["delta_dir"] / "approved" / "dry-run-delta.json"
        ).exists()

    def test_list_approved_deltas(self, temp_workspace):
        """Test listing approved deltas"""
        # Create multiple approved deltas
        for i in range(3):
            delta_dict = {
                "delta_id": f"delta-{i}",
                "created": "2026-02-05T07:00:00Z",
                "source": "ace_curator",
                "source_insights": [],
                "lesson_id": f"lesson-{i}",
                "operations": [],
                "rationale": f"Test {i}",
                "review_status": "approved",
            }
            (temp_workspace["delta_dir"] / "approved" / f"delta-{i}.json").write_text(
                json.dumps(delta_dict)
            )

        applier = DeltaApplier(
            lessons_dir=temp_workspace["lessons_dir"],
            delta_dir=temp_workspace["delta_dir"],
        )

        deltas = applier.list_approved_deltas()
        assert len(deltas) == 3
        assert {d.delta_id for d in deltas} == {"delta-0", "delta-1", "delta-2"}


class TestDeltaOperations:
    """Tests for individual delta operations"""

    def test_add_creates_new_section(self, temp_workspace):
        """Test ADD creates new section if it doesn't exist"""
        applier = DeltaApplier(
            lessons_dir=temp_workspace["lessons_dir"],
            delta_dir=temp_workspace["delta_dir"],
        )

        op = DeltaOperation(
            type="add",
            section="NewSection",
            content="New section content",
            position="append",
        )

        content = temp_workspace["lesson_path"].read_text()
        new_content = applier._apply_operation(content, op, "test-lesson")

        assert "## NewSection" in new_content
        assert "New section content" in new_content

    def test_remove_by_text(self, temp_workspace):
        """Test REMOVE operation by exact text match"""
        applier = DeltaApplier(
            lessons_dir=temp_workspace["lessons_dir"],
            delta_dir=temp_workspace["delta_dir"],
        )

        op = DeltaOperation(
            type="remove",
            section="Rule",
            target={"text": "This is the rule section."},
        )

        content = temp_workspace["lesson_path"].read_text()
        new_content = applier._apply_operation(content, op, "test-lesson")

        assert "This is the rule section." not in new_content

    def test_modify_by_text(self, temp_workspace):
        """Test MODIFY operation by text replacement"""
        applier = DeltaApplier(
            lessons_dir=temp_workspace["lessons_dir"],
            delta_dir=temp_workspace["delta_dir"],
        )

        op = DeltaOperation(
            type="modify",
            section="Rule",
            target={"text": "This is the rule section."},
            content="This is the MODIFIED rule section.",
        )

        content = temp_workspace["lesson_path"].read_text()
        new_content = applier._apply_operation(content, op, "test-lesson")

        assert "This is the MODIFIED rule section." in new_content
        assert "This is the rule section." not in new_content
