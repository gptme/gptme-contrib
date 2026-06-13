#!/usr/bin/env python3
"""Inter-agent messaging over SSH.

Simple file-based messaging between agents running on different VMs.
Messages are YAML files transferred via SCP.

Usage:
    # Send a message to Alice
    python3 scripts/agent-msg.py send alice "Subject" "Message body"

    # List unread messages
    python3 scripts/agent-msg.py list

    # List only messages awaiting a reply from you
    python3 scripts/agent-msg.py list --needs-reply

    # Reply to an inbox message (sends to sender, marks it replied)
    python3 scripts/agent-msg.py reply <inbox-filename> "Reply body"

    # Send to all agents
    python3 scripts/agent-msg.py broadcast "Subject" "Message body"

    # Check connectivity (and how many messages await a reply)
    python3 scripts/agent-msg.py status

Configuration:
    Agent registry is loaded from messages/agents.yaml in the workspace root.
    Example agents.yaml:

        bob:
          ssh: bob@bob.example.com
          workspace: /home/bob/bob
        alice:
          ssh: alice@alice.example.com
          workspace: /home/alice/alice

    The current agent is detected from AGENT_NAME env var, then USER env var.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml

    HAS_YAML = True
except ImportError:
    HAS_YAML = False


def get_repo_root() -> Path:
    """Get the repository root directory.

    Uses git to find the workspace root, so symlinks are handled correctly.
    When this script is symlinked into an agent workspace, git will return
    the agent workspace root (not the gptme-contrib repo).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        if isinstance(e, FileNotFoundError):
            raise RuntimeError("git not found in PATH. agent-msg.py requires git.")
        raise RuntimeError(
            "Not in a git repository. agent-msg.py must be run from a git workspace."
        )


def load_agents() -> dict[str, dict[str, str]]:
    """Load agent registry from messages/agents.yaml.

    Returns empty dict if config file doesn't exist.
    """
    config_path = get_repo_root() / "messages" / "agents.yaml"
    if not config_path.exists():
        print(
            f"Warning: No agent registry at {config_path}\n"
            "Create messages/agents.yaml with agent SSH targets.\n"
            "See: gptme-contrib/scripts/agent-msg.py --help",
            file=sys.stderr,
        )
        return {}

    if not HAS_YAML:
        print(
            "Error: PyYAML required. Install with: pip install pyyaml", file=sys.stderr
        )
        return {}

    return yaml.safe_load(config_path.read_text()) or {}


def get_self() -> str:
    """Detect current agent name from environment."""
    return os.environ.get("AGENT_NAME", os.environ.get("USER", "unknown"))


def get_messages_dir() -> Path:
    """Get the messages directory for the current agent."""
    return get_repo_root() / "messages"


def ensure_dirs() -> None:
    """Create message directories if they don't exist."""
    msg_dir = get_messages_dir()
    (msg_dir / "inbox").mkdir(parents=True, exist_ok=True)
    (msg_dir / "outbox").mkdir(parents=True, exist_ok=True)


