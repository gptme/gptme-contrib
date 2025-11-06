"""Message formatting and composition utilities.

Provides cross-platform utilities for message formatting, thread composition,
and text processing that can be used across Twitter, Discord, email, and other platforms.
"""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ThreadMessage:
    """Represents a message in a thread.

    Attributes:
        text: Message content
        in_reply_to: ID of parent message (None for root)
        order: Position in thread (0-indexed)
    """

    text: str
    in_reply_to: Optional[str] = None
    order: int = 0

    def __post_init__(self):
        """Validate message text."""
        if not self.text.strip():
            raise ValueError("Message text cannot be empty")


def split_thread(
    text: str, delimiter: str = "\n---\n", max_length: Optional[int] = None
) -> List[ThreadMessage]:
    """Split text into thread messages using a delimiter.

    Args:
        text: Full text to split into thread
        delimiter: String to split on (default: "\n---\n")
        max_length: Optional maximum length per message (for auto-splitting)

    Returns:
        List of ThreadMessage objects in order

    Examples:
        >>> messages = split_thread("Part 1\n---\nPart 2\n---\nPart 3")
        >>> len(messages)
        3
        >>> messages[0].text
        'Part 1'
        >>> messages[1].order
        1
    """
    parts = text.split(delimiter)
    messages: list[ThreadMessage] = []

    for i, part in enumerate(parts):
        part_text = part.strip()
        if not part_text:
            continue

        # If max_length specified and part exceeds it, split further
        if max_length and len(part_text) > max_length:
            # Simple word-based splitting
            words = part_text.split()
            current: list[str] = []
            current_len = 0

            for word in words:
                word_len = len(word) + 1  # +1 for space
                if current_len + word_len > max_length:
                    # Flush current buffer as message
                    if current:
                        messages.append(
                            ThreadMessage(
                                text=" ".join(current),
                                order=len(messages),
                            )
                        )
                    current = [word]
                    current_len = len(word)
                else:
                    current.append(word)
                    current_len += word_len

            # Flush remaining
            if current:
                messages.append(
                    ThreadMessage(
                        text=" ".join(current),
                        order=len(messages),
                    )
                )
        else:
            messages.append(
                ThreadMessage(
                    text=part_text,
                    order=len(messages),
                )
            )

    return messages


def format_for_platform(
    text: str,
    platform: str = "twitter",
    max_length: Optional[int] = None,
    truncate_suffix: str = "...",
) -> str:
    """Format text for specific platform constraints.

    Args:
        text: Text to format
        platform: Platform name (twitter, discord, email)
        max_length: Override default platform max length
        truncate_suffix: Suffix to add if truncated

    Returns:
        Formatted text respecting platform limits

    Examples:
        >>> format_for_platform("A" * 300, platform="twitter")
        'AAA...'  # Truncated to 280 chars + suffix
    """
    # Platform defaults
    platform_limits = {
        "twitter": 280,
        "discord": 2000,
        "email": None,  # No hard limit
    }

    limit = max_length or platform_limits.get(platform)
    if limit is None:
        return text

    # Truncate if needed
    if len(text) > limit:
        # Account for truncate suffix in limit
        truncate_at = max(0, limit - len(truncate_suffix))
        return text[:truncate_at] + truncate_suffix

    return text


def join_thread(messages: List[ThreadMessage], delimiter: str = "\n---\n") -> str:
    """Join thread messages back into single text.

    Args:
        messages: List of ThreadMessage objects
        delimiter: String to join with

    Returns:
        Combined text

    Examples:
        >>> msgs = [ThreadMessage("Part 1", order=0), ThreadMessage("Part 2", order=1)]
        >>> join_thread(msgs)
        'Part 1\n---\nPart 2'
    """
    # Sort by order to ensure correct sequence
    sorted_msgs = sorted(messages, key=lambda m: m.order)
    return delimiter.join(msg.text for msg in sorted_msgs)


def sanitize_text(text: str, platform: str = "twitter") -> str:
    """Sanitize text for platform-specific requirements.

    Args:
        text: Text to sanitize
        platform: Platform name

    Returns:
        Sanitized text safe for platform

    Examples:
        >>> sanitize_text("Test\\x00text")  # Remove null bytes
        'Testtext'
    """
    # Remove null bytes (universal)
    text = text.replace("\x00", "")

    # Platform-specific sanitization
    if platform == "twitter":
        # Twitter doesn't allow certain control characters
        # Remove or replace as needed
        pass
    elif platform == "discord":
        # Discord has different restrictions
        pass

    return text
