"""Tests for full-text session search."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from gptme_sessions.cli import cli
from gptme_sessions.search import (
    SearchResult,
    _make_snippet,
    _search_path,
    search_sessions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _gptme_session(
    tmp_path: Path, messages: list[tuple[str, str]], ts_base: str = "2026-03-01T10:00:00+00:00"
) -> Path:
    """Create a gptme session directory with a conversation.jsonl."""
    session_dir = tmp_path / "2026-03-01_10-00-00_test-session"
    session_dir.mkdir(parents=True)
    records = [
        {"role": role, "content": content, "timestamp": ts_base} for role, content in messages
    ]
    _write_jsonl(session_dir / "conversation.jsonl", records)
    return session_dir


def _cc_session(tmp_path: Path, text: str, ts: str = "2026-03-01T10:00:00.000Z") -> Path:
    """Create a Claude Code session JSONL file."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    jsonl = tmp_path / "abc123.jsonl"
    records = [
        {
            "type": "assistant",
            "timestamp": ts,
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": text}],
            },
        }
    ]
    _write_jsonl(jsonl, records)
    return jsonl


# ---------------------------------------------------------------------------
# Unit tests: SearchResult
# ---------------------------------------------------------------------------


class TestSearchResult:
    def test_display_date_with_timestamp(self):
        dt = datetime(2026, 3, 1, 10, 30, tzinfo=timezone.utc)
        r = SearchResult(session_id="abc", harness="gptme", path="/x", hit_count=1, started_at=dt)
        assert r.display_date == "2026-03-01 10:30"

    def test_display_date_without_timestamp(self):
        r = SearchResult(session_id="abc", harness="gptme", path="/x", hit_count=1, started_at=None)
        assert r.display_date == "unknown"


# ---------------------------------------------------------------------------
# Unit tests: _make_snippet
# ---------------------------------------------------------------------------


class TestMakeSnippet:
    def test_returns_none_for_no_match(self):
        import re

        assert _make_snippet("hello world", re.compile("xyz")) is None

    def test_returns_snippet_for_match(self):
        import re

        result = _make_snippet("hello world", re.compile("world"))
        assert result is not None
        assert "world" in result

    def test_adds_ellipsis_for_truncated_context(self):
        import re

        long_text = "A" * 200 + " TARGET " + "B" * 200
        result = _make_snippet(long_text, re.compile("TARGET"))
        assert result is not None
        assert "…" in result
        assert "TARGET" in result

    def test_no_ellipsis_for_short_content(self):
        import re

        result = _make_snippet("TARGET", re.compile("TARGET"))
        assert result is not None
        assert "…" not in result


# ---------------------------------------------------------------------------
# Unit tests: _search_path
# ---------------------------------------------------------------------------


class TestSearchPath:
    def test_returns_none_for_no_match(self, tmp_path: Path):
        import re

        session = _gptme_session(tmp_path, [("user", "hello world")])
        result = _search_path(session, re.compile("xyz"))
        assert result is None

    def test_returns_result_for_match(self, tmp_path: Path):
        import re

        session = _gptme_session(tmp_path, [("assistant", "module not found error")])
        result = _search_path(session, re.compile("module not found", re.IGNORECASE))
        assert result is not None
        assert result.hit_count == 1
        assert result.harness == "gptme"

    def test_counts_multiple_hits_in_same_session(self, tmp_path: Path):
        import re

        session = _gptme_session(
            tmp_path,
            [
                ("user", "error: module not found"),
                (
                    "assistant",
                    "I see the module not found issue. The module not found is because...",
                ),
            ],
        )
        result = _search_path(session, re.compile("module not found", re.IGNORECASE))
        assert result is not None
        assert result.hit_count == 3

    def test_returns_none_for_unreadable_file(self, tmp_path: Path):
        import re

        bad_path = tmp_path / "nonexistent"
        result = _search_path(bad_path, re.compile("test"))
        assert result is None

    def test_extracts_snippets(self, tmp_path: Path):
        import re

        session = _gptme_session(tmp_path, [("assistant", "The CORS bug was fixed")])
        result = _search_path(session, re.compile("cors bug", re.IGNORECASE))
        assert result is not None
        assert len(result.snippets) == 1
        assert "CORS bug" in result.snippets[0].text

    def test_limits_snippets_to_max(self, tmp_path: Path):
        import re

        messages = [("user", f"hit number {i}: target word here") for i in range(10)]
        session = _gptme_session(tmp_path, messages)
        result = _search_path(session, re.compile("target word", re.IGNORECASE))
        assert result is not None
        assert len(result.snippets) <= 3  # MAX_SNIPPETS_PER_SESSION


