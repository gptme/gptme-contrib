"""Tests for gptodo ready --skip-claimed: filters out cascade-claimed tasks."""

import json
import sqlite3
from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import _get_claimed_task_ids, cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_task(tasks_dir: Path, name: str, **metadata: object) -> None:
    lines = ["---"]
    for key, value in metadata.items():
        lines.append(f"{key}: {value}")
    lines.extend(["---", f"# {name}"])
    (tasks_dir / f"{name}.md").write_text("\n".join(lines))


def write_coord_db(db_path: Path, claims: list[dict]) -> None:
    """Write a minimal coordination DB with given claim rows."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS work (
                task_id TEXT NOT NULL,
                claimer TEXT,
                status TEXT NOT NULL,
                expires_at TEXT
            )"""
        )
        for c in claims:
            conn.execute(
                "INSERT INTO work (task_id, claimer, status, expires_at) VALUES (?,?,?,?)",
                (c["task_id"], c.get("claimer", "other-agent"), c["status"], c["expires_at"]),
            )


# ---------------------------------------------------------------------------
# _get_claimed_task_ids unit tests
# ---------------------------------------------------------------------------


class TestGetClaimedTaskIds:
    def test_returns_empty_when_db_absent(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        assert _get_claimed_task_ids(repo) == set()

    def test_strips_cascade_prefix(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state" / "coordination" / "coord.db"
        write_coord_db(
            db_path,
            [
                {
                    "task_id": "cascade:task:my-task",
                    "status": "claimed",
                    "expires_at": "2099-01-01 00:00:00",
                }
            ],
        )
        result = _get_claimed_task_ids(tmp_path)
        assert result == {"my-task"}

    def test_ignores_expired_claims(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state" / "coordination" / "coord.db"
        write_coord_db(
            db_path,
            [
                {
                    "task_id": "cascade:task:expired-task",
                    "status": "claimed",
                    "expires_at": "2000-01-01 00:00:00",  # past
                }
            ],
        )
        assert _get_claimed_task_ids(tmp_path) == set()

    def test_excludes_own_agent_claims(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "state" / "coordination" / "coord.db"
        monkeypatch.setenv("AGENT_ID", "me")
        write_coord_db(
            db_path,
            [
                {
                    "task_id": "cascade:task:my-own-task",
                    "claimer": "me",
                    "status": "claimed",
                    "expires_at": "2099-01-01 00:00:00",
                }
            ],
        )
        assert _get_claimed_task_ids(tmp_path) == set()


# ---------------------------------------------------------------------------
# gptodo ready --skip-claimed integration tests
# ---------------------------------------------------------------------------


class TestReadySkipClaimed:
    def _setup(self, tmp_path: Path) -> tuple[Path, Path]:
        repo = tmp_path / "repo"
        tasks_dir = repo / "tasks"
        tasks_dir.mkdir(parents=True)
        return repo, tasks_dir

    def test_skip_claimed_hides_claimed_task(self, tmp_path: Path) -> None:
        repo, tasks_dir = self._setup(tmp_path)
        write_task(tasks_dir, "free-task", state="backlog", created="2026-01-01T00:00:00")
        write_task(tasks_dir, "claimed-task", state="backlog", created="2026-01-01T00:00:00")
        db_path = repo / "state" / "coordination" / "coord.db"
        write_coord_db(
            db_path,
            [
                {
                    "task_id": "cascade:task:claimed-task",
                    "status": "claimed",
                    "expires_at": "2099-01-01 00:00:00",
                }
            ],
        )

        runner = CliRunner()
        result = runner.invoke(
            cli, ["--tasks-dir", str(tasks_dir), "ready", "--skip-claimed", "--jsonl"]
        )
        assert result.exit_code == 0, result.output
        names = [
            json.loads(line)["name"] for line in result.output.strip().splitlines() if line.strip()
        ]
        assert "free-task" in names
        assert "claimed-task" not in names

    def test_without_flag_shows_claimed_task(self, tmp_path: Path) -> None:
        repo, tasks_dir = self._setup(tmp_path)
        write_task(tasks_dir, "free-task", state="backlog", created="2026-01-01T00:00:00")
        write_task(tasks_dir, "claimed-task", state="backlog", created="2026-01-01T00:00:00")
        db_path = repo / "state" / "coordination" / "coord.db"
        write_coord_db(
            db_path,
            [
                {
                    "task_id": "cascade:task:claimed-task",
                    "status": "claimed",
                    "expires_at": "2099-01-01 00:00:00",
                }
            ],
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["--tasks-dir", str(tasks_dir), "ready", "--jsonl"])
        assert result.exit_code == 0, result.output
        names = [
            json.loads(line)["name"] for line in result.output.strip().splitlines() if line.strip()
        ]
        assert "claimed-task" in names

    def test_skip_claimed_degrades_without_db(self, tmp_path: Path) -> None:
        repo, tasks_dir = self._setup(tmp_path)
        write_task(tasks_dir, "my-task", state="backlog", created="2026-01-01T00:00:00")
        # No coord DB exists

        runner = CliRunner()
        result = runner.invoke(
            cli, ["--tasks-dir", str(tasks_dir), "ready", "--skip-claimed", "--jsonl"]
        )
        assert result.exit_code == 0, result.output
        names = [
            json.loads(line)["name"] for line in result.output.strip().splitlines() if line.strip()
        ]
        assert "my-task" in names
