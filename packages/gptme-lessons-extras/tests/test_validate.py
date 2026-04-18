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

# A fully valid two-file-format lesson (includes Related section).
_VALID_LESSON = """\
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
- Signal 2

## Pattern
```txt
example
```

## Outcome
- Benefit 1

## Related
- Full context: knowledge/lessons/test-lesson.md
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


# ---------------------------------------------------------------------------
# version field tests (Issue #614)
# ---------------------------------------------------------------------------


def test_version_int_accepted():
    """version as a positive integer should be accepted without errors/warnings."""
    content = _VALID_LESSON.format(extra="version: 2")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        assert not validator.errors, f"Unexpected errors: {validator.errors}"
        version_warnings = [w for w in validator.warnings if "version" in w]
        assert not version_warnings, f"Unexpected version warnings: {version_warnings}"


def test_version_semver_string_accepted():
    """version as a semver-style string should be accepted without errors."""
    content = _VALID_LESSON.format(extra='version: "2.1.0"')
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        assert not validator.errors, f"Unexpected errors: {validator.errors}"


def test_version_descriptive_tag_accepted():
    """version as a descriptive tag string should be accepted without errors."""
    content = _VALID_LESSON.format(extra='version: "v2-compact-primary"')
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        assert not validator.errors, f"Unexpected errors: {validator.errors}"


def test_version_zero_rejected():
    """version: 0 (non-positive int) should produce an error."""
    content = _MINIMAL_LESSON.format(extra="version: 0")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        version_errors = [e for e in validator.errors if "version" in e]
        assert version_errors, "version: 0 should produce an error"


def test_version_negative_rejected():
    """version: -1 (negative int) should produce an error."""
    content = _MINIMAL_LESSON.format(extra="version: -1")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        version_errors = [e for e in validator.errors if "version" in e]
        assert version_errors, "Negative version should produce an error"


def test_version_empty_string_rejected():
    """Empty version string should produce an error."""
    content = _MINIMAL_LESSON.format(extra='version: ""')
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        version_errors = [e for e in validator.errors if "version" in e]
        assert version_errors, "Empty version string should produce an error"


def test_version_wrong_type_rejected():
    """version as a list should produce an error."""
    content = _MINIMAL_LESSON.format(extra="version:\n  - a\n  - b")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        version_errors = [e for e in validator.errors if "version" in e]
        assert version_errors, "version as a list should produce an error"


def test_version_bool_rejected():
    """version: true (YAML bool, Python bool subclasses int) should be rejected."""
    content = _MINIMAL_LESSON.format(extra="version: true")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        version_errors = [e for e in validator.errors if "version" in e]
        assert version_errors, "version: true (bool) should produce an error"


def test_target_grade_single_dim_accepted():
    """target_grade as a single known dimension should be accepted."""
    content = _VALID_LESSON.format(extra="target_grade: harm")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        assert not validator.errors, f"Unexpected errors: {validator.errors}"
        target_warnings = [w for w in validator.warnings if "target_grade" in w]
        assert (
            not target_warnings
        ), f"Unexpected target_grade warnings: {target_warnings}"


def test_target_grade_list_accepted():
    """target_grade as a list of known dimensions should be accepted."""
    content = _VALID_LESSON.format(extra='target_grade: ["harm", "alignment"]')
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        assert not validator.errors, f"Unexpected errors: {validator.errors}"


def test_target_grade_unknown_dim_rejected():
    """Unknown target_grade dimensions should produce an error."""
    content = _VALID_LESSON.format(extra="target_grade: craftsmanship")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        target_errors = [e for e in validator.errors if "target_grade" in e]
        assert target_errors, "Unknown target_grade dims should produce an error"


def test_target_grade_non_string_list_item_rejected():
    """List values must all be non-empty strings."""
    content = _VALID_LESSON.format(extra='target_grade: ["harm", 3]')
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        target_errors = [e for e in validator.errors if "target_grade" in e]
        assert target_errors, "Non-string target_grade entries should produce an error"