def make_message_filename(sender: str, subject: str) -> str:
    """Generate a unique message filename."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    safe_sender = "".join(c if c.isalnum() or c in "-_" else "-" for c in sender)
    safe_sender = safe_sender[:20].strip("-")
    safe_subject = "".join(c if c.isalnum() or c in "-_" else "-" for c in subject)
    safe_subject = safe_subject[:40].strip("-")
    return f"{ts}-{safe_sender}-{safe_subject}.md"


def format_message(
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
) -> str:
    """Format a message as YAML frontmatter + markdown body.

    in_reply_to, when set, is the inbox filename of the message being answered.
    It lets the sender's outbox record which incoming message a reply closes out.
    """
    if not HAS_YAML:
        raise RuntimeError(
            "PyYAML is required to send messages. Install with: pip install pyyaml"
        )
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
    # Use yaml.dump to safely escape special characters in all fields
    frontmatter = yaml.dump(
        meta,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    ).rstrip()
    return f"---\n{frontmatter}\n---\n\n{body}\n"


def send_message(
    agents: dict[str, dict[str, str]],
    self_name: str,
    recipient: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
) -> bool:
    """Send a message to another agent."""
    if recipient not in agents:
        print(f"Error: Unknown agent '{recipient}'. Known: {', '.join(agents.keys())}")
        return False

    if recipient == self_name:
        print("Error: Cannot send message to self.")
        return False

    ensure_dirs()

    filename = make_message_filename(self_name, subject)
    msg_content = format_message(self_name, recipient, subject, body, in_reply_to)

    # Save to local outbox
    outbox = get_messages_dir() / "outbox"
    local_path = outbox / filename
    local_path.write_text(msg_content)

    # Deliver to recipient's inbox via SSH/SCP
    agent = agents[recipient]
    ssh_target = agent["ssh"]
    workspace = agent["workspace"]
    remote_inbox = f"{workspace}/messages/inbox/"

    # Ensure remote inbox exists
    try:
        subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "BatchMode=yes",
                ssh_target,
                f"mkdir -p {shlex.quote(remote_inbox)}",
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"Error creating remote inbox: {e}")
        return False

    # SCP the message
    try:
        subprocess.run(
            [
                "scp",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "BatchMode=yes",
                str(local_path),
                f"{ssh_target}:{shlex.quote(str(remote_inbox) + filename)}",
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
        print(f"Sent to {recipient}: {subject}")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"Error sending to {recipient}: {e}")
        return False


def list_inbox(show_all: bool = False) -> list[dict]:
    """List messages in inbox."""
    if not HAS_YAML:
        print(
            "Error: PyYAML required to list messages. Install with: pip install pyyaml",
            file=sys.stderr,
        )
        return []

    ensure_dirs()
    inbox = get_messages_dir() / "inbox"
    messages = []

    for f in sorted(inbox.glob("*.md")):
        content = f.read_text()
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    meta = yaml.safe_load(parts[1])
                    body = parts[2].strip()
                    meta["body"] = body
                    meta["file"] = f.name
                    if show_all or not meta.get("read", False):
                        messages.append(meta)
                except Exception as e:
                    print(f"Warning: failed to parse {f.name}: {e}", file=sys.stderr)

    return messages


def read_message(filename: str) -> str | None:
    """Read a specific message and mark as read."""
    ensure_dirs()
    inbox = get_messages_dir() / "inbox"
    filepath = (inbox / filename).resolve()

    # Prevent path traversal attacks
    if not str(filepath).startswith(str(inbox.resolve()) + "/"):
        print(f"Error: Invalid filename: {filename}")
        return None

    if not filepath.exists():
        print(f"Error: Message not found: {filename}")
        return None

    content = filepath.read_text()

    # Mark as read — only update frontmatter, never touch body
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3 and "read: false" in parts[1]:
            parts[1] = parts[1].replace("read: false", "read: true")
            content = "---".join(parts)
            filepath.write_text(content)

    return content


def _meta_of(f: Path) -> dict | None:
    """Parse a message file's frontmatter into a dict (None if unparseable)."""
    if not HAS_YAML:
        return None
    try:
        content = f.read_text()
    except OSError:
        return None
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        meta = yaml.safe_load(parts[1])
    except Exception:
        return None
    return meta if isinstance(meta, dict) else None


def mark_replied(filename: str) -> bool:
    """Stamp an inbox message as replied (and read). Idempotent."""
    inbox = get_messages_dir() / "inbox"
    filepath = (inbox / filename).resolve()
    if (
        not str(filepath).startswith(str(inbox.resolve()) + "/")
        or not filepath.exists()
    ):
        return False
    content = filepath.read_text()
    if not content.startswith("---"):
        return False
    parts = content.split("---", 2)
    if len(parts) < 3:
        return False
    fm = parts[1]
    fm = fm.replace("read: false", "read: true")
    if "replied:" not in fm:
        # Append before the trailing newline of the frontmatter block.
        fm = fm.rstrip("\n") + "\nreplied: true\n"
    else:
        fm = fm.replace("replied: false", "replied: true")
    filepath.write_text("---".join([parts[0], fm, parts[2]]))
    return True


# Messages older than this are assumed handled and no longer flagged as
# awaiting a reply — a "timely reply" SLA, not an unbounded backlog. Override
# with AGENT_MSG_REPLY_WINDOW_DAYS (0 = no age limit).
REPLY_WINDOW_DAYS = int(os.environ.get("AGENT_MSG_REPLY_WINDOW_DAYS", "7"))


