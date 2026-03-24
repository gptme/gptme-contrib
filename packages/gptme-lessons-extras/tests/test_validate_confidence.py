"""Test that the confidence field is accepted in lesson frontmatter."""

import tempfile
from pathlib import Path

from gptme_lessons_extras.validate import LessonValidator


def _write_lesson(tmp: Path, content: str) -> Path:
    """Write a lesson file and return its path."""
    p = tmp / "test-lesson.md"
    p.write_text(content)
    return p


def test_confidence_field_accepted():
    """confidence block in frontmatter should not produce warnings."""
    content = """\
---
match:
  keywords:
    - "test keyword phrase"
status: active
confidence:
  score: 0.112
  action: promote
  evidence: 0.91
  updated: 2026-03-24
---

# Test Lesson

## Rule
Test rule.

## Context
Test context.

## Detection
- Signal 1

## Pattern
```text
example
```

## Outcome
- Benefit 1
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        # No warnings about confidence being an extra field
        confidence_warnings = [
            w for w in validator.warnings if "confidence" in w.lower()
        ]
        assert (
            confidence_warnings == []
        ), f"confidence field should be allowed, got warnings: {confidence_warnings}"


def test_unknown_field_still_warned():
    """Fields not in allowed_fields should still produce warnings."""
    content = """\
---
match:
  keywords:
    - "test keyword phrase"
status: active
bogus_field: true
---

# Test Lesson

## Rule
Test rule.

## Context
Test context.

## Detection
- Signal 1

## Pattern
```text
example
```

## Outcome
- Benefit 1
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        bogus_warnings = [w for w in validator.warnings if "bogus_field" in w]
        assert len(bogus_warnings) > 0, "Unknown fields should still produce warnings"
