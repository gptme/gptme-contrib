"""Tests for gptme-sessions CLI commands: query, show, stats, runs, annotate, append."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from gptme_sessions.cli import cli
from gptme_sessions.record import SessionRecord
from gptme_sessions.store import SessionStore


# -- Helpers -----------------------------------------------------------------


def _seed_store(tmp_path: Path, n: int = 5) -> SessionStore:
    """Create a store with N records of varying attributes."""
    store = SessionStore(sessions_dir=tmp_path)
    models = ["opus", "sonnet", "haiku", "opus", "sonnet"]
    categories = ["code", "infrastructure", "triage", "code", "hygiene"]
    outcomes = ["productive", "productive", "noop", "failed", "productive"]
    run_types = ["autonomous", "autonomous", "monitoring", "autonomous", "dispatch"]
    for i in range(n):
        r = SessionRecord(
            harness="claude-code",
            model=models[i % len(models)],
            run_type=run_types[i % len(run_types)],
            category=categories[i % len(categories)],
            outcome=outcomes[i % len(outcomes)],
            duration_seconds=600 + i * 120,
            deliverables=[f"abc{i:04d}"] if outcomes[i % len(outcomes)] == "productive" else [],
        )
        store.append(r)
    return store


def _invoke(args: list[str], tmp_path: Path) -> tuple[int, str]:
    """Run CLI via CliRunner, return (exit_code, output)."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--sessions-dir", str(tmp_path)] + args)
    return result.exit_code, result.output


# -- query -------------------------------------------------------------------


