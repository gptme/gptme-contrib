"""
Conversation and message state tracking.

Provides thread-safe state management for tracking conversations,
messages, and completion status across platforms.
"""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from .locks import file_lock


class MessageState(Enum):
    """State of a message in processing."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_REPLY_NEEDED = "no_reply_needed"


@dataclass
class MessageInfo:
    """Information about a message."""

    message_id: str
    conversation_id: str | None = None
    in_reply_to: str | None = None
    state: MessageState = MessageState.PENDING
    created_at: str | None = None
    updated_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        data["state"] = self.state.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "MessageInfo":
        """Create from dictionary."""
        data = data.copy()
        data["state"] = MessageState(data["state"])
        return cls(**data)


class ConversationTracker:
    """
    Track conversations and message states.

    Provides thread-safe state management using file locks
    to coordinate multiple processes accessing the same state.
    """

    def __init__(self, state_dir: str | Path):
        """
        Initialize conversation tracker.

        Args:
            state_dir: Directory for storing state files
        """
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _get_state_file(self, conversation_id: str) -> Path:
        """Get path to state file for conversation."""
        return self.state_dir / f"{conversation_id}.json"

    def _get_lock_file(self, conversation_id: str) -> Path:
        """Get path to lock file for conversation."""
        return self.state_dir / f"{conversation_id}.lock"

    def get_message_state(self, conversation_id: str, message_id: str) -> MessageInfo | None:
        """
        Get state of a specific message.

        Args:
            conversation_id: Conversation identifier
            message_id: Message identifier

        Returns:
            MessageInfo or None if not found
        """
        state_file = self._get_state_file(conversation_id)
        if not state_file.exists():
            return None

        with file_lock(self._get_lock_file(conversation_id)):
            with open(state_file) as f:
                data = json.load(f)

            message_data = data.get("messages", {}).get(message_id)
            if not message_data:
                return None

            return MessageInfo.from_dict(message_data)

    def set_message_state(
        self,
        conversation_id: str,
        message_id: str,
        state: MessageState,
        error: str | None = None,
    ) -> None:
        """
        Update message state.

        Args:
            conversation_id: Conversation identifier
            message_id: Message identifier
            state: New message state
            error: Optional error message if state is FAILED
        """
        state_file = self._get_state_file(conversation_id)

        with file_lock(self._get_lock_file(conversation_id)):
            # Load existing state
            if state_file.exists():
                with open(state_file) as f:
                    data = json.load(f)
            else:
                data = {"conversation_id": conversation_id, "messages": {}}

            # Update message info
            if message_id not in data["messages"]:
                data["messages"][message_id] = {
                    "message_id": message_id,
                    "conversation_id": conversation_id,
                    "state": state.value,
                    "created_at": datetime.now().isoformat(),
                }
            else:
                data["messages"][message_id]["state"] = state.value
                data["messages"][message_id]["updated_at"] = datetime.now().isoformat()

            if error:
                data["messages"][message_id]["error"] = error

            # Save updated state
            with open(state_file, "w") as f:
                json.dump(data, f, indent=2)

    def track_message(
        self,
        conversation_id: str,
        message_id: str,
        in_reply_to: str | None = None,
    ) -> MessageInfo:
        """
        Start tracking a new message.

        Args:
            conversation_id: Conversation identifier
            message_id: Message identifier
            in_reply_to: Optional parent message ID

        Returns:
            Created MessageInfo
        """
        state_file = self._get_state_file(conversation_id)

        with file_lock(self._get_lock_file(conversation_id)):
            # Load existing state
            if state_file.exists():
                with open(state_file) as f:
                    data = json.load(f)
            else:
                data = {"conversation_id": conversation_id, "messages": {}}

            # Create message info
            message_info = MessageInfo(
                message_id=message_id,
                conversation_id=conversation_id,
                in_reply_to=in_reply_to,
                state=MessageState.PENDING,
                created_at=datetime.now().isoformat(),
            )

            data["messages"][message_id] = message_info.to_dict()

            # Save state
            with open(state_file, "w") as f:
                json.dump(data, f, indent=2)

            return message_info

    def get_pending_messages(self, conversation_id: str) -> list[MessageInfo]:
        """
        Get all pending messages for a conversation.

        Args:
            conversation_id: Conversation identifier

        Returns:
            List of pending MessageInfo objects
        """
        state_file = self._get_state_file(conversation_id)
        if not state_file.exists():
            return []

        with file_lock(self._get_lock_file(conversation_id)):
            with open(state_file) as f:
                data = json.load(f)

            messages = []
            for msg_data in data.get("messages", {}).values():
                msg_info = MessageInfo.from_dict(msg_data)
                if msg_info.state == MessageState.PENDING:
                    messages.append(msg_info)

            return messages

    def cleanup_old_conversations(self, days: int = 30) -> int:
        """
        Remove state files for old conversations.

        Args:
            days: Remove conversations older than this many days

        Returns:
            Number of conversations cleaned up
        """
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=days)
        removed = 0

        for state_file in self.state_dir.glob("*.json"):
            if state_file.stat().st_mtime < cutoff.timestamp():
                state_file.unlink()
                # Remove associated lock file
                lock_file = state_file.with_suffix(".lock")
                if lock_file.exists():
                    lock_file.unlink()
                removed += 1

        return removed
