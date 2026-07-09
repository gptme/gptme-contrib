"""Tests for gptme_sessions.harm_detect."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch


from gptme_sessions.harm_detect import (
    _default_repos,
    _is_sha_reverted,
    detect_harm_revert,
    extract_commit_shas,
)


class TestExtractCommitShas:
    def test_bare_40char_sha(self):
        shas = extract_commit_shas(["8f12aa2ca1b65b3632d37d9400ce875adedc9b58"])
        assert "8f12aa2ca1b65b3632d37d9400ce875adedc9b58" in shas

    def test_bare_short_sha(self):
        shas = extract_commit_shas(["abc1234"])
        assert "abc1234" in shas

    def test_commit_message_with_sha(self):
        shas = extract_commit_shas(["fix(thing): do it (abc1234)"])
        assert "abc1234" in shas

    def test_pr_url_skipped(self):
        shas = extract_commit_shas(["https://github.com/gptme/gptme/pull/3020"])
        assert shas == []

    def test_mixed_list(self):
        deliverables = [
            "https://github.com/gptme/gptme/pull/100",
            "8f12aa2ca1b65b3632d37d9400ce875adedc9b58",
            "fix(thing): update (deadbee)",
        ]
        shas = extract_commit_shas(deliverables)
        assert "8f12aa2ca1b65b3632d37d9400ce875adedc9b58" in shas
        assert "deadbee" in shas
        # PR URL should not produce anything
        assert not any("github" in s for s in shas)

    def test_deduplication(self):
        sha = "abc1234567890abcdef1234567890abcdef12345"
        shas = extract_commit_shas([sha, sha])
        assert shas.count(sha) == 1

    def test_empty_list(self):
        assert extract_commit_shas([]) == []


class TestIsShaReverted:
    def test_returns_true_when_revert_found(self, tmp_path):
        """When git log returns output, the SHA was reverted."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='a6212255a Revert "feat(cli): status line" (#2061)\n',
            )
            assert _is_sha_reverted("8f12aa2", tmp_path) is True

    def test_returns_false_when_no_match(self, tmp_path):
        """When git log returns empty, the SHA was NOT reverted."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            assert _is_sha_reverted("abc1234", tmp_path) is False

    def test_returns_false_on_timeout(self, tmp_path):
        """Timeout means we can't confirm — treat as clean (conservative)."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 10)):
            assert _is_sha_reverted("abc1234", tmp_path) is False

    def test_returns_false_on_nonzero_exit(self, tmp_path):
        """Non-zero exit (e.g. not a git repo) treated as clean."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            assert _is_sha_reverted("abc1234", tmp_path) is False


class TestDetectHarmRevert:
    def test_returns_clean_when_no_deliverables(self):
        """Sessions with no deliverables are clean by default."""
        result = detect_harm_revert("test-session", deliverables=[])
        assert result == 1.0

    def test_returns_clean_when_no_shas(self):
        """PR URLs only — no SHAs to check."""
        result = detect_harm_revert(
            "test-session",
            deliverables=["https://github.com/org/repo/pull/123"],
        )
        assert result == 1.0

    def test_returns_harm_when_sha_reverted(self, tmp_path):
        """Returns 0.0 when a deliverable commit was reverted."""
        with patch("gptme_sessions.harm_detect._is_sha_reverted", return_value=True):
            result = detect_harm_revert(
                "test-session",
                deliverables=["8f12aa2ca1b65b3632d37d9400ce875adedc9b58"],
                repos=[tmp_path],
            )
        assert result == 0.0

    def test_returns_clean_when_no_revert(self, tmp_path):
        """Returns 1.0 when no deliverable commit was reverted."""
        with patch("gptme_sessions.harm_detect._is_sha_reverted", return_value=False):
            result = detect_harm_revert(
                "test-session",
                deliverables=["abc1234567890abcdef1234567890abcdef12345"],
                repos=[tmp_path],
            )
        assert result == 1.0

    def test_grade_convention(self):
        """Grade semantics: 1.0=clean, 0.0=harm. Verify convention is correct
        for the weighted-average trajectory_grade computation."""
        clean = detect_harm_revert("s", deliverables=[])
        assert clean == 1.0, "Clean session must contribute positively to trajectory_grade"

        with patch("gptme_sessions.harm_detect._is_sha_reverted", return_value=True):
            harmed = detect_harm_revert("s", deliverables=["abc1234"], repos=[Path(".")])
        assert harmed == 0.0, "Harmed session must pull trajectory_grade down"

    def test_session_not_in_store_returns_clean(self, tmp_path):
        """Unknown session ID → can't confirm harm, return clean."""
        fake_store_path = tmp_path / "sessions"
        fake_store_path.mkdir()
        (fake_store_path / "session-records.jsonl").write_text("")
        with patch("gptme_sessions.harm_detect._workspace_root", return_value=tmp_path):
            result = detect_harm_revert("nonexistent-session-id")
        assert result == 1.0


class TestDefaultRepos:
    def test_returns_list(self):
        repos = _default_repos()
        assert isinstance(repos, list)

    def test_no_nonexistent_repos(self):
        for repo in _default_repos():
            assert repo.exists(), f"Repo {repo} does not exist"
            assert (repo / ".git").exists(), f"{repo} is not a git repo"
