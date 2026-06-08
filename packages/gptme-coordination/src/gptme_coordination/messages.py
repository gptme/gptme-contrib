"""Append-only message passing between agents.

Messages are stored in SQLite and never modified after creation.
Agents can send targeted messages (to a specific agent) or
broadcasts (recipient=None). Channels allow topic-based filtering.

Every message includes an HMAC-SHA256 over the canonical
(sender|recipient|channel|body) tuple to authenticate the sender's identity.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gptme_coordination.db import CoordinationDB


@dataclass
class Message:
    id: int
    sender: str
    recipient: str | None
    channel: str
    body: str
    created_at: datetime
    hmac: str | None = None
    verified: bool = True  # True means HMAC validates (or no hmac column in legacy DB)


class MessageBus:
    """Append-only message bus for inter-agent communication."""

    def __init__(self, db: CoordinationDB):
        self.db = db

    @staticmethod
    def compute_hmac(
        sender: str,
        recipient: str | None,
        channel: str,
        body: str,
        secret: bytes,
    ) -> str:
        """HMAC-SHA256 over canonical (sender|recipient|channel|body) JSON."""
        canonical = json.dumps(
            [sender, recipient, channel, body],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        mac = hmac.new(secret, canonical, hashlib.sha256).digest()
        return base64.b64encode(mac).decode("ascii")

    def send(
        self,
        sender: str,
        body: str,
        recipient: str | None = None,
        channel: str = "general",
        secret: bytes | None = None,
    ) -> Message:
        """Send a message. If recipient is None, it's a broadcast.

        If ``secret`` is provided, an HMAC is stored to authenticate the sender.
        Without a secret, ``hmac`` is NULL (legacy mode).
        """
        hmac_val = None
        if secret is not None:
            hmac_val = self.compute_hmac(sender, recipient or "", channel, body, secret)
        cursor = self.db.conn.execute(
            """INSERT INTO messages (sender, recipient, channel, body, hmac)
            VALUES (?, ?, ?, ?, ?)""",
            (sender, recipient, channel, body, hmac_val),
        )
        row = self.db.conn.execute(
            "SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row_to_message(row)

    def inbox(
        self,
        agent_id: str,
        since: datetime | None = None,
        channel: str | None = None,
        secrets: dict[str, bytes] | None = None,
    ) -> list[Message]:
        """Get messages for an agent (targeted + broadcasts), optionally filtered.

        If ``secrets`` is provided, messages with mismatched HMACs are marked
        as ``verified=False`` rather than being dropped (advisory verification).
        """
        query = """SELECT * FROM messages
            WHERE (recipient = ? OR recipient IS NULL)"""
        params: list[str | None] = [agent_id]

        if since is not None:
            query += " AND created_at > ?"
            params.append(since.strftime("%Y-%m-%d %H:%M:%S"))

        if channel is not None:
            query += " AND channel = ?"
            params.append(channel)

        query += " ORDER BY id ASC"

        rows = self.db.conn.execute(query, params).fetchall()
        messages = [_row_to_message(r) for r in rows]

        if secrets is not None:
            for msg in messages:
                if msg.hmac is not None:
                    secret = secrets.get(msg.sender)
                    if secret is not None:
                        expected = self.compute_hmac(
                            msg.sender,
                            msg.recipient or "",
                            msg.channel,
                            msg.body,
                            secret,
                        )
                        msg.verified = hmac.compare_digest(expected, msg.hmac)
                    else:
                        msg.verified = False  # no secret known for this sender

        return messages

    def history(
        self,
        channel: str = "general",
        limit: int = 50,
    ) -> list[Message]:
        """Get recent messages from a channel, in chronological order."""
        rows = self.db.conn.execute(
            """SELECT * FROM (
                SELECT * FROM messages WHERE channel = ?
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC""",
            (channel, limit),
        ).fetchall()
        return [_row_to_message(r) for r in rows]


def _row_to_message(row: Any) -> Message:
    """Convert a sqlite3.Row to a Message dataclass."""
    return Message(
        id=row["id"],
        sender=row["sender"],
        recipient=row["recipient"],
        channel=row["channel"],
        body=row["body"],
        created_at=datetime.fromisoformat(row["created_at"]),
        hmac=row["hmac"] if "hmac" in row.keys() else None,
        verified="hmac"
        not in row.keys(),  # True only for legacy DBs without HMAC column
    )
