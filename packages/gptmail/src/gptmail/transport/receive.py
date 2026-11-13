"""Receive-only inbox publication for forced-command agent mail.

This is the recipient half of a receive-only transport. A peer sends one message
on stdin and the receiver validates it and publishes it under
``messages/inbox/`` — nothing else. Because the whole transport is one command,
an operator can pin a sender's key with a forced command in ``authorized_keys``::

    command="gptmail agent receive",restrict ssh-ed25519 AAAA... alice

The sender then never needs a login shell on the recipient, and the recipient's
safety tier no longer depends on the least trusted sibling that can message it.

Security properties enforced here (the receiver is the trust boundary):

- **Bounded.** At most :data:`MAX_MESSAGE_BYTES` are read; the receiver never
  buffers an unbounded stream.
- **No client-chosen paths or commands.** The filename is derived from validated
  frontmatter; ``SSH_ORIGINAL_COMMAND`` is only *compared*, never executed.
  Only a receive invocation is accepted, so ``ssh recipient 'ls'`` fails loudly
  instead of feeding ``ls`` to the receiver as an empty message.
- **Validated frontmatter.** ``from``, ``to`` and ``subject`` must be present and
  the sender must be a plain token (no separators), so it cannot shape a path.
- **Per-key sender binding.** ``expected_sender`` (from a forced command such as
  ``command="gptmail agent receive --expect-sender alice"``, or
  ``GPTMAIL_SENDER`` when ``PermitUserEnvironment`` is enabled) must match the
  message's ``from``.
- **Addressed-to-self.** When the recipient knows its own name, a message
  addressed elsewhere is refused rather than silently filed.
- **Atomic, no-clobber publication.** The message is written to a dot-prefixed
  staging file in the inbox and hard-linked into place, so a reader never sees a
  partial file and a concurrent publication cannot overwrite an existing
  message. Arrival time is receiver-stamped (the sender's mtime is discarded).

Email-stack-free, like the rest of ``gptmail.transport.agent``: no
``imaplib``/``smtplib`` imports, so it runs in an isolated LXC.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from gptmail.transport.agent import meta_of, split_frontmatter

#: A single message, not a bulk import. Guards the receiver against a stream.
MAX_MESSAGE_BYTES = 4 * 1024 * 1024

#: Client commands accepted when ``SSH_ORIGINAL_COMMAND`` is set. Under a forced
#: command sshd runs the forced command anyway; this only decides whether the
#: sender's *request* is a receive invocation or an attempt to use us as a shell.
_RECEIVE_PREFIXES = (
    ("gptmail", "agent", "receive"),
    ("uv", "run", "gptmail", "agent", "receive"),
)

#: A sender name becomes part of a filename, so keep it a plain token.
_SENDER_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

#: Same rule as ``AgentTransport`` uses for mailbox names.
_MAILBOX_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: A shell-free word: enough for ``--flag=value``, no metacharacters.
_PLAIN_WORD = re.compile(r"^[-A-Za-z0-9_./@=:]+$")

#: A leading ``NAME=value`` env assignment (e.g. ``AGENT_NAME=bob``).
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=[-A-Za-z0-9_./@:]*$")


class ReceiveError(ValueError):
    """A message or invocation the receive-only endpoint refuses."""


def check_original_command(original: str | None) -> None:
    """Refuse a client-requested command that is not the receive verb.

    ``ssh recipient 'ls'`` under a forced command would otherwise run the
    forced command with ``ls`` in ``SSH_ORIGINAL_COMMAND`` and an empty stdin,
    which reads as a confusing "empty message". Refusing here makes the
    restriction observable instead.
    """
    if not original or not original.strip():
        return
    tokens = original.split()
    # A leading env assignment is how an operator sets AGENT_NAME for the
    # addressee check; it is never executed here, so accept a plain one.
    while tokens and _ENV_ASSIGN.fullmatch(tokens[0]):
        tokens.pop(0)
    for prefix in _RECEIVE_PREFIXES:
        if tuple(tokens[: len(prefix)]) == prefix:
            # Arguments are ignored (the forced command decides what runs), but
            # they must stay plain words: no shell metacharacters, so the
            # request cannot look like a compound command.
            if all(_PLAIN_WORD.fullmatch(token) for token in tokens[len(prefix) :]):
                return
    raise ReceiveError(
        "receive-only endpoint: refused command "
        f"{original!r}; only 'gptmail agent receive' is accepted"
    )


def _safe_part(value: object, *, limit: int, fallback: str) -> str:
    """Reduce ``value`` to characters that are safe in a filename component."""
    text = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(value))
    text = text[:limit].strip("-")
    return text or fallback


def message_filename(meta: dict, *, now: datetime | None = None) -> str:
    """A safe inbox filename derived from validated frontmatter.

    The timestamp is generated here rather than taken from the message, so a
    sender cannot choose (or collide on) a name, and arrival order stays
    monotonic in the filename.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S-%f")
    sender = _safe_part(meta.get("from"), limit=20, fallback="unknown")
    subject = _safe_part(meta.get("subject"), limit=40, fallback="message")
    return f"{stamp}-{sender}-{subject}.md"


