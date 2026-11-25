"""Tests for the accepted-outcome yield join (gptme_sessions.accepted_yield)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from gptme_sessions.accepted_yield import (
    compute_accepted_yield,
    format_accepted_yield,
    load_outcomes,
    session_items,
)
from gptme_sessions.cli import cli
from gptme_sessions.record import SessionRecord
from gptme_sessions.store import SessionStore

PR_URL = "https://github.com/Org/Repo/pull/7"


def _rec(kind: str | None = None, deliverables: list[str] | None = None, **cause: str) -> dict:
    dispatch = {"kind": kind, **cause} if kind or cause else None
    return {"dispatch_cause": dispatch, "deliverables": deliverables or []}


class TestSessionItems:
    def test_task_key_wins_over_pr_and_deliverables(self):
        rec = _rec("worker", [PR_URL], task="t1", pr="org/repo#9")
        assert session_items(rec) == ["task:t1"]

    def test_pr_key_is_lowercased(self):
        assert session_items(_rec("pm-slot", pr="Org/Repo#9")) == ["pr:org/repo#9"]

    def test_deliverable_prs_deduplicated_and_shas_ignored(self):
        rec = _rec(
            "event", [PR_URL, PR_URL + "#issuecomment-1", "abc1234", PR_URL.replace("7", "8")]
        )
        assert session_items(rec) == ["pr:org/repo#7", "pr:org/repo#8"]

    def test_task_key_is_lowercased_like_pr_keys(self):
        # The two namespaces join with the same rule, so a host that lowercases
        # its outcome keys still matches a mixed-case dispatch task id.
        rec = _rec("worker", [], task="T-123")
        assert session_items(rec) == ["task:t-123"]
        assert load_outcomes([{"item": "task:T-123", "outcome": "accepted"}]) == {
            "task:t-123": "accepted"
        }
        assert (
            compute_accepted_yield([rec], {"task:t-123": "accepted"})["worker"]["accepted_yield"]
            == 1.0
        )

    def test_unkeyed(self):
        assert session_items(_rec("fanout", ["abc1234"])) == []
        assert session_items({"dispatch_cause": None, "deliverables": None}) == []

    def test_accepts_session_record(self):
        rec = SessionRecord(dispatch_cause={"kind": "worker", "task": "t1"})
        assert session_items(rec) == ["task:t1"]


class TestLoadOutcomes:
    def test_last_row_wins(self):
        rows = [{"item": "task:a", "outcome": "pending"}, {"item": "task:a", "outcome": "accepted"}]
        assert load_outcomes(rows) == {"task:a": "accepted"}

    def test_unknown_label_rejected(self):
        with pytest.raises(ValueError, match="unknown outcome 'merged\\?'"):
            load_outcomes([{"item": "pr:o/r#1", "outcome": "merged?"}])

    def test_non_object_row_rejected(self):
        with pytest.raises(ValueError, match="expected a JSON object"):
            load_outcomes([["task:a", "accepted"]])  # type: ignore[list-item]

    def test_missing_item_rejected(self):
        with pytest.raises(ValueError, match="missing 'item'"):
            load_outcomes([{"outcome": "accepted"}])


class TestComputeAcceptedYield:
    def test_report_fields(self):
        records = [
            _rec("worker", task="a"),
            _rec("worker", task="b"),
            _rec("worker", task="b"),  # same item twice: counted once
            _rec("worker"),  # unkeyed
            _rec("fanout", [PR_URL]),
            {"dispatch_cause": None},  # no cause at all -> "unknown"
        ]
        outcomes = {"task:a": "accepted", "task:b": "zero", "pr:org/repo#7": "unverified"}
        report = compute_accepted_yield(records, outcomes)

        assert list(report) == ["worker", "fanout", "unknown"]
        w = report["worker"]
        assert w["sessions"] == 4
        assert w["keyed_sessions"] == 3
        assert w["unkeyed_sessions"] == 1
        assert w["coverage"] == 0.75
        assert w["items"] == 2
        assert w["outcomes"] == {"accepted": 1, "zero": 1}
        assert w["decided"] == 2
        assert w["accepted_yield"] == 0.5
        # Cost ceiling uses ALL sessions, including the unkeyed one.
        assert w["sessions_per_accepted"] == 4.0

        f = report["fanout"]
        assert f["outcomes"] == {"unverified": 1}
        assert f["decided"] == 0
        assert f["accepted_yield"] is None
        assert f["sessions_per_accepted"] is None

        u = report["unknown"]
        assert u["coverage"] == 0.0
        assert u["outcomes"] == {}

    def test_missing_outcome_is_pending(self):
        report = compute_accepted_yield([_rec("event", task="x")], {})
        assert report["event"]["outcomes"] == {"pending": 1}
        assert report["event"]["decided"] == 0

    def test_format_marks_unkeyed_and_na(self):
        text = format_accepted_yield(compute_accepted_yield([_rec("fanout")], {}), "last 7 days")
        assert "last 7 days" in text
        assert "unkeyed" in text.splitlines()[2]
        assert "n/a" in text


class TestYieldCommand:
    def _invoke(self, tmp_path: Path, *args: str):
        return CliRunner().invoke(cli, ["--sessions-dir", str(tmp_path), "yield", *args])

    def test_json_round_trip(self, tmp_path: Path):
        store = SessionStore(sessions_dir=tmp_path)
        store.append(SessionRecord(dispatch_cause={"kind": "worker", "task": "a"}))
        store.append(SessionRecord(dispatch_cause={"kind": "worker"}, deliverables=[PR_URL]))
        outcomes = tmp_path / "outcomes.jsonl"
        outcomes.write_text(
            json.dumps({"item": "task:a", "outcome": "accepted"})
            + "\n\n"
            + json.dumps({"item": "pr:org/repo#7", "outcome": "durable"})
            + "\n"
        )
        result = self._invoke(tmp_path, "--outcomes", str(outcomes), "--json")
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["worker"]["outcomes"] == {"accepted": 1, "durable": 1}
        assert data["worker"]["accepted_yield"] == 0.5
        assert data["worker"]["sessions_per_accepted"] == 2.0

    def test_text_output(self, tmp_path: Path):
        SessionStore(sessions_dir=tmp_path).append(
            SessionRecord(dispatch_cause={"kind": "event", "task": "a"})
        )
        result = self._invoke(tmp_path)
        assert result.exit_code == 0, result.output
        assert "event" in result.output
        assert "pending=1" in result.output

    def test_bad_outcome_file_is_a_usage_error(self, tmp_path: Path):
        bad = tmp_path / "bad.jsonl"
        bad.write_text(json.dumps({"item": "task:a", "outcome": "great"}) + "\n")
        result = self._invoke(tmp_path, "--outcomes", str(bad))
        assert result.exit_code == 1
        assert "unknown outcome 'great'" in result.output

    def test_empty_store(self, tmp_path: Path):
        SessionStore(sessions_dir=tmp_path)
        result = self._invoke(tmp_path, "--since", "all")
        assert result.exit_code == 0
        assert "No sessions found (all time)." in result.output
