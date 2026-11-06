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
    monkeypatch.delenv("GPTODO_AGENT_NAME", raising=False)
    return tmp_path


def _meta(workspace: Path, slug: str) -> dict:
    return frontmatter.load(workspace / "tasks" / f"{slug}.md").metadata


def test_wait_date_written_to_frontmatter(workspace: Path) -> None:
    result = CliRunner().invoke(cli, ["add", "--wait", "2099-01-15", "Later task"], input="")
    assert result.exit_code == 0, result.output
    assert str(_meta(workspace, "later-task")["wait"]) == "2099-01-15"


def test_wait_datetime_written_to_frontmatter(workspace: Path) -> None:
    result = CliRunner().invoke(cli, ["add", "--wait", "2099-01-15T09:30", "Timed task"], input="")
    assert result.exit_code == 0, result.output
    assert str(_meta(workspace, "timed-task")["wait"]).startswith("2099-01-15")
    assert "09:30" in (workspace / "tasks" / "timed-task.md").read_text()


def test_wait_hides_task_from_ready(workspace: Path) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["add", "--state", "todo", "--wait", "2099-01-15", "Hidden"], input="")
    runner.invoke(cli, ["add", "--state", "todo", "Visible"], input="")
    out = runner.invoke(cli, ["ready", "--json"]).output
    assert "visible" in out
    assert "hidden" not in out


def test_invalid_wait_rejected_without_creating(workspace: Path) -> None:
    result = CliRunner().invoke(cli, ["add", "--wait", "next tuesday", "Bad"], input="")
    assert result.exit_code != 0
    assert "Invalid --wait" in result.output
    assert not (workspace / "tasks" / "bad.md").exists()


def test_no_wait_by_default(workspace: Path) -> None:
    CliRunner().invoke(cli, ["add", "Plain"], input="")
    assert "wait" not in _meta(workspace, "plain")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("---\nwait: 2026-12-01\n---\nDetails", True),
        ("---\nwait: 2026-12-01\npriority: high\n---", True),
        ("Plain body", False),
        ("---\nJust a horizontal rule above prose\n---", False),
        ("---\nwait: 2026-12-01\nno closing delimiter", False),
        ("", False),
    ],
)
def test_body_looks_like_frontmatter(body: str, expected: bool) -> None:
    assert _body_looks_like_frontmatter(body) is expected


def test_frontmatter_like_body_warns(workspace: Path) -> None:
    result = CliRunner().invoke(
        cli, ["add", "--body-stdin", "Piped gate"], input="---\nwait: 2099-01-15\n---\nBody"
    )
    assert result.exit_code == 0, result.output
    assert "stored as body text" in result.output
    assert "wait" not in _meta(workspace, "piped-gate")
