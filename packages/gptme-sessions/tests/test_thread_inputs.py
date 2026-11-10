"""Tests for thread_inputs (records + journals -> threads) and the threads/thread CLI."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from gptme_sessions.cli import cli
from gptme_sessions.record import SessionRecord
from gptme_sessions.store import SessionStore
from gptme_sessions.thread_inputs import build_forest, from_records, journal_digest, journal_key

JOURNAL = """# Autonomous Session 8316

**Date**: 2026-11-10
**Session ID**: 8316
**Outcome**: productive — released the contrib#2057 gate after #2059 merged

## Why this work
Forward-drive probe flagged tasks/issue-sweep-gptme-gptme-contrib-2057.md as
waiting on gptme/gptme-contrib#2059, which merged at 20:19Z.

## What I Did
Followed up on session 02fd and session 8316 (self). Cited `5e478fed442` and
https://github.com/gptme/gptme/pull/4851. See journal/2026-11-10/autonomous-session-02fd.md.
Not hashes: 2026111 and the UUID 506de844-5f8c-4692-af0e-d7c9bb8baa1a.
"""


def _rec(sid: str, start: str, end: str, **kw: object) -> SessionRecord:
    return SessionRecord(session_id=sid, start_time=start, end_time=end, timestamp=end, **kw)  # type: ignore[arg-type]


def test_journal_digest_extracts_text_and_references() -> None:
    d = journal_digest(JOURNAL)
    assert d["title"] == "Autonomous Session 8316"
    assert d["short"] == "8316"
    assert d["outcome_line"].startswith("productive")
    assert d["why"].startswith("Forward-drive probe flagged")
    assert d["sessions_mentioned"] == ["02fd"]  # own label dropped
    assert d["shas_mentioned"] == ["5e478fed442"]  # no dates, no UUID segments
    assert set(d["prs_mentioned"]) == {"gptme/gptme-contrib#2059", "gptme/gptme#4851"}
    assert d["tasks_mentioned"] == ["issue-sweep-gptme-gptme-contrib-2057"]
    assert d["journal_links"] == ["journal/2026-11-10/autonomous-session-02fd.md"]


def test_journal_key_normalizes_absolute_and_relative_paths() -> None:
    assert journal_key("/home/x/ws/journal/2026-11-10/a.md") == "journal/2026-11-10/a.md"
    assert journal_key("journal/2026-11-10/a.md") == "journal/2026-11-10/a.md"
    assert journal_key("/tmp/notes/a.md") == "a.md"


def test_from_records_coerces_real_record_shapes(tmp_path: Path) -> None:
    rec = _rec(
        "aaaa1111-0000-0000-0000-000000000000",
        "2026-11-10T10:00:00+00:00",
        "2026-11-10T10:30:00+00:00",
        dispatch_cause={"kind": "cascade-task", "task": {"id": "t-slug"}, "pr": None},
        cascade_intent={"task_label": "Do the thing", "selector_context": {"x": 1}},
        deliverables=[
            "feat(x): thing (0cfdcd206)",
            "(commit output truncated) (a276bbe64)",
            "c9a1657ec1f3f7b47c9c6a1673ab25c6de3cad16",
            "https://github.com/gptme/gptme/pull/12",
        ],
        trigger="timer",
    )
    (n,) = from_records([rec], agent="bob", read_journal=None)
    assert n["task"] == "t-slug"  # dict coerced, never handed to node_keys raw
    assert n["task_label"] == "Do the thing"
    assert n["delivered_shas"] == [
        "0cfdcd206",
        "a276bbe64",
        "c9a1657ec1f3f7b47c9c6a1673ab25c6de3cad16",
    ]
    assert n["commits"] == ["feat(x): thing"]
    assert n["delivered_prs"] == ["gptme/gptme#12"]
    assert n["end"] - n["start"] == 1800


def test_from_records_reads_relative_journal_and_skips_shared_ones(tmp_path: Path) -> None:
    jdir = tmp_path / "journal" / "2026-11-10"
    jdir.mkdir(parents=True)
    (jdir / "autonomous-session-8316.md").write_text(JOURNAL)
    (jdir / "project-monitoring.md").write_text("Followed up on session 02fd.\n")
    own = _rec(
        "bbbb",
        "2026-11-10T20:00:00+00:00",
        "2026-11-10T20:10:00+00:00",
        journal_path="journal/2026-11-10/autonomous-session-8316.md",
    )
    shared = [
        _rec(
            f"cccc{i}",
            f"2026-11-10T1{i}:00:00+00:00",
            f"2026-11-10T1{i}:05:00+00:00",
            journal_path=str(jdir / "project-monitoring.md"),
        )
        for i in range(2)
    ]
    nodes = {n["id"]: n for n in from_records([own, *shared], agent="bob", journal_root=tmp_path)}
    assert nodes["bbbb"]["short"] == "8316"
    assert nodes["bbbb"]["sessions_mentioned"] == ["02fd"]
    for i in range(2):
        assert nodes[f"cccc{i}"]["journal"] == "journal/2026-11-10/project-monitoring.md"
        assert "sessions_mentioned" not in nodes[f"cccc{i}"]


def test_build_forest_reports_winning_link_and_thread() -> None:
    first = _rec(
        "02fd0000-aaaa",
        "2026-11-10T19:00:00+00:00",
        "2026-11-10T19:30:00+00:00",
        journal_path="journal/2026-11-10/autonomous-session-02fd.md",
        dispatch_cause={"task": "t-slug"},
    )
    child = _rec(
        "8316ffff-bbbb",
        "2026-11-10T20:00:00+00:00",
        "2026-11-10T20:30:00+00:00",
        parent_session_id="02fd0000-aaaa",
        dispatch_cause={"task": "t-slug"},
    )
    unrelated = _rec("9999", "2026-11-10T21:00:00+00:00", "2026-11-10T21:10:00+00:00")
    rows, stats = build_forest(from_records([child, unrelated, first], agent="bob"))
    by_id = {r["id"]: r for r in rows}
    assert by_id["8316ffff-bbbb"]["predecessor"] == "02fd0000-aaaa"
    assert by_id["8316ffff-bbbb"]["kind"] == "parent"
    assert by_id["8316ffff-bbbb"]["weight"] == 1.0
    assert by_id["8316ffff-bbbb"]["thread"] == "02fd0000-aaaa"
    assert by_id["9999"]["predecessor"] is None and by_id["9999"]["thread"] == "9999"
    assert stats["sessions"] == 3 and stats["linked"] == 1 and stats["threads"] == 2


def _store(tmp_path: Path) -> Path:
    store = SessionStore(sessions_dir=tmp_path)
    for i, sid in enumerate(["a0000001", "a0000002", "a0000003"]):
        store.append(
            _rec(
                sid,
                f"2026-11-10T0{i}:00:00+00:00",
                f"2026-11-10T0{i}:30:00+00:00",
                dispatch_cause={"task": "same-task"},
            )
        )
    return tmp_path


def test_threads_cli_json_contract(tmp_path: Path) -> None:
    res = CliRunner().invoke(
        cli, ["--sessions-dir", str(_store(tmp_path)), "threads", "--since", "all", "--json"]
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["stats"]["linked"] == 2
    assert [r["predecessor"] for r in data["sessions"]] == [None, "a0000001", "a0000002"]
    assert {r["thread"] for r in data["sessions"]} == {"a0000001"}
    assert data["sessions"][1]["kind"] == "task"
    assert data["sessions"][1]["key"] == "task:same-task"


def test_thread_cli_shows_ancestry_and_descendants(tmp_path: Path) -> None:
    d = str(_store(tmp_path))
    res = CliRunner().invoke(
        cli, ["--sessions-dir", d, "thread", "a0000002", "--since", "all", "--json"]
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["session"]["id"] == "a0000002"
    assert [r["id"] for r in data["ancestry"]] == ["a0000001"]
    assert [(r["id"], r["depth"]) for r in data["descendants"]] == [("a0000003", 1)]

    text = CliRunner().invoke(cli, ["--sessions-dir", d, "thread", "a00000", "--since", "all"])
    assert text.exit_code != 0 and "ambiguous" in text.output
