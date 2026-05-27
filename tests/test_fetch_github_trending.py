"""Tests for scripts/fetch-github-trending.py keyword parsing/filtering.

Focus: parse_keywords() drops empty entries so a trailing/doubled comma in
--filter does not silently disable filtering (an empty string is a substring
of every text and would match all repos).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / "scripts" / "fetch-github-trending.py"
_spec = importlib.util.spec_from_file_location("fetch_github_trending", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

parse_keywords = _mod.parse_keywords
format_compact = _mod.format_compact


REPO_AGENT = {
    "name": "foo/agent-thing",
    "description": "an llm agent",
    "language": "Python",
    "stars": 10,
    "today_stars": 1,
    "url": "https://github.com/foo/agent-thing",
}
REPO_WEB = {
    "name": "bar/web-app",
    "description": "a website",
    "language": "JavaScript",
    "stars": 20,
    "today_stars": 2,
    "url": "https://github.com/bar/web-app",
}


class TestParseKeywords:
    def test_none_arg_returns_none(self) -> None:
        assert parse_keywords(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert parse_keywords("") is None

    def test_single_keyword(self) -> None:
        assert parse_keywords("agent") == ["agent"]

    def test_multiple_keywords(self) -> None:
        assert parse_keywords("agent,llm") == ["agent", "llm"]

    def test_strips_whitespace(self) -> None:
        assert parse_keywords(" agent , llm ") == ["agent", "llm"]

    def test_trailing_comma_dropped(self) -> None:
        # The bug: "agent," previously produced ["agent", ""]
        assert parse_keywords("agent,") == ["agent"]

    def test_doubled_comma_dropped(self) -> None:
        assert parse_keywords("agent,,llm") == ["agent", "llm"]

    def test_all_empty_returns_none(self) -> None:
        # "," or ",," has no usable keywords -> no filter
        assert parse_keywords(",") is None
        assert parse_keywords(" , ") is None


class TestFilterRegression:
    """End-to-end: a trailing comma must not match every repo."""

    def test_trailing_comma_does_not_match_all(self) -> None:
        keywords = parse_keywords("agent,")
        out = format_compact([REPO_AGENT, REPO_WEB], keywords)
        assert "foo/agent-thing" in out
        assert "bar/web-app" not in out

    def test_real_filter_still_works(self) -> None:
        keywords = parse_keywords("website")
        out = format_compact([REPO_AGENT, REPO_WEB], keywords)
        assert "bar/web-app" in out
        assert "foo/agent-thing" not in out

    def test_all_empty_filter_shows_all(self) -> None:
        keywords = parse_keywords(",")
        out = format_compact([REPO_AGENT, REPO_WEB], keywords)
        assert "foo/agent-thing" in out
        assert "bar/web-app" in out
