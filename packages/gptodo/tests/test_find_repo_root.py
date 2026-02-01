"""Tests for workspace detection in find_repo_root."""

from pathlib import Path


from gptodo.utils import find_repo_root


class TestFindRepoRoot:
    """Test cases for find_repo_root workspace detection."""

    def test_gptme_toml_takes_priority(self, tmp_path):
        """gptme.toml should be found before plain .git directories."""
        # Create nested structure: root/sub1/sub2
        root = tmp_path / "root"
        sub1 = root / "sub1"
        sub2 = sub1 / "sub2"
        sub2.mkdir(parents=True)

        # Put gptme.toml at root and .git at sub1
        (root / "gptme.toml").touch()
        (sub1 / ".git").mkdir()

        # From sub2, should find root (gptme.toml), not sub1 (.git)
        assert find_repo_root(sub2) == root

    def test_git_with_tasks_beats_git_alone(self, tmp_path):
        """A .git directory with tasks/ sibling should win over plain .git."""
        # Create nested structure
        outer = tmp_path / "outer"
        inner = outer / "inner"
        deep = inner / "deep"
        deep.mkdir(parents=True)

        # outer has .git with tasks/
        (outer / ".git").mkdir()
        (outer / "tasks").mkdir()

        # inner has only .git
        (inner / ".git").mkdir()

        # From deep, should find outer (has tasks), not inner
        assert find_repo_root(deep) == outer

    def test_falls_back_to_plain_git(self, tmp_path):
        """Without gptme.toml or tasks/, should fall back to .git."""
        repo = tmp_path / "repo"
        subdir = repo / "src" / "module"
        subdir.mkdir(parents=True)

        # Only .git, no gptme.toml or tasks/
        (repo / ".git").mkdir()

        assert find_repo_root(subdir) == repo

    def test_gptodo_tasks_dir_env_override(self, tmp_path, monkeypatch):
        """GPTODO_TASKS_DIR should take highest priority."""
        custom_tasks = tmp_path / "custom" / "tasks"
        custom_tasks.mkdir(parents=True)

        # Create a repo that would normally be found
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "tasks").mkdir()

        monkeypatch.setenv("GPTODO_TASKS_DIR", str(custom_tasks))

        # Should return parent of custom_tasks, not the repo
        result = find_repo_root(repo)
        assert result == custom_tasks.parent

    def test_tasks_repo_root_env_legacy(self, tmp_path, monkeypatch):
        """TASKS_REPO_ROOT (legacy) should work as fallback."""
        # Clear any GPTODO_TASKS_DIR
        monkeypatch.delenv("GPTODO_TASKS_DIR", raising=False)

        custom_root = tmp_path / "custom_root"
        custom_root.mkdir()
        (custom_root / ".git").mkdir()

        monkeypatch.setenv("TASKS_REPO_ROOT", str(custom_root))

        # Should use custom_root as starting point
        result = find_repo_root(Path("/nonexistent"))
        assert result == custom_root

    def test_fallback_to_start_path(self, tmp_path, monkeypatch):
        """Without any markers, should return start_path."""
        # Clear environment variables
        monkeypatch.delenv("GPTODO_TASKS_DIR", raising=False)
        monkeypatch.delenv("TASKS_REPO_ROOT", raising=False)

        # Create a directory with no markers
        empty = tmp_path / "empty"
        empty.mkdir()

        result = find_repo_root(empty)
        assert result == empty
