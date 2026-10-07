"""parse_trajectory must skip lines that are valid JSON but not objects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gptme_sessions.signals import detect_format, extract_from_path, parse_trajectory


@pytest.mark.parametrize("line", ["[]", "[1, 2]", '"text"', "42", "null", "true"])
def test_non_object_lines_are_skipped(tmp_path: Path, line: str) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text(
        line + "\n" + json.dumps({"role": "user", "content": "hi"}) + "\n",
        encoding="utf-8",
    )
    assert parse_trajectory(path) == [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize("content", ["[]\n", "[1, 2]\n", "null\n"])
def test_file_of_only_non_objects_does_not_crash(tmp_path: Path, content: str) -> None:
    path = tmp_path / "t.json"
    path.write_text(content, encoding="utf-8")
    msgs = parse_trajectory(path)
    assert msgs == []
    assert detect_format(msgs) == "gptme"
    extract_from_path(path)  # must not raise AttributeError
