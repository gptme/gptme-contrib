"""Email transport: a thin adapter over ``lib.AgentEmail``.

The IMAP/SMTP implementation stays in :class:`gptmail.lib.AgentEmail`. This
adapter exposes that stack behind the :class:`~gptmail.transport.Transport`
seam so a unified CLI can dispatch to it alongside the forthcoming inter-agent
transport. It deliberately adds no behavior — every method delegates.
"""

from datetime import datetime
from pathlib import Path

from ..lib import AgentEmail


class EmailTransport:
    """Transport adapter wrapping :class:`gptmail.lib.AgentEmail`."""

    #: Channel identifier stamped onto tracked messages for this transport.
    channel = "email"

    #: All emails share a single tracker conversation (matches AgentEmail).
    _CONVERSATION_ID = "email"

    def __init__(
        self,
        workspace_dir: str | Path,
        own_email: str | None = None,
        own_email_name: str | None = None,
    ):
        self._email = AgentEmail(workspace_dir, own_email, own_email_name)

    @property
    def email(self) -> AgentEmail:
        """The underlying ``AgentEmail`` (for tracker access and email-only ops)."""
        return self._email

    def send(
        self,
        to: str,
        subject: str,
        content: str,
        *,
        reply_to: str | None = None,
    ) -> str:
        """Compose and deliver an email, returning its message ID."""
        references = [reply_to] if reply_to else None
        message_id = self._email.compose(
            to, subject, content, reply_to=reply_to, references=references
        )
        self._email.send(message_id)
        return message_id

    def list_inbox(self, folder: str = "inbox") -> list[tuple[str, str, datetime]]:
        return self._email.list_messages(folder)

    def read(self, message_id: str, include_thread: bool = False) -> str:
        return self._email.read_message(message_id, include_thread=include_thread)

    def conversation_id_for(self, message_id: str) -> str:
        return self._CONVERSATION_ID