def _msg_age_days(meta: dict, now: datetime) -> float | None:
    """Age of a message in days from its `timestamp` field (None if unparseable)."""
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
    """Whether a message is addressed to us.

    Accepts a string `to` (single recipient) or a list (broadcast). A message
    with no `to` field is treated as addressed to us — older messages and
    manually-authored notes may omit it, and we prefer to over-flag (surface
    for reply) rather than silently drop a real message.
    """
    to = meta.get("to")
    if to is None:
        return True
    if isinstance(to, list | tuple | set):
        return self_name in {str(t) for t in to}
    return str(to) == self_name


def needs_reply_messages(self_name: str, window_days: int | None = None) -> list[dict]:
    """Recent inbox messages addressed to us that we haven't replied to yet.

    A reply requirement is satisfied either by stamping the inbox message
    `replied: true` (the `reply` command does this) or by any outbox message
    whose `in_reply_to` points at it (manual replies still count). Messages
    older than the reply window are treated as handled (timely-reply SLA).
    """
    ensure_dirs()
    inbox = get_messages_dir() / "inbox"
    outbox = get_messages_dir() / "outbox"
    window = REPLY_WINDOW_DAYS if window_days is None else window_days
    now = datetime.now(timezone.utc)

    replied_to: set[str] = set()
    for f in outbox.glob("*.md"):
        m = _meta_of(f)
        if m and m.get("in_reply_to"):
            replied_to.add(str(m["in_reply_to"]))

    pending = []
    for f in sorted(inbox.glob("*.md")):
        m = _meta_of(f)
        if not m:
            continue
        sender = m.get("from")
        if sender in (None, self_name):  # skip self-sent / malformed
            continue
        if not _addressed_to(m, self_name):  # not actually for us
            continue
        if m.get("replied"):
            continue
        if f.name in replied_to:
            continue
        if window > 0:
            age = _msg_age_days(m, now)
            if age is not None and age > window:
                continue
        m["file"] = f.name
        pending.append(m)
    return pending


def cmd_reply(
    agents: dict[str, dict[str, str]], self_name: str, filename: str, body: str
) -> bool:
    """Reply to an inbox message: send back to its sender and mark it replied."""
    inbox = get_messages_dir() / "inbox"
    filepath = (inbox / filename).resolve()
    if (
        not str(filepath).startswith(str(inbox.resolve()) + "/")
        or not filepath.exists()
    ):
        print(f"Error: Message not found: {filename}")
        return False
    meta = _meta_of(filepath)
    if not meta:
        print(f"Error: Could not parse {filename}")
        return False
    sender = meta.get("from")
    if not sender or sender == self_name:
        print(f"Error: No valid sender to reply to in {filename}")
        return False
    subject = str(meta.get("subject", ""))
    re_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

    ok = send_message(agents, self_name, sender, re_subject, body, in_reply_to=filename)
    if ok:
        if mark_replied(filename):
            print(f"Replied to {sender} and marked {filename} as replied.")
        else:
            # Reply was delivered, but the inbox stamp failed (file became
            # unreadable/removed mid-call). The outbox `in_reply_to` still
            # satisfies needs-reply tracking, so warn rather than fail.
            print(
                f"Replied to {sender}, but could not stamp {filename} as replied "
                "(outbox in_reply_to still records the reply)."
            )
    return ok


