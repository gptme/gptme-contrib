"""Tests for core LessonValidator behavior."""

import tempfile
from pathlib import Path

from gptme_lessons_extras.validate import LessonValidator


def _write_lesson(tmp: Path, content: str) -> Path:
    """Write a lesson file and return its path."""
    p = tmp / "test-lesson.md"
    p.write_text(content)
    return p


_MINIMAL_LESSON = """\
---
match:
  keywords:
    - "test keyword phrase"
status: active
{extra}
---

# Test Lesson

## Rule
Test rule.

## Context
Test context.

## Detection
- Signal 1

## Pattern
```txt
example
```

## Outcome
- Benefit 1
"""


def test_unknown_field_still_warned():
    """Fields not in allowed_fields should produce a warning."""
    content = _MINIMAL_LESSON.format(extra="bogus_field: true")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        bogus_warnings = [w for w in validator.warnings if "bogus_field" in w]
        assert len(bogus_warnings) > 0, "Unknown fields should produce warnings"


def test_confidence_field_now_warned():
    """confidence field should produce a warning after revert of #535."""
    content = _MINIMAL_LESSON.format(extra="confidence:\n  score: 0.5")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        confidence_warnings = [w for w in validator.warnings if "confidence" in w]
        assert (
            len(confidence_warnings) > 0
        ), "confidence field should produce a warning (store scores in state files, not frontmatter)"


def test_version_field_allowed():
    """version field should not produce a warning — it's an approved frontmatter field."""
    content = _MINIMAL_LESSON.format(extra="version: 2")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        version_warnings = [w for w in validator.warnings if "version" in w]
        assert (
            len(version_warnings) == 0
        ), "version field should be allowed without warnings"


def test_version_field_must_be_int():
    """version field should warn if not an integer."""
    content = _MINIMAL_LESSON.format(extra='version: "1.0"')
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        version_warnings = [w for w in validator.warnings if "version" in w]
        assert len(version_warnings) > 0, "non-integer version should produce a warning"
