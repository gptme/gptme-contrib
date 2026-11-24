"""Regression tests for gptme_daily_briefing.collectors bug-hunt fixes (2026-11-24)."""

from __future__ import annotations

import json
from pathlib import Path

from gptme_daily_briefing import collectors


def test_waiting_tasks_survives_non_mapping_frontmatter(tmp_path: Path):
    # One task file whose frontmatter parses to a non-dict (list/scalar) makes
    # fm.get() raise AttributeError outside the per-file try; it must be skipped
    # without dropping every other waiting task.
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "a-broken.md").write_text("---\n- just a list\n---\n# oops\n")
    (tasks / "b-waiting.md").write_text(
        "---\nstate: waiting\nwaiting_for: 'review on PR #1'\n---\n# t\n"
    )
    result = collectors.collect_waiting_tasks(tmp_path)
    assert result == [{"task": "b-waiting", "waiting_for": "review on PR #1"}]


def test_open_prs_tolerates_null_user(monkeypatch):
    # GitHub's REST schema marks pull.user as nullable; pr.get("user", {})
    # returns None for an explicit null, so .get("login") would raise
    # AttributeError (only KeyError is caught) and crash briefing collection.
    page = [
        {"number": 1, "title": "orphan", "user": None},
        {"number": 2, "title": "mine", "user": {"login": "bob"}, "draft": False},
    ]
    monkeypatch.setattr(collectors, "_run", lambda *a, **k: json.dumps(page))
    prs = collectors.collect_open_prs(["o/r"], "bob")
    assert [p["number"] for p in prs] == [2]
