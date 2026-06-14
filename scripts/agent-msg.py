#!/usr/bin/env python3
"""Inter-agent messaging over SSH — thin shim over ``gptmail agent``.

The implementation now lives in the gptmail package
(``gptmail.agent_cli``), which serves both email and inter-agent messaging
through one tool. This script is a compatibility shim: it preserves the
``agent-msg.py <command> …`` command surface agents (and muscle memory) use, and
delegates to the email-free ``gptmail agent`` CLI group.

"Email-free" is load-bearing: ``gptmail.agent_cli`` imports only the filesystem
``AgentTransport`` and the shared ``ConversationTracker`` — never the IMAP/SMTP
stack (``lib.AgentEmail``, which needs ``markdown`` etc.). So this shim runs in
isolated LXC sessions with no email infra. We deliberately import that group
directly rather than going through ``python -m gptmail`` / ``gptmail.cli``, whose
top-level entry point eagerly imports the email stack.

Usage (unchanged):
    # Send a message to Alice
    python3 scripts/agent-msg.py send alice "Subject" "Message body"

    # List unread messages
    python3 scripts/agent-msg.py list

    # List only messages awaiting a reply from you
    python3 scripts/agent-msg.py list --needs-reply   # -> gptmail agent pending

    # Reply to an inbox message (sends to sender, marks it replied)
    python3 scripts/agent-msg.py reply <inbox-filename> "Reply body"

    # Send to all agents
    python3 scripts/agent-msg.py broadcast "Subject" "Message body"

    # Check connectivity (and how many messages await a reply)
    python3 scripts/agent-msg.py status

Configuration (unchanged):
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

import sys
from pathlib import Path


def _load_agent_group():
    """Import the email-free ``gptmail agent`` click group.

    Tries a normal import first (gptmail installed on PATH/site-packages); if
    that fails, falls back to the sibling source checkout at
    ``../packages/gptmail/src`` so the shim works from a bare gptme-contrib
    clone with no install step.
    """
    try:
        from gptmail.agent_cli import agent
    except ImportError:
        src = Path(__file__).resolve().parent.parent / "packages" / "gptmail" / "src"
        sys.path.insert(0, str(src))
        try:
            from gptmail.agent_cli import agent
        except ImportError as e:
            print(
                "Error: could not import the gptmail agent CLI.\n"
                f"Tried an installed gptmail and the source checkout at {src}.\n"
                "Install it with: pip install -e packages/gptmail\n"
                "(or run from a gptme-contrib checkout where packages/gptmail exists).",
                file=sys.stderr,
            )
            raise SystemExit(1) from e
    return agent


def _translate(argv: list[str]) -> list[str]:
    """Map legacy agent-msg.py args onto ``gptmail agent`` subcommands.

    The surfaces are near-identical (send/broadcast/list/read/reply/status pass
    through unchanged). The one rename: ``list --needs-reply`` became its own
    ``pending`` subcommand. Any other flags on that invocation are carried over
    (not silently dropped) so click validates them and errors on anything
    ``pending`` doesn't accept.
    """
    if argv and argv[0] == "list" and "--needs-reply" in argv[1:]:
        rest = [a for a in argv[1:] if a != "--needs-reply"]
        return ["pending", *rest]
    return argv


def main() -> None:
    agent = _load_agent_group()
    # standalone_mode=True: let click handle exit codes, --help, and error output
    # exactly as a real CLI invocation would.
    agent(args=_translate(sys.argv[1:]), prog_name="agent-msg.py")


if __name__ == "__main__":
    main()
