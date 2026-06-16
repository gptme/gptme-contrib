#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "click>=8.0.0",
#   "pyyaml>=6.0",
# ]
# ///
"""CLI subgroup ``gptmail agent`` — inter-agent SSH messaging over AgentTransport.

This is the unified-comms home for the inter-agent messaging that
``scripts/agent-msg.py`` implements today (fold task
``fold-agent-msg-into-gptmail-single-comms-tool``). It is **email-free by
construction**: it imports only :class:`~gptmail.transport.agent.AgentTransport`
and the shared :class:`~gptmail.communication_utils.state.tracking.ConversationTracker`,
never ``gptmail.lib``/``imaplib``/``smtplib``. So ``gptmail agent …`` runs in
isolated LXC sessions with no email infra (Bob's hard constraint). A guard test
(``test_agent_cli_no_email_imports.py``) locks that in.

Wiring:

- Transport file I/O (send/list/read/reply) goes through ``AgentTransport``.
- SSH/SCP delivery is the injected ``deliver`` hook (sync layer lives here, not
  in the transport — Q2 sync-agnostic).
- Each sent/replied message is also stamped into the shared ``ConversationTracker``
  with ``channel="agent"`` so the unified store can answer cross-channel
  "what do I owe a reply to" queries.
- ``pending`` is an authoritative filesystem scan of inbox/outbox (the proven
  ``agent-msg.py`` ``needs_reply_messages`` logic), so it works cross-agent
  without depending on tracker state being populated on every host.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml

from gptmail.communication_utils.state.tracking import (
    ConversationTracker,
    MessageState,
)
from gptmail.transport.agent import AgentTransport, Deliver, _FM_DELIM, meta_of


# Inbox messages older than this (days) are assumed handled and no longer
# flagged as awaiting a reply — a timely-reply SLA, not an unbounded backlog.
# Override with AGENT_MSG_REPLY_WINDOW_DAYS (0 = no age limit). Mirrors
# scripts/agent-msg.py so behaviour is identical during the migration.
def _reply_window_days() -> int:
    try:
        return int(os.environ.get("AGENT_MSG_REPLY_WINDOW_DAYS", "7"))
    except ValueError:
        return 7


@functools.lru_cache(maxsize=1)
def _repo_root() -> Path:
    """Workspace root via ``git rev-parse --show-toplevel`` (symlink-correct).

    Cached: a CLI invocation calls this several times (``_messages_dir`` is hit
    by both the command body and ``_transport``) but cwd is fixed for the
    process, so one ``git rev-parse`` per run suffices.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        if isinstance(e, FileNotFoundError):
            raise RuntimeError("git not found in PATH; gptmail agent requires git.")
        raise RuntimeError("Not in a git repository; run gptmail agent from a workspace.")
    return Path(out.stdout.strip())


def _messages_dir() -> Path:
    """Messages directory for the current agent (``<repo>/messages``)."""
    return _repo_root() / "messages"


def _normalize_mailbox(mailbox: str) -> str:
    try:
        return AgentTransport._normalize_mailbox(mailbox)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--mailbox")


def _mailbox_root(mailbox: str = "default") -> Path:
    return AgentTransport.mailbox_dir(_messages_dir(), _normalize_mailbox(mailbox))


def _known_mailboxes() -> list[str]:
    mailboxes = ["default"]
    root = _messages_dir() / "mailboxes"
    if not root.exists():
        return mailboxes
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            name = _normalize_mailbox(child.name)
        except click.BadParameter:
            continue
        if name != "default":
            mailboxes.append(name)
    return mailboxes


def _selected_mailboxes(mailbox: str, all_mailboxes: bool) -> list[str]:
    if all_mailboxes:
        if mailbox not in (None, "default"):
            raise click.BadParameter(
                "--mailbox and --all-mailboxes are mutually exclusive",
                param_hint="--mailbox",
            )
        return _known_mailboxes()
    return [_normalize_mailbox(mailbox)]


def _format_mailbox_prefix(mailbox: str, *, show: bool) -> str:
    return f"[{mailbox}] " if show else ""


