"""Tests for core LessonValidator behavior."""

import os
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

# A fully valid lesson for testing non-companion fields (version, target_grade, etc.).
# Intentionally has no companion link so tests that expect zero errors pass regardless
# of whether knowledge/lessons/test-lesson.md exists on disk.
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
- See also: some-other-resource
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


def test_description_field_not_warned():
    """description field is used by hybrid semantic matcher (gptme#2469); should not warn."""
    content = _MINIMAL_LESSON.format(
        extra='description: "Persist insights across sessions to prevent rediscovery"'
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        desc_warnings = [w for w in validator.warnings if "description" in w]
        assert (
            len(desc_warnings) == 0
        ), "description field is load-bearing for semantic matching and should not warn"


def test_metadata_field_not_warned():
    """metadata field (e.g. metadata.tags) is structural categorisation; should not warn."""
    content = _MINIMAL_LESSON.format(
        extra="metadata:\n  tags: [meta-learning, persistent-insight]"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        meta_warnings = [w for w in validator.warnings if "metadata" in w]
        assert (
            len(meta_warnings) == 0
        ), "metadata field is structural categorisation and should not warn"


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


# ---------------------------------------------------------------------------
# automation field tests
# ---------------------------------------------------------------------------


_AUTOMATION_BLOCK = """\
automation:
  status: automated
  validator: scripts/precommit/validators/validate_example.py
  enforcement: {enforcement}
  automated_date: 2026-04-19"""


def test_automation_field_accepted():
    """A well-formed automation mapping should be accepted silently."""
    content = _VALID_LESSON.format(
        extra=_AUTOMATION_BLOCK.format(enforcement="warning")
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        assert not validator.errors, f"Unexpected errors: {validator.errors}"
        automation_warnings = [w for w in validator.warnings if "automation" in w]
        assert (
            not automation_warnings
        ), f"Unexpected automation warnings: {automation_warnings}"


def test_automation_enforcement_error_accepted():
    """enforcement: error should also be valid."""
    content = _VALID_LESSON.format(extra=_AUTOMATION_BLOCK.format(enforcement="error"))
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        assert not validator.errors, f"Unexpected errors: {validator.errors}"


def test_automation_invalid_enforcement_rejected():
    """enforcement must be 'warning' or 'error'."""
    content = _VALID_LESSON.format(
        extra=_AUTOMATION_BLOCK.format(enforcement="critical")
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        automation_errors = [e for e in validator.errors if "automation" in e]
        assert (
            automation_errors
        ), "Invalid automation.enforcement should produce an error"


def test_automation_unknown_subfield_warns():
    """Unknown sub-fields under automation should produce a warning (not error)."""
    content = _VALID_LESSON.format(
        extra="automation:\n  status: automated\n  weirdfield: nope"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        assert not [
            e for e in validator.errors if "automation" in e
        ], f"Unknown sub-fields should warn, not error: {validator.errors}"
        warnings = [
            w for w in validator.warnings if "automation" in w and "weirdfield" in w
        ]
        assert warnings, "Unknown automation sub-fields should produce a warning"


def test_automation_must_be_mapping():
    """automation as a non-mapping (e.g. a string) should be an error."""
    content = _VALID_LESSON.format(extra='automation: "just a string"')
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        automation_errors = [e for e in validator.errors if "automation" in e]
        assert automation_errors, "Non-mapping automation value should produce an error"


def test_automation_coexists_with_flat_fields_warns():
    """Having both 'automation' block and flat automated_by/automated_date should warn."""
    content = _VALID_LESSON.format(
        extra="automation:\n  status: automated\nautomated_by: some-validator\nautomated_date: 2025-01-01"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        coexist_warnings = [
            w for w in validator.warnings if "automation" in w and "automated_by" in w
        ]
        assert (
            coexist_warnings
        ), "Coexisting 'automation' block and flat automated_by field should warn"


# confound_note field tests


def test_confound_note_string_accepted():
    """A non-empty confound_note string should be accepted without errors."""
    content = _VALID_LESSON.format(
        extra='confound_note: "corrective lesson — fires in higher-harm contexts"'
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        confound_errors = [e for e in validator.errors if "confound_note" in e]
        assert (
            not confound_errors
        ), f"Valid confound_note should not produce errors: {confound_errors}"


def test_confound_note_empty_string_rejected():
    """An empty confound_note string should produce an error."""
    content = _VALID_LESSON.format(extra='confound_note: ""')
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        confound_errors = [e for e in validator.errors if "confound_note" in e]
        assert confound_errors, "Empty confound_note should produce an error"


def test_confound_note_bool_rejected():
    """A boolean confound_note should produce an error."""
    content = _VALID_LESSON.format(extra="confound_note: true")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        confound_errors = [e for e in validator.errors if "confound_note" in e]
        assert confound_errors, "Boolean confound_note should produce an error"


def test_confound_note_wrong_type_rejected():
    """A non-string confound_note (e.g. integer) should produce an error."""
    content = _VALID_LESSON.format(extra="confound_note: 42")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        confound_errors = [e for e in validator.errors if "confound_note" in e]
        assert confound_errors, "Non-string confound_note should produce an error"


# An archived lesson that is long AND has no companion link — would normally
# trigger both the companion-doc and length soft warnings.
_ARCHIVED_LONG_LESSON = (
    """\
---
match:
  keywords:
    - "test keyword phrase"
status: archived
---

# Archived Test Lesson

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
"""
    + "\n".join(f"- Benefit {i}" for i in range(120))
    + "\n"
)


def test_archived_lesson_skips_length_warning():
    """Archived lessons are frozen — no length nag even when over target."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), _ARCHIVED_LONG_LESSON)
        validator = LessonValidator(path)
        validator.validate()
        length_warnings = [w for w in validator.warnings if "lines (target" in w]
        assert not length_warnings, f"Unexpected length warnings: {length_warnings}"


def test_archived_lesson_skips_companion_warning():
    """Archived lessons should not warn about missing/unlinked companion docs."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), _ARCHIVED_LONG_LESSON)
        validator = LessonValidator(path)
        validator.validate()
        companion_warnings = [w for w in validator.warnings if "companion" in w.lower()]
        assert (
            not companion_warnings
        ), f"Unexpected companion warnings: {companion_warnings}"


def test_active_long_lesson_still_warns():
    """Guard: the skip is archived-only — active lessons still get the length nag."""
    active = _ARCHIVED_LONG_LESSON.replace("status: archived", "status: active")
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), active)
        validator = LessonValidator(path)
        validator.validate()
        length_warnings = [w for w in validator.warnings if "lines (target" in w]
        assert length_warnings, "Active long lesson should still warn about length"


# A concise lesson (<100 lines) that links a companion doc that does not exist.
_LESSON_WITH_DEAD_COMPANION_LINK = """\
---
match:
  keywords:
    - "test keyword phrase"
status: active
---

# Test Lesson With Dead Companion Link

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

## Related
- Full context: [knowledge/lessons/test-lesson.md](../../knowledge/lessons/test-lesson.md)
"""


def test_dead_companion_link_raises_error():
    """A lesson that links a companion doc that doesn't exist should error."""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), _LESSON_WITH_DEAD_COMPANION_LINK)
        validator = LessonValidator(path)
        validator.validate()
        companion_errors = [e for e in validator.errors if "companion" in e.lower()]
        assert (
            companion_errors
        ), "Lesson linking a nonexistent companion doc should raise an error"


def test_dead_companion_link_archived_skips():
    """Archived lessons skip companion checks, so dead links are not flagged."""
    content = _LESSON_WITH_DEAD_COMPANION_LINK.replace(
        "status: active", "status: archived"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_lesson(Path(tmp), content)
        validator = LessonValidator(path)
        validator.validate()
        companion_errors = [e for e in validator.errors if "companion" in e.lower()]
        assert (
            not companion_errors
        ), f"Archived lesson should not flag dead companion link: {companion_errors}"


# A concise lesson with no companion link at all — used to check that the
# suggested companion path mirrors the lesson's category subdir.
_LESSON_WITHOUT_COMPANION_LINK = _LESSON_WITH_DEAD_COMPANION_LINK.replace(
    "- Full context: [knowledge/lessons/test-lesson.md](../../knowledge/lessons/test-lesson.md)",
    "- Some other reference",
)


def test_existing_companion_suggestion_names_real_subdir_path():
    """The 'exists but not linked' warning must name where the companion ACTUALLY is.

    Companions live under a category subdir (knowledge/lessons/<category>/<stem>.md).
    Suggesting a flat knowledge/lessons/<stem>.md sends the author to a path that
    does not exist, and the resulting link fails the markdown-link check.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "knowledge" / "lessons" / "monitoring").mkdir(parents=True)
        (root / "knowledge" / "lessons" / "monitoring" / "test-lesson.md").write_text(
            "# companion\n"
        )
        lesson_dir = root / "lessons" / "monitoring"
        lesson_dir.mkdir(parents=True)
        path = _write_lesson(lesson_dir, _LESSON_WITHOUT_COMPANION_LINK)

        cwd = os.getcwd()
        try:
            os.chdir(root)
            validator = LessonValidator(path)
            validator.validate()
        finally:
            os.chdir(cwd)

        warnings = [w for w in validator.warnings if "companion" in w.lower()]
        assert warnings, "Existing-but-unlinked companion should warn"
        assert "knowledge/lessons/monitoring/test-lesson.md" in warnings[0], warnings[0]


def test_missing_companion_suggestion_mirrors_category_subdir():
    """With no companion on disk, the suggested path still mirrors the category."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "knowledge" / "lessons").mkdir(parents=True)
        lesson_dir = root / "lessons" / "monitoring"
        lesson_dir.mkdir(parents=True)
        path = _write_lesson(lesson_dir, _LESSON_WITH_DEAD_COMPANION_LINK)

        cwd = os.getcwd()
        try:
            os.chdir(root)
            validator = LessonValidator(path)
            validator.validate()
        finally:
            os.chdir(cwd)

        errors = [e for e in validator.errors if "companion" in e.lower()]
        assert errors, "Dead companion link should error"
        assert "knowledge/lessons/monitoring/test-lesson.md" in errors[0], errors[0]


def test_cross_category_companion_not_counted():
    """A companion in a different category must not be treated as this lesson's companion.

    When two lessons share the same stem but live in different category subdirs,
    the validator must not suggest (or treat as linked) a companion from the
    other category — doing so would point the author at the wrong document.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Only a cross-category companion exists (e.g. for lessons/workflow/test-lesson.md)
        (root / "knowledge" / "lessons" / "workflow").mkdir(parents=True)
        (root / "knowledge" / "lessons" / "workflow" / "test-lesson.md").write_text(
            "# companion for a different lesson\n"
        )
        # Our lesson is in the 'monitoring' category — no companion there
        lesson_dir = root / "lessons" / "monitoring"
        lesson_dir.mkdir(parents=True)
        path = _write_lesson(lesson_dir, _LESSON_WITHOUT_COMPANION_LINK)

        cwd = os.getcwd()
        try:
            os.chdir(root)
            validator = LessonValidator(path)
            validator.validate()
        finally:
            os.chdir(cwd)

        # The monitoring lesson has no companion — the workflow companion must be ignored.
        companion_warnings = [w for w in validator.warnings if "companion" in w.lower()]
        # Should NOT warn "exists but not linked" (the cross-category file is not ours)
        for w in companion_warnings:
            assert "workflow" not in w, (
                "Must not suggest cross-category companion: " + w
            )
        # The suggested path should mirror the lesson's own category (monitoring), not workflow
        # (we can only check this via the dead-link path; use the link-bearing version)
        path2 = _write_lesson(lesson_dir, _LESSON_WITH_DEAD_COMPANION_LINK)
        try:
            os.chdir(root)
            validator2 = LessonValidator(path2)
            validator2.validate()
        finally:
            os.chdir(cwd)
        errors = [e for e in validator2.errors if "companion" in e.lower()]
        assert errors, "Dead companion link should still error"
        assert "knowledge/lessons/monitoring/test-lesson.md" in errors[0], errors[0]
        assert "workflow" not in errors[0], (
            "Error must not reference cross-category companion: " + errors[0]
        )


def test_cross_category_link_to_existing_companion_not_dead():
    """A link that resolves to an existing companion must not be flagged as dead.

    If a lesson in lessons/monitoring explicitly links
    knowledge/lessons/workflow/test-lesson.md and that file exists, the
    validator must not raise a dead-link error — even though there is no
    monitoring-category companion.  The previous behavior (has_companion based
    on any stem match) was correct for this case; the fix must preserve it while
    still catching genuinely broken links.
    """
    # Craft a lesson that links to a cross-category companion that exists.
    cross_cat_link_lesson = _LESSON_WITH_DEAD_COMPANION_LINK.replace(
        "../../knowledge/lessons/test-lesson.md",
        "../../knowledge/lessons/workflow/test-lesson.md",
    ).replace(
        "[knowledge/lessons/test-lesson.md]",
        "[knowledge/lessons/workflow/test-lesson.md]",
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Create the cross-category companion that the lesson links to.
        (root / "knowledge" / "lessons" / "workflow").mkdir(parents=True)
        (root / "knowledge" / "lessons" / "workflow" / "test-lesson.md").write_text(
            "# cross-category companion\n"
        )
        lesson_dir = root / "lessons" / "monitoring"
        lesson_dir.mkdir(parents=True)
        path = _write_lesson(lesson_dir, cross_cat_link_lesson)

        cwd = os.getcwd()
        try:
            os.chdir(root)
            validator = LessonValidator(path)
            validator.validate()
        finally:
            os.chdir(cwd)

        companion_errors = [e for e in validator.errors if "companion" in e.lower()]
        assert not companion_errors, (
            "Lesson linking an existing cross-category companion must not be "
            f"flagged as dead: {companion_errors}"
        )


def test_missing_companion_suggestion_mirrors_full_nested_category():
    """Suggested companion path must use the full category path for nested lessons.

    For lessons/foo/bar/lesson.md the validator must suggest
    knowledge/lessons/foo/bar/lesson.md, not knowledge/lessons/bar/lesson.md
    (which would be produced by using only .parent.name).
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "knowledge" / "lessons").mkdir(parents=True)
        # Lesson is nested two levels deep: lessons/outer/inner/test-lesson.md
        lesson_dir = root / "lessons" / "outer" / "inner"
        lesson_dir.mkdir(parents=True)
        path = _write_lesson(lesson_dir, _LESSON_WITH_DEAD_COMPANION_LINK)

        cwd = os.getcwd()
        try:
            os.chdir(root)
            validator = LessonValidator(path)
            validator.validate()
        finally:
            os.chdir(cwd)

        errors = [e for e in validator.errors if "companion" in e.lower()]
        assert errors, "Dead companion link should error for nested lesson"
        # Must suggest the full nested path, not just the immediate parent
        assert (
            "knowledge/lessons/outer/inner/test-lesson.md" in errors[0]
        ), f"Expected full nested path in suggestion, got: {errors[0]}"
        assert (
            "knowledge/lessons/inner/test-lesson.md" not in errors[0]
        ), f"Must not use only immediate parent name: {errors[0]}"


def test_nested_companion_link_detected_as_live():
    """A link to a multi-level companion path must be detected as live.

    For lessons/outer/inner/test-lesson.md with companion at
    knowledge/lessons/outer/inner/test-lesson.md, a Related link to
    knowledge/lessons/outer/inner/test-lesson.md must NOT trigger
    "Companion doc exists but not linked" — the link IS present.

    The detection regex must allow more than one subdir component.
    """
    # Lesson linking to its own two-level companion path
    nested_link_lesson = _LESSON_WITH_DEAD_COMPANION_LINK.replace(
        "../../knowledge/lessons/test-lesson.md",
        "../../knowledge/lessons/outer/inner/test-lesson.md",
    ).replace(
        "[knowledge/lessons/test-lesson.md]",
        "[knowledge/lessons/outer/inner/test-lesson.md]",
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Create the own-category companion that the lesson links to
        (root / "knowledge" / "lessons" / "outer" / "inner").mkdir(parents=True)
        companion = (
            root / "knowledge" / "lessons" / "outer" / "inner" / "test-lesson.md"
        )
        companion.write_text("# companion doc\n")
        lesson_dir = root / "lessons" / "outer" / "inner"
        lesson_dir.mkdir(parents=True)
        path = _write_lesson(lesson_dir, nested_link_lesson)

        cwd = os.getcwd()
        try:
            os.chdir(root)
            validator = LessonValidator(path)
            validator.validate()
        finally:
            os.chdir(cwd)

        companion_errors = [e for e in validator.errors if "companion" in e.lower()]
        companion_warnings = [w for w in validator.warnings if "companion" in w.lower()]
        assert not companion_errors, (
            "Lesson with a live nested-companion link must not have companion errors: "
            f"{companion_errors}"
        )
        assert not companion_warnings, (
            "Lesson with a live nested-companion link must not warn 'exists but not linked': "
            f"{companion_warnings}"
        )


def test_prose_mention_of_existing_cross_category_does_not_mask_dead_related_link():
    """A prose mention of an existing cross-category companion must not silence the
    dead-link error when the Related section links a non-existent own-category path.

    P1 regression: re.search found the cross-category mention first (file exists →
    linked_companion_exists=True), so the dead Related link was never caught.
    With re.finditer the validator checks ALL mentions; any non-existent path triggers
    the error regardless of what earlier mentions resolve to.
    """
    # Lesson body references an *existing* cross-category companion in prose, but
    # the Related section links to a non-existent monitoring companion.
    lesson_with_prose_mention = (
        "---\n"
        "description: Test lesson\n"
        "status: active\n"
        "---\n"
        "\n"
        "## Rule\n"
        "Test rule.\n"
        "\n"
        "## Context\n"
        "See also knowledge/lessons/workflow/test-lesson.md for the workflow variant.\n"
        "\n"
        "## Detection\n"
        "- signal one\n"
        "- signal two (three four five six)\n"
        "\n"
        "## Pattern\n"
        "```\npattern\n```\n"
        "\n"
        "## Outcome\n"
        "Good things happen.\n"
        "\n"
        "## Related\n"
        "- Full context: [knowledge/lessons/monitoring/test-lesson.md]"
        "(../../knowledge/lessons/monitoring/test-lesson.md)\n"
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # The cross-category companion EXISTS (workflow), the own-category does NOT.
        (root / "knowledge" / "lessons" / "workflow").mkdir(parents=True)
        (root / "knowledge" / "lessons" / "workflow" / "test-lesson.md").write_text(
            "# workflow companion\n"
        )
        # No file at knowledge/lessons/monitoring/test-lesson.md
        (root / "knowledge" / "lessons" / "monitoring").mkdir(parents=True)

        lesson_dir = root / "lessons" / "monitoring"
        lesson_dir.mkdir(parents=True)
        path = _write_lesson(lesson_dir, lesson_with_prose_mention)

        cwd = os.getcwd()
        try:
            os.chdir(root)
            validator = LessonValidator(path)
            validator.validate()
        finally:
            os.chdir(cwd)

        companion_errors = [e for e in validator.errors if "companion" in e.lower()]
        assert companion_errors, (
            "Dead Related link must be flagged even when an earlier prose mention "
            "of a cross-category companion exists on disk"
        )


def test_cross_category_link_does_not_silence_own_companion_unlinked_warning():
    """A cross-category companion link must not suppress the 'exists but not linked'
    warning when the lesson's own-category companion exists but is unlinked.

    P2 regression: has_companion_link was True for ANY companion mention, so a
    cross-category link silenced the warning even though the own companion was not
    specifically referenced.  own_companion_linked now checks the exact path.
    """
    # Lesson links to a cross-category companion (workflow) that exists, but its
    # own monitoring companion also exists and is NOT linked.
    cross_cat_link_lesson = (
        "---\n"
        "description: Test lesson\n"
        "status: active\n"
        "---\n"
        "\n"
        "## Rule\n"
        "Test rule.\n"
        "\n"
        "## Context\n"
        "Context text.\n"
        "\n"
        "## Detection\n"
        "- signal one\n"
        "- signal two (three four five six)\n"
        "\n"
        "## Pattern\n"
        "```\npattern\n```\n"
        "\n"
        "## Outcome\n"
        "Good things happen.\n"
        "\n"
        "## Related\n"
        "- Workflow variant: [knowledge/lessons/workflow/test-lesson.md]"
        "(../../knowledge/lessons/workflow/test-lesson.md)\n"
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Both companions exist: own-category (monitoring) and cross-category (workflow)
        (root / "knowledge" / "lessons" / "monitoring").mkdir(parents=True)
        (root / "knowledge" / "lessons" / "monitoring" / "test-lesson.md").write_text(
            "# monitoring companion\n"
        )
        (root / "knowledge" / "lessons" / "workflow").mkdir(parents=True)
        (root / "knowledge" / "lessons" / "workflow" / "test-lesson.md").write_text(
            "# workflow companion\n"
        )
        lesson_dir = root / "lessons" / "monitoring"
        lesson_dir.mkdir(parents=True)
        path = _write_lesson(lesson_dir, cross_cat_link_lesson)

        cwd = os.getcwd()
        try:
            os.chdir(root)
            validator = LessonValidator(path)
            validator.validate()
        finally:
            os.chdir(cwd)

        companion_warnings = [w for w in validator.warnings if "companion" in w.lower()]
        assert companion_warnings, (
            "Own-category companion must still be flagged as 'exists but not linked' "
            "even when the lesson links a cross-category companion"
        )
        assert "monitoring/test-lesson.md" in companion_warnings[0], companion_warnings[
            0
        ]


def test_own_companion_linked_but_dead_prose_mention_reports_dead_path():
    """When a lesson's own companion is correctly linked but the content also contains
    a dead companion path mention (e.g. prose cross-category reference), the error
    should name the dead path rather than telling the author to update the correct link.

    P2 regression (edaae34c): the 'Update the Related link' message fired even when
    own_companion_linked was True — the own companion was already correctly referenced,
    so the instruction to update/remove it was misleading. The fix reports the
    specific dead path(s) and says 'remove or fix', preserving the correct link.
    """
    lesson_content = (
        "---\n"
        "description: Test lesson\n"
        "status: active\n"
        "---\n"
        "\n"
        "## Rule\n"
        "Test rule.\n"
        "\n"
        "## Context\n"
        "Context text.\n"
        "\n"
        "## Detection\n"
        "- signal one\n"
        "- signal two (three four five six)\n"
        "\n"
        "## Pattern\n"
        "```\npattern\n```\n"
        "\n"
        "## Outcome\n"
        "Good things happen.\n"
        "\n"
        "## Related\n"
        # Own companion correctly linked
        "- Companion doc: [knowledge/lessons/monitoring/test-lesson.md]"
        "(../../knowledge/lessons/monitoring/test-lesson.md)\n"
        # Dead prose mention of a non-existent cross-category path
        "- See also knowledge/lessons/workflow/test-lesson.md for the workflow variant.\n"
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Own companion exists; cross-category file does NOT
        (root / "knowledge" / "lessons" / "monitoring").mkdir(parents=True)
        (root / "knowledge" / "lessons" / "monitoring" / "test-lesson.md").write_text(
            "# monitoring companion\n"
        )
        lesson_dir = root / "lessons" / "monitoring"
        lesson_dir.mkdir(parents=True)
        path = _write_lesson(lesson_dir, lesson_content)

        cwd = os.getcwd()
        try:
            os.chdir(root)
            validator = LessonValidator(path)
            validator.validate()
        finally:
            os.chdir(cwd)

    # Must flag the dead path
    assert validator.errors, "Expected an error for the dead prose companion mention"
    error = validator.errors[0]
    assert "knowledge/lessons/workflow/test-lesson.md" in error, (
        "Error should name the specific dead path, not tell the author to update "
        f"the correct Related link. Got: {error!r}"
    )
    assert "remove or fix" in error.lower(), (
        f"Error should say 'remove or fix the dead reference', got: {error!r}"
    )
    # Must NOT tell the author to update/remove the correct own-companion link
    assert "update the related link" not in error.lower(), (
        "Error must not instruct author to change the correctly-linked own companion. "
        f"Got: {error!r}"
    )
