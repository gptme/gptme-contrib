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

    def test_stats_no_matches_with_filter(self, tmp_path: Path):
        """stats with filter that matches nothing shows appropriate message."""
        _seed_store(tmp_path)
        rc, out = _invoke(["stats", "--model", "nonexistent"], tmp_path)
        assert rc == 0
        assert "no records" in out.lower()

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

    def test_parse_since_hours(self, tmp_path: Path):
        """--since 2h parses correctly as fractional days."""
        _seed_store(tmp_path)
        rc, out = _invoke(["runs", "--since", "2h"], tmp_path)
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
