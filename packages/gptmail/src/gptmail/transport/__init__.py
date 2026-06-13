"""Transport seam for gptmail.

gptmail is being folded into a single comms tool that serves both email
(IMAP/SMTP) and inter-agent (filesystem/SSH) messaging. The two differ only in
*how* a message is sent, listed, and read — the reply-tracking core
(``ConversationTracker``) is shared across both, keyed by a ``channel`` field on
each tracked message.

This module defines the ``Transport`` Protocol: the narrow seam a CLI dispatches
to regardless of the underlying medium. ``EmailTransport`` (see ``email.py``) is
a thin adapter over the existing ``lib.AgentEmail`` IMAP/SMTP stack. A future
``AgentTransport`` will implement the same Protocol over a filesystem inbox with
**no email-stack imports**, so inter-agent messaging stays testable in isolation
(no IMAP/SMTP infra). See task ``fold-agent-msg-into-gptmail-single-comms-tool``.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

__all__ = ["Transport", "EmailTransport"]


@runtime_checkable
class Transport(Protocol):
    """A messaging transport: send, list, read, and locate a conversation.

    Implementations are deliberately thin. Reply-tracking is **not** part of the
    Protocol — it lives in the shared ``ConversationTracker``, which transports
    stamp with their :attr:`channel`. This keeps the seam medium-agnostic and
    lets a single tracker store answer cross-channel "what do I owe a reply to"
    queries.
    """

    #: Stable channel identifier stamped onto tracked messages (e.g. ``"email"``,
    #: ``"agent"``). Used as the ``channel`` field on ``MessageInfo``.
    channel: str

    def send(
        self,
        to: str,
        subject: str,
        content: str,
        *,
        reply_to: str | None = None,
    ) -> str:
        """Send a message and return its message ID.

        Args:
            to: Recipient identifier (email address, agent name, …).
            subject: Message subject.
            content: Message body (Markdown).
            reply_to: Optional message ID this is a reply to (for threading).
        """
        ...

    def list_inbox(self, folder: str = "inbox") -> list[tuple[str, str, datetime]]:
        """List messages in a folder as ``(message_id, subject, timestamp)``."""
        ...

    def read(self, message_id: str, include_thread: bool = False) -> str:
        """Return the rendered message body, optionally with its full thread."""
        ...

    def conversation_id_for(self, message_id: str) -> str:
        """Return the ``ConversationTracker`` conversation_id for this message."""
        ...


# Re-exported so callers import both the seam and its first implementation from
# one place. EmailTransport satisfies Transport structurally (it does not import
# it), so there is no circular dependency.
from .email import EmailTransport  # noqa: E402