def validate_message(
    raw: str,
    *,
    expected_sender: str | None = None,
    self_name: str | None = None,
) -> dict:
    """Validate one received message and return its frontmatter mapping.

    Raises :class:`ReceiveError` for anything the endpoint should not publish.
    """
    if "\x00" in raw:
        raise ReceiveError("refused: message contains a NUL byte")
    meta, body = split_frontmatter(raw)
    if meta is None:
        raise ReceiveError("refused: message has no valid YAML frontmatter")

    # Never stringify untrusted YAML collections: aliased lists can have an
    # exponentially larger repr than their bounded input document.
    for field in ("from", "to", "subject"):
        value = meta.get(field)
        if value is None:
            raise ReceiveError(f"refused: message has no '{field}' field")
        if not isinstance(value, str):
            raise ReceiveError(f"refused: '{field}' must be a string")
        if field in ("from", "to") and value != value.strip():
            # We publish the original bytes; accept only addresses downstream
            # readers and reply routing will interpret the same way.
            raise ReceiveError(f"refused: '{field}' has surrounding whitespace")
    sender = meta["from"].lower()
    recipient = meta["to"].lower()
    subject = meta["subject"].strip()
    if not sender:
        raise ReceiveError("refused: message has no 'from' field")
    if not _SENDER_TOKEN.fullmatch(sender):
        raise ReceiveError(f"refused: invalid sender name {sender!r}")
    if not recipient:
        raise ReceiveError("refused: message has no 'to' field")
    if not subject:
        raise ReceiveError("refused: message has no 'subject' field")
    if not body.strip():
        raise ReceiveError("refused: message has an empty body")

    if expected_sender and sender != expected_sender.strip().lower():
        raise ReceiveError(
            f"refused: this key is bound to sender {expected_sender!r}, "
            f"message claims {sender!r}"
        )
    if self_name and recipient != self_name.strip().lower():
        raise ReceiveError(f"refused: message is addressed to {recipient!r}, not {self_name!r}")
    return meta


def _link_no_clobber(staged: Path, inbox: Path, filename: str, *, limit: int = 100) -> Path:
    """Hard-link ``staged`` into ``inbox`` without ever replacing a message."""
    stem = filename[:-3] if filename.endswith(".md") else filename
    for attempt in range(1, limit + 1):
        name = filename if attempt == 1 else f"{stem}-{attempt}.md"
        destination = inbox / name
        try:
            os.link(staged, destination)
        except FileExistsError:
            continue  # A concurrent or earlier message owns this name.
        except OSError as exc:  # e.g. a directory squatting on the name
            raise ReceiveError(f"refused: cannot publish {name}: {exc}") from exc
        return destination
    raise ReceiveError(f"refused: too many name collisions for {filename!r} in {inbox}")


def publish_message(
    raw: str,
    inbox: Path,
    *,
    expected_sender: str | None = None,
    self_name: str | None = None,
) -> Path:
    """Validate ``raw`` and publish it into ``inbox`` atomically.

    Returns the published path. Readers glob ``*.md``, so the dot-prefixed
    staging file is invisible until the link makes it a complete message.
    """
    meta = validate_message(raw, expected_sender=expected_sender, self_name=self_name)
    # A legacy pull or previous push may already have delivered this logical
    # message. Keep its local read/replied stamps rather than publishing again.
    logical_id = meta.get("message_id")
    if isinstance(logical_id, str) and logical_id:
        for existing in sorted(inbox.glob("*.md")):
            prior = meta_of(existing) or {}
            if prior.get("message_id", existing.name) == logical_id and prior.get(
                "from"
            ) == meta.get("from"):
                return existing
    filename = message_filename(meta)
    inbox.mkdir(parents=True, exist_ok=True)
    fd, staged_name = tempfile.mkstemp(prefix=".receive-", dir=inbox)
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(raw if raw.endswith("\n") else raw + "\n")
        arrival = time.time()
        os.utime(staged, (arrival, arrival))  # Discard a sender-chosen mtime.
        return _link_no_clobber(staged, inbox, filename)
    finally:
        staged.unlink(missing_ok=True)


def mailbox_inbox(messages_dir: Path, mailbox: str) -> Path:
    """Resolve ``messages/`` plus a mailbox name to its inbox directory."""
    name = (mailbox or "default").strip().lower()
    if name == "default":
        return messages_dir / "inbox"
    if not _MAILBOX_NAME.fullmatch(name):
        raise ReceiveError(
            f"refused: invalid mailbox name {mailbox!r}; use lowercase letters, "
            "digits, '-' or '_'"
        )
    return messages_dir / "mailboxes" / name / "inbox"