def _within(base: Path, *parts: str) -> Path | None:
    """Resolve ``base/parts`` and return it only if it stays inside ``base``.

    Resolve-based containment: defeats both ``..`` traversal and symlinks whose
    names look benign but point outside ``base``. Returns ``None`` when the
    resolved path escapes ``base`` (callers decide whether that's a silent skip
    or a hard error).
    """
    base_resolved = base.resolve()
    candidate = base.joinpath(*parts).resolve()
    if str(candidate).startswith(str(base_resolved) + os.sep):
        return candidate
    return None


def _self_name() -> str:
    """Current agent name from the environment."""
    return os.environ.get("AGENT_NAME", os.environ.get("USER", "unknown")).lower()


def _load_agents(*, warn_missing: bool = True) -> dict[str, dict[str, str]]:
    """Load the agent registry from ``messages/agents.yaml`` ({} if absent)."""
    config_path = _messages_dir() / "agents.yaml"
    if not config_path.exists():
        if warn_missing:
            click.echo(
                f"Warning: no agent registry at {config_path}\n"
                "Create messages/agents.yaml with agent SSH targets.",
                err=True,
            )
        return {}
    raw = yaml.safe_load(config_path.read_text()) or {}
    # Normalise keys to lowercase so lookups (which use ``to.lower()``) match
    # registries that use mixed-case agent names like ``Bob``.
    return {k.lower(): v for k, v in raw.items()}


def _tracker() -> ConversationTracker:
    """Shared conversation tracker (state under ``messages/.tracking``)."""
    return ConversationTracker(_messages_dir() / ".tracking")