class TestQueryCommand:
    def test_query_lists_records(self, tmp_path: Path):
        """query lists all records with human-readable output."""
        store = _seed_store(tmp_path)
        records = store.load_all()
        rc, out = _invoke(["query"], tmp_path)
        assert rc == 0
        assert f"{len(records)} records" in out

    def test_query_json_output(self, tmp_path: Path):
        """query --json produces valid JSON array."""
        _seed_store(tmp_path)
        rc, out = _invoke(["query", "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert isinstance(data, list)
        assert len(data) == 5

    def test_query_filter_by_model(self, tmp_path: Path):
        """query --model filters records."""
        _seed_store(tmp_path)
        rc, out = _invoke(["query", "--model", "opus", "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert all(r.get("model_normalized") == "opus" or r.get("model") == "opus" for r in data)
        assert len(data) == 2

    def test_query_filter_by_outcome(self, tmp_path: Path):
        """query --outcome filters records."""
        _seed_store(tmp_path)
        rc, out = _invoke(["query", "--outcome", "noop", "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert all(r["outcome"] == "noop" for r in data)
        assert len(data) == 1

    def test_query_filter_by_category(self, tmp_path: Path):
        """query --category filters records."""
        _seed_store(tmp_path)
        rc, out = _invoke(["query", "--category", "code", "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert all(r.get("category") == "code" for r in data)
        assert len(data) == 2

    def test_query_filter_by_harness(self, tmp_path: Path):
        """query --harness filters records."""
        _seed_store(tmp_path)
        rc, out = _invoke(["query", "--harness", "gptme", "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        # All seeded records are claude-code, so gptme filter returns empty
        assert len(data) == 0

    def test_query_filter_by_run_type(self, tmp_path: Path):
        """query --run-type filters records."""
        _seed_store(tmp_path)
        rc, out = _invoke(["query", "--run-type", "monitoring", "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert len(data) == 1
        assert data[0]["run_type"] == "monitoring"

    def test_query_stats_flag(self, tmp_path: Path):
        """query --stats shows statistics (format_stats writes to sys.stdout)."""
        _seed_store(tmp_path)
        rc, out = _invoke(["query", "--stats"], tmp_path)
        assert rc == 0
        # format_stats writes to sys.stdout, not click.echo — verify via --json
        rc2, out2 = _invoke(["query", "--stats", "--json"], tmp_path)
        assert rc2 == 0
        data = json.loads(out2)
        assert data["total"] == 5

    def test_query_stats_json(self, tmp_path: Path):
        """query --stats --json outputs stats as JSON."""
        _seed_store(tmp_path)
        rc, out = _invoke(["query", "--stats", "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert "total" in data
        assert data["total"] == 5

    def test_query_empty_store(self, tmp_path: Path):
        """query on empty store reports 0 records."""
        SessionStore(sessions_dir=tmp_path)
        rc, out = _invoke(["query"], tmp_path)
        assert rc == 0
        assert "0 records" in out

    def test_query_combined_filters(self, tmp_path: Path):
        """query with multiple filters intersects them."""
        _seed_store(tmp_path)
        rc, out = _invoke(
            ["query", "--model", "opus", "--outcome", "productive", "--json"],
            tmp_path,
        )
        assert rc == 0
        data = json.loads(out)
        assert len(data) == 1
        assert data[0]["outcome"] == "productive"


# -- show --------------------------------------------------------------------


class TestShowCommand:
    def test_show_by_full_id(self, tmp_path: Path):
        """show displays details for a session by full ID."""
        store = _seed_store(tmp_path)
        records = store.load_all()
        session_id = records[0].session_id
        rc, out = _invoke(["show", session_id], tmp_path)
        assert rc == 0
        assert session_id in out
        assert "Harness:" in out
        assert "Model:" in out

    def test_show_by_prefix(self, tmp_path: Path):
        """show matches by ID prefix."""
        store = _seed_store(tmp_path)
        records = store.load_all()
        session_id = records[0].session_id
        # Use first 8 chars as prefix (should be unique with 5 records)
        prefix = session_id[:8]
        rc, out = _invoke(["show", prefix], tmp_path)
        assert rc == 0
        assert session_id in out

    def test_show_json(self, tmp_path: Path):
        """show --json outputs record as JSON."""
        store = _seed_store(tmp_path)
        records = store.load_all()
        session_id = records[0].session_id
        rc, out = _invoke(["show", session_id, "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert data["session_id"] == session_id

    def test_show_not_found(self, tmp_path: Path):
        """show with unknown ID exits with error."""
        _seed_store(tmp_path)
        rc, out = _invoke(["show", "nonexistent000"], tmp_path)
        assert rc != 0

    def test_show_displays_deliverables(self, tmp_path: Path):
        """show includes deliverables in output."""
        store = _seed_store(tmp_path)
        records = store.load_all()
        productive = [r for r in records if r.outcome == "productive"][0]
        rc, out = _invoke(["show", productive.session_id], tmp_path)
        assert rc == 0
        assert "Deliverables:" in out

    def test_show_displays_duration(self, tmp_path: Path):
        """show formats duration as human-readable."""
        store = _seed_store(tmp_path)
        records = store.load_all()
        session_id = records[0].session_id
        rc, out = _invoke(["show", session_id], tmp_path)
        assert rc == 0
        assert "Duration:" in out
        assert "m" in out  # minutes indicator

    def test_show_displays_category(self, tmp_path: Path):
        """show displays category field."""
        store = _seed_store(tmp_path)
        records = store.load_all()
        session_id = records[0].session_id
        rc, out = _invoke(["show", session_id], tmp_path)
        assert rc == 0
        assert "Category:" in out


# -- stats -------------------------------------------------------------------


class TestStatsCommand:
    def test_stats_basic(self, tmp_path: Path):
        """stats shows summary for all records."""
        _seed_store(tmp_path)
        rc, out = _invoke(["stats"], tmp_path)
        assert rc == 0
        # format_stats writes to sys.stdout directly, not click.echo
        # Validate content via --json variant instead
        rc2, out2 = _invoke(["stats", "--json"], tmp_path)
        data = json.loads(out2)
        assert data["total"] == 5

    def test_stats_json(self, tmp_path: Path):
        """stats --json outputs structured data."""
        _seed_store(tmp_path)
        rc, out = _invoke(["stats", "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert data["total"] == 5

    def test_stats_with_model_filter(self, tmp_path: Path):
        """stats --model filters before computing."""
        _seed_store(tmp_path)
        rc, out = _invoke(["stats", "--model", "opus", "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert data["total"] == 2

    def test_stats_empty_store(self, tmp_path: Path):
        """stats on empty store shows discovery fallback."""
        SessionStore(sessions_dir=tmp_path)
        rc, out = _invoke(["stats"], tmp_path)
        assert rc == 0
        assert "discover" in out.lower() or "sync" in out.lower() or "session" in out.lower()

    def test_stats_empty_store_no_duplicate_hint(self, tmp_path: Path):
        """stats on empty store with discovered sessions shows sync hint exactly once.

        _show_discovery_fallback already prints a sync recommendation; the new
        _count_unsynced hint must be suppressed in this code path to avoid
        printing two nearly identical sync suggestions.
        """
        from unittest.mock import patch

        SessionStore(sessions_dir=tmp_path)
        fake_discovered = [
            {"harness": "claude-code", "path": Path("/fake/session1.jsonl")},
        ]
        with patch("gptme_sessions.cli._discover_all", return_value=fake_discovered):
            rc, out = _invoke(["stats"], tmp_path)
        assert rc == 0
        # Exactly one sync recommendation — not two
        assert (
            out.count("gptme-sessions sync") == 1
        ), f"Expected exactly one sync recommendation, got:\n{out}"

    def test_stats_no_matches_with_filter(self, tmp_path: Path):
        """stats with filter that matches nothing shows appropriate message."""
        _seed_store(tmp_path)
        rc, out = _invoke(["stats", "--model", "nonexistent"], tmp_path)
        assert rc == 0
        assert "no records" in out.lower()

    def test_stats_no_matches_shows_unsynced_hint(self, tmp_path: Path):
        """stats with filter that matches nothing shows hint when unsynced sessions exist."""
        from unittest.mock import patch

        _seed_store(tmp_path)
        # Mock _discover_all to return fake unsynced sessions
        fake_discovered = [
            {"harness": "claude-code", "path": Path("/fake/session1.jsonl")},
            {"harness": "gptme", "path": Path("/fake/session2.jsonl")},
        ]
        with patch("gptme_sessions.cli._discover_all", return_value=fake_discovered):
            rc, out = _invoke(["stats", "--model", "nonexistent"], tmp_path)
        assert rc == 0
        assert "no records" in out.lower()
        assert "hint" in out.lower()
        assert "2 session(s) discovered but not synced" in out
        assert "sync" in out

    def test_stats_no_matches_no_hint_when_all_synced(self, tmp_path: Path):
        """stats with filter that matches nothing shows no hint when all sessions are synced."""
        from unittest.mock import patch

        _seed_store(tmp_path)
        # Mock _discover_all to return empty (nothing to sync)
        with patch("gptme_sessions.cli._discover_all", return_value=[]):
            rc, out = _invoke(["stats", "--model", "nonexistent"], tmp_path)
        assert rc == 0
        assert "no records" in out.lower()
        assert "hint" not in out.lower()

    def test_stats_no_matches_no_hint_when_synced_via_trajectory_path(self, tmp_path: Path):
        """stats shows no hint when discovered sessions are already synced via trajectory_path.

        This is the primary sync workflow: sync writes trajectory_path (not journal_path)
        on imported records, so _count_unsynced must check both fields.
        """
        from unittest.mock import patch

        store = SessionStore(sessions_dir=tmp_path)
        fake_paths = ["/fake/logs/session1.jsonl", "/fake/logs/session2.jsonl"]
        for path in fake_paths:
            r = SessionRecord(
                harness="claude-code",
                model="sonnet",
                run_type="autonomous",
                category="code",
                outcome="productive",
                duration_seconds=600,
                trajectory_path=path,
            )
            store.append(r)

        fake_discovered = [{"harness": "claude-code", "path": Path(p)} for p in fake_paths]
        with patch("gptme_sessions.cli._discover_all", return_value=fake_discovered):
            rc, out = _invoke(["stats", "--model", "nonexistent"], tmp_path)
        assert rc == 0
        assert "no records" in out.lower()
        assert "hint" not in out.lower(), f"False-positive hint shown:\n{out}"

    def test_stats_with_results_shows_unsynced_hint(self, tmp_path: Path):
        """stats with matching results still shows hint when unsynced sessions exist."""
        from unittest.mock import patch

        _seed_store(tmp_path)
        fake_discovered = [
            {"harness": "claude-code", "path": Path("/fake/new-session.jsonl")},
        ]
        with patch("gptme_sessions.cli._discover_all", return_value=fake_discovered):
            rc, out = _invoke(["stats"], tmp_path)
        assert rc == 0
        assert "hint" in out.lower()
        assert "1 session(s) discovered but not synced" in out

    def test_stats_shows_model_breakdown(self, tmp_path: Path):
        """stats --json includes per-model breakdown."""
        _seed_store(tmp_path)
        rc, out = _invoke(["stats", "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert "by_model" in data
        assert "opus" in data["by_model"]
        assert "sonnet" in data["by_model"]

    def test_stats_shows_run_type_breakdown(self, tmp_path: Path):
        """stats --json includes per-run-type breakdown."""
        _seed_store(tmp_path)
        rc, out = _invoke(["stats", "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert "by_run_type" in data
        assert "autonomous" in data["by_run_type"]


# -- runs --------------------------------------------------------------------


class TestRunsCommand:
    def test_runs_basic(self, tmp_path: Path):
        """runs shows analytics for recent sessions."""
        _seed_store(tmp_path)
        rc, out = _invoke(["runs"], tmp_path)
        assert rc == 0

    def test_runs_json(self, tmp_path: Path):
        """runs --json outputs analytics as JSON."""
        _seed_store(tmp_path)
        rc, out = _invoke(["runs", "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert "total" in data

    def test_runs_since_filter(self, tmp_path: Path):
        """runs --since filters by time window."""
        _seed_store(tmp_path)
        rc, out = _invoke(["runs", "--since", "7d", "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert isinstance(data, dict)

    def test_runs_empty_store(self, tmp_path: Path):
        """runs on empty store shows discovery fallback."""
        SessionStore(sessions_dir=tmp_path)
        rc, out = _invoke(["runs"], tmp_path)
        assert rc == 0

    def test_runs_json_has_outcome_counts(self, tmp_path: Path):
        """runs --json includes outcome counts."""
        _seed_store(tmp_path)
        rc, out = _invoke(["runs", "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        if data.get("total", 0) > 0:
            assert "outcomes" in data or "noop_by_run_type" in data


# -- annotate ----------------------------------------------------------------


class TestAnnotateCommand:
    def test_annotate_updates_category(self, tmp_path: Path):
        """annotate updates a record's category."""
        store = _seed_store(tmp_path)
        records = store.load_all()
        session_id = records[0].session_id
        rc, out = _invoke(
            ["annotate", session_id, "--category", "strategic"],
            tmp_path,
        )
        assert rc == 0
        # Reload and verify
        updated = store.load_all()
        record = [r for r in updated if r.session_id == session_id][0]
        assert record.category == "strategic"

    def test_annotate_updates_outcome(self, tmp_path: Path):
        """annotate can change outcome."""
        store = _seed_store(tmp_path)
        records = store.load_all()
        session_id = records[0].session_id
        rc, out = _invoke(
            ["annotate", session_id, "--outcome", "noop"],
            tmp_path,
        )
        assert rc == 0
        updated = store.load_all()
        record = [r for r in updated if r.session_id == session_id][0]
        assert record.outcome == "noop"

    def test_annotate_updates_model(self, tmp_path: Path):
        """annotate can change model."""
        store = _seed_store(tmp_path)
        records = store.load_all()
        session_id = records[0].session_id
        rc, out = _invoke(
            ["annotate", session_id, "--model", "haiku"],
            tmp_path,
        )
        assert rc == 0
        updated = store.load_all()
        record = [r for r in updated if r.session_id == session_id][0]
        assert record.model == "haiku"

    def test_annotate_not_found(self, tmp_path: Path):
        """annotate with unknown session ID fails."""
        _seed_store(tmp_path)
        rc, out = _invoke(
            ["annotate", "nonexistent000", "--category", "code"],
            tmp_path,
        )
        assert rc != 0


# -- append (deprecated) -----------------------------------------------------


class TestAppendCommand:
    def test_append_creates_record(self, tmp_path: Path):
        """append still creates records (with deprecation warning)."""
        rc, out = _invoke(
            [
                "append",
                "--harness",
                "gptme",
                "--model",
                "opus",
                "--outcome",
                "productive",
                "--duration",
                "900",
            ],
            tmp_path,
        )
        assert rc == 0
        # Verify record was created
        store = SessionStore(sessions_dir=tmp_path)
        records = store.load_all()
        assert len(records) == 1
        assert records[0].harness == "gptme"

    def test_append_with_deliverables(self, tmp_path: Path):
        """append accepts multiple --deliverables flags."""
        rc, out = _invoke(
            [
                "append",
                "--harness",
                "claude-code",
                "--outcome",
                "productive",
                "--deliverables",
                "abc1234",
                "--deliverables",
                "def5678",
            ],
            tmp_path,
        )
        assert rc == 0
        store = SessionStore(sessions_dir=tmp_path)
        records = store.load_all()
        assert len(records[0].deliverables) == 2


# -- _parse_since helper ----------------------------------------------------


class TestParseSince:
    def test_parse_since_with_d_suffix(self, tmp_path: Path):
        """--since 7d parses correctly."""
        _seed_store(tmp_path)
        rc, out = _invoke(["runs", "--since", "7d"], tmp_path)
        assert rc == 0

    def test_parse_since_bare_number(self, tmp_path: Path):
        """--since 30 (no suffix) parses as days."""
        _seed_store(tmp_path)
        rc, out = _invoke(["runs", "--since", "30"], tmp_path)
        assert rc == 0

    def test_parse_since_invalid(self, tmp_path: Path):
        """--since with invalid value fails."""
        _seed_store(tmp_path)
        rc, out = _invoke(["runs", "--since", "abc"], tmp_path)
        assert rc != 0


# -- top-level invocation (no subcommand) ------------------------------------


class TestTopLevelCli:
    def test_no_subcommand_shows_stats(self, tmp_path: Path):
        """Invoking without subcommand shows stats (format_stats → sys.stdout)."""
        _seed_store(tmp_path)
        rc, out = _invoke([], tmp_path)
        assert rc == 0
        # format_stats writes to sys.stdout (not captured by CliRunner)
        # but the tip line IS captured via click.echo
        # Just verify it doesn't error

    def test_no_subcommand_empty_store(self, tmp_path: Path):
        """Empty store without subcommand shows discovery message."""
        SessionStore(sessions_dir=tmp_path)
        rc, out = _invoke([], tmp_path)
        assert rc == 0
        assert "discover" in out.lower() or "sync" in out.lower() or "session" in out.lower()


# -- classify-stats ----------------------------------------------------------


def _seed_journal(journal_dir: Path, sessions: list[tuple[str, str]]) -> None:
    """Create journal entries with (date, content) pairs for classify-stats tests."""
    for i, (date, content) in enumerate(sessions):
        day_dir = journal_dir / date
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / f"autonomous-session-{i:04x}.md").write_text(content)


class TestClassifyStatsCommand:
    def test_diversity_alert_triggers_on_last_three(self, tmp_path: Path) -> None:
        """Bug 3 regression: alert fires on LAST 3 sessions, not oldest 3 ([:3] → [-3:] fix).

        Setup: 5 sessions — first 2 monitoring, last 3 code.
        Old bug ([:3]) checks [monitoring, monitoring, code] → no alert.
        Fix ([-3:]) checks [code, code, code] → alert fires.
        """
        journal_dir = tmp_path / "journal"
        sessions = [
            ("2026-01-01", "# Monitoring: project checks\nChecked repos, all green."),
            ("2026-01-02", "# Monitoring: project checks\nChecked repos, all green."),
            ("2026-01-03", "# Code: implement feature\nOpened PR, tests passing."),
            ("2026-01-04", "# Code: implement feature\nOpened PR, tests passing."),
            ("2026-01-05", "# Code: implement feature\nOpened PR, tests passing."),
        ]
        _seed_journal(journal_dir, sessions)
        rc, out = _invoke(
            ["classify-stats", "--journal-dir", str(journal_dir), "--diversity-window", "5"],
            tmp_path,
        )
        assert rc == 0
        assert "consecutive" in out.lower() or "diversifying" in out.lower()

    def test_diversity_alert_not_triggered_below_threshold(self, tmp_path: Path) -> None:
        """Diversity alert guard: fewer than 3 sessions in window does not fire alert."""
        journal_dir = tmp_path / "journal"
        sessions = [
            ("2026-01-01", "# Code: implement feature\nOpened PR, tests passing."),
            ("2026-01-02", "# Code: implement feature\nOpened PR, tests passing."),
        ]
        _seed_journal(journal_dir, sessions)
        rc, out = _invoke(
            ["classify-stats", "--journal-dir", str(journal_dir), "--diversity-window", "5"],
            tmp_path,
        )
        assert rc == 0
        assert "consecutive" not in out.lower()


# -- sync timestamp fix ------------------------------------------------------


class TestSyncTimestamp:
    def test_sync_uses_session_date_not_now(self, tmp_path: Path):
        """sync should use session date from discovery, not datetime.now()."""
        # Create a gptme session directory with a known date
        logs_dir = tmp_path / "logs"
        session_dir = logs_dir / "2026-03-10-test-session"
        session_dir.mkdir(parents=True)
        # No conversation.jsonl — path will be the directory itself

        store_dir = tmp_path / "store"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--sessions-dir",
                str(store_dir),
                "sync",
                "--since",
                "30d",
            ],
            env={"GPTME_LOGS_DIR": str(logs_dir)},
        )
        assert result.exit_code == 0

        # Load the store and check the timestamp
        store = SessionStore(sessions_dir=store_dir)
        records = store.load_all()
        # Should have imported one record
        gptme_records = [r for r in records if r.harness == "gptme"]
        if gptme_records:
            # The timestamp should start with the session date, not today
            assert gptme_records[0].timestamp.startswith("2026-03-10")

    def test_sync_imports_real_start_time_from_trajectory(self, tmp_path: Path):
        """sync should record the real start time, not a noon-UTC placeholder.

        Regression test: previously, every synced Claude Code session landed at
        YYYY-MM-DDT12:00:00 because sync only had a date, not a datetime.  This
        collapsed 100+ sessions into a single hour and produced bogus noop
        spikes in downstream analytics (bandit, inference-review).
        """
        # CLAUDE_HOME points at a directory containing a `projects/` subdir
        claude_home = tmp_path / "cc"
        proj = claude_home / "projects" / "-home-user-proj"
        proj.mkdir(parents=True)
        traj = proj / "abc12345-aaaa-bbbb-cccc-ddddeeeeffff.jsonl"
        # File must exceed CC_MIN_SESSION_SIZE (4096 bytes) so it isn't filtered
        # out as a stub session by discover_cc_sessions.
        lines = [
            json.dumps({"type": "system", "timestamp": "2026-04-15T22:42:48Z"}),
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-04-15T22:43:00Z",
                    "message": {"role": "assistant", "content": "x" * 5000},
                }
            ),
        ]
        traj.write_text("\n".join(lines) + "\n")

        store_dir = tmp_path / "store"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--sessions-dir",
                str(store_dir),
                "sync",
                "--harness",
                "claude-code",
                "--since",
                "all",
            ],
            env={"CLAUDE_HOME": str(claude_home)},
        )
        assert result.exit_code == 0

        store = SessionStore(sessions_dir=store_dir)
        cc_records = [r for r in store.load_all() if r.harness == "claude-code"]
        assert len(cc_records) == 1
        # Must preserve the real hour/minute/second — not the noon placeholder
        assert cc_records[0].timestamp.startswith("2026-04-15T22:42:48")


class TestSyncFixTimestamps:
    def test_fix_timestamps_corrects_records(self, tmp_path: Path):
        """sync --fix-timestamps corrects timestamps from trajectory paths."""
        store = SessionStore(sessions_dir=tmp_path)
        # Create a record with wrong timestamp but correct trajectory_path
        rec = SessionRecord(
            harness="gptme",
            timestamp="2026-03-20T12:00:00+00:00",  # wrong: sync date
            trajectory_path="/fake/logs/2026-03-10-my-session/conversation.jsonl",
        )
        store.append(rec)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--sessions-dir", str(tmp_path), "sync", "--fix-timestamps"],
        )
        assert result.exit_code == 0
        assert "Fixed 1 timestamp" in result.output

        # Verify the timestamp was corrected
        records = store.load_all()
        assert records[0].timestamp.startswith("2026-03-10")

    def test_fix_timestamps_restores_real_time_from_trajectory(self, tmp_path: Path):
        """--fix-timestamps restores the real start time (not noon placeholder).

        Regression test for the noon-UTC placeholder bug: when sync imports a
        trajectory without extracting its first-event timestamp, every record
        lands at YYYY-MM-DDT12:00:00 with duration_seconds=0, collapsing the
        hourly distribution.  --fix-timestamps must detect these placeholders
        and recover the real start time from the trajectory file.
        """
        # Create a real Claude Code trajectory with a non-noon start time
        traj_dir = tmp_path / "projects" / "-home-user-proj"
        traj_dir.mkdir(parents=True)
        traj = traj_dir / "abc12345-0000-0000-0000-000000000000.jsonl"
        traj.write_text(json.dumps({"type": "system", "timestamp": "2026-04-15T22:42:48Z"}) + "\n")

        # Seed a placeholder record pointing at that trajectory
        store = SessionStore(sessions_dir=tmp_path / "store")
        store.append(
            SessionRecord(
                harness="claude-code",
                timestamp="2026-04-15T12:00:00+00:00",  # noon placeholder
                duration_seconds=0,
                trajectory_path=str(traj),
            )
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--sessions-dir", str(tmp_path / "store"), "sync", "--fix-timestamps"],
        )
        assert result.exit_code == 0
        assert "Fixed 1 timestamp" in result.output

        rec = store.load_all()[0]
        assert rec.timestamp.startswith("2026-04-15T22:42:48")


# -- stats defaults ----------------------------------------------------------


class TestStatsDefaults:
    def test_stats_defaults_to_30d(self, tmp_path: Path):
        """stats without --since defaults to 30d window."""
        _seed_store(tmp_path)
        rc, out = _invoke(["stats"], tmp_path)
        assert rc == 0
        # Should show the 30-day header
        assert "30 days" in out.lower() or "all-time" in out.lower()

    def test_stats_since_all(self, tmp_path: Path):
        """stats --since all shows all-time stats."""
        _seed_store(tmp_path)
        rc, out = _invoke(["stats", "--since", "all", "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert data["total"] == 5

    def test_stats_old_records_no_misleading_fallback(self, tmp_path: Path):
        """stats on store with only old records shows a helpful message, not 'run sync'."""
        from unittest.mock import patch

        store = SessionStore(sessions_dir=tmp_path)
        # Insert a record with a timestamp far in the past (outside the implicit 30d window)
        old_record = SessionRecord(
            harness="claude-code",
            model="opus",
            timestamp="2020-01-01T00:00:00+00:00",
        )
        store.append(old_record)
        with patch("gptme_sessions.cli._discover_all", return_value=[]):
            rc, out = _invoke(["stats"], tmp_path)
        assert rc == 0
        # Should NOT tell the user to run sync (misleading — data is already synced)
        assert "sync" not in out.lower()
        # Should point to --since all for all-time data
        assert "--since all" in out.lower() or "since all" in out.lower()

    def test_toplevel_old_records_no_misleading_fallback(self, tmp_path: Path):
        """Top-level cli() with only old records shows helpful message, not 'run sync'."""
        store = SessionStore(sessions_dir=tmp_path)
        old_record = SessionRecord(
            harness="claude-code",
            model="opus",
            timestamp="2020-01-01T00:00:00+00:00",
        )
        store.append(old_record)
        # Invoke without subcommand (top-level cli())
        rc, out = _invoke([], tmp_path)
        assert rc == 0
        # Should NOT tell the user to run sync
        assert "sync" not in out.lower()
        # Should point to --since all
        assert "--since all" in out.lower() or "since all" in out.lower()


# -- project filter ----------------------------------------------------------


class TestProjectFilter:
    def test_query_filter_by_project(self, tmp_path: Path):
        """query --project filters records by project name."""
        store = SessionStore(sessions_dir=tmp_path)
        store.append(
            SessionRecord(
                harness="claude-code",
                model="opus",
                outcome="productive",
                project="/Users/erb/myproject",
            )
        )
        store.append(
            SessionRecord(
                harness="claude-code",
                model="sonnet",
                outcome="noop",
                project="/Users/erb/other",
            )
        )

        rc, out = _invoke(["query", "--project", "myproject", "--json"], tmp_path)
        assert rc == 0
        data = json.loads(out)
        assert len(data) == 1
        assert data[0]["project"] == "/Users/erb/myproject"
