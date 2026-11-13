"""Regression tests for lesson export frontmatter and file output."""

from pathlib import Path

import pytest
from gptme_lessons_extras.export import export_lesson, parse_lesson_frontmatter


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
