"""Message formatting and composition utilities."""

from .formatting import (
    ThreadMessage,
    format_for_platform,
    join_thread,
    sanitize_text,
    split_thread,
)
from .headers import MessageHeaders, parse_headers

__all__ = [
    "ThreadMessage",
    "format_for_platform",
    "join_thread",
    "sanitize_text",
    "split_thread",
    "MessageHeaders",
    "parse_headers",
]
