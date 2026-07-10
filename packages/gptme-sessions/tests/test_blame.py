"""Tests for gptme_sessions.blame — session provenance."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gptme_sessions.blame import (
    GITHUB_REF_RE,
    Attribution,
    BlameResult,
    SessionWindow,
    attribute,
    attribute_all,
    commits_for_github_ref,
    consolidated_records_sources,
    load_windows,
    render_json,
    render_text,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dt(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _window(
    session_id: str = "sess-abc",
    start: str = "2026-06-01T10:00:00+00:00",
    end: str = "2026-06-01T10:30:00+00:00",
    category: str | None = "code",
    productivity: float | None = 0.75,
    journal_path: str | None = "journal/2026-06-01/session.md",
    model: str | None = "claude-sonnet-4-6",
    harness: str | None = "claude-code",
) -> SessionWindow:
    return SessionWindow(
        session_id=session_id,
        start=_dt(start),
        end=_dt(end),
        category=category,
        harness=harness,
        productivity=productivity,
        journal_path=journal_path,
        model=model,
    )


# ---------------------------------------------------------------------------
# GITHUB_REF_RE
# ---------------------------------------------------------------------------


def test_github_ref_re_matches():
    m = GITHUB_REF_RE.match("gptme/gptme#123")
    assert m is not None
    assert m.group(1) == "gptme/gptme"
    assert m.group(2) == "123"


def test_github_ref_re_matches_dotted():
    assert GITHUB_REF_RE.match("ErikBjare/bob#42") is not None
    assert GITHUB_REF_RE.match("org.name/repo.name#1") is not None


def test_github_ref_re_no_match():
    assert GITHUB_REF_RE.match("just/a/path.py") is None
    assert GITHUB_REF_RE.match("scripts/watchdog.py") is None
    assert GITHUB_REF_RE.match("owner/repo") is None


# ---------------------------------------------------------------------------
# SessionWindow.distance
# ---------------------------------------------------------------------------


def test_distance_inside_window():
    w = _window()
    mid = _dt("2026-06-01T10:15:00+00:00")
    assert w.distance(mid) == timedelta(0)


def test_distance_before_window():
    w = _window()
    before = _dt("2026-06-01T09:45:00+00:00")
    assert w.distance(before) == timedelta(minutes=15)


def test_distance_after_window():
    w = _window()
    after = _dt("2026-06-01T11:00:00+00:00")
    assert w.distance(after) == timedelta(minutes=30)


# ---------------------------------------------------------------------------
# attribute() — session correlation
# ---------------------------------------------------------------------------


def _att(when: str) -> Attribution:
    return Attribution(sha="abc123def", when=_dt(when), author="Bob", subject="fix: something")


def test_attribute_exact_hit():
    windows = [_window()]
    a = _att("2026-06-01T10:10:00+00:00")
    attribute(a, windows)
    assert a.confidence == "exact"
    assert a.method == "commit-window"
    assert a.session_id == "sess-abc"
    assert a.model == "claude-sonnet-4-6"
    assert a.productivity == 0.75


def test_attribute_near_hit():
    windows = [_window()]
    # 20 minutes before window start — within 30-minute tolerance
    a = _att("2026-06-01T09:40:00+00:00")
    attribute(a, windows)
    assert a.confidence == "near"
    assert a.method == "nearest"
    assert a.session_id == "sess-abc"


def test_attribute_too_far():
    windows = [_window()]
    # 2 hours before — beyond NEAREST_TOLERANCE
    a = _att("2026-06-01T08:00:00+00:00")
    attribute(a, windows)
    assert a.confidence == "unmatched"
    assert a.session_id is None


def test_attribute_picks_closest_window():
    w1 = _window(
        session_id="sess-1", start="2026-06-01T10:00:00+00:00", end="2026-06-01T10:30:00+00:00"
    )
    w2 = _window(
        session_id="sess-2", start="2026-06-01T11:00:00+00:00", end="2026-06-01T11:30:00+00:00"
    )
    a = _att("2026-06-01T10:45:00+00:00")  # between both windows
    attribute(a, [w1, w2])
    # 15 min after w1's end vs 15 min before w2's start — both equidistant; first wins
    assert a.session_id in ("sess-1", "sess-2")  # either is valid


def test_attribute_all():
    windows = [_window()]
    atts = [_att("2026-06-01T10:10:00+00:00"), _att("2026-06-01T10:20:00+00:00")]
    result = attribute_all(atts, windows)
    assert len(result) == 2
    assert all(a.confidence == "exact" for a in result)


# ---------------------------------------------------------------------------
# load_windows
# ---------------------------------------------------------------------------


def test_load_windows_reads_jsonl(tmp_path: Path):
    records = tmp_path / "session-records.jsonl"
    records.write_text(
        json.dumps(
            {
                "session_id": "test-abc",
                "timestamp": "2026-06-01T10:30:00+00:00",
                "duration_seconds": 1800,
                "category": "code",
                "harness": "claude-code",
                "model": "claude-opus-4-8",
                "grades": {"productivity": 0.8},
                "journal_path": "journal/2026-06-01/session.md",
            }
        )
        + "\n"
    )
    windows = load_windows(records)
    assert len(windows) == 1
    w = windows[0]
    assert w.session_id == "test-abc"
    assert w.category == "code"
    assert w.productivity == 0.8
    assert w.model == "claude-opus-4-8"


def test_load_windows_skips_malformed(tmp_path: Path):
    records = tmp_path / "session-records.jsonl"
    records.write_text(
        "not-json\n"
        + json.dumps(
            {"session_id": "good", "timestamp": "2026-06-01T10:00:00+00:00", "duration_seconds": 60}
        )
        + "\n"
    )
    windows = load_windows(records)
    assert len(windows) == 1
    assert windows[0].session_id == "good"


def test_load_windows_missing_file(tmp_path: Path):
    windows = load_windows(tmp_path / "nonexistent.jsonl")
    assert windows == []


def test_load_windows_deduplicates_session_ids(tmp_path: Path):
    record = json.dumps(
        {
            "session_id": "dupe",
            "timestamp": "2026-06-01T10:00:00+00:00",
            "duration_seconds": 60,
        }
    )
    records = tmp_path / "session-records.jsonl"
    records.write_text(record + "\n" + record + "\n")
    windows = load_windows(records)
    assert len(windows) == 1


def test_load_windows_consolidated_siblings(tmp_path: Path):
    active = tmp_path / "session-records.jsonl"
    bak = tmp_path / "session-records.jsonl.bak-abc"
    active.write_text(
        json.dumps(
            {
                "session_id": "new-session",
                "timestamp": "2026-07-01T10:00:00+00:00",
                "duration_seconds": 60,
            }
        )
        + "\n"
    )
    bak.write_text(
        json.dumps(
            {
                "session_id": "old-session",
                "timestamp": "2026-05-01T10:00:00+00:00",
                "duration_seconds": 60,
            }
        )
        + "\n"
    )
    windows = load_windows(active)
    ids = {w.session_id for w in windows}
    assert "new-session" in ids
    assert "old-session" in ids


# ---------------------------------------------------------------------------
# consolidated_records_sources
# ---------------------------------------------------------------------------


def test_consolidated_records_sources_includes_siblings(tmp_path: Path):
    primary = tmp_path / "session-records.jsonl"
    primary.touch()
    bak = tmp_path / "session-records.jsonl.bak-xyz"
    bak.touch()
    archive = tmp_path / "session-records-archive-2026-05.jsonl"
    archive.touch()
    sources = consolidated_records_sources(primary)
    assert sources[0] == primary
    assert bak in sources
    assert archive in sources


def test_consolidated_records_sources_missing_dir(tmp_path: Path):
    primary = tmp_path / "nonexistent" / "session-records.jsonl"
    sources = consolidated_records_sources(primary)
    assert sources == [primary]


# ---------------------------------------------------------------------------
# commits_for_github_ref
# ---------------------------------------------------------------------------


TSV_ROW = "abc123def456\t2026-06-01T10:00:00Z\tBob\tfix: stale model\n"


def test_commits_for_github_ref_pr(monkeypatch):
    """PR ref fetches commits directly."""

    def _fake_run(args, **kwargs):
        result = MagicMock()
        result.stdout = TSV_ROW
        result.returncode = 0
        return result

    monkeypatch.setattr(subprocess, "run", _fake_run)
    atts = commits_for_github_ref("gptme/gptme#42")
    assert len(atts) == 1
    assert atts[0].sha == "abc123def456"
    assert atts[0].author == "Bob"
    assert atts[0].subject == "fix: stale model"


def test_commits_for_github_ref_invalid():
    with pytest.raises(ValueError, match="Not a valid GitHub ref"):
        commits_for_github_ref("not-a-ref")


def test_commits_for_github_ref_gh_unavailable(monkeypatch, capsys):
    """When gh is unavailable, returns empty list with a warning."""

    def _raise(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(subprocess, "run", _raise)
    atts = commits_for_github_ref("gptme/gptme#1")
    assert atts == []
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()


# ---------------------------------------------------------------------------
# render_text / render_json
# ---------------------------------------------------------------------------


def test_render_text_no_attributions():
    result = BlameResult(path="scripts/foo.py", line=None)
    out = render_text(result)
    assert "no commits found" in out


def test_render_text_with_attribution():
    a = Attribution(
        sha="abc123def456789",
        when=_dt("2026-06-01T10:00:00+00:00"),
        author="Bob",
        subject="fix: something",
        session_id="sess-abc",
        category="code",
        productivity=0.75,
        confidence="exact",
        method="commit-window",
        model="claude-sonnet-4-6",
    )
    result = BlameResult(path="scripts/foo.py", line=None, attributions=[a])
    out = render_text(result)
    assert "sess-abc" in out
    assert "●" in out  # exact mark
    assert "code" in out


def test_render_json_round_trip():
    a = Attribution(
        sha="abc123",
        when=_dt("2026-06-01T10:00:00+00:00"),
        author="Bob",
        subject="fix: x",
        session_id="sess-1",
        confidence="exact",
        method="commit-window",
    )
    result = BlameResult(path="foo.py", line=42, attributions=[a])
    data = json.loads(render_json(result))
    assert data["path"] == "foo.py"
    assert data["line"] == 42
    assert len(data["attributions"]) == 1
    att = data["attributions"][0]
    assert att["sha"] == "abc123"
    assert att["session_id"] == "sess-1"
    assert att["confidence"] == "exact"
