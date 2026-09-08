"""Acceptance tests for the `draft` task state.

Draft is the hold for in-progress plans: filed so the plan isn't lost, but
excluded from ready/next/claim so eager agents cannot jump on unfinished work.
`paused` is *not* this hold — it normalizes to backlog.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from gptodo.checker import VALID_TRANSITIONS
from gptodo.cli import cli
from gptodo.frontmatter_compat import frontmatter
from gptodo.utils import get_canonical_states, load_tasks, normalize_state


def write_task(tasks_dir: Path, name: str, **metadata: object) -> Path:
    lines = ["---"]
    for key, value in metadata.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    lines.extend(["---", f"# {name}"])
    path = tasks_dir / f"{name}.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def _ready_ids(tmp_path: Path, *args: str) -> list[str]:
    result = CliRunner().invoke(cli, ["ready", *args, "--json"])
    assert result.exit_code == 0, result.output
    payload_text = result.output.split("\nNo ready tasks found", 1)[0]
    payload = json.loads(payload_text)
    return [t["id"] for t in payload["ready_tasks"]]


def test_draft_is_canonical_not_normalized() -> None:
    assert "draft" in get_canonical_states()
    assert normalize_state("draft", warn=False) == "draft"


def test_paused_is_not_a_guard() -> None:
    """`paused` still normalizes to backlog — it is not a draft/hold alias."""
    assert normalize_state("paused", warn=False) == "backlog"


def test_draft_transitions_out_to_backlog_todo_cancelled() -> None:
    assert set(VALID_TRANSITIONS["draft"]) == {"backlog", "todo", "cancelled"}


def test_ready_excludes_draft_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Bob"\n')
    write_task(
        tasks_dir,
        "in-progress-plan",
        state="draft",
        created="2026-09-08T00:00:00",
        priority="high",
    )
    write_task(
        tasks_dir,
        "real-task",
        state="backlog",
        created="2026-09-08T01:00:00",
        priority="low",
    )
    monkeypatch.chdir(tmp_path)

    ids = _ready_ids(tmp_path, "--state", "both")
    assert "in-progress-plan" not in ids
    assert "real-task" in ids

    ids = _ready_ids(tmp_path, "--state", "actionable")
    assert "in-progress-plan" not in ids
    assert "real-task" in ids


def test_ready_draft_explicit_query_returns_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "in-progress-plan", state="draft", created="2026-09-08T00:00:00")
    monkeypatch.chdir(tmp_path)

    ids = _ready_ids(tmp_path, "--state", "draft")
    assert ids == ["in-progress-plan"]


def test_next_skips_draft_even_when_higher_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "in-progress-plan",
        state="draft",
        created="2026-09-08T00:00:00",
        priority="high",
    )
    write_task(
        tasks_dir,
        "real-task",
        state="backlog",
        created="2026-09-08T01:00:00",
        priority="low",
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["next", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["next_task"]["id"] == "real-task"


def test_claim_refuses_draft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Bob"\n')
    path = write_task(tasks_dir, "in-progress-plan", state="draft", created="2026-09-08T00:00:00")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GPTODO_AGENT_NAME", raising=False)

    result = CliRunner().invoke(cli, ["claim", "in-progress-plan", "--agent", "bob"])
    assert result.exit_code != 0
    assert "Refusing to claim" in result.output
    meta = dict(frontmatter.load(path).metadata)
    assert meta["state"] == "draft"
    assert "assigned_to" not in meta
    assert "assigned_at" not in meta


def test_status_renders_draft_distinctly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Bob"\n')
    write_task(tasks_dir, "in-progress-plan", state="draft", created="2026-09-08T00:00:00")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "in-progress-plan" in result.output
    assert "📝" in result.output
    assert "DRAFT" in result.output.upper()


def test_status_compact_excludes_draft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "active-task", state="active", created="2026-09-08T00:00:00")
    write_task(tasks_dir, "in-progress-plan", state="draft", created="2026-09-08T00:00:00")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["status", "--compact"])
    assert result.exit_code == 0, result.output
    assert "active-task" in result.output
    assert "in-progress-plan" not in result.output


def test_legal_release_from_draft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "in-progress-plan", state="draft", created="2026-09-08T00:00:00")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["edit", "in-progress-plan", "--set", "state", "todo"])
    assert result.exit_code == 0, result.output
    assert "illegal" not in result.output.lower()
    assert load_tasks(tasks_dir)[0].metadata["state"] == "todo"


def test_add_accepts_draft_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "tasks").mkdir()
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Bob"\n')
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["add", "Planning still in flight", "--state", "draft"])
    assert result.exit_code == 0, result.output
    tasks = load_tasks(tmp_path / "tasks")
    assert len(tasks) == 1
    assert tasks[0].metadata["state"] == "draft"
