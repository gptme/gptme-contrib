"""``gptodo add --wait`` writes a real frontmatter wait gate."""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest
from click.testing import CliRunner

from gptodo.cli import _body_looks_like_frontmatter, cli


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "tasks").mkdir()
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Bob"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GPTODO_TASKS_DIR", raising=False)
    monkeypatch.delenv("GPTODO_AGENT_NAME", raising=False)
    return tmp_path


def _meta(workspace: Path, slug: str) -> dict:
    return frontmatter.load(workspace / "tasks" / f"{slug}.md").metadata


def test_wait_date_written_to_frontmatter(workspace: Path) -> None:
    result = CliRunner().invoke(cli, ["add", "--wait", "2099-01-15", "Later task"], input="")
    assert result.exit_code == 0, result.output
    assert str(_meta(workspace, "later-task")["wait"]) == "2099-01-15"


@pytest.mark.parametrize(
    "value", ["2099-01-15T09:30", "2099-01-15 09:30", "2099-01-15T09:30:00+02:00"]
)
def test_wait_datetime_written_to_frontmatter(workspace: Path, value: str) -> None:
    from datetime import datetime

    result = CliRunner().invoke(cli, ["add", "--wait", value, "Timed task"], input="")
    assert result.exit_code == 0, result.output
    assert _meta(workspace, "timed-task")["wait"] == datetime.fromisoformat(value)
    ready = CliRunner().invoke(cli, ["ready", "--json"])
    assert ready.exit_code == 0, ready.output
    assert "timed-task" not in ready.output


def test_wait_hides_task_from_ready(workspace: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["add", "--state", "todo", "--wait", "2099-01-15", "Hidden"], input="")
    runner.invoke(cli, ["add", "--state", "todo", "Visible"], input="")
    out = runner.invoke(cli, ["ready", "--json"]).output
    assert "visible" in out
    assert "hidden" not in out


@pytest.mark.parametrize(
    "value", ["next tuesday", "2099-01-15junk", "2099-01-15t09:30", "2099-02-30", ""]
)
def test_invalid_wait_rejected_without_creating(workspace: Path, value: str) -> None:
    result = CliRunner().invoke(cli, ["add", "--wait", value, "Bad"], input="")
    assert result.exit_code != 0
    assert "Invalid --wait" in result.output
    assert not (workspace / "tasks" / "bad.md").exists()


def test_wait_rejected_for_ready_for_review(workspace: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["add", "--state", "ready_for_review", "--wait", "2099-01-15", "Later review"],
        input="",
    )
    assert result.exit_code != 0
    assert "--wait cannot be used with --state ready_for_review" in result.output
    assert not (workspace / "tasks" / "later-review.md").exists()


def test_no_wait_by_default(workspace: Path) -> None:
    CliRunner().invoke(cli, ["add", "Plain"], input="")
    assert "wait" not in _meta(workspace, "plain")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("---\nwait: 2026-12-01\n---\nDetails", True),
        ("---\nwait: 2026-12-01\npriority: high\n---", True),
        ("---\n# gate\nwait: 2099-01-15\n---", True),
        ("---\nwait: 2099-01-15\ntags:\n  - followup\n---", True),
        ("---\nwait: 2099-01-15\nnext_action: |\n  Follow up later\n---", True),
        ("---\n---", False),
        ("---\n# Just a comment\n---", False),
        ("Plain body", False),
        ("---\nJust a horizontal rule above prose\n---", False),
        ("---\nwait: 2026-12-01\nno closing delimiter", False),
        ("", False),
    ],
)
def test_body_looks_like_frontmatter(body: str, expected: bool) -> None:
    assert _body_looks_like_frontmatter(body) is expected


@pytest.mark.parametrize(
    "block",
    [
        "wait: 2099-01-15",
        "wait: 2099-01-15\ntags:\n  - followup",
        "# A deferred task\nwait: 2099-01-15",
        "wait: 2099-01-15\nnext_action: |\n  Follow up later",
    ],
)
def test_frontmatter_like_body_warns(workspace: Path, block: str) -> None:
    body = f"---\n{block}\n---\nBody"
    result = CliRunner().invoke(cli, ["add", "--body-stdin", "Piped gate"], input=body)
    assert result.exit_code == 0, result.output
    assert "stored as body text" in result.stderr
    assert "wait" not in _meta(workspace, "piped-gate")
    task = frontmatter.load(workspace / "tasks" / "piped-gate.md")
    assert task.content.endswith(body)
