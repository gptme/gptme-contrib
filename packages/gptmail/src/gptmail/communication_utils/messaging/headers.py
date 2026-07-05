"""Message header parsing and formatting utilities."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List


@dataclass
class MessageHeaders:
    """Cross-platform message headers.

    Provides unified interface for message metadata across platforms.
    """

    message_id: str
    from_address: str
    to_address: str
    date: datetime
    subject: str
    platform: str
    platform_message_id: str
    conversation_id: str | None = None
    in_reply_to: str | None = None
    references: List[str] | None = None

    def __post_init__(self):
        if self.references is None:
            self.references = []

    @classmethod
    def create(
        cls,
        from_address: str,
        to_address: str,
        subject: str,
        platform: str,
        platform_message_id: str,
        conversation_id: str | None = None,
        in_reply_to: str | None = None,
        references: List[str] | None = None,
    ) -> "MessageHeaders":
        """Create new message headers with generated universal ID.

        Args:
            from_address: Sender address (platform-specific format)
            to_address: Recipient address
            subject: Message subject/preview
            platform: Platform identifier (email, twitter, discord)
            platform_message_id: Platform's native message ID
            conversation_id: Cross-platform conversation ID (optional)
            in_reply_to: Parent message ID for threading
            references: Full thread chain

        Returns:
            MessageHeaders instance
        """
        return cls(
            message_id=str(uuid.uuid4()),
            from_address=from_address,
            to_address=to_address,
            date=datetime.now(),
            subject=subject,
            platform=platform,
            platform_message_id=platform_message_id,
            conversation_id=conversation_id or str(uuid.uuid4()),
            in_reply_to=in_reply_to,
            references=references or [],
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert headers to dictionary format.

        Returns:
            Dict representation of headers
        """
        return {
            "message_id": self.message_id,
            "from": self.from_address,
            "to": self.to_address,
            "date": self.date.isoformat(),
            "subject": self.subject,
            "platform": self.platform,
            "platform_message_id": self.platform_message_id,
            "conversation_id": self.conversation_id,
            "in_reply_to": self.in_reply_to,
            "references": self.references,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MessageHeaders":
        """Create headers from dictionary.

        Args:
            data: Dictionary with header fields

        Returns:
            MessageHeaders instance
        """
        return cls(
            message_id=data["message_id"],
            from_address=data["from"],
            to_address=data["to"],
            date=datetime.fromisoformat(data["date"]),
            subject=data["subject"],
            platform=data["platform"],
            platform_message_id=data["platform_message_id"],
            conversation_id=data.get("conversation_id"),
            in_reply_to=data.get("in_reply_to"),
            references=data.get("references", []),
        )

    @classmethod
    def from_email(cls, email_headers: Dict[str, str]) -> "MessageHeaders":
        """Parse email headers into unified format.

        Args:
            email_headers: Email header dictionary

        Returns:
            MessageHeaders instance
        """
        # Parse email date (simplified - real impl would use email.utils.parsedate)
        date = datetime.now()  # Placeholder

        return cls(
            message_id=email_headers.get("Message-ID", str(uuid.uuid4())),
            from_address=email_headers["From"],
            to_address=email_headers["To"],
            date=date,
            subject=email_headers.get("Subject", ""),
            platform="email",
            platform_message_id=email_headers.get("Message-ID", ""),
            conversation_id=email_headers.get("Thread-ID"),
            in_reply_to=email_headers.get("In-Reply-To"),
            references=email_headers.get("References", "").split()
            if email_headers.get("References")
            else [],
        )


def parse_headers(content: str, platform: str = "email") -> MessageHeaders:
    """Parse message headers from raw content.

    Args:
        content: Raw message content
        platform: Platform identifier

    Returns:
        Parsed MessageHeaders
    """
    # Simple implementation - real version would parse platform-specific formats
    lines = content.split("\n")
    headers = {}
    for line in lines:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip()] = value.strip()
        else:
            break  # End of headers

    if platform == "email":
        return MessageHeaders.from_email(headers)
    else:
        # Generic parsing for other platforms
        return MessageHeaders.create(
            from_address=headers.get("From", "unknown"),
            to_address=headers.get("To", "unknown"),
            subject=headers.get("Subject", ""),
            platform=platform,
            platform_message_id=headers.get("Message-ID", str(uuid.uuid4())),
        )
