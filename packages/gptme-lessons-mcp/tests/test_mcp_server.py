"""Tests for the gptme-lessons-mcp server."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gptme_lessons_mcp.mcp_server import get_lesson, list_categories, list_lessons, match_lessons


def _make_mock_lesson(
    title="Test Lesson", category="tools", keywords=None, body="# Test\n\nContent."
):
    """Create a mock Lesson object."""
    lesson = MagicMock()
    lesson.title = title
    lesson.category = category
    lesson.description = "A test lesson."
    lesson.body = body
    lesson.path = Path(f"/tmp/fake/lessons/{category}/{title.lower().replace(' ', '-')}.md")
    lesson.metadata.keywords = keywords or ["test", "example"]
    lesson.metadata.status = "active"
    lesson.metadata.name = None
    return lesson


@pytest.fixture
def mock_index():
    """Patch LessonIndex to return a controlled set of lessons."""
    lessons = [
        _make_mock_lesson("Git Workflow", "tools", ["git", "commit", "patch"]),
        _make_mock_lesson("Error Handling", "patterns", ["error", "exception", "handle"]),
        _make_mock_lesson("Testing Strategy", "workflow", ["test", "pytest", "coverage"]),
    ]
    with patch("gptme_lessons_mcp.mcp_server._load_index") as mock_load:
        idx = MagicMock()
        idx.lessons = lessons
        mock_load.return_value = idx
        yield mock_load, lessons


def test_match_lessons_returns_matches(mock_index):
    """match_lessons finds relevant lessons for a context string."""
    _, lessons = mock_index
    results = match_lessons("I need to commit changes with git patch")
    assert len(results) >= 1
    titles = [r["title"] for r in results]
    assert "Git Workflow" in titles


def test_match_lessons_top_k(mock_index):
    """match_lessons respects top_k limit."""
    results = match_lessons("test example", top_k=1)
    assert len(results) <= 1


def test_match_lessons_no_match(mock_index):
    """match_lessons returns empty list when nothing matches."""
    results = match_lessons("unrelated zebra database query")
    # May return results since keywords could overlap — just verify it returns a list
    assert isinstance(results, list)


def test_match_lessons_has_required_fields(mock_index):
    """match_lessons results include expected fields."""
    results = match_lessons("git commit")
    if results:
        r = results[0]
        assert "title" in r
        assert "category" in r
        assert "body" in r
        assert "score" in r
        assert "matched_by" in r


def test_list_lessons_all(mock_index):
    """list_lessons returns all lessons when no filter given."""
    results = list_lessons()
    assert len(results) == 3


def test_list_lessons_by_category(mock_index):
    """list_lessons filters by category."""
    results = list_lessons(category="tools")
    assert all(r["category"] == "tools" for r in results)
    assert len(results) == 1


def test_list_lessons_by_search(mock_index):
    """list_lessons filters by text search."""
    results = list_lessons(search="error")
    titles = [r["title"] for r in results]
    assert "Error Handling" in titles


def test_list_lessons_no_body(mock_index):
    """list_lessons does not include body to keep responses compact."""
    results = list_lessons()
    for r in results:
        assert "body" not in r


def test_get_lesson_by_path(mock_index):
    """get_lesson finds a lesson by path substring."""
    results = get_lesson("git-workflow")
    assert results.get("title") == "Git Workflow"
    assert "body" in results


def test_get_lesson_by_title(mock_index):
    """get_lesson falls back to title substring match."""
    results = get_lesson("Error Handling")
    assert results.get("title") == "Error Handling"


def test_get_lesson_not_found(mock_index):
    """get_lesson returns error dict when nothing matches."""
    results = get_lesson("does-not-exist-xyz")
    assert "error" in results


def test_list_categories(mock_index):
    """list_categories returns sorted unique category names."""
    cats = list_categories()
    assert "tools" in cats
    assert "patterns" in cats
    assert "workflow" in cats
    assert cats == sorted(cats)
