"""Tests for Discord bot conversation log persistence.

Verifies that conversation messages survive restarts when the bot stores
LogManager objects and calls write() after each mutation.

Key design constraint: LogManager.logfile for the 'main' branch resolves to
get_logs_dir() / self.chat_id / "conversation.jsonl", NOT to self.logdir.
The load path in LogManager.load() MUST match this write path for persistence
to work. If the load logdir and the implicit write dir differ, writes land in
the central directory while reload reads from a stale location.
"""

from __future__ import annotations

from pathlib import Path

from gptme.chat import Message
from gptme.dirs import get_logs_dir
from gptme.logmanager import LogManager


def test_user_message_survives_reload(tmp_path: Path) -> None:
    """User message appended through LogManager persists across reload.

    Uses a logdir under get_logs_dir() so that LogManager.logfile resolves
    to the same location LogManager.load() reads from.
    """
    # Simulate the channel path the Discord bot would use
    logsdir = get_logs_dir()
    channel_id = "test_persistence_channel_948b"
    logdir = logsdir / channel_id

    initial_msgs = [
        Message("system", "You are a helpful assistant."),
    ]

    # Simulate get_conversation(): load or create
    manager = LogManager.load(logdir, initial_msgs, create=True)

    # Simulate on_message(): append a user message
    manager.append(Message("user", "Hello, bot!"))

    # Verify the message is in memory
    user_msgs = [m for m in manager.log if m.role == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "Hello, bot!"

    # Simulate a restart: load again with same path
    manager2 = LogManager.load(logdir, initial_msgs, create=False)

    # Messages should survive the reload
    user_msgs2 = [m for m in manager2.log if m.role == "user"]
    assert len(user_msgs2) >= 1
    assert any(m.content == "Hello, bot!" for m in user_msgs2)


def test_multiple_turns_survive_reload(tmp_path: Path) -> None:
    """A full conversation turn (user + assistant + system) persists."""
    logsdir = get_logs_dir()
    channel_id = "test_multi_turn_948b"
    logdir = logsdir / channel_id

    initial_msgs = [
        Message("system", "You are a helpful assistant."),
    ]

    manager = LogManager.load(logdir, initial_msgs, create=True)

    # Turn 1
    manager.append(Message("user", "What is 2+2?"))
    manager.append(Message("assistant", "2+2 is 4."))

    # Turn 2
    manager.append(Message("user", "What about 3+3?"))
    manager.append(Message("assistant", "3+3 is 6."))
    manager.append(Message("system", "Ran command: echo done"))

    # Reload
    manager2 = LogManager.load(logdir, initial_msgs, create=False)

    # All non-initial messages should survive
    all_msgs = list(manager2.log.messages)
    roles = [m.role for m in all_msgs]
    contents = {m.content for m in all_msgs}

    assert "user" in roles
    assert "assistant" in roles
    assert "What is 2+2?" in contents
    assert "2+2 is 4." in contents
    assert "3+3 is 6." in contents


def test_message_count_across_sessions(tmp_path: Path) -> None:
    """Message count is correct after multiple appends and a reload."""
    logsdir = get_logs_dir()
    channel_id = "test_count_948b"
    logdir = logsdir / channel_id

    initial_msgs = [Message("system", "You are a helpful assistant.")]

    manager = LogManager.load(logdir, initial_msgs, create=True)

    # Append 5 user messages
    for i in range(5):
        manager.append(Message("user", f"Message {i}"))

    assert len(manager.log) == len(initial_msgs) + 5

    # Reload — count should be the same
    manager2 = LogManager.load(logdir, initial_msgs, create=False)
    assert len(manager2.log) == len(initial_msgs) + 5
