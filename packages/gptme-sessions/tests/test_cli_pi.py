"""CLI discovery and sync coverage for native Pi sessions."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from gptme_sessions.cli import _discover_all, cli
from gptme_sessions.discovery import extract_session_name
from gptme_sessions.pi import PiSessionFormatError
from gptme_sessions.store import SessionStore


PI_FIXTURE = Path(__file__).parent / "fixtures" / "pi" / "productive-codex.jsonl"


def test_discover_all_isolates_pi_metadata_reread_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One file changing between discovery and metadata reads cannot abort siblings."""
    changing = tmp_path / "changing.jsonl"
    changing.touch()
    monkeypatch.setattr(
        "gptme_sessions.cli.discover_pi_sessions",
        lambda start, end: [changing, PI_FIXTURE],
    )

    def extract_name(harness: str, path: Path) -> str | None:
        if path == changing:
            raise PiSessionFormatError("concurrent append exposed a future entry")
        return extract_session_name(harness, path)

    monkeypatch.setattr("gptme_sessions.cli.extract_session_name", extract_name)

    with caplog.at_level(logging.WARNING):
        discovered = _discover_all(since_days=1, harness_filter="pi")

    assert [entry["path"] for entry in discovered] == [PI_FIXTURE]
    assert "changed during metadata extraction" in caplog.text


def test_discover_pi_with_signals(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "gptme_sessions.cli.discover_pi_sessions",
        lambda start, end: [PI_FIXTURE],
    )

    result = CliRunner().invoke(
        cli,
        [
            "--sessions-dir",
            str(tmp_path / "records"),
            "discover",
            "--harness",
            "pi",
            "--signals",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total_discovered"] == 1
    [session] = payload["sessions"]
    assert session["harness"] == "pi"
    assert session["path"] == str(PI_FIXTURE)
    assert session["session_date"] == "2026-08-31"
    assert session["productive"] is True
    assert session["tool_calls"] > 0
    assert session["git_commits"] == 1
    assert session["synced"] is False


def test_sync_pi_with_signals_retains_source_and_deduplicates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "gptme_sessions.cli.discover_pi_sessions",
        lambda start, end: [PI_FIXTURE],
    )
    source_bytes = PI_FIXTURE.read_bytes()
    source_stat = PI_FIXTURE.stat()
    sessions_dir = tmp_path / "records"
    runner = CliRunner()

    first = runner.invoke(
        cli,
        [
            "--sessions-dir",
            str(sessions_dir),
            "sync",
            "--harness",
            "pi",
            "--signals",
        ],
    )

    assert first.exit_code == 0, first.output
    assert "Imported 1 session(s)" in first.output
    [record] = SessionStore(sessions_dir=sessions_dir).load_all()
    assert record.harness == "pi"
    assert record.trajectory_path == str(PI_FIXTURE.resolve())
    assert record.timestamp == "2026-08-31T18:25:22.018000+00:00"
    assert record.session_name == "pi-fixture-productive-codex-001"
    assert record.project == "/workspace/pi-fixture-productive"
    assert record.model == "gpt-5.6-luna"
    assert record.outcome == "productive"
    assert record.token_count == 1317
    assert record.input_tokens == 1154
    assert record.output_tokens == 163
    assert record.cache_read_tokens == 0
    assert record.cache_creation_tokens == 0
    assert "test: create Pi fixture (526d692)" in record.deliverables

    second = runner.invoke(
        cli,
        [
            "--sessions-dir",
            str(sessions_dir),
            "sync",
            "--harness",
            "pi",
            "--signals",
        ],
    )
    assert second.exit_code == 0, second.output
    assert "1 unchanged" in second.output
    assert len(SessionStore(sessions_dir=sessions_dir).load_all()) == 1

    unsynced = runner.invoke(
        cli,
        [
            "--sessions-dir",
            str(sessions_dir),
            "discover",
            "--harness",
            "pi",
            "--unsynced",
            "--json",
        ],
    )
    assert unsynced.exit_code == 0, unsynced.output
    assert json.loads(unsynced.output) == {"sessions": [], "total_discovered": 1}

    after_stat = PI_FIXTURE.stat()
    assert PI_FIXTURE.read_bytes() == source_bytes
    assert after_stat.st_ino == source_stat.st_ino
    assert after_stat.st_mtime_ns == source_stat.st_mtime_ns
    assert after_stat.st_mode == source_stat.st_mode


def test_sync_pi_canonicalizes_symlink_aliases(tmp_path: Path, monkeypatch) -> None:
    alias = tmp_path / "pi-alias.jsonl"
    alias.symlink_to(PI_FIXTURE.resolve())
    discovered = [alias]
    monkeypatch.setattr(
        "gptme_sessions.cli.discover_pi_sessions",
        lambda start, end: list(discovered),
    )
    sessions_dir = tmp_path / "records"
    runner = CliRunner()

    first = runner.invoke(
        cli,
        ["--sessions-dir", str(sessions_dir), "sync", "--harness", "pi"],
    )
    assert first.exit_code == 0, first.output
    [record] = SessionStore(sessions_dir=sessions_dir).load_all()
    assert record.trajectory_path == str(PI_FIXTURE.resolve())

    discovered[:] = [PI_FIXTURE]
    second = runner.invoke(
        cli,
        ["--sessions-dir", str(sessions_dir), "sync", "--harness", "pi"],
    )
    assert second.exit_code == 0, second.output
    assert "1 unchanged" in second.output
    assert len(SessionStore(sessions_dir=sessions_dir).load_all()) == 1


def test_sync_pi_dry_run_does_not_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "gptme_sessions.cli.discover_pi_sessions",
        lambda start, end: [PI_FIXTURE],
    )
    sessions_dir = tmp_path / "records"

    result = CliRunner().invoke(
        cli,
        [
            "--sessions-dir",
            str(sessions_dir),
            "sync",
            "--harness",
            "pi",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "would import: pi" in result.output
    assert SessionStore(sessions_dir=sessions_dir).load_all() == []