def cmd_status(agents: dict[str, dict[str, str]], self_name: str) -> None:
    """Show messaging status and connectivity."""
    ensure_dirs()
    inbox = get_messages_dir() / "inbox"
    outbox = get_messages_dir() / "outbox"
    inbox_count = len(list(inbox.glob("*.md")))

    def _is_unread(f: Path) -> bool:
        content = f.read_text()
        if not content.startswith("---"):
            return False
        parts = content.split("---", 2)
        return len(parts) >= 3 and "read: false" in parts[1]

    unread = len([f for f in inbox.glob("*.md") if _is_unread(f)])
    outbox_count = len(list(outbox.glob("*.md")))
    pending = needs_reply_messages(self_name)

    print(f"Agent: {self_name}")
    print(f"Inbox: {inbox_count} messages ({unread} unread)")
    print(f"Outbox: {outbox_count} sent")
    print(f"Needs reply: {len(pending)}")
    for m in pending:
        print(f"  ⚠ {m.get('from')}: {m.get('subject')}  ({m['file']})")

    if not agents:
        print("\nNo agents configured. Create messages/agents.yaml.")
        return

    print(f"\nKnown agents: {', '.join(agents.keys())}")

    for name, config in agents.items():
        if name == self_name:
            print(f"  {name}: local (self)")
            continue
        ssh_target = config["ssh"]
        try:
            subprocess.run(
                [
                    "ssh",
                    "-o",
                    "ConnectTimeout=3",
                    "-o",
                    "BatchMode=yes",
                    ssh_target,
                    "echo ok",
                ],
                check=True,
                capture_output=True,
                timeout=5,
            )
            print(f"  {name}: reachable ✓")
        except Exception:
            print(f"  {name}: unreachable ✗")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inter-agent messaging over SSH",
        epilog="Configure agents in messages/agents.yaml. See script docstring for format.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # send
    send_parser = subparsers.add_parser("send", help="Send a message")
    send_parser.add_argument("recipient", help="Target agent name")
    send_parser.add_argument("subject", help="Message subject")
    send_parser.add_argument("body", help="Message body")

    # broadcast
    broadcast_parser = subparsers.add_parser("broadcast", help="Send to all agents")
    broadcast_parser.add_argument("subject", help="Message subject")
    broadcast_parser.add_argument("body", help="Message body")

    # list
    list_parser = subparsers.add_parser("list", help="List inbox messages")
    list_parser.add_argument(
        "--all", action="store_true", help="Show all (including read)"
    )

    # read
    read_parser = subparsers.add_parser("read", help="Read a specific message")
    read_parser.add_argument("filename", help="Message filename")

    # reply
    reply_parser = subparsers.add_parser(
        "reply", help="Reply to an inbox message (sends to sender, marks it replied)"
    )
    reply_parser.add_argument("filename", help="Inbox message filename to reply to")
    reply_parser.add_argument("body", help="Reply body")

    # list
    list_parser.add_argument(
        "--needs-reply",
        action="store_true",
        help="Show only messages awaiting a reply from you",
    )

    # status
    subparsers.add_parser("status", help="Show messaging status")

    args = parser.parse_args()
    self_name = get_self()

    # Only load agents config for commands that need remote delivery
    # (list/read work on local inbox — no need to warn about missing agents.yaml)
    agents = (
        load_agents()
        if args.command in ("send", "broadcast", "status", "reply")
        else {}
    )

    if args.command == "send":
        success = send_message(
            agents, self_name, args.recipient, args.subject, args.body
        )
        sys.exit(0 if success else 1)

    elif args.command == "broadcast":
        failures = 0
        for agent in agents:
            if agent != self_name:
                if not send_message(agents, self_name, agent, args.subject, args.body):
                    failures += 1
        sys.exit(1 if failures else 0)

    elif args.command == "list":
        if getattr(args, "needs_reply", False):
            messages = needs_reply_messages(self_name)
            if not messages:
                print("No messages awaiting a reply.")
                return
            for msg in messages:
                ts = msg.get("timestamp", "unknown")
                sender = msg.get("from", "unknown")
                subject = msg.get("subject", "(no subject)")
                print(f"  ⚠ [{ts}] {sender}: {subject}  ({msg['file']})")
                print(f"    reply with: agent-msg.py reply {msg['file']} \"...\"")
            return

        messages = list_inbox(show_all=args.all)
        if not messages:
            print("No unread messages." if not args.all else "No messages.")
            return

        for msg in messages:
            read_marker = " " if msg.get("read") else "*"
            ts = msg.get("timestamp", "unknown")
            sender = msg.get("from", "unknown")
            subject = msg.get("subject", "(no subject)")
            print(f"  {read_marker} [{ts}] {sender}: {subject}  ({msg['file']})")

    elif args.command == "read":
        content = read_message(args.filename)
        if content:
            print(content)

    elif args.command == "reply":
        success = cmd_reply(agents, self_name, args.filename, args.body)
        sys.exit(0 if success else 1)

    elif args.command == "status":
        cmd_status(agents, self_name)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
