"""Tests for link checking in task files."""

from gptodo.utils import check_links


class TestCheckLinks:
    """Test cases for check_links utility function."""

    def test_no_links(self, tmp_path):
        """Content with no links returns empty list."""
        task_path = tmp_path / "tasks" / "test.md"
        task_path.parent.mkdir(parents=True)
        task_path.touch()
        assert check_links("No links here.", task_path, tmp_path) == []

    def test_valid_relative_link(self, tmp_path):
        """Valid relative file links are not reported."""
        task_path = tmp_path / "tasks" / "test.md"
        task_path.parent.mkdir(parents=True)
        task_path.touch()
        # Create the target file
        target = tmp_path / "tasks" / "other.md"
        target.touch()
        content = "See [other task](other.md) for details."
        assert check_links(content, task_path, tmp_path) == []

    def test_broken_relative_link(self, tmp_path):
        """Broken relative file links are reported."""
        task_path = tmp_path / "tasks" / "test.md"
        task_path.parent.mkdir(parents=True)
        task_path.touch()
        content = "See [missing](nonexistent.md) for details."
        result = check_links(content, task_path, tmp_path)
        assert len(result) == 1
        assert "nonexistent.md" in result[0]

    def test_url_links_skipped(self, tmp_path):
        """HTTP/HTTPS URLs are not checked."""
        task_path = tmp_path / "tasks" / "test.md"
        task_path.parent.mkdir(parents=True)
        task_path.touch()
        content = "See [GitHub](https://github.com/example) and [docs](http://example.com)."
        assert check_links(content, task_path, tmp_path) == []

    def test_anchor_links_skipped(self, tmp_path):
        """Anchor-only links are not checked."""
        task_path = tmp_path / "tasks" / "test.md"
        task_path.parent.mkdir(parents=True)
        task_path.touch()
        content = "See [section](#section-name) below."
        assert check_links(content, task_path, tmp_path) == []

    def test_mailto_links_skipped(self, tmp_path):
        """Mailto links are not checked."""
        task_path = tmp_path / "tasks" / "test.md"
        task_path.parent.mkdir(parents=True)
        task_path.touch()
        content = "Email [bob](mailto:bob@example.com)."
        assert check_links(content, task_path, tmp_path) == []

    def test_link_with_anchor_fragment(self, tmp_path):
        """Links with anchor fragments check the file portion only."""
        task_path = tmp_path / "tasks" / "test.md"
        task_path.parent.mkdir(parents=True)
        task_path.touch()
        # Create the target file
        target = tmp_path / "tasks" / "guide.md"
        target.touch()
        content = "See [section](guide.md#heading) for details."
        assert check_links(content, task_path, tmp_path) == []

    def test_broken_link_with_anchor_fragment(self, tmp_path):
        """Broken links with anchor fragments are still reported."""
        task_path = tmp_path / "tasks" / "test.md"
        task_path.parent.mkdir(parents=True)
        task_path.touch()
        content = "See [section](missing.md#heading) for details."
        result = check_links(content, task_path, tmp_path)
        assert len(result) == 1
        assert "missing.md#heading" in result[0]

    def test_repo_root_relative_link(self, tmp_path):
        """Links resolvable from repo root are valid."""
        task_path = tmp_path / "tasks" / "test.md"
        task_path.parent.mkdir(parents=True)
        task_path.touch()
        # Create a file at repo root level
        knowledge = tmp_path / "knowledge" / "design.md"
        knowledge.parent.mkdir(parents=True)
        knowledge.touch()
        # Link relative to repo root (from tasks/ dir, needs ../)
        content = "See [design](../knowledge/design.md)."
        assert check_links(content, task_path, tmp_path) == []

    def test_repo_root_fallback(self, tmp_path):
        """Links resolved relative to repo root when task-relative fails."""
        task_path = tmp_path / "tasks" / "test.md"
        task_path.parent.mkdir(parents=True)
        task_path.touch()
        # Create a file at repo root level
        readme = tmp_path / "README.md"
        readme.touch()
        # Link using repo-root-relative path (not valid from tasks/ dir normally)
        content = "See [readme](README.md)."
        # This should fail relative to task dir, but succeed from repo root
        assert check_links(content, task_path, tmp_path) == []

    def test_image_links_not_checked(self, tmp_path):
        """Image links (![alt](path)) are not checked."""
        task_path = tmp_path / "tasks" / "test.md"
        task_path.parent.mkdir(parents=True)
        task_path.touch()
        content = "![screenshot](nonexistent.png)"
        assert check_links(content, task_path, tmp_path) == []

    def test_multiple_broken_links(self, tmp_path):
        """Multiple broken links are all reported."""
        task_path = tmp_path / "tasks" / "test.md"
        task_path.parent.mkdir(parents=True)
        task_path.touch()
        content = "See [a](missing1.md) and [b](missing2.md) and [c](missing3.md)."
        result = check_links(content, task_path, tmp_path)
        assert len(result) == 3

    def test_mixed_valid_and_broken(self, tmp_path):
        """Only broken links are reported, valid ones are not."""
        task_path = tmp_path / "tasks" / "test.md"
        task_path.parent.mkdir(parents=True)
        task_path.touch()
        (tmp_path / "tasks" / "exists.md").touch()
        content = "See [valid](exists.md) and [broken](missing.md)."
        result = check_links(content, task_path, tmp_path)
        assert len(result) == 1
        assert "missing.md" in result[0]

    def test_directory_link(self, tmp_path):
        """Links to directories are valid."""
        task_path = tmp_path / "tasks" / "test.md"
        task_path.parent.mkdir(parents=True)
        task_path.touch()
        (tmp_path / "knowledge").mkdir()
        content = "See [knowledge](../knowledge)."
        assert check_links(content, task_path, tmp_path) == []
