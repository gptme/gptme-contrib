"""CLI discovery and sync coverage for native Pi sessions."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from gptme_sessions import SessionRecord
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
    assert record.provider == "openai-codex"
    assert record.stop_reason == "stop"
    assert record.cost_usd == pytest.approx(0.0004264)
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


def test_sync_pi_missing_revision_backfills_route_metadata_once(
    tmp_path: Path, monkeypatch
) -> None:
    """A pre-revision Pi record gets one enrichment pass, then stays stable."""
    source = tmp_path / "legacy.jsonl"
    source.write_bytes(PI_FIXTURE.read_bytes())
    monkeypatch.setattr(
        "gptme_sessions.cli.discover_pi_sessions",
        lambda start, end: [source],
    )
    sessions_dir = tmp_path / "records"
    store = SessionStore(sessions_dir=sessions_dir)
    store.append(
        SessionRecord(
            harness="pi",
            trajectory_path=str(source.resolve()),
            outcome="productive",
            token_count=1317,
        )
    )
    command = [
        "--sessions-dir",
        str(sessions_dir),
        "sync",
        "--harness",
        "pi",
        "--signals",
    ]
    runner = CliRunner()

    first = runner.invoke(cli, command)
    assert first.exit_code == 0, first.output
    assert "updated 1" in first.output
    [enriched] = store.load_all()
    assert enriched.provider == "openai-codex"
    assert enriched.model == "gpt-5.6-luna"
    assert enriched.stop_reason == "stop"
    assert enriched.cost_usd == pytest.approx(0.0004264)
    assert enriched.trajectory_revision is not None

    second = runner.invoke(cli, command)
    assert second.exit_code == 0, second.output
    assert "1 unchanged" in second.output
    [stable] = store.load_all()
    assert stable.to_dict() == enriched.to_dict()


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


def test_sync_pi_deduplicates_aliases_within_one_run(tmp_path: Path, monkeypatch) -> None:
    alias_a = tmp_path / "pi-alias-a.jsonl"
    alias_b = tmp_path / "pi-alias-b.jsonl"
    alias_a.symlink_to(PI_FIXTURE.resolve())
    alias_b.symlink_to(PI_FIXTURE.resolve())
    monkeypatch.setattr(
        "gptme_sessions.cli.discover_pi_sessions",
        lambda start, end: [alias_a, PI_FIXTURE, alias_b],
    )
    sessions_dir = tmp_path / "records"

    result = CliRunner().invoke(
        cli,
        ["--sessions-dir", str(sessions_dir), "sync", "--harness", "pi"],
    )

    assert result.exit_code == 0, result.output
    assert "Imported 1 session(s)" in result.output
    assert len(SessionStore(sessions_dir=sessions_dir).load_all()) == 1


def test_sync_pi_refreshes_resumed_session_monotonically(tmp_path: Path, monkeypatch) -> None:
    """A Pi file is resumable after any turn; changed revisions refresh signals."""
    source = tmp_path / "resumed.jsonl"
    fixture_lines = PI_FIXTURE.read_text().splitlines(keepends=True)
    source.write_text(fixture_lines[0])
    monkeypatch.setattr(
        "gptme_sessions.cli.discover_pi_sessions",
        lambda start, end: [source],
    )
    sessions_dir = tmp_path / "records"
    runner = CliRunner()
    command = [
        "--sessions-dir",
        str(sessions_dir),
        "sync",
        "--harness",
        "pi",
        "--signals",
    ]

    first = runner.invoke(cli, command)
    assert first.exit_code == 0, first.output
    [initial] = SessionStore(sessions_dir=sessions_dir).load_all()
    initial_id = initial.session_id
    initial_revision = initial.trajectory_revision
    assert initial.outcome == "noop"
    assert initial.duration_seconds == 0
    assert initial.token_count is None
    assert initial_revision is not None

    source.write_text("".join(fixture_lines[:7]))
    second = runner.invoke(cli, command)
    assert second.exit_code == 0, second.output
    assert "updated 1" in second.output
    [midway] = SessionStore(sessions_dir=sessions_dir).load_all()
    midway_revision = midway.trajectory_revision
    assert midway.session_id == initial_id
    assert midway.outcome == "noop"
    assert midway.duration_seconds == 2
    assert midway.token_count == 344
    assert midway.stop_reason == "toolUse"
    assert midway.cost_usd == pytest.approx(0.0001448)
    assert midway_revision not in (None, initial_revision)

    source.write_text("".join(fixture_lines))
    third = runner.invoke(cli, command)
    assert third.exit_code == 0, third.output
    assert "updated 1" in third.output
    [complete] = SessionStore(sessions_dir=sessions_dir).load_all()
    assert complete.session_id == initial_id
    assert complete.outcome == "productive"
    assert complete.duration_seconds == 6
    assert complete.token_count == 1317
    assert complete.provider == "openai-codex"
    assert complete.stop_reason == "stop"
    assert complete.cost_usd == pytest.approx(0.0004264)
    assert "test: create Pi fixture (526d692)" in complete.deliverables
    assert complete.trajectory_revision not in (None, midway_revision)

    unchanged = runner.invoke(cli, command)
    assert unchanged.exit_code == 0, unchanged.output
    assert "1 unchanged" in unchanged.output
    [stable] = SessionStore(sessions_dir=sessions_dir).load_all()
    assert stable.to_dict() == complete.to_dict()


def test_sync_pi_extraction_failure_does_not_advance_revision(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "retry.jsonl"
    source.write_bytes(PI_FIXTURE.read_bytes())
    monkeypatch.setattr(
        "gptme_sessions.cli.discover_pi_sessions",
        lambda start, end: [source],
    )
    monkeypatch.setattr(
        "gptme_sessions.cli.extract_from_path",
        lambda path: (_ for _ in ()).throw(RuntimeError("injected extraction failure")),
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
            "--signals",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "signals extraction failed" in result.output
    [record] = SessionStore(sessions_dir=sessions_dir).load_all()
    assert record.trajectory_revision is None


def test_sync_pi_extractor_time_append_keeps_pre_read_revision(tmp_path: Path, monkeypatch) -> None:
    """An append during extraction must force another refresh on the next sync."""
    import gptme_sessions.cli as cli_module

    source = tmp_path / "concurrent.jsonl"
    fixture_lines = PI_FIXTURE.read_text().splitlines(keepends=True)
    source.write_text("".join(fixture_lines[:7]))
    monkeypatch.setattr(
        "gptme_sessions.cli.discover_pi_sessions",
        lambda start, end: [source],
    )
    real_extract = cli_module.extract_from_path
    revision_during_extract: str | None = None

    def append_after_extract(path: Path) -> dict:
        nonlocal revision_during_extract
        result = real_extract(path)
        revision_during_extract = cli_module._trajectory_revision(path)
        path.write_text("".join(fixture_lines))
        return result

    monkeypatch.setattr("gptme_sessions.cli.extract_from_path", append_after_extract)
    sessions_dir = tmp_path / "records"
    command = [
        "--sessions-dir",
        str(sessions_dir),
        "sync",
        "--harness",
        "pi",
        "--signals",
    ]
    runner = CliRunner()

    first = runner.invoke(cli, command)
    assert first.exit_code == 0, first.output
    [partial] = SessionStore(sessions_dir=sessions_dir).load_all()
    assert partial.token_count == 344
    assert partial.trajectory_revision == revision_during_extract
    assert partial.trajectory_revision != cli_module._trajectory_revision(source)

    monkeypatch.setattr("gptme_sessions.cli.extract_from_path", real_extract)
    second = runner.invoke(cli, command)
    assert second.exit_code == 0, second.output
    assert "updated 1" in second.output
    [complete] = SessionStore(sessions_dir=sessions_dir).load_all()
    assert complete.token_count == 1317
    assert complete.outcome == "productive"


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
