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
parse_trending = _mod.parse_trending
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


def _article_legacy_stars(name: str = "owner/repo", stars: int = 1000) -> str:
    """Pre-2026 GitHub trending markup: digits directly in the stargazers <a>."""
    return f"""<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a href="/{name}">{name}</a>
  </h2>
  <p class="col-9 color-fg-muted my-1 pr-4">A test repo</p>
  <div class="f6 color-fg-muted mt-2">
    <span itemprop="programmingLanguage">Python</span>
    <a href="/{name}/stargazers" class="Link--muted d-inline-block mr-3">
      {stars:,}
    </a>
    <span class="d-inline-block float-sm-right">100 stars today</span>
  </div>
</article>"""


def _article_svg_stars(name: str = "THU-MAIC/OpenMAIC", stars: int = 22230) -> str:
    """Live 2026-08-30 markup: octicon SVG inside the stargazers <a>."""
    return f"""<article class="Box-row">
  <h2 class="h3 lh-condensed">
    <a href="/{name}">{name}</a>
  </h2>
  <p class="col-9 color-fg-muted my-1 pr-4">Open Multi-Agent Interactive Classroom</p>
  <div class="f6 color-fg-muted mt-2">
    <span itemprop="programmingLanguage">TypeScript</span>
    <a href="/{name}/stargazers" data-view-component="true" class="Link Link--muted d-inline-block"><svg aria-label="star" role="img" height="16" viewBox="0 0 16 16" width="16" class="octicon octicon-star">
    <path d="M8 .25a.75.75 0 0 1 .673.418l1.882 3.815 4.21.612a.75.75 0 0 1 .416 1.279l-3.046 2.97.719 4.192a.751.751 0 0 1-1.088.791L8 12.347l-3.766 1.98a.75.75 0 0 1-1.088-.79l.72-4.194L.818 6.374a.75.75 0 0 1 .416-1.28l4.21-.611L7.327.668A.75.75 0 0 1 8 .25Z"></path>
</svg>
        {stars:,}</a>
    <span class="d-inline-block float-sm-right">907 stars today</span>
  </div>
</article>"""


class TestParseTrendingStars:
    def test_legacy_digits_immediately_after_anchor(self) -> None:
        repos = parse_trending(_article_legacy_stars(stars=1234))
        assert repos[0]["stars"] == 1234
        assert repos[0]["today_stars"] == 100

    def test_octicon_svg_inside_stargazers_anchor(self) -> None:
        repos = parse_trending(_article_svg_stars(stars=22230))
        assert repos[0]["name"] == "THU-MAIC/OpenMAIC"
        assert repos[0]["stars"] == 22230
        assert repos[0]["today_stars"] == 907

    def test_comma_grouped_count_inside_svg_anchor(self) -> None:
        repos = parse_trending(
            _article_svg_stars(name="bigskysoftware/htmx", stars=49117)
        )
        assert repos[0]["stars"] == 49117
