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


def test_timestamp_only_synced_record_starts_at_first_event() -> None:
    record = SessionRecord(
        session_id="synced",
        timestamp="2026-11-10T10:00:00+00:00",
        duration_seconds=3600,
    )
    (node,) = from_records([record], agent="bob", read_journal=None)
    from datetime import datetime

    assert node["start"] == datetime.fromisoformat(record.timestamp).timestamp()
    assert node["end"] == node["start"] + 3600


def test_bare_abbreviated_deliverable_links_to_later_commit_reference() -> None:
    first = _rec(
        "producer",
        "2026-11-10T10:00:00Z",
        "2026-11-10T10:30:00Z",
        deliverables=["0cfdcd206"],
    )
    later = _rec(
        "consumer",
        "2026-11-10T11:00:00Z",
        "2026-11-10T11:30:00Z",
        journal_path="journal/2026-11-10/consumer.md",
    )
    nodes = from_records(
        [first, later], agent="bob", read_journal=lambda _: "Follow-up to `0cfdcd206`."
    )
    assert nodes[0]["delivered_shas"] == ["0cfdcd206"]
    rows, _ = build_forest(nodes)
    assert rows[1]["predecessor"] == "producer"
    assert rows[1]["kind"] == "commit"


def test_journal_sharing_uses_resolved_file_not_suffix(tmp_path: Path) -> None:
    suffix = "journal/2026-11-10/session-ab12.md"
    records = [
        _rec(
            f"project-{name}",
            "2026-11-10T10:00:00Z",
            "2026-11-10T10:30:00Z",
            project=str(tmp_path / name),
            journal_path=suffix,
        )
        for name in ("a", "b")
    ]
    reads: list[Path] = []

    def reader(path: Path) -> str:
        reads.append(path)
        return f"# {path.parents[2].name} journal\nFollow-up to session 02fd."

    nodes = from_records(records, agent="bob", read_journal=reader)
    assert reads == [tmp_path / "a" / suffix, tmp_path / "b" / suffix]
    assert [n["title"] for n in nodes] == ["a journal", "b journal"]
    assert all(n["sessions_mentioned"] == ["02fd"] for n in nodes)

    # Absolute and relative aliases of the same file must still skip digestion.
    records[1].journal_path = str(tmp_path / "a" / "journal" / ".." / suffix)
    reads.clear()
    nodes = from_records(records, agent="bob", read_journal=reader)
    assert reads == []
    assert all("sessions_mentioned" not in n for n in nodes)


def test_unusable_journal_does_not_abort_record_processing(tmp_path: Path) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    record = _rec(
        "loop",
        "2026-11-10T10:00:00Z",
        "2026-11-10T10:30:00Z",
        journal_path=str(loop / "entry.md"),
        dispatch_cause={"task": "keep-me"},
    )
    for reader in (None, lambda _: "# Must not read"):
        (node,) = from_records([record], agent="bob", read_journal=reader)
        assert node["task"] == "keep-me"
        assert "title" not in node


def test_hardlinked_journals_are_shared(tmp_path: Path) -> None:
    journal = tmp_path / "daily.md"
    journal.write_text("# Shared log\nFollow-up to session 02fd.")
    alias = tmp_path / "alias.md"
    alias.hardlink_to(journal)
    records = [
        _rec(str(i), "2026-11-10T10:00:00Z", "2026-11-10T10:30:00Z", journal_path=str(path))
        for i, path in enumerate((journal, alias))
    ]
    nodes = from_records(records, agent="bob")
    assert len(nodes) == 2
    assert all("sessions_mentioned" not in n for n in nodes)


def test_nul_journal_path_keeps_record_evidence() -> None:
    record = _rec(
        "nul",
        "2026-11-10T10:00:00Z",
        "2026-11-10T10:30:00Z",
        journal_path="journal/bad\0.md",
        dispatch_cause={"task": "keep-me"},
    )
    (node,) = from_records([record], agent="bob")
    assert node["task"] == "keep-me"
    assert "title" not in node


def test_thread_text_output_strips_journal_terminal_controls(tmp_path: Path) -> None:
    journal = tmp_path / "entry.md"
    journal.write_text("# title \x1b]52;c;SGVsbG8=\x07 end\n")
    store_path = tmp_path / "store"
    SessionStore(sessions_dir=store_path).append(
        _rec(
            "session-one",
            "2026-11-10T10:00:00Z",
            "2026-11-10T10:30:00Z",
            journal_path=str(journal),
        )
    )
    for command in (["threads"], ["thread", "session-one"]):
        result = CliRunner().invoke(
            cli, ["--sessions-dir", str(store_path), *command, "--since", "all"], color=True
        )
        assert result.exit_code == 0, result.output
        assert "title" in result.output
        assert "\x1b" not in result.output
        assert "\x07" not in result.output
