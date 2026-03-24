#!/usr/bin/env python3
"""Inter-agent messaging over SSH.

Simple file-based messaging between agents running on different VMs.
Messages are YAML files transferred via SCP.

Usage:
    # Send a message to Alice
    python3 scripts/agent-msg.py send alice "Subject" "Message body"

    # Read inbox
    python3 scripts/agent-msg.py inbox

    # List unread messages
    python3 scripts/agent-msg.py list

    # Send to all agents
    python3 scripts/agent-msg.py broadcast "Subject" "Message body"

    # Check connectivity
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
    """Get the repository root directory."""
    return Path(__file__).resolve().parent.parent


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
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_subject = "".join(c if c.isalnum() or c in "-_" else "-" for c in subject)
    safe_subject = safe_subject[:40].strip("-")
    return f"{ts}-{sender}-{safe_subject}.md"


def format_message(sender: str, recipient: str, subject: str, body: str) -> str:
    """Format a message as YAML frontmatter + markdown body."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"""---
from: {sender}
to: {recipient}
timestamp: {ts}
subject: "{subject}"
read: false
---

{body}
"""


def send_message(
    agents: dict[str, dict[str, str]],
    self_name: str,
    recipient: str,
    subject: str,
    body: str,
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
    msg_content = format_message(self_name, recipient, subject, body)

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
                f"mkdir -p {remote_inbox}",
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
                f"{ssh_target}:{remote_inbox}{filename}",
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
    ensure_dirs()
    inbox = get_messages_dir() / "inbox"
    messages = []

    for f in sorted(inbox.glob("*.md")):
        content = f.read_text()
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3 and HAS_YAML:
                try:
                    meta = yaml.safe_load(parts[1])
                    body = parts[2].strip()
                    meta["body"] = body
                    meta["file"] = f.name
                    if show_all or not meta.get("read", False):
                        messages.append(meta)
                except Exception:
                    pass

    return messages


def read_message(filename: str) -> str | None:
    """Read a specific message and mark as read."""
    ensure_dirs()
    inbox = get_messages_dir() / "inbox"
    filepath = inbox / filename

    if not filepath.exists():
        print(f"Error: Message not found: {filename}")
        return None

    content = filepath.read_text()

    # Mark as read
    if "read: false" in content:
        content = content.replace("read: false", "read: true")
        filepath.write_text(content)

    return content


def cmd_status(agents: dict[str, dict[str, str]], self_name: str) -> None:
    """Show messaging status and connectivity."""
    ensure_dirs()
    inbox = get_messages_dir() / "inbox"
    outbox = get_messages_dir() / "outbox"
    inbox_count = len(list(inbox.glob("*.md")))
    unread = len([f for f in inbox.glob("*.md") if "read: false" in f.read_text()])
    outbox_count = len(list(outbox.glob("*.md")))

    print(f"Agent: {self_name}")
    print(f"Inbox: {inbox_count} messages ({unread} unread)")
    print(f"Outbox: {outbox_count} sent")

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

    # status
    subparsers.add_parser("status", help="Show messaging status")

    args = parser.parse_args()
    agents = load_agents()
    self_name = get_self()

    if args.command == "send":
        success = send_message(
            agents, self_name, args.recipient, args.subject, args.body
        )
        sys.exit(0 if success else 1)

    elif args.command == "broadcast":
        for agent in agents:
            if agent != self_name:
                send_message(agents, self_name, agent, args.subject, args.body)

    elif args.command == "list":
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

    elif args.command == "status":
        cmd_status(agents, self_name)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
