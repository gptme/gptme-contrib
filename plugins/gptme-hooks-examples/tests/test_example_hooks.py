"""Tests for example hooks plugin."""

from pathlib import Path
from unittest.mock import Mock

from gptme.message import Message
from gptme_example_hooks.hooks.example_hooks import (
    message_post_process_hook,
    register,
    session_start_hook,
    tool_pre_execute_hook,
)


def test_session_start_hook():
    """Test session start hook generates welcome message."""
    logdir = Path("/tmp/test-log")
    workspace = Path("/tmp/test-workspace")
    initial_msgs: list[Message] = []

    messages = list(session_start_hook(logdir, workspace, initial_msgs))

    assert len(messages) == 1
    assert messages[0].role == "system"
    assert "Example hooks plugin loaded" in messages[0].content
    assert str(workspace) in messages[0].content


def test_session_start_hook_no_workspace():
    """Test session start hook handles no workspace."""
    logdir = Path("/tmp/test-log")
    workspace = None
    initial_msgs: list[Message] = []

    messages = list(session_start_hook(logdir, workspace, initial_msgs))

    assert len(messages) == 1
    assert messages[0].role == "system"
    assert "Example hooks plugin loaded" in messages[0].content


def test_tool_pre_execute_hook():
    """Test tool pre-execute hook logs tool usage."""
    mock_log = Mock()
    workspace = Path("/tmp/test-workspace")
    mock_tool_use = Mock()
    mock_tool_use.tool = "shell"
    mock_tool_use.content = "echo 'test'"

    messages = list(tool_pre_execute_hook(mock_log, workspace, mock_tool_use))

    assert len(messages) == 1
    assert messages[0].role == "system"
    assert "About to execute tool: shell" in messages[0].content


def test_message_post_process_hook():
    """Test message post-process hook analyzes messages."""
    mock_manager = Mock()
    mock_message = Mock()
    mock_message.role = "user"
    mock_message.content = "Hello, world!"
    mock_manager.log.messages = [mock_message]

    messages = list(message_post_process_hook(mock_manager))

    assert len(messages) == 1
    assert messages[0].role == "system"
    assert "Processed user message" in messages[0].content
    assert "13 chars" in messages[0].content


def test_message_post_process_hook_empty_log():
    """Test message post-process hook handles empty log."""
    mock_manager = Mock()
    mock_manager.log.messages = []

    messages = list(message_post_process_hook(mock_manager))

    assert len(messages) == 0


def test_register():
    """Test register() function exists and is callable."""
    # Simply verify the function can be called without error
    # Actual registration testing would require mocking the global registry
    register()
