"""
State management utilities for cross-platform communication.

Provides file locks, conversation tracking, and completion status
for coordinating multiple communication processes.
"""

from .locks import FileLock, LockError
from .tracking import ConversationTracker, MessageState

__all__ = ["FileLock", "LockError", "ConversationTracker", "MessageState"]
