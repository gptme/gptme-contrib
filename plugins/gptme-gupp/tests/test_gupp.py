"""Tests for GUPP (work persistence) plugin."""

import json

import pytest


# We need to patch the hooks directory for tests
@pytest.fixture
def hooks_dir(tmp_path, monkeypatch):
    """Create a temporary hooks directory."""
    hooks_dir = tmp_path / "state" / "hooks"
    hooks_dir.mkdir(parents=True)

    # Patch _get_hooks_dir to return our temp dir
    from gptme_gupp.tools import gupp

    monkeypatch.setattr(gupp, "_get_hooks_dir", lambda: hooks_dir)
    return hooks_dir


def test_hook_start(hooks_dir):
    """Test creating a new hook."""
    from gptme_gupp.tools import hook_start

    result = hook_start(
        task_id="test-task",
        context_summary="Test context",
        next_action="Test action",
    )

    assert "✅ Hook created: test-task" in result
    assert (hooks_dir / "test-task.json").exists()

    # Verify hook content
    hook = json.loads((hooks_dir / "test-task.json").read_text())
    assert hook["task_id"] == "test-task"
    assert hook["context_summary"] == "Test context"
    assert hook["next_action"] == "Test action"
    assert hook["priority"] == "medium"


def test_hook_update(hooks_dir):
    """Test updating an existing hook."""
    from gptme_gupp.tools import hook_start, hook_update

    # Create hook first
    hook_start("test-task", "Initial context", "Initial action")

    # Update it
    result = hook_update(
        "test-task", current_step="Step 2", next_action="Updated action"
    )

    assert "✅ Hook updated: test-task" in result

    # Verify updates
    hook = json.loads((hooks_dir / "test-task.json").read_text())
    assert hook["current_step"] == "Step 2"
    assert hook["next_action"] == "Updated action"


def test_hook_update_not_found(hooks_dir):
    """Test updating a non-existent hook."""
    from gptme_gupp.tools import hook_update

    result = hook_update("nonexistent", current_step="Step 2")
    assert "❌ Hook not found" in result


def test_hook_complete(hooks_dir):
    """Test completing a hook."""
    from gptme_gupp.tools import hook_complete, hook_start

    hook_start("test-task", "Context", "Action")
    assert (hooks_dir / "test-task.json").exists()

    result = hook_complete("test-task")
    assert "✅ Hook completed: test-task" in result
    assert not (hooks_dir / "test-task.json").exists()


def test_hook_list(hooks_dir):
    """Test listing hooks."""
    from gptme_gupp.tools import hook_list, hook_start

    # Create multiple hooks
    hook_start("task-1", "Context 1", "Action 1", priority="high")
    hook_start("task-2", "Context 2", "Action 2", priority="low")
    hook_start("task-3", "Context 3", "Action 3", priority="medium")

    hooks = hook_list()
    assert len(hooks) == 3

    # Should be sorted by priority (high first)
    assert hooks[0]["task_id"] == "task-1"
    assert hooks[0]["priority"] == "high"


def test_hook_status_empty(hooks_dir):
    """Test status with no hooks."""
    from gptme_gupp.tools import hook_status

    result = hook_status()
    assert "No pending hooks" in result


def test_hook_status_with_hooks(hooks_dir):
    """Test status with pending hooks."""
    from gptme_gupp.tools import hook_start, hook_status

    hook_start("test-task", "Test context", "Test action", priority="high")

    result = hook_status()
    assert "Pending Work Hooks" in result
    assert "test-task" in result
    assert "high" in result


def test_hook_abandon(hooks_dir):
    """Test abandoning a hook."""
    from gptme_gupp.tools import hook_abandon, hook_start

    hook_start("test-task", "Context", "Action")

    result = hook_abandon("test-task", "No longer needed")
    assert "✅ Hook abandoned: test-task" in result
    assert not (hooks_dir / "test-task.json").exists()

    # Check archive
    archive_dir = hooks_dir / "archive"
    assert archive_dir.exists()
    archived_files = list(archive_dir.glob("test-task-*.json"))
    assert len(archived_files) == 1

    # Verify archived content
    archived = json.loads(archived_files[0].read_text())
    assert archived["abandon_reason"] == "No longer needed"


def test_hook_id_sanitization(hooks_dir):
    """Test that task IDs with special chars are sanitized."""
    from gptme_gupp.tools import hook_start

    hook_start("task/with/slashes", "Context", "Action")

    # Should create file with sanitized name
    assert (hooks_dir / "task-with-slashes.json").exists()
