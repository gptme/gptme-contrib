"""Regressions for LessonValidator frontmatter/section parsing (bug hunt 2026-11-24)."""

from pathlib import Path

import pytest
from gptme_lessons_extras.validate import LessonValidator, validate_directory

ORIGINAL_BODY = """
# Example lesson

## Rule
Do the thing.

## Context
When doing things.

## Failure Signals
- signal one
- signal two

## Anti-pattern (concise)
Bad.

## Recommended Pattern
Good.

## Fix Recipe
1. Fix it.

## Rationale
Because.

## Verification Checklist
- [ ] Checked

## Exceptions
None.

## Automation Hooks
- pre-commit check

## Origin
Session.

## Related
- none
"""


def test_triple_dash_inside_keyword_value_is_valid(tmp_path: Path):
    lesson = tmp_path / "lessons" / "git" / "diff-headers.md"
    lesson.parent.mkdir(parents=True)
    # A lesson about unified diffs: the keyword legitimately contains '---'.
    lesson.write_text(
        '---\nmatch:\n  keywords:\n    - "--- a/ header missing"\nstatus: active\n---\n'
        + ORIGINAL_BODY
    )
    v = LessonValidator(lesson)
    assert v.validate(), v.errors
    assert v.status == "active"


def test_failure_signals_under_subheading_are_counted(tmp_path: Path):
    body = ORIGINAL_BODY.replace(
        "## Failure Signals\n- signal one\n- signal two\n",
        "## Failure Signals\n### Runtime\n- signal one\n- signal two\n"
        "### Review\n- signal three\n",
    ).replace(
        "## Verification Checklist\n- [ ] Checked\n",
        "## Verification Checklist\n### Before commit\n- [ ] Checked\n",
    )
    lesson = tmp_path / "lessons" / "workflow" / "x.md"
    lesson.parent.mkdir(parents=True)
    lesson.write_text("---\nmatch:\n  keywords: [x]\n---\n" + body)
    v = LessonValidator(lesson)
    assert v.validate(), v.errors


@pytest.mark.parametrize("fm", ["- keyword one\n- keyword two\n", "just some text\n"])
def test_nonmapping_frontmatter_reported_not_crash(tmp_path: Path, fm: str):
    lesson = tmp_path / "lessons" / "workflow" / "x.md"
    lesson.parent.mkdir(parents=True)
    lesson.write_text("---\n" + fm + "---\n" + ORIGINAL_BODY)
    v = LessonValidator(lesson)
    assert v.validate() is False  # must not raise AttributeError
    assert any("frontmatter" in e.lower() for e in v.errors)


def test_checkout_under_templates_dir_still_validated(tmp_path: Path):
    # e.g. ~/templates/my-agent/lessons  -- 'templates' is an ancestor of the root
    root = tmp_path / "templates" / "my-agent" / "lessons"
    (root / "workflow").mkdir(parents=True)
    (root / "workflow" / "broken.md").write_text("no frontmatter at all\n")
    valid, total = validate_directory(root)
    assert (
        total == 1
    ), "lesson silently skipped because an ancestor dir is named 'templates'"
    assert valid == 0


def test_uppercase_checked_box_counts(tmp_path: Path):
    lesson = tmp_path / "lessons" / "workflow" / "x.md"
    lesson.parent.mkdir(parents=True)
    lesson.write_text(
        "---\nmatch:\n  keywords: [x]\n---\n"
        + ORIGINAL_BODY.replace("- [ ] Checked", "- [X] Checked")
    )
    v = LessonValidator(lesson)
    assert v.validate(), v.errors


def test_skill_body_with_dashes_in_frontmatter_validates(tmp_path: Path):
    skill = tmp_path / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: demo\ndescription: handles --- separators\n---\n\n"
        "# Demo\n\nDo the thing.\n"
    )
    v = LessonValidator(skill)
    assert v.is_skill
    v.validate()
    assert v.errors == []