# ---------------------------------------------------------------------------
# Integration tests: search_sessions
# ---------------------------------------------------------------------------


class TestSearchSessions:
    def test_returns_empty_when_no_sessions(self, tmp_path: Path):
        with (
            patch("gptme_sessions.search.discover_gptme_sessions", return_value=[]),
            patch("gptme_sessions.search.discover_cc_sessions", return_value=[]),
        ):
            results = search_sessions("anything")
        assert results == []

    def test_finds_gptme_session(self, tmp_path: Path):
        session = _gptme_session(tmp_path, [("assistant", "module not found error")])
        with (
            patch("gptme_sessions.search.discover_gptme_sessions", return_value=[session]),
            patch("gptme_sessions.search.discover_cc_sessions", return_value=[]),
        ):
            results = search_sessions("module not found")
        assert len(results) == 1
        assert results[0].harness == "gptme"

    def test_finds_cc_session(self, tmp_path: Path):
        cc = _cc_session(tmp_path, "The CORS bug was fixed")
        with (
            patch("gptme_sessions.search.discover_gptme_sessions", return_value=[]),
            patch("gptme_sessions.search.discover_cc_sessions", return_value=[cc]),
        ):
            results = search_sessions("CORS bug")
        assert len(results) == 1
        assert results[0].harness == "claude-code"

    def test_case_insensitive_by_default(self, tmp_path: Path):
        session = _gptme_session(tmp_path, [("assistant", "Module Not Found")])
        with (
            patch("gptme_sessions.search.discover_gptme_sessions", return_value=[session]),
            patch("gptme_sessions.search.discover_cc_sessions", return_value=[]),
        ):
            results = search_sessions("module not found")
        assert len(results) == 1

    def test_case_sensitive_misses_different_case(self, tmp_path: Path):
        session = _gptme_session(tmp_path, [("assistant", "Module Not Found")])
        with (
            patch("gptme_sessions.search.discover_gptme_sessions", return_value=[session]),
            patch("gptme_sessions.search.discover_cc_sessions", return_value=[]),
        ):
            results = search_sessions("module not found", case_sensitive=True)
        assert len(results) == 0

    def test_case_sensitive_finds_exact_match(self, tmp_path: Path):
        session = _gptme_session(tmp_path, [("assistant", "module not found")])
        with (
            patch("gptme_sessions.search.discover_gptme_sessions", return_value=[session]),
            patch("gptme_sessions.search.discover_cc_sessions", return_value=[]),
        ):
            results = search_sessions("module not found", case_sensitive=True)
        assert len(results) == 1

    def test_respects_max_results(self, tmp_path: Path):
        sessions = [
            _gptme_session(tmp_path / f"s{i}", [("user", "target query")]) for i in range(5)
        ]
        with (
            patch("gptme_sessions.search.discover_gptme_sessions", return_value=sessions),
            patch("gptme_sessions.search.discover_cc_sessions", return_value=[]),
        ):
            results = search_sessions("target query", max_results=3)
        assert len(results) <= 3

    def test_harness_filter_gptme_only(self, tmp_path: Path):
        gptme = _gptme_session(tmp_path / "gptme", [("user", "target")])
        cc = _cc_session(tmp_path / "cc", "target")
        with (
            patch("gptme_sessions.search.discover_gptme_sessions", return_value=[gptme]) as mock_g,
            patch("gptme_sessions.search.discover_cc_sessions", return_value=[cc]) as mock_c,
        ):
            results = search_sessions("target", harness="gptme")
        mock_g.assert_called_once()
        mock_c.assert_not_called()
        assert all(r.harness == "gptme" for r in results)

    def test_harness_filter_cc_only(self, tmp_path: Path):
        gptme = _gptme_session(tmp_path / "gptme", [("user", "target")])
        cc = _cc_session(tmp_path / "cc", "target")
        with (
            patch("gptme_sessions.search.discover_gptme_sessions", return_value=[gptme]) as mock_g,
            patch("gptme_sessions.search.discover_cc_sessions", return_value=[cc]) as mock_c,
        ):
            search_sessions("target", harness="claude-code")
        mock_g.assert_not_called()
        mock_c.assert_called_once()

    def test_sorts_by_recency(self, tmp_path: Path):
        old_session = _gptme_session(
            tmp_path / "old", [("user", "target")], ts_base="2026-01-01T10:00:00+00:00"
        )
        new_session = _gptme_session(
            tmp_path / "new", [("user", "target")], ts_base="2026-06-01T10:00:00+00:00"
        )
        with (
            patch(
                "gptme_sessions.search.discover_gptme_sessions",
                return_value=[old_session, new_session],
            ),
            patch("gptme_sessions.search.discover_cc_sessions", return_value=[]),
        ):
            results = search_sessions("target")
        assert len(results) == 2
        assert results[0].started_at > results[1].started_at  # type: ignore[operator]

    def test_skips_nonexistent_paths(self, tmp_path: Path):
        bad = tmp_path / "does_not_exist"
        with (
            patch("gptme_sessions.search.discover_gptme_sessions", return_value=[bad]),
            patch("gptme_sessions.search.discover_cc_sessions", return_value=[]),
        ):
            results = search_sessions("anything")
        assert results == []


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestSearchCLI:
    def test_search_command_exists(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "--help"])
        assert result.exit_code == 0
        assert "query" in result.output.lower() or "QUERY" in result.output

    def test_search_returns_no_sessions_message(self, tmp_path: Path):
        runner = CliRunner()
        with (
            patch("gptme_sessions.search.discover_gptme_sessions", return_value=[]),
            patch("gptme_sessions.search.discover_cc_sessions", return_value=[]),
        ):
            result = runner.invoke(cli, ["search", "nonexistent-xyz-query"])
        assert result.exit_code == 0
        assert "No sessions found" in result.output

    def test_search_outputs_json(self, tmp_path: Path):
        session = _gptme_session(tmp_path, [("assistant", "target found here")])
        runner = CliRunner()
        with (
            patch("gptme_sessions.search.discover_gptme_sessions", return_value=[session]),
            patch("gptme_sessions.search.discover_cc_sessions", return_value=[]),
        ):
            result = runner.invoke(cli, ["search", "target", "--json"])
        assert result.exit_code == 0
        # Strip any progress/status lines before the JSON array
        json_start = result.output.index("[")
        data = json.loads(result.output[json_start:])
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["hit_count"] == 1
        assert "snippets" in data[0]

    def test_search_no_snippets_flag(self, tmp_path: Path):
        session = _gptme_session(tmp_path, [("assistant", "target found here")])
        runner = CliRunner()
        with (
            patch("gptme_sessions.search.discover_gptme_sessions", return_value=[session]),
            patch("gptme_sessions.search.discover_cc_sessions", return_value=[]),
        ):
            result = runner.invoke(cli, ["search", "target", "--no-snippets"])
        assert result.exit_code == 0
        assert "target found here" not in result.output

    def test_search_harness_choice(self, tmp_path: Path):
        runner = CliRunner()
        with (
            patch("gptme_sessions.search.discover_gptme_sessions", return_value=[]),
            patch("gptme_sessions.search.discover_cc_sessions", return_value=[]),
        ):
            result = runner.invoke(cli, ["search", "test", "--harness", "gptme"])
        assert result.exit_code == 0

    def test_search_invalid_harness_rejected(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["search", "test", "--harness", "invalid-harness"])
        assert result.exit_code != 0
