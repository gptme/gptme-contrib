"""Agent transport: filesystem inter-agent messaging.

Implements the :class:`~gptmail.transport.Transport` seam over a filesystem
``messages/{inbox,outbox}`` layout — the medium ``scripts/agent-msg.py`` uses for
SSH inter-agent messages. This module imports **nothing email-related** (no
``imaplib``/``smtplib``, no ``lib.AgentEmail``, no ``transport.email``), so
inter-agent messaging stays testable in isolated LXC sessions with no email
infra. A guard test (``test_agent_transport_no_email_imports.py``) locks that in.

Two deliberate design choices, both resolved with Bob (see task
``fold-agent-msg-into-gptmail-single-comms-tool``):

- **Sync-agnostic (Q2).** The transport only reads and writes local files. Remote
  delivery (SSH/SCP, git commit+push, shared-repo sync) is injected as a
  ``deliver`` callable, never built in. Tests pass no ``deliver`` and stay both
  network- and git-free; production wires in the SSH delivery currently in
  ``agent-msg.py``. A failed delivery stamps ``delivered: false`` on the outbox
  copy so reply-tracking does not count an undelivered message as a sent reply.
- **Reply-tracking lives in the shared tracker, not here (per the Protocol).**
  This class stamps each message with :attr:`channel` (``"agent"``); the unified
  ``ConversationTracker`` does the bookkeeping. The transport stays a thin seam.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import yaml

# A delivery hook: given the local outbox path and the recipient name, place the
# message in the recipient's inbox (SSH/SCP, shared repo, …) and return whether
# delivery succeeded. None means local-only (tests, single-host).
Deliver = Callable[[Path, str], bool]


def meta_of(path: Path) -> dict | None:
    """Parse the YAML frontmatter of a message file (``None`` if absent/invalid).

    Shared by ``AgentTransport`` and the ``gptmail agent`` CLI so the two never
    diverge. Email-stack-free, preserving the agent transport's isolation.
    """
    try:
        content = path.read_text()
    except OSError:
        return None
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    return meta if isinstance(meta, dict) else None


class AgentTransport:
    """Transport over a filesystem ``messages/{inbox,outbox}`` directory.

    Message IDs are inbox/outbox *filenames* — stable, unique, and already the
    handle ``agent-msg.py`` users pass to ``read``/``reply``.
    """

    #: Channel identifier stamped onto tracked messages for this transport.
    channel = "agent"

    def __init__(
        self,
        messages_dir: str | Path,
        self_name: str,
        *,
        deliver: Deliver | None = None,
    ) -> None:
        self._dir = Path(messages_dir)
        self._self = self_name.lower()
        self._deliver = deliver
        self._ensure_dirs()

    @property
    def inbox(self) -> Path:
        return self._dir / "inbox"

    @property
    def outbox(self) -> Path:
        return self._dir / "outbox"

    def _ensure_dirs(self) -> None:
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _make_filename(sender: str, subject: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        safe_sender = "".join(c if c.isalnum() or c in "-_" else "-" for c in sender)
        safe_sender = safe_sender[:20].strip("-")
        safe_subject = "".join(c if c.isalnum() or c in "-_" else "-" for c in subject)
        safe_subject = safe_subject[:40].strip("-")
        return f"{ts}-{safe_sender}-{safe_subject}.md"

    @staticmethod
    def _format(
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
    ) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta: dict[str, object] = {
            "from": sender,
            "to": recipient,
            "timestamp": ts,
            "subject": subject,
            "read": False,
        }
        if in_reply_to:
            meta["in_reply_to"] = in_reply_to
        frontmatter = yaml.dump(
            meta, default_flow_style=False, allow_unicode=True, sort_keys=False
        ).rstrip()
        return f"---\n{frontmatter}\n---\n\n{body}\n"

    def _meta_of(self, path: Path) -> dict | None:
        return meta_of(path)

    @staticmethod
    def _resolve_within(folder: Path, message_id: str) -> Path | None:
        """Resolve ``message_id`` inside ``folder``, rejecting path traversal.

        ``message_id`` may come from a remote agent's frontmatter (e.g. an
        ``in_reply_to`` link), so an attacker-crafted ``../../etc/passwd`` must
        not escape ``folder``. ``Path.__truediv__`` does not reject traversal,
        hence the explicit prefix check after ``resolve()``.
        """
        path = (folder / message_id).resolve()
        if not str(path).startswith(str(folder.resolve()) + "/"):
            return None
        return path if path.exists() else None

    def _resolve_in_inbox(self, message_id: str) -> Path | None:
        """Resolve a message_id to an inbox path, rejecting path traversal."""
        return self._resolve_within(self.inbox, message_id)

    def send(
        self,
        to: str,
        subject: str,
        content: str,
        *,
        reply_to: str | None = None,
        references: list[str] | None = None,
    ) -> str:
        """Write a message to the outbox and (optionally) deliver it.

        ``references`` is accepted for Protocol parity; agent messages carry a
        single ``in_reply_to`` link rather than a full ancestor chain, so only
        the immediate parent (``reply_to``) is recorded. Returns the message ID
        (outbox filename).
        """
        filename = self._make_filename(self._self, subject)
        local_path = self.outbox / filename
        local_path.write_text(
            self._format(self._self, to.lower(), subject, content, in_reply_to=reply_to)
        )
        if self._deliver is not None and not self._deliver(local_path, to.lower()):
            self._stamp_delivery_failed(local_path)
        return filename

    def _stamp_delivery_failed(self, local_path: Path) -> None:
        """Mark an outbox file ``delivered: false`` so reply-tracking skips it."""
        content = local_path.read_text()
        if not content.startswith("---"):
            return
        parts = content.split("---", 2)
        if len(parts) < 3:
            return
        fm = parts[1]
        if not re.search(r"^delivered:", fm, flags=re.MULTILINE):
            fm = fm.rstrip("\n") + "\ndelivered: false\n"
        local_path.write_text("---".join([parts[0], fm, parts[2]]))

    def list_inbox(self, folder: str = "inbox") -> list[tuple[str, str, datetime]]:
        """List ``(message_id, subject, timestamp)`` for messages in a folder."""
        if "/" in folder or folder.startswith("."):
            raise ValueError(f"Invalid folder name: {folder!r}")
        target = self._dir / folder
        if not target.exists():
            return []
        out: list[tuple[str, str, datetime]] = []
        for f in sorted(target.glob("*.md")):
            meta = self._meta_of(f)
            if not meta:
                continue
            subject = str(meta.get("subject", ""))
            ts = self._parse_ts(meta.get("timestamp"))
            out.append((f.name, subject, ts))
        return out

    @staticmethod
    def _parse_ts(raw: object) -> datetime:
        if not raw:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def read(self, message_id: str, include_thread: bool = False) -> str:
        """Return an inbox message, marking it read. Walks the thread if asked.

        ``include_thread`` prepends locally-available ancestors (following
        ``in_reply_to`` across inbox and outbox), oldest first, so a reply reads
        with its context. Ancestors that live only on another agent's disk are
        simply absent — the transport never reaches over the network to read.
        """
        path = self._resolve_in_inbox(message_id)
        if path is None:
            raise FileNotFoundError(f"Message not found: {message_id}")
        content = self._mark_read(path)
        if not include_thread:
            return content
        chain = self._thread_chain(message_id)
        return "\n\n---\n\n".join(chain + [content]) if chain else content

    def _mark_read(self, path: Path) -> str:
        content = path.read_text()
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3 and "read: false" in parts[1]:
                parts[1] = re.sub(r"^read: false$", "read: true", parts[1], flags=re.MULTILINE)
                content = "---".join(parts)
                path.write_text(content)
        return content

    def _lookup_meta(self, message_id: str) -> dict | None:
        for folder in (self.inbox, self.outbox):
            path = self._resolve_within(folder, message_id)
            if path is not None:
                return self._meta_of(path)
        return None

    def _lookup_content(self, message_id: str) -> str | None:
        for folder in (self.inbox, self.outbox):
            path = self._resolve_within(folder, message_id)
            if path is not None:
                return path.read_text()
        return None

    def _thread_chain(self, message_id: str) -> list[str]:
        """Ancestor message bodies (oldest first), following in_reply_to links."""
        chain: list[str] = []
        seen: set[str] = set()
        current = message_id
        while True:
            meta = self._lookup_meta(current)
            parent = meta.get("in_reply_to") if meta else None
            if not parent or parent in seen:
                break
            seen.add(str(parent))
            body = self._lookup_content(str(parent))
            if body is None:
                break
            chain.append(body)
            current = str(parent)
        chain.reverse()
        return chain

    def conversation_id_for(self, message_id: str) -> str:
        """Conversation ID for the unified tracker: the agent pair, order-free.

        All messages between the same two agents share one conversation
        (``agent:alice|bob``), so the shared ``ConversationTracker`` threads a
        back-and-forth exchange together regardless of direction. Falls back to
        the message ID when participants can't be determined.
        """
        meta = self._lookup_meta(message_id)
        if not meta:
            return f"agent:{message_id}"
        sender = str(meta.get("from", "")).lower()
        recipient = str(meta.get("to", "")).lower()
        pair = sorted(p for p in (sender, recipient) if p)
        if len(pair) != 2:
            return f"agent:{message_id}"
        return f"agent:{pair[0]}|{pair[1]}"

    # -- delivery helpers (sync layer; not part of the Protocol) -------------

    @staticmethod
    def local_deliver(recipient_inbox: str | Path) -> Deliver:
        """A ``deliver`` hook that copies into a local recipient inbox.

        For single-host setups and tests that want delivery without SSH. The
        SSH/SCP delivery from ``agent-msg.py`` plugs in here as an alternative.
        """
        dest = Path(recipient_inbox)

        def _deliver(local_path: Path, _recipient: str) -> bool:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, dest / local_path.name)
            return True

        return _deliver
