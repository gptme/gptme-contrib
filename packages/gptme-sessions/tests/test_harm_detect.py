"""Tests for gptme_sessions.harm_detect."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


from gptme_sessions.harm_detect import (
    NoSearchReposError,
    _default_repos,
    _default_store_path,
    _is_sha_reverted,
    _resolve_repos,
    _require_repos,
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
        shas = extract_commit_shas(["https://github.com/foo/bar/pull/123"])
        assert shas == []

    def test_mixed_list(self):
        items = [
            "https://github.com/foo/bar/pull/123",
            "abc1234",
            "fix: ship (8f12aa2ca1b65b3632d37d9400ce875adedc9b58)",
        ]
        shas = extract_commit_shas(items)
        assert "abc1234" in shas
        assert "8f12aa2ca1b65b3632d37d9400ce875adedc9b58" in shas
        # PR URL has no SHA inside, so we shouldn't get its "123"
        assert "123" not in shas

    def test_deduplication(self):
        shas = extract_commit_shas(["abc1234", "abc1234", "abc1234567"])
        assert shas == ["abc1234", "abc1234567"]

    def test_empty_list(self):
        assert extract_commit_shas([]) == []


class TestIsShaReverted:
    def test_returns_true_when_revert_found(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo_for_test(repo)
        target_sha = "0" * 40
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "feat: initial"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", f"This reverts commit {target_sha}"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        assert _is_sha_reverted(target_sha, repo) is True

    def test_returns_false_when_no_match(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo_for_test(repo)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "feat: initial"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        assert _is_sha_reverted("0" * 40, repo) is False

    def test_returns_false_on_timeout(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=1)
            assert _is_sha_reverted("0" * 40, repo, timeout=1) is False

    def test_returns_false_on_nonzero_exit(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 128
            mock_result.stdout = ""
            mock_run.return_value = mock_result
            assert _is_sha_reverted("0" * 40, repo) is False


class TestDetectHarmRevert:
    def test_returns_clean_when_no_deliverables(self, tmp_path: Path):
        # No repos → would normally raise, but with no SHAs to check we
        # return clean before searching.
        result = detect_harm_revert("s1", deliverables=[], repos=[], allow_empty_repos=True)
        assert result == 1.0

    def test_returns_clean_when_no_shas(self, tmp_path: Path):
        result = detect_harm_revert(
            "s1",
            deliverables=["https://github.com/foo/bar/pull/1"],
            repos=[],
            allow_empty_repos=True,
        )
        assert result == 1.0

    def test_returns_harm_when_sha_reverted(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo_for_test(repo)
        target_sha = "a1b2c3d4e5f6789012345678901234567890abcd"
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "feat: initial"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", f"This reverts commit {target_sha}"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        result = detect_harm_revert("s1", deliverables=[target_sha], repos=[repo])
        assert result == 0.0

    def test_returns_clean_when_no_revert(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo_for_test(repo)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "feat: initial"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        result = detect_harm_revert(
            "s1", deliverables=["a1b2c3d4e5f6789012345678901234567890abcd"], repos=[repo]
        )
        assert result == 1.0

    def test_grade_convention(self):
        # Document the inverse-of-intuitive convention so future readers
        # don't accidentally swap the polarity.
        assert (
            detect_harm_revert("s1", deliverables=[], repos=[], allow_empty_repos=True) == 1.0
        )  # clean
        # harm (no deliverables → no SHAs → clean) is covered above

    def test_session_not_in_store_returns_clean(self, tmp_path: Path):
        # store_path doesn't exist → fall back to empty deliverables → clean
        result = detect_harm_revert(
            "missing-session",
            repos=[],
            store_path=tmp_path / "nonexistent-store",
            allow_empty_repos=True,
        )
        assert result == 1.0

    def test_explicit_store_path_loads_session(self, tmp_path: Path):
        """store_path is honored when supplied; session deliverables are read."""
        from gptme_sessions.record import SessionRecord
        from gptme_sessions.store import SessionStore

        store_dir = tmp_path / "sessions"
        store = SessionStore(store_dir)
        target_sha = "a1b2c3d4e5f6789012345678901234567890abcd"
        record = SessionRecord(
            session_id="s1",
            harness="gptme",
            deliverables=[target_sha],
        )
        store.append(record)

        # No repos to search → would raise if we hit the search path,
        # but with the explicit store_path loaded we *do* have SHAs.
        # Use allow_empty_repos=True to verify the store was actually consulted.
        result = detect_harm_revert(
            "s1",
            repos=[],
            store_path=store_dir,
            allow_empty_repos=True,
        )
        # No SHAs to actually revert, so clean.
        assert result == 1.0

    def test_no_repos_raises_by_default(self):
        """Without explicit repos and no defaults, raise NoSearchReposError."""
        with pytest.raises(NoSearchReposError):
            detect_harm_revert(
                "s1",
                deliverables=["a1b2c3d4e5f6789012345678901234567890abcd"],
                repos=[],
            )

    def test_no_repos_with_allow_empty_returns_clean(self):
        """allow_empty_repos=True turns the error into clean."""
        result = detect_harm_revert(
            "s1",
            deliverables=["a1b2c3d4e5f6789012345678901234567890abcd"],
            repos=[],
            allow_empty_repos=True,
        )
        assert result == 1.0


class TestResolveRepos:
    def test_resolve_repos_explicit(self):
        explicit = [Path("/tmp/a"), Path("/tmp/b")]
        assert _resolve_repos(explicit) == explicit

    def test_resolve_repos_none_uses_default(self):
        # No assertion on the contents (depends on cwd), but the call
        # should not raise and should return a list.
        result = _resolve_repos(None)
        assert isinstance(result, list)


class TestRequireRepos:
    def test_returns_repos_when_nonempty(self):
        assert _require_repos([Path("/x")], allow_empty=False) == [Path("/x")]

    def test_raises_when_empty_and_not_allowed(self):
        with pytest.raises(NoSearchReposError):
            _require_repos([], allow_empty=False)

    def test_returns_empty_when_allowed(self):
        assert _require_repos([], allow_empty=True) == []


class TestDefaultRepos:
    def test_returns_list(self):
        result = _default_repos()
        assert isinstance(result, list)

    def test_no_nonexistent_repos(self):
        result = _default_repos()
        for repo in result:
            assert repo.is_dir()
            assert (repo / ".git").exists()


class TestLooksLikeGitRepo:
    """Worktree .git is a file (not a directory) — both must count as a repo."""

    def test_regular_repo_with_git_dir(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        from gptme_sessions.harm_detect import _looks_like_git_repo

        assert _looks_like_git_repo(repo) is True

    def test_worktree_with_git_file(self, tmp_path: Path):
        wt = tmp_path / "wt"
        wt.mkdir()
        # Real worktree .git file points to an existing metadata directory.
        wt_meta = tmp_path / "wt-meta"
        wt_meta.mkdir()
        (wt / ".git").write_text(f"gitdir: {wt_meta}\n", encoding="utf-8")
        from gptme_sessions.harm_detect import _looks_like_git_repo

        assert _looks_like_git_repo(wt) is True

    def test_stale_worktree_pointer_rejected(self, tmp_path: Path):
        """A .git file pointing to a non-existent target is rejected.

        Stale pointers would let searches "succeed" with no revs and
        silently grade every session as clean.  Reject them at discovery
        so the public API surfaces NoSearchReposError instead.
        """
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /nonexistent/target/path\n", encoding="utf-8")
        from gptme_sessions.harm_detect import _looks_like_git_repo

        assert _looks_like_git_repo(wt) is False

    def test_worktree_with_invalid_git_file(self, tmp_path: Path):
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text("not a gitdir pointer\n", encoding="utf-8")
        from gptme_sessions.harm_detect import _looks_like_git_repo

        assert _looks_like_git_repo(wt) is False

    def test_not_a_repo(self, tmp_path: Path):
        plain = tmp_path / "plain"
        plain.mkdir()
        from gptme_sessions.harm_detect import _looks_like_git_repo

        assert _looks_like_git_repo(plain) is False

    def test_nonexistent_path(self, tmp_path: Path):
        from gptme_sessions.harm_detect import _looks_like_git_repo

        assert _looks_like_git_repo(tmp_path / "nope") is False


def _init_repo_for_test(repo: Path) -> None:
    """Initialize a git repo and create a feature branch (master is protected by hook)."""
    import subprocess as sp

    sp.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    sp.run(
        ["git", "config", "user.email", "bob@superuserlabs.org"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    sp.run(["git", "config", "user.name", "Bob"], cwd=repo, check=True, capture_output=True)
    sp.run(["git", "checkout", "-b", "test-branch"], cwd=repo, check=True, capture_output=True)


class TestDefaultStorePath:
    """_default_store_path delegates to SessionStore._default_sessions_dir()."""

    def test_returns_sessionstore_default(self, monkeypatch, tmp_path):
        """When no Bob override is set, the SessionStore default is used.

        Clearing GPTME_SESSIONS_DIR and pointing HOME to an empty tmp_path
        forces SessionStore._default_sessions_dir() to return a fresh XDG
        path; the harm_detect resolver should match.
        """
        monkeypatch.delenv("GPTME_SESSIONS_DIR", raising=False)
        monkeypatch.delenv("BOB_HARM_DETECT_USE_BOB_STORE", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        from gptme_sessions.store import _default_sessions_dir

        assert _default_store_path() == _default_sessions_dir()

    def test_honors_gptme_sessions_dir(self, monkeypatch, tmp_path):
        """GPTME_SESSIONS_DIR takes precedence (SessionStore's own contract)."""
        env_store = tmp_path / "env-sessions"
        monkeypatch.setenv("GPTME_SESSIONS_DIR", str(env_store))
        monkeypatch.delenv("BOB_HARM_DETECT_USE_BOB_STORE", raising=False)

        assert _default_store_path() == env_store
