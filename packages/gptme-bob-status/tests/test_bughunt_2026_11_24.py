"""Regression tests for gptme_bob_status.provider bug-hunt fixes (2026-11-24)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from gptme_bob_status import provider

VENV_BIN = Path(sys.executable).parent


def _make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "tasks").mkdir(parents=True)
    (ws / "gptme.toml").write_text("")
    (ws / "tasks" / "blocked-task.md").write_text(
        "---\nstate: waiting\ncreated: 2026-11-20\n"
        "waiting_for: Erik to approve the deploy\nwaiting_since: 2026-11-21\n---\n"
        "# Blocked task\n"
    )
    (ws / "tasks" / "backlog-task.md").write_text(
        "---\nstate: backlog\ncreated: 2026-11-20\n---\n# Backlog task\n"
    )
    (ws / "tasks" / "active-task.md").write_text(
        "---\nstate: active\ncreated: 2026-11-20\n---\n# Active task\n"
    )
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    return ws


@pytest.fixture
def real_gptodo(tmp_path, monkeypatch):
    """Route provider._run's gptodo calls to the real gptodo CLI in a temp workspace."""
    path = f"{VENV_BIN}{os.pathsep}{os.environ.get('PATH', '')}"
    if shutil.which("gptodo", path=path) is None:
        pytest.skip("gptodo CLI not installed")
    ws = _make_workspace(tmp_path)
    monkeypatch.chdir(ws)
    monkeypatch.setenv("GPTODO_TASKS_DIR", str(ws / "tasks"))
    monkeypatch.setenv("PATH", path)
    return ws


def test_blockers_surfaces_waiting_task(real_gptodo):
    # `waiting` is not a valid --state choice for `gptodo ready`; the old
    # invocation errored with empty stdout, so _blockers always returned [].
    blockers = provider._blockers(3)
    assert [b["id"] for b in blockers] == ["blocked-task"]


def test_active_tasks_returns_only_active_tasks(real_gptodo):
    # `gptodo status --compact` prints section headers like "📥 BACKLOG (1):"
    # plus backlog/todo rows; only rows under the ACTIVE header count.
    tasks = provider._active_tasks(3)
    ids = [t["id"] for t in tasks]
    assert ids == ["active-task"], ids


def test_journal_entries_ignore_non_date_dirs(tmp_path, monkeypatch):
    # Bob's real journal/ has a templates/ dir (daily.md, session.md).
    # "templates" sorts after "2026-..." so a reverse sort would list it first.
    root = tmp_path / "repo"
    (root / "journal" / "2026-11-24").mkdir(parents=True)
    (root / "journal" / "2026-11-24" / "session-abc.md").write_text("x")
    (root / "journal" / "templates").mkdir()
    (root / "journal" / "templates" / "session.md").write_text("tmpl")
    (root / "journal" / "templates" / "daily.md").write_text("tmpl")
    monkeypatch.setattr(provider, "_git_root", lambda: root)
    entries = provider._journal_entries(5)
    assert entries[0] == "journal/2026-11-24/session-abc.md", entries
    assert not any(e.startswith("journal/templates/") for e in entries), entries


def test_active_tasks_stops_at_hyphenated_section_header(monkeypatch):
    # "🚫 CLAIM-BLOCKED (1):" follows ACTIVE in real compact output; its
    # header and rows must not be read as active tasks.
    compact = (
        "📋 Tasks Status\n"
        "🏃 ACTIVE (1):\n"
        "  active-task  Do it  (5m ago)\n"
        "🚫 CLAIM-BLOCKED (1):\n"
        "  held-task [active] (2m ago) - held by someone\n"
        "📋 Summary: 2 total\n"
    )
    monkeypatch.setattr(provider, "_run", lambda cmd, **k: compact)
    assert [t["id"] for t in provider._active_tasks(3)] == ["active-task"]
