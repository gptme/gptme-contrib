"""
Conversation and message state tracking.

Provides thread-safe state management for tracking conversations,
messages, and completion status across platforms.
"""

import json
import uuid
from dataclasses import asdict, dataclass, field
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
    """Information about a message (cross-platform support)."""

    # Core identifiers
    message_id: str
    conversation_id: str | None = None

    # Platform-specific fields (Phase 3.1)
    platform: str = "email"  # Platform identifier: email, twitter, discord
    platform_message_id: str = ""  # Native platform ID (tweet_id, discord_id, etc.)

    # Threading
    in_reply_to: str | None = None  # Parent message ID (universal)
    references: list[str] | None = field(default_factory=list)  # Full thread chain

    # Metadata
    from_user: str | None = None
    to_user: str | None = None
    subject: str | None = None

    # State tracking
    state: MessageState = MessageState.PENDING
    created_at: str | None = None
    updated_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        data = asdict(self)
        data["state"] = self.state.value
        # Ensure references is always a list (not None)
        if data.get("references") is None:
            data["references"] = []
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "MessageInfo":
        """Create from dictionary with backward compatibility."""
        data = data.copy()
        data["state"] = MessageState(data["state"])

        # Ensure new fields have defaults for backward compatibility
        data.setdefault("platform", "email")
        data.setdefault("platform_message_id", "")
        data.setdefault("references", [])
        data.setdefault("from_user", None)
        data.setdefault("to_user", None)
        data.setdefault("subject", None)

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

    def get_message_state(
        self, conversation_id: str, message_id: str
    ) -> MessageInfo | None:
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

    def create_unified_message(
        self,
        conversation_id: str,
        platform: str,
        platform_message_id: str,
        in_reply_to: str | None = None,
        from_user: str | None = None,
        to_user: str | None = None,
        subject: str | None = None,
    ) -> MessageInfo:
        """
        Create a message with universal ID for cross-platform tracking.

        Args:
            conversation_id: Conversation identifier
            platform: Platform identifier (email, twitter, discord)
            platform_message_id: Native platform message ID
            in_reply_to: Optional parent message ID (universal)
            from_user: Sender identifier
            to_user: Recipient identifier
            subject: Message subject/preview

        Returns:
            Created MessageInfo with universal UUID
        """
        # Generate universal message ID
        universal_id = str(uuid.uuid4())

        # Build references list from parent
        references = []
        if in_reply_to:
            parent = self.get_message_state(conversation_id, in_reply_to)
            if parent and parent.references:
                references = parent.references.copy()
            if in_reply_to not in references:
                references.append(in_reply_to)

        # Create message info
        message_info = MessageInfo(
            message_id=universal_id,
            conversation_id=conversation_id,
            platform=platform,
            platform_message_id=platform_message_id,
            in_reply_to=in_reply_to,
            references=references,
            from_user=from_user,
            to_user=to_user,
            subject=subject,
            state=MessageState.PENDING,
            created_at=datetime.now().isoformat(),
        )

        # Save to state
        state_file = self._get_state_file(conversation_id)
        with file_lock(self._get_lock_file(conversation_id)):
            if state_file.exists():
                with open(state_file) as f:
                    data = json.load(f)
            else:
                data = {"conversation_id": conversation_id, "messages": {}}

            data["messages"][universal_id] = message_info.to_dict()

            with open(state_file, "w") as f:
                json.dump(data, f, indent=2)

        return message_info

    def get_conversation_thread(self, conversation_id: str) -> list[MessageInfo]:
        """
        Get full conversation thread across all platforms.

        Returns messages in chronological order.
        """
        state_file = self._get_state_file(conversation_id)
        if not state_file.exists():
            return []

        with file_lock(self._get_lock_file(conversation_id)):
            with open(state_file) as f:
                data = json.load(f)

        messages = [
            MessageInfo.from_dict(msg_data)
            for msg_data in data.get("messages", {}).values()
        ]
        return sorted(messages, key=lambda m: m.created_at or "")

    def get_message_by_platform_id(
        self, conversation_id: str, platform: str, platform_message_id: str
    ) -> MessageInfo | None:
        """
        Get message by platform-specific ID.

        Args:
            conversation_id: Conversation identifier
            platform: Platform identifier (email, twitter, discord)
            platform_message_id: Native platform message ID

        Returns:
            MessageInfo if found, None otherwise
        """
        state_file = self._get_state_file(conversation_id)
        if not state_file.exists():
            return None

        with file_lock(self._get_lock_file(conversation_id)):
            with open(state_file) as f:
                data = json.load(f)

        # Search for message with matching platform and platform_message_id
        for msg_data in data.get("messages", {}).values():
            msg_info = MessageInfo.from_dict(msg_data)
            if (
                msg_info.platform == platform
                and msg_info.platform_message_id == platform_message_id
            ):
                return msg_info

        return None

    def link_cross_platform(
        self,
        conversation_id: str,
        message_id: str,
        linked_platform: str,
        linked_message_id: str,
    ) -> None:
        """
        Link a message to its counterpart on another platform.

        This allows tracking the same logical message across multiple platforms
        (e.g., a tweet that was also sent via email).

        Args:
            conversation_id: Conversation identifier
            message_id: Universal message ID
            linked_platform: Platform of the linked message
            linked_message_id: Platform-specific ID of linked message
        """
        state_file = self._get_state_file(conversation_id)

        with file_lock(self._get_lock_file(conversation_id)):
            with open(state_file) as f:
                data = json.load(f)

            if message_id not in data["messages"]:
                raise ValueError(f"Message {message_id} not found")

            # Add cross-platform link
            if "cross_platform_links" not in data["messages"][message_id]:
                data["messages"][message_id]["cross_platform_links"] = {}

            data["messages"][message_id]["cross_platform_links"][linked_platform] = (
                linked_message_id
            )
            data["messages"][message_id]["updated_at"] = datetime.now().isoformat()

            with open(state_file, "w") as f:
                json.dump(data, f, indent=2)
