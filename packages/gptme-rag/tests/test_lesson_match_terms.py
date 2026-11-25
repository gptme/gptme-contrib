"""Lesson ``keywords``/``patterns`` parsing (gptme/gptme-contrib#2356, L1-L3)."""

import json
import sys
from pathlib import Path

import pytest
import gptme_rag.lesson_matcher as matcher
from gptme_rag.lesson_matcher import _parse_lesson_file


@pytest.fixture(params=[False, True], autouse=True)
def yaml_backend(request, monkeypatch):
    if request.param:
        monkeypatch.setitem(sys.modules, "yaml", None)
    else:
        pytest.importorskip("yaml")


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


@pytest.mark.parametrize("nested", [False, True])
def test_explicit_pattern_list_keeps_independent_expressions(tmp_path, nested):
    fields = 'patterns: ["foo", "bar", "x{1,3}y"]\n'
    if nested:
        fields = "match:\n  " + fields
    assert _parse(tmp_path, fields)["patterns"] == ["foo", "bar", "x{1,3}y"]


@pytest.mark.parametrize("nested", [False, True])
def test_numeric_match_terms_preserve_source_text(tmp_path, nested):
    fields = "keywords: [404, 3.10, 1_000, 0x1F, '12:30']\n" "patterns:\n  - 3.10\n  - 12:30\n"
    if nested:
        fields = "match:\n" + "".join("  " + line for line in fields.splitlines(True))
    parsed = _parse(tmp_path, fields)
    assert parsed["keywords"] == ["404", "3.10", "1_000", "0x1F", "12:30"]
    assert parsed["patterns"] == ["3.10", "12:30"]
    assert matcher.match_keyword(parsed["keywords"][1], "python 3.10")
    assert not matcher.match_keyword(parsed["keywords"][1], "python 3.12")


@pytest.mark.parametrize("nested", [False, True])
def test_date_match_terms_preserve_source_text(tmp_path, nested):
    fields = "keywords: [2026-08-05]\npatterns:\n  - 2026-08-05\n"
    if nested:
        fields = "match:\n" + "".join("  " + line for line in fields.splitlines(True))
    parsed = _parse(tmp_path, fields)
    assert parsed["keywords"] == ["2026-08-05"]
    assert parsed["patterns"] == ["2026-08-05"]


def test_scan_invalidates_pre_normalization_disk_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("GPTME_LESSON_SCAN_CACHE", str(tmp_path / "cache"))
    lessons_dir = tmp_path / "lessons"
    lessons_dir.mkdir()
    _parse(lessons_dir, 'match:\n  keywords: "alpha phrase, beta phrase"\n')
    path = lessons_dir / "lesson.md"
    old_lesson = matcher._parse_lesson_file(path)
    assert old_lesson is not None
    old_lesson["keywords"] = ["alpha phrase, beta phrase"]
    old_lesson["n_keywords"] = 1
    cache_file = tmp_path / "cache" / "scan-v1.json"
    cache_file.parent.mkdir()
    cache_file.write_text(
        json.dumps(
            {
                "version": 2,
                "files": {
                    str(path.resolve()): {
                        "mtime_ns": path.stat().st_mtime_ns,
                        "size": path.stat().st_size,
                        "lesson": old_lesson,
                    }
                },
            }
        )
    )
    matcher.clear_scan_cache()
    try:
        lessons = matcher.scan_lessons([lessons_dir])
        assert lessons[0]["keywords"] == ["alpha phrase", "beta phrase"]
        assert json.loads(cache_file.read_text())["version"] > 2
    finally:
        matcher.clear_scan_cache()
