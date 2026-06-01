"""Tests for durability scoring."""

from __future__ import annotations

import subprocess as sp
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gptme_sessions.durability import (
    _commit_was_reverted,
    _extract_commit_shas,
    _is_old_enough,
    _sha_from_deliverable,
    compute_durability,
)
from gptme_sessions.record import SessionRecord


def _git(repo: Path, *args: str) -> sp.CompletedProcess:
    """Run git bypassing global hooks that block direct master commits."""
    return sp.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null"] + list(args),
        check=True,
        capture_output=True,
        text=True,
    )


def _make_temp_repo(base_dir: str) -> Path:
    """Create a temp git repo with an initial commit."""
    repo = Path(base_dir) / "repo"
    sp.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# Test\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial commit")
    return repo


class TestShaFromDeliverable:
    def test_bare_sha(self):
        assert _sha_from_deliverable("abc1234def5678") == "abc1234def5678"

    def test_short_sha(self):
        assert _sha_from_deliverable("abc1234") == "abc1234"

    def test_commit_with_message(self):
        assert _sha_from_deliverable("fix(webui): resolve TS errors (28ba77c)") == "28ba77c"

    def test_merge_commit(self):
        assert _sha_from_deliverable("merge-commit (abc1234def)") == "abc1234def"

    def test_not_a_sha(self):
        assert _sha_from_deliverable("gptme/gptme#2674") is None
        assert _sha_from_deliverable("some file.md") is None

    def test_full_40_char_sha(self):
        sha40 = "0" * 40
        assert _sha_from_deliverable(sha40) == sha40


class TestExtractCommitShas:
    def test_bare_shas(self):
        record = SessionRecord(
            harness="gptme",
            model="sonnet",
            deliverables=["abc1234", "def5678"],
        )
        assert _extract_commit_shas(record) == ["abc1234", "def5678"]

    def test_commit_with_message_format(self):
        record = SessionRecord(
            harness="gptme",
            model="sonnet",
            deliverables=[
                "fix: description (abc1234d)",
            ],
        )
        assert _extract_commit_shas(record) == ["abc1234d"]

    def test_merge_commit_format(self):
        record = SessionRecord(
            harness="gptme",
            model="sonnet",
            deliverables=[
                "merge-commit (abc1234d)",
            ],
        )
        assert _extract_commit_shas(record) == ["abc1234d"]

    def test_mixed_deliverables(self):
        record = SessionRecord(
            harness="gptme",
            model="sonnet",
            deliverables=[
                "abc1234",
                "fix: thing (def5678)",
                "gptme/gptme#2674",  # PR ref — not a commit
                "merge-commit (8889999)",
            ],
        )
        assert _extract_commit_shas(record) == ["abc1234", "def5678", "8889999"]

    def test_empty_deliverables(self):
        record = SessionRecord(harness="gptme", model="sonnet", deliverables=[])
        assert _extract_commit_shas(record) == []

    def test_none_deliverables(self):
        record = SessionRecord(harness="gptme", model="sonnet", deliverables=None)
        assert _extract_commit_shas(record) == []


class TestIsOldEnough:
    def test_old_session(self):
        old_time = datetime.now(timezone.utc) - timedelta(days=31)
        record = SessionRecord(
            harness="gptme",
            model="sonnet",
            start_time=old_time.isoformat(),
        )
        assert _is_old_enough(record, age_days=30) is True

    def test_young_session(self):
        recent_time = datetime.now(timezone.utc) - timedelta(days=5)
        record = SessionRecord(
            harness="gptme",
            model="sonnet",
            start_time=recent_time.isoformat(),
        )
        assert _is_old_enough(record, age_days=30) is False

    def test_no_timestamp(self):
        record = SessionRecord(harness="gptme", model="sonnet")
        assert _is_old_enough(record, age_days=30) is False

    def test_exactly_at_threshold(self):
        edge_time = datetime.now(timezone.utc) - timedelta(days=30)
        record = SessionRecord(
            harness="gptme",
            model="sonnet",
            start_time=edge_time.isoformat(),
        )
        assert _is_old_enough(record, age_days=30) is True


class TestCommitWasReverted:
    def test_commit_not_reverted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_temp_repo(tmpdir)
            (repo / "file.txt").write_text("content")
            _git(repo, "add", "file.txt")
            _git(repo, "commit", "-m", "add file")
            sha = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()

            assert _commit_was_reverted(sha, repo) is False

    def test_commit_reverted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_temp_repo(tmpdir)
            (repo / "file.txt").write_text("content")
            _git(repo, "add", "file.txt")
            _git(repo, "commit", "-m", "add file")
            sha_full = _git(repo, "rev-parse", "HEAD").stdout.strip()
            sha_short = sha_full[:7]

            # "git revert HEAD" tries to open an editor — bypass
            _git(repo, "revert", "--no-edit", "HEAD")

            assert _commit_was_reverted(sha_short, repo) is True


class TestComputeDurability:
    def test_too_young(self):
        record = SessionRecord(
            harness="gptme",
            model="sonnet",
            start_time=datetime.now(timezone.utc).isoformat(),
            deliverables=["abc1234"],
        )
        assert compute_durability(record, "/fake/repo") is None

    def test_no_commits(self):
        old_time = datetime.now(timezone.utc) - timedelta(days=31)
        record = SessionRecord(
            harness="gptme",
            model="sonnet",
            start_time=old_time.isoformat(),
            deliverables=["gptme/gptme#2674"],  # PR ref, not a commit
        )
        assert compute_durability(record, "/fake/repo") is None

    def test_all_survived(self):
        old_time = datetime.now(timezone.utc) - timedelta(days=31)
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_temp_repo(tmpdir)
            (repo / "f.txt").write_text("x")
            _git(repo, "add", "f.txt")
            _git(repo, "commit", "-m", "add f")
            sha = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()

            record = SessionRecord(
                harness="gptme",
                model="sonnet",
                start_time=old_time.isoformat(),
                deliverables=[sha],
            )
            assert compute_durability(record, repo) == 1.0

    def test_mixed_survival(self):
        old_time = datetime.now(timezone.utc) - timedelta(days=31)
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_temp_repo(tmpdir)

            # Commit A: survives
            (repo / "a.txt").write_text("a")
            _git(repo, "add", "a.txt")
            _git(repo, "commit", "-m", "add a")
            sha_a = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()

            # Commit B: gets reverted
            (repo / "b.txt").write_text("b")
            _git(repo, "add", "b.txt")
            _git(repo, "commit", "-m", "add b")
            sha_b = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()

            _git(repo, "revert", "--no-edit", "HEAD")

            record = SessionRecord(
                harness="gptme",
                model="sonnet",
                start_time=old_time.isoformat(),
                deliverables=[sha_a, sha_b],
            )
            assert compute_durability(record, repo) == 0.5