def _ssh_deliver(agents: dict[str, dict[str, str]], *, mailbox: str = "default") -> Deliver:
    """Build a ``deliver`` hook that SCPs a message into a recipient's inbox.

    Returns False (so the transport stamps ``delivered: false``) on unknown
    recipient or any SSH/SCP failure — the same contract agent-msg.py relies on.
    """

    def _deliver(local_path: Path, recipient: str) -> bool:
        agent = agents.get(recipient)
        if not agent:
            click.echo(
                f"Error: unknown agent '{recipient}'. Known: {', '.join(agents)}",
                err=True,
            )
            return False
        # Pull-only recipients (e.g. humans who poll the outbox) have no SSH
        # target — write the message to the outbox and report success so the
        # message is NOT stamped ``delivered: false`` (which would make every
        # poll re-compose a fresh, duplicate reply).
        if agent.get("delivery") == "pull-only":
            return True
        missing = [k for k in ("ssh", "workspace") if not agent.get(k)]
        if missing:
            click.echo(
                f"Error: agent '{recipient}' config missing required key(s): {', '.join(missing)}",
                err=True,
            )
            return False
        ssh_target = agent["ssh"]
        remote_root = f"{agent['workspace']}/messages"
        if mailbox == "default":
            remote_inbox = f"{remote_root}/inbox/"
        else:
            remote_inbox = f"{remote_root}/mailboxes/{mailbox}/inbox/"
        ssh_opts = ["-o", "ConnectTimeout=5", "-o", "BatchMode=yes"]
        try:
            subprocess.run(
                ["ssh", *ssh_opts, ssh_target, f"mkdir -p {shlex.quote(remote_inbox)}"],
                check=True,
                capture_output=True,
                timeout=10,
            )
            subprocess.run(
                [
                    "scp",
                    *ssh_opts,
                    str(local_path),
                    f"{ssh_target}:{remote_inbox}{local_path.name}",
                ],
                check=True,
                capture_output=True,
                timeout=15,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            click.echo(f"Error delivering to {recipient}: {e}", err=True)
            return False
        return True

    return _deliver


def _transport(*, deliver: Deliver | None = None, mailbox: str = "default") -> AgentTransport:
    return AgentTransport(
        _messages_dir(),
        _self_name(),
        mailbox=_normalize_mailbox(mailbox),
        deliver=deliver,
    )


def _delivery_failed(message_id: str, *, mailbox: str = "default") -> bool:
    """True if the just-sent outbox message was stamped ``delivered: false``.

    The transport stamps that field only when the deliver hook reports failure;
    a successful send leaves it absent. Lets the CLI report real failures (and
    exit non-zero) instead of always printing "Sent".
    """
    path = _mailbox_root(mailbox) / "outbox" / message_id
    try:
        content = path.read_text()
    except OSError:
        return False
    if not content.startswith("---"):
        return False
    parts = _FM_DELIM.split(content, maxsplit=2)
    return len(parts) >= 3 and any(
        line.strip() == "delivered: false" for line in parts[1].splitlines()
    )


def _track_sent(transport: AgentTransport, message_id: str, reply_to: str | None) -> None:
    """Stamp a sent message into the shared tracker (channel='agent')."""
    tracker = _tracker()
    conv = transport.conversation_id_for(message_id)
    tracker.track_message(conv, message_id, in_reply_to=reply_to, channel="agent")
    if reply_to:
        # The inbound message we just answered is no longer pending.
        tracker.set_message_state(
            conv, reply_to, MessageState.COMPLETED, metadata={"reply_id": message_id}
        )


# -- frontmatter scan helpers (authoritative pending check) ------------------
# ``meta_of`` is imported from transport.agent so the CLI and the transport
# share one frontmatter parser and can never silently diverge.


def _age_days(meta: dict, now: datetime) -> float | None:
    ts = meta.get("timestamp")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 86400.0


def _addressed_to(meta: dict, self_name: str) -> bool:
    to = meta.get("to")
    if to is None:
        return True
    if isinstance(to, (list, tuple, set)):  # noqa: UP038
        return self_name in {str(t).lower() for t in to}
    return str(to).lower() == self_name


def _matches_recipient(meta: dict, recipient: str) -> bool:
    """True only when ``meta['to']`` explicitly targets ``recipient``.

    Inbox scans treat a missing ``to`` as addressed-to-self for legacy
    frontmatter-free messages. Outbox recipient filtering is stricter: a missing
    recipient is malformed and must not leak into every ``pending --for`` view.
    """
    to = meta.get("to")
    if to is None:
        return False
    if isinstance(to, (list, tuple, set)):  # noqa: UP038
        return recipient in {str(t).lower() for t in to}
    return str(to).lower() == recipient


def _is_push_reachable(recipient: str, agents: dict[str, dict[str, str]]) -> bool:
    """True if ``recipient`` has a working push transport in the registry.

    Mirrors the ``_ssh_deliver`` contract: a recipient is push-reachable only if
    it is registered with non-empty ``ssh`` and ``workspace`` keys. Pull-based
    recipients (not in the registry, or missing those keys — e.g. Erik, who polls
    the outbox) can never be delivered to, so their replies stay ``delivered:
    false`` permanently rather than as a transient, retryable failure.
    """
    agent = agents.get(recipient.lower())
    if not agent:
        return False
    return all(agent.get(k) for k in ("ssh", "workspace"))


def _pending_messages(
    messages_dir: Path,
    self_name: str,
    window: int,
    agents: dict[str, dict[str, str]] | None = None,
) -> list[dict]:
    """Inbox messages addressed to us that we haven't replied to (timely SLA).

    A reply requirement is satisfied by an inbox ``replied: true`` stamp or by an
    outbox message whose ``in_reply_to`` points at it. Reply-once: a draft reply
    counts even when stamped ``delivered: false`` **if the recipient is
    pull-based** (no push transport), where that stamp is the permanent steady
    state — otherwise every poll re-composes a fresh, non-identical reply (the
    duplicate-reply bug). For push-reachable recipients, ``delivered: false``
    still means a transient failure, so the message stays pending to retry.
    """
    inbox = messages_dir / "inbox"
    outbox = messages_dir / "outbox"
    now = datetime.now(timezone.utc)
    if agents is None:
        agents = _load_agents(warn_missing=False)

    replied_to: set[str] = set()
    my_outbox_ids: set[str] = set()
    if outbox.exists():
        for f in outbox.glob("*.md"):
            m = meta_of(f)
            if m and m.get("in_reply_to"):
                recipient = str(m.get("to") or "")
                delivered_ok = m.get("delivered") is not False
                if delivered_ok or not _is_push_reachable(recipient, agents):
                    replied_to.add(str(m["in_reply_to"]))
            my_outbox_ids.add(f.name)

    pending: list[dict] = []
    if not inbox.exists():
        return pending
    for f in sorted(inbox.glob("*.md")):
        m = meta_of(f)
        if not m:
            continue
        if m.get("from") in (None, self_name):
            continue
        if not _addressed_to(m, self_name):
            continue
        if m.get("replied"):
            continue
        if f.name in replied_to:
            continue
        if m.get("in_reply_to") in my_outbox_ids:
            continue
        if window > 0:
            age = _age_days(m, now)
            if age is not None and age > window:
                continue
        m["file"] = f.name
        pending.append(m)
    return pending


def _mark_replied(path: Path) -> None:
    """Stamp an inbox message ``replied: true`` (and ``read: true``). Idempotent."""
    if not path.exists():
        return
    content = path.read_text()
    if not content.startswith("---"):
        return
    parts = _FM_DELIM.split(content, maxsplit=2)
    if len(parts) < 3:
        return
    fm = re.sub(r"^read: false$", "read: true", parts[1], flags=re.MULTILINE)
    if "replied:" not in fm:
        fm = fm.rstrip("\n") + "\nreplied: true\n"
    else:
        fm = re.sub(r"^replied: false$", "replied: true", fm, flags=re.MULTILINE)
    path.write_text("---".join([parts[0], fm, parts[2]]))


def _resolve_message(
    message_id: str,
    *,
    folder: str = "inbox",
    mailbox: str | None = None,
) -> tuple[str, Path] | None:
    mailboxes = [_normalize_mailbox(mailbox)] if mailbox is not None else _known_mailboxes()
    for mailbox_name in mailboxes:
        path = _within(_mailbox_root(mailbox_name) / folder, message_id)
        if path is not None and path.exists():
            return mailbox_name, path
    return None


def _pending_for_mailboxes(
    mailboxes: list[str],
    *,
    self_name: str,
    window: int,
    agents: dict[str, dict[str, str]],
) -> list[dict]:
    pending: list[dict] = []
    for mailbox in mailboxes:
        messages = _pending_messages(_mailbox_root(mailbox), self_name, window, agents=agents)
        for meta in messages:
            row = dict(meta)
            row["mailbox"] = str(row.get("mailbox") or mailbox)
            pending.append(row)
    pending.sort(
        key=lambda meta: str(meta.get("timestamp", "")),
    )
    return pending


def _outbox_rows_for_recipient(
    mailboxes: list[str],
    *,
    recipient: str,
    self_name: str,
) -> list[dict]:
    target = recipient.lower()
    rows: list[dict] = []
    workspace = str(_repo_root())
    for mailbox in mailboxes:
        outbox = _mailbox_root(mailbox) / "outbox"
        if not outbox.exists():
            continue
        for message_path in sorted(outbox.glob("*.md")):
            meta = meta_of(message_path)
            if not meta or not _matches_recipient(meta, target):
                continue
            row = dict(meta)
            row["agent"] = str(row.get("from") or self_name)
            row["file"] = message_path.name
            row["mailbox"] = str(row.get("mailbox") or mailbox)
            row["workspace"] = workspace
            rows.append(row)
    rows.sort(
        key=lambda meta: (
            str(meta.get("timestamp", "")),
            str(meta.get("agent", "")),
            str(meta.get("mailbox", "")),
            str(meta.get("file", "")),
        )
    )
    return rows


def _remote_pending_rows(
    agent_name: str,
    agent: dict[str, str],
    *,
    recipient: str,
    mailboxes: list[str],
) -> list[dict]:
    missing = [k for k in ("ssh", "workspace") if not agent.get(k)]
    if missing:
        click.echo(
            f"Warning: skipping {agent_name}; missing required key(s): {', '.join(missing)}",
            err=True,
        )
        return []
    cmd = ["uv", "run", "gptmail", "agent", "pending", "--for", recipient, "--json", "--local-only"]
    if len(mailboxes) == 1:
        cmd.extend(["--mailbox", mailboxes[0]])
    else:
        cmd.append("--all-mailboxes")
    remote_cmd = " && ".join(
        [
            f"cd {shlex.quote(agent['workspace'])}",
            f"AGENT_NAME={shlex.quote(agent_name)} {shlex.join(cmd)}",
        ]
    )
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", agent["ssh"], remote_cmd],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        click.echo(f"Warning: failed to collect pending rows from {agent_name}: {e}", err=True)
        return []
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as e:
        click.echo(f"Warning: invalid pending JSON from {agent_name}: {e}", err=True)
        return []
    if not isinstance(payload, list):
        click.echo(f"Warning: invalid pending payload from {agent_name}: expected a list", err=True)
        return []
    rows: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["agent"] = str(row.get("agent") or agent_name)
        row["workspace"] = str(row.get("workspace") or agent["workspace"])
        rows.append(row)
    return rows


def _fleet_pending_rows(
    mailboxes: list[str],
    *,
    recipient: str,
    self_name: str,
    agents: dict[str, dict[str, str]],
    fleet: bool,
) -> list[dict]:
    rows = _outbox_rows_for_recipient(mailboxes, recipient=recipient, self_name=self_name)
    if not fleet:
        return rows
    for agent_name, agent in agents.items():
        if agent_name == self_name:
            continue
        rows.extend(
            _remote_pending_rows(
                agent_name,
                agent,
                recipient=recipient,
                mailboxes=mailboxes,
            )
        )
    rows.sort(
        key=lambda meta: (
            str(meta.get("timestamp", "")),
            str(meta.get("agent", "")),
            str(meta.get("mailbox", "")),
            str(meta.get("file", "")),
        )
    )
    return rows


# -- CLI ---------------------------------------------------------------------


@click.group(name="agent")
def agent() -> None:
    """Inter-agent SSH messaging (filesystem inbox/outbox over AgentTransport)."""


@agent.command()
@click.argument("to")
@click.argument("subject")
@click.argument("content", required=False)
@click.option("--mailbox", default="default", show_default=True, help="Mailbox to send from.")
def send(to: str, subject: str, content: str | None, mailbox: str) -> None:
    """Send a message to another agent."""
    body = content if content is not None else sys.stdin.read()
    agents = _load_agents()
    if to.lower() == _self_name():
        click.echo("Error: cannot send a message to yourself.", err=True)
        sys.exit(1)
    if to.lower() not in agents:
        click.echo(f"Error: unknown agent '{to}'. Known: {', '.join(agents)}", err=True)
        sys.exit(1)
    mailbox = _normalize_mailbox(mailbox)
    transport = _transport(deliver=_ssh_deliver(agents, mailbox=mailbox), mailbox=mailbox)
    message_id = transport.send(to, subject, body)
    _track_sent(transport, message_id, reply_to=None)
    if _delivery_failed(message_id, mailbox=mailbox):
        click.echo(
            f"Delivery to {to.lower()} FAILED — saved to outbox (delivered: false). "
            "It was NOT received.",
            err=True,
        )
        sys.exit(1)
    click.echo(f"Sent to {to.lower()}: {subject}")


@agent.command()
@click.argument("subject")
@click.argument("content", required=False)
@click.option("--mailbox", default="default", show_default=True, help="Mailbox to send from.")
def broadcast(subject: str, content: str | None, mailbox: str) -> None:
    """Send a message to every agent in the registry except self."""
    body = content if content is not None else sys.stdin.read()
    agents = _load_agents()
    mailbox = _normalize_mailbox(mailbox)
    transport = _transport(deliver=_ssh_deliver(agents, mailbox=mailbox), mailbox=mailbox)
    recipients = [name for name in agents if name != _self_name()]
    if not recipients:
        click.echo("No other agents in registry.", err=True)
        return
    failures = []
    for name in recipients:
        message_id = transport.send(name, subject, body)
        _track_sent(transport, message_id, reply_to=None)
        if _delivery_failed(message_id, mailbox=mailbox):
            click.echo(f"Delivery to {name} FAILED (delivered: false).", err=True)
            failures.append(name)
        else:
            click.echo(f"Sent to {name}: {subject}")
    if failures:
        click.echo(f"Broadcast incomplete: {len(failures)} delivery failure(s).", err=True)
        sys.exit(1)


@agent.command(name="list")
@click.argument("folder", default="inbox")
@click.option("--all", "-a", "show_all", is_flag=True, help="Include already-read messages.")
@click.option("--mailbox", default="default", show_default=True, help="Mailbox to inspect.")
@click.option("--all-mailboxes", is_flag=True, help="Scan default plus every named mailbox.")
def list_cmd(folder: str, show_all: bool, mailbox: str, all_mailboxes: bool) -> None:
    """List messages in a folder (default: inbox, unread only).

    Mirrors ``agent-msg.py list``: the inbox shows only unread messages by
    default (each line prefixed ``*``); pass ``--all`` to include read ones.
    Non-inbox folders (outbox/sent) always list everything. The line format
    ``  {marker} [{ts}] {sender}: {subject}  ({file})`` matches agent-msg.py so
    the planned thin shim is a faithful drop-in.
    """
    mailboxes = _selected_mailboxes(mailbox, all_mailboxes)
    unread_only = folder == "inbox" and not show_all
    shown = 0
    show_prefix = all_mailboxes or any(name != "default" for name in mailboxes)
    for mailbox_name in mailboxes:
        folder_dir = _within(_mailbox_root(mailbox_name), folder)
        if folder_dir is None:
            raise click.ClickException(f"Invalid folder name: {folder!r}")
        transport = _transport(mailbox=mailbox_name)
        rows = transport.list_inbox(folder)
        for message_id, _subject, _ts in rows:
            meta = meta_of(folder_dir / message_id) or {}
            is_read = bool(meta.get("read"))
            if unread_only and is_read:
                continue
            ts = meta.get("timestamp", "unknown")
            sender = str(meta.get("from", "unknown"))
            subject = str(meta.get("subject", "(no subject)"))
            marker = " " if is_read else "*"
            prefix = _format_mailbox_prefix(mailbox_name, show=show_prefix)
            click.echo(f"  {marker} {prefix}[{ts}] {sender}: {subject}  ({message_id})")
            shown += 1
    if shown == 0:
        scope = "across mailboxes" if all_mailboxes else f"in {folder}"
        click.echo("No unread messages." if unread_only else f"No messages {scope}.")


@agent.command()
@click.argument("message_id")
@click.option("--thread", is_flag=True, help="Include locally-available ancestors.")
@click.option("--mailbox", default=None, help="Restrict lookup to a specific mailbox.")
def read(message_id: str, thread: bool, mailbox: str | None) -> None:
    """Read a message (marks it read), optionally with its thread."""
    resolved = _resolve_message(message_id, folder="inbox", mailbox=mailbox)
    if resolved is None:
        click.echo(f"Error: message not found: {message_id}", err=True)
        sys.exit(1)
    mailbox_name, _path = resolved
    transport = _transport(mailbox=mailbox_name)
    try:
        click.echo(transport.read(message_id, include_thread=thread))
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@agent.command()
@click.argument("message_id")
@click.argument("content", required=False)
@click.option("--mailbox", default=None, help="Restrict lookup to a specific mailbox.")
def reply(message_id: str, content: str | None, mailbox: str | None) -> None:
    """Reply to an inbox message, threading via in_reply_to."""
    resolved = _resolve_message(message_id, folder="inbox", mailbox=mailbox)
    if resolved is None:
        click.echo(f"Error: message not found: {message_id}", err=True)
        sys.exit(1)
    mailbox_name, msg_path = resolved
    original = meta_of(msg_path)
    if original is None:
        click.echo(f"Error: message not found: {message_id}", err=True)
        sys.exit(1)
    # Idempotency guard: skip if a prior session already stamped replied:true.
    # Protects against cascade reprocessing sending duplicate replies.
    if original.get("replied"):
        click.echo(
            f"Skipping {message_id}: already replied (idempotency guard).",
            err=True,
        )
        return
    recipient = str(original.get("from", "")).lower()
    if not recipient:
        click.echo(f"Error: cannot determine sender of {message_id}", err=True)
        sys.exit(1)
    subject = str(original.get("subject", ""))
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    body = content if content is not None else sys.stdin.read()
    agents = _load_agents()
    transport = _transport(
        deliver=_ssh_deliver(agents, mailbox=mailbox_name),
        mailbox=mailbox_name,
    )
    reply_id = transport.send(recipient, subject, body, reply_to=message_id)
    if _delivery_failed(reply_id, mailbox=mailbox_name):
        click.echo(
            f"Delivery to {recipient} FAILED — saved to outbox (delivered: false). "
            "It was NOT received.",
            err=True,
        )
        sys.exit(1)
    _track_sent(transport, reply_id, reply_to=message_id)
    _mark_replied(msg_path)
    click.echo(f"Replied to {recipient}: {subject}")


@agent.command()
@click.option("--mailbox", default="default", show_default=True, help="Mailbox to inspect.")
@click.option("--all-mailboxes", is_flag=True, help="Scan default plus every named mailbox.")
@click.option("--for", "for_recipient", default=None, help="Show messages pending for a recipient.")
@click.option("--fleet", is_flag=True, help="Fan out across all registered agents.")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--local-only", is_flag=True, hidden=True, help="Skip fleet fan-out.")
def pending(
    mailbox: str,
    all_mailboxes: bool,
    for_recipient: str | None,
    fleet: bool,
    json_output: bool,
    local_only: bool,
) -> None:
    """Show inbox messages awaiting a reply (timely-reply SLA)."""
    agents = _load_agents(warn_missing=False)
    mailboxes = _selected_mailboxes(mailbox, all_mailboxes)
    self_name = _self_name()
    if for_recipient:
        if fleet and local_only:
            raise click.BadParameter("--fleet and --local-only are mutually exclusive")
        rows = _fleet_pending_rows(
            mailboxes,
            recipient=for_recipient,
            self_name=self_name,
            agents=agents,
            fleet=fleet and not local_only,
        )
        if json_output:
            click.echo(json.dumps(rows, indent=2, sort_keys=True, default=str))
            return
        if not rows:
            click.echo(f"No messages pending for {for_recipient.lower()}.")
            return
        click.echo(f"{len(rows)} message(s) pending for {for_recipient.lower()}:")
        show_prefix = all_mailboxes or any(
            str(row.get("mailbox", "default")) != "default" for row in rows
        )
        for row in rows:
            prefix = _format_mailbox_prefix(str(row.get("mailbox", "default")), show=show_prefix)
            timestamp = str(row.get("timestamp", "unknown"))
            subject = str(row.get("subject", ""))
            agent_name = str(row.get("agent", "unknown"))
            file_name = str(row.get("file", ""))
            workspace = str(row.get("workspace", ""))
            click.echo(
                f"  {agent_name} {prefix}[{timestamp}] {subject}  ({file_name} @ {workspace})"
            )
        return
    if fleet:
        raise click.BadParameter("--fleet requires --for", param_hint="--fleet")
    if json_output:
        raise click.BadParameter("--json requires --for", param_hint="--json")
    if local_only:
        raise click.BadParameter("--local-only requires --for", param_hint="--local-only")
    msgs = _pending_for_mailboxes(
        mailboxes,
        self_name=self_name,
        window=_reply_window_days(),
        agents=agents,
    )
    if not msgs:
        click.echo("No messages awaiting reply.")
        return
    click.echo(f"{len(msgs)} message(s) awaiting reply:")
    show_prefix = all_mailboxes or any(name != "default" for name in mailboxes)
    for m in msgs:
        prefix = _format_mailbox_prefix(str(m.get("mailbox", "default")), show=show_prefix)
        click.echo(f"  {prefix}{m['file']}  from {m.get('from')}: {m.get('subject', '')}")


@agent.command()
@click.option("--mailbox", default="default", show_default=True, help="Mailbox to inspect.")
@click.option("--all-mailboxes", is_flag=True, help="Scan default plus every named mailbox.")
def status(mailbox: str, all_mailboxes: bool) -> None:
    """Show messaging status (self, registry, inbox/outbox/pending counts)."""
    agents = _load_agents()
    mailboxes = _selected_mailboxes(mailbox, all_mailboxes)
    inbox_count = 0
    outbox_count = 0
    for mailbox_name in mailboxes:
        transport = _transport(mailbox=mailbox_name)
        inbox_count += len(transport.list_inbox("inbox"))
        outbox_count += len(transport.list_inbox("outbox"))
    pend = _pending_for_mailboxes(
        mailboxes,
        self_name=_self_name(),
        window=_reply_window_days(),
        agents=agents,
    )
    click.echo(f"Agent:    {_self_name()}")
    click.echo(f"Registry: {', '.join(agents) or '(none)'}")
    click.echo(f"Mailbox:  {mailboxes[0] if len(mailboxes) == 1 else ', '.join(mailboxes)}")
    click.echo(f"Inbox:    {inbox_count}")
    click.echo(f"Outbox:   {outbox_count}")
    click.echo(f"Pending:  {len(pend)}")


if __name__ == "__main__":
    agent()
