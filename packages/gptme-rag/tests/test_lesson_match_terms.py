"""Lesson ``keywords``/``patterns`` parsing (gptme/gptme-contrib#2356, L1-L3)."""

from pathlib import Path

import pytest
from gptme_rag.lesson_matcher import _parse_lesson_file


def _parse(tmp_path: Path, frontmatter: str) -> dict:
    path = tmp_path / "lesson.md"
    path.write_text(f"---\nstatus: active\n{frontmatter}---\n# Title\n")
    parsed = _parse_lesson_file(path)
    assert parsed is not None
    return parsed


def test_nested_keyword_string_is_split_like_the_top_level_one(tmp_path):
    nested = _parse(tmp_path, 'match:\n  keywords: "alpha phrase, beta phrase"\n')
    top = _parse(tmp_path, 'keywords: "alpha phrase, beta phrase"\n')
    assert nested["keywords"] == ["alpha phrase", "beta phrase"]
    assert top["keywords"] == nested["keywords"]


@pytest.mark.parametrize(
    "frontmatter",
    [
        "match:\n  keywords:\n    - 404\n    - not found\n",
        "keywords:\n  - 404\n  - not found\n",
    ],
)
def test_numeric_keywords_are_kept(tmp_path, frontmatter):
    assert _parse(tmp_path, frontmatter)["keywords"] == ["404", "not found"]


@pytest.mark.parametrize(
    "frontmatter",
    [
        'patterns: "x{1,3}y"\n',
        'match:\n  patterns: "x{1,3}y"\n',
    ],
)
def test_pattern_with_a_comma_is_not_split(tmp_path, frontmatter):
    assert _parse(tmp_path, frontmatter)["patterns"] == ["x{1,3}y"]
