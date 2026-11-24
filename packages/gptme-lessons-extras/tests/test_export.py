"""Regression tests for lesson export frontmatter and file output."""

from pathlib import Path

import pytest
import yaml
from gptme_lessons_extras.export import (
    export_all_lessons,
    export_lesson,
    parse_lesson_frontmatter,
)


@pytest.mark.parametrize("prefix,newline", [("", "\n"), ("", "\r\n"), ("\ufeff", "\n")])
def test_parse_lesson_preserves_dashes_and_body(prefix: str, newline: str):
    content = prefix + newline.join(
        [
            "---",
            'description: "Run echo --- safely"',
            "probe: |",
            "  echo ---",
            "---",
            "",
            "  Lesson body.",
            "---",
            "Second section.  ",
            "",
        ]
    )
    metadata, body = parse_lesson_frontmatter(content)
    assert metadata == {
        "description": "Run echo --- safely",
        "probe": "echo ---\n",
    }
    assert body == "Lesson body.\n---\nSecond section."


@pytest.mark.parametrize(
    "content",
    [
        "  Plain body.\n",
        "---\ndescription: No closing fence\n",
        "---description: Invalid opening fence\n---\nBody.",
        "---\ndescription: Invalid closing fence\n---junk\nBody.",
        "---\ndescription: [invalid YAML\n---\nBody.",
        "---\n- sequence\n---\nBody.",
        "---\nscalar\n---\nBody.",
        "---\n42\n---\nBody.",
    ],
)
def test_parse_lesson_invalid_document_preserves_original(content: str):
    assert parse_lesson_frontmatter(content) == (None, content)


def test_export_lesson_with_embedded_dashes(tmp_path: Path):
    lesson = tmp_path / "lessons" / "workflow" / "shell.md"
    lesson.parent.mkdir(parents=True)
    lesson.write_text('---\ndescription: "echo ---"\n---\n\n# Shell\n\n---\nBody.\n')
    output = tmp_path / "exported"

    assert export_lesson(lesson, output, agent_origin="bob")

    exported = output / "workflow" / "shell.md"
    metadata, body = parse_lesson_frontmatter(exported.read_text())
    assert metadata is not None
    assert metadata["description"] == "echo ---"
    assert metadata["network"]["agent_origin"] == "bob"
    assert body == "# Shell\n\n---\nBody."


def test_export_lesson_skips_nonmapping_metadata(tmp_path: Path, capsys):
    lesson = tmp_path / "lessons" / "workflow" / "invalid.md"
    lesson.parent.mkdir(parents=True)
    lesson.write_text("---\n- sequence\n---\nBody.")
    output = tmp_path / "exported"

    assert not export_lesson(lesson, output)
    assert not output.exists()
    assert "skipping" in capsys.readouterr().out


MINIMAL_LESSON = "---\nmatch:\n  keywords: [x]\n---\n# T\n"


def test_export_all_reports_existing_exports_as_skipped(tmp_path: Path):
    lessons = tmp_path / "lessons"
    (lessons / "git").mkdir(parents=True)
    (lessons / "git" / "a.md").write_text(MINIMAL_LESSON)
    out = tmp_path / "out"

    assert export_all_lessons(lessons, out) == {"success": 1, "skipped": 0, "failed": 0}
    assert export_all_lessons(lessons, out) == {"success": 0, "skipped": 1, "failed": 0}
    forced = export_all_lessons(lessons, out, force=True)
    assert forced == {"success": 1, "skipped": 0, "failed": 0}


def test_export_all_keeps_nested_categories_apart(tmp_path: Path):
    lessons = tmp_path / "lessons"
    for cat in ("tools/shell", "workflow/shell"):
        (lessons / cat).mkdir(parents=True)
        (lessons / cat / "quoting.md").write_text(MINIMAL_LESSON)
    (lessons / "git").mkdir()
    (lessons / "git" / "commit.md").write_text(MINIMAL_LESSON)
    out = tmp_path / "out"

    counts = export_all_lessons(lessons, out, agent_origin="bob")

    assert counts == {"success": 3, "skipped": 0, "failed": 0}
    exported = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.md"))
    assert exported == [
        "git/commit.md",
        "tools/shell/quoting.md",
        "workflow/shell/quoting.md",
    ]
    ids = {
        rel: yaml.safe_load((out / rel).read_text().split("---\n")[1])["network"][
            "lesson_id"
        ]
        for rel in exported
    }
    # Single-level ids are unchanged; nested ones are distinct
    assert ids == {
        "git/commit.md": "bob-git-commit",
        "tools/shell/quoting.md": "bob-tools-shell-quoting",
        "workflow/shell/quoting.md": "bob-workflow-shell-quoting",
    }


def test_export_keeps_network_metadata_with_unquoted_timestamps(tmp_path: Path):
    lesson = tmp_path / "lessons" / "git" / "a.md"
    lesson.parent.mkdir(parents=True)
    lesson.write_text(
        "---\nmatch:\n  keywords: [x]\nnetwork:\n"
        "  lesson_id: alice-git-a-custom\n  agent_origin: alice\n"
        "  created: 2025-01-02T03:04:05\n  updated: 2025-01-03\n"
        "  confidence: 0.95\n  adoption_count: 7\n---\n# T\n"
    )
    out = tmp_path / "out"

    assert export_lesson(lesson, out, agent_origin="bob")

    fm = yaml.safe_load((out / "git" / "a.md").read_text().split("---\n")[1])
    assert fm["network"]["lesson_id"] == "alice-git-a-custom"
    assert fm["network"]["agent_origin"] == "alice"
    assert fm["network"]["confidence"] == 0.95
    assert fm["network"]["adoption_count"] == 7
