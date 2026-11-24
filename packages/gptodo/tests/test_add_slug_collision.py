"""``gptodo add`` never overwrites an existing task and always yields a usable id."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import frontmatter
import pytest
from click.testing import CliRunner

import gptodo.cli as gptodo_cli
from gptodo.cli import cli


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "tasks").mkdir()
    (tmp_path / "gptme.toml").write_text('[agent]\nname = "Bob"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GPTODO_TASKS_DIR", raising=False)
    monkeypatch.delenv("TASKS_REPO_ROOT", raising=False)
    monkeypatch.delenv("GPTODO_AGENT_NAME", raising=False)
    return tmp_path


class _FrozenDatetime(datetime):
    """Every call lands in the same second, like fast repeated invocations."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls(2026, 11, 24, 8, 38, 32, tzinfo=tz)


def test_same_title_in_same_second_never_overwrites(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gptodo_cli, "datetime", _FrozenDatetime)
    runner = CliRunner()
    for priority in ("low", "medium", "high"):
        result = runner.invoke(cli, ["add", "--priority", priority, "Same title"], input="")
        assert result.exit_code == 0, result.output

    files = sorted((workspace / "tasks").glob("*.md"))
    assert len(files) == 3, [f.name for f in files]
    priorities = sorted(frontmatter.load(f).metadata["priority"] for f in files)
    assert priorities == ["high", "low", "medium"]


@pytest.mark.parametrize("title", ["日本語のタスク", "!!!", "+++"])
def test_title_without_ascii_alnum_gets_usable_id(workspace: Path, title: str) -> None:
    runner = CliRunner()
    ids = []
    for _ in range(2):
        result = runner.invoke(cli, ["add", title], input="")
        assert result.exit_code == 0, result.output
        ids.append(result.stdout.strip().splitlines()[-1])

    assert len(set(ids)) == 2, ids
    for task_id in ids:
        assert task_id and not task_id.startswith(("-", ".")), task_id
        assert (workspace / "tasks" / f"{task_id}.md").exists()
        shown = runner.invoke(cli, ["show", task_id])
        assert shown.exit_code == 0, shown.output
        assert title in shown.output
