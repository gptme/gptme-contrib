#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "click>=8.0.0",
#   "markdown>=3.4.0",
# ]
# ///
"""CLI interface for the email system."""

import os
import sys
import tempfile
from pathlib import Path
from datetime import timezone


import click

from lib import AgentEmail  # type: ignore[import-not-found]


def get_workspace_dir() -> Path:
    """Get the agent workspace directory.

    TODO: Make workspace detection configurable via env vars (GPTME_WORKSPACE, etc.)
    Currently assumes gptme-contrib is a submodule in agent workspace.
    """
    return Path(__file__).parent.parent.parent.parent


def load_env_file(workspace_dir: Path) -> None:
    """Load environment variables from .env file.

    Simple .env loader that doesn't require python-dotenv.
    """
    env_path = workspace_dir / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text().splitlines():
        line = line.strip()
        # Skip empty lines and comments
        if not line or line.startswith("#"):
            continue
        # Parse KEY=VALUE
        if "=" in line:
            key, value = line.split("=", 1)
            # Only set if not already in environment (don't override)
            os.environ.setdefault(key.strip(), value.strip())


# Load .env file at module initialization
load_env_file(get_workspace_dir())


def get_editor() -> str:
    """Get the user's preferred editor."""
    return os.environ.get("EDITOR", "vim")


def edit_content() -> str:
    """Open editor for content editing."""
    editor = get_editor()
    with tempfile.NamedTemporaryFile(suffix=".md") as tf:
        os.system(f"{editor} {tf.name}")
        return Path(tf.name).read_text()


@click.group()
def cli() -> None:
    """Email system for agent communication."""
    pass


@cli.command()
@click.argument("to")
@click.argument("subject")
@click.argument("content", required=False)
@click.option(
    "--from",
    "from_address",
    help="Custom sender address (defaults to bob@superuserlabs.org)",
)
def compose(
    to: str, subject: str, content: str | None = None, from_address: str | None = None
) -> None:
    """Create new email.

    If CONTENT is not provided, opens an editor to compose the message.
    Use --from to specify a custom sender address.
    """
    workspace_dir = get_workspace_dir()
    email = AgentEmail(workspace_dir)

    # Get content from editor if not provided
    if content is None:
        content = edit_content()

    message_id = email.compose(to, subject, content, from_address=from_address)
    click.echo(f"Created draft: {message_id}")
    if from_address:
        click.echo(f"Using sender address: {from_address}")


@cli.command()
@click.argument("message_id")
def send(message_id: str) -> None:
    """Send draft email."""
    workspace_dir = get_workspace_dir()
    email = AgentEmail(workspace_dir)
    email.send(message_id)


@cli.command()
@click.argument("folder", default="inbox")
def list(folder: str) -> None:
    """List messages in folder (default: inbox)."""
    workspace_dir = get_workspace_dir()
    email = AgentEmail(workspace_dir)

    messages = email.list_messages(folder)
    if not messages:
        click.echo(f"No messages in {folder}")
        return

    # Print table header
    click.echo(f"{'Date':<20} | {'Subject':<40} | Message ID")
    click.echo("-" * 80)

    # Print messages
    for msg_id, subject, date in messages:
        date_str = date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        click.echo(f"{date_str:<20} | {subject[:40]:<40} | {msg_id}")


@cli.command()
@click.argument("message_id")
@click.option("--thread", is_flag=True, help="Show the entire conversation thread")
@click.option(
    "--thread-only",
    is_flag=True,
    help="Show only thread structure without message bodies",
)
def read(message_id: str, thread: bool = False, thread_only: bool = False) -> None:
    """Read message, optionally with full conversation thread."""
    workspace_dir = get_workspace_dir()
    email = AgentEmail(workspace_dir)

    if thread_only:
        # Show just the thread structure
        thread_messages = email.get_thread_messages(message_id)
        if not thread_messages:
            click.echo(f"No thread found for message: {message_id}")
            return

        click.echo(f"THREAD STRUCTURE ({len(thread_messages)} messages):")
        click.echo("=" * 60)

        for i, msg in enumerate(thread_messages):
            headers = msg["headers"]
            is_current = msg["id"] == message_id
            marker = " <-- CURRENT" if is_current else ""
            date = headers.get("Date", "Unknown")[:16]  # Truncate date
            sender = headers.get("From", "Unknown")
            if "<" in sender and ">" in sender:
                # Extract email from "Name <email>" format
                sender = sender[sender.find("<") + 1 : sender.find(">")]
            subject = headers.get("Subject", "No Subject")[:40]  # Truncate subject

            click.echo(f"{i + 1:2d}. {date} | {sender:25s} | {subject}{marker}")

    elif thread:
        # Show full thread with message bodies
        content = email.read_message(message_id, include_thread=True)
        click.echo(content)
    else:
        # Show single message (original behavior)
        content = email.read_message(message_id)
        click.echo(content)


@cli.command()
@click.argument("message_id")
@click.argument("content", required=False)
@click.option(
    "--from",
    "from_address",
    help="Custom sender address (defaults to bob@superuserlabs.org)",
)
def reply(message_id: str, content: str | None = None, from_address: str | None = None) -> None:
    """Reply to message.

    If CONTENT is not provided, opens an editor to compose the reply.
    Use --from to specify a custom sender address.
    """
    workspace_dir = get_workspace_dir()
    email = AgentEmail(workspace_dir)

    # Get original message details
    original = email.read_message(message_id)

    # Parse headers from original message
    headers = {}
    for line in original.split("\n"):
        if not line or line.isspace():
            break
        if ": " in line:
            key, value = line.split(": ", 1)
            headers[key] = value

    if "From" not in headers or "Subject" not in headers:
        click.echo("Error: Could not parse original message headers", err=True)
        sys.exit(1)

    # Reply goes to the original sender (To: in sent messages, From: in received)
    # if "To" in headers:  # This is a sent message
    #     to = headers["To"]
    # else:  # This is a received message
    to = headers["From"]

    # Keep original subject, add Re: if needed
    subject = headers["Subject"]
    if not subject.startswith("Re: "):
        subject = "Re: " + subject

    # Get content from editor if not provided
    if content is None:
        content = edit_content()
    else:
        # Interpret escape sequences in content
        content = bytes(content, "utf-8").decode("unicode_escape")

    # Build proper References chain for threading
    references_chain = []

    # If original message has References, include them
    if "References" in headers:
        # Parse existing references (remove angle brackets if present)
        existing_refs = headers["References"].strip()
        if existing_refs:
            # Split on whitespace and clean up
            existing_refs = existing_refs.replace("<", "").replace(">", "")
            references_chain.extend(existing_refs.split())

    # Add the original message ID to the chain
    clean_message_id = message_id.replace("<", "").replace(">", "")
    references_chain.append(clean_message_id)

    # Create reply draft with proper threading
    reply_id = email.compose(
        to=to,
        subject=subject,
        content=content,
        from_address=from_address,
        reply_to=message_id,
        references=references_chain,
    )
    click.echo(f"Created reply draft: {reply_id}")
    if from_address:
        click.echo(f"Using sender address: {from_address}")
    click.echo("\n")


@cli.command()
@click.argument("message_id")
def archive(message_id: str) -> None:
    """Archive message."""
    workspace_dir = get_workspace_dir()
    email = AgentEmail(workspace_dir)
    email.archive(message_id)


@cli.command()
@click.argument("message_id")
@click.option("--structure", is_flag=True, help="Show thread structure only")
@click.option("--stats", is_flag=True, help="Show thread statistics")
def thread(message_id: str, structure: bool = False, stats: bool = False) -> None:
    """Show conversation thread for a message."""
    workspace_dir = get_workspace_dir()
    email = AgentEmail(workspace_dir)

    thread_messages = email.get_thread_messages(message_id)
    if not thread_messages:
        click.echo(f"No thread found for message: {message_id}")
        return

    if stats:
        # Show thread statistics
        total_messages = len(thread_messages)
        folders: dict[str, int] = {}
        senders = set()
        date_range = []

        for msg in thread_messages:
            # Count by folder
            folder = msg["folder"]
            folders[folder] = folders.get(folder, 0) + 1

            # Track senders
            sender = msg["headers"].get("From", "")
            if "<" in sender and ">" in sender:
                sender = sender[sender.find("<") + 1 : sender.find(">")]
            senders.add(sender)

            # Track date range
            date_range.append(msg["timestamp"])

        date_range.sort()

        click.echo("THREAD STATISTICS:")
        click.echo(f"Total messages: {total_messages}")
        click.echo(f"Participants: {len(senders)}")
        click.echo(f"  - {', '.join(sorted(senders))}")
        click.echo(
            f"Date range: {date_range[0].strftime('%Y-%m-%d')} to {date_range[-1].strftime('%Y-%m-%d')}"
        )
        click.echo(f"Folders: {dict(folders)}")

    elif structure:
        # Show thread structure only
        click.echo(f"THREAD STRUCTURE ({len(thread_messages)} messages):")
        click.echo("=" * 80)

        for i, msg in enumerate(thread_messages):
            headers = msg["headers"]
            is_current = msg["id"] == message_id
            marker = " <-- CURRENT" if is_current else ""

            date = headers.get("Date", "Unknown")
            if date != "Unknown":
                try:
                    parsed_date = email._parse_email_date(date)
                    date = parsed_date.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    date = date[:16]

            sender = headers.get("From", "Unknown")
            if "<" in sender and ">" in sender:
                sender = sender[sender.find("<") + 1 : sender.find(">")]
            elif " " in sender:
                sender = sender.split()[0]  # Take first part if no email brackets

            subject = headers.get("Subject", "No Subject")
            folder = msg["folder"]

            click.echo(f"{i + 1:2d}. [{folder:7s}] {date} | {sender:25s} | {subject[:35]}{marker}")

    else:
        # Show full thread (same as read --thread)
        content = email._format_thread_display(message_id)
        click.echo(content)


@cli.command()
@click.argument("message_id")
@click.option("--reason", default="no reply needed", help="Reason for not replying")
def mark_no_reply(message_id: str, reason: str) -> None:
    """Mark email as processed but no reply needed."""
    workspace_dir = get_workspace_dir()
    email = AgentEmail(workspace_dir)
    email._mark_no_reply_needed(message_id, reason)
    click.echo(f"Marked {message_id} as no reply needed: {reason}")


@cli.command()
@click.option("--threshold", default=0.6, help="Complexity threshold (0.0-1.0)")
@click.option(
    "--mark-complex/--no-mark",
    default=False,
    help="Automatically mark complex emails as no-reply-needed",
)
def check_complexity(threshold: float, mark_complex: bool) -> None:
    """Check complexity of unreplied emails and optionally mark complex ones."""
    import re

    workspace_dir = get_workspace_dir()
    email = AgentEmail(workspace_dir)

    # Inline complexity detection
    SENSITIVE_KEYWORDS = {
        "financial",
        "money",
        "payment",
        "invoice",
        "contract",
        "legal",
        "lawsuit",
        "attorney",
        "court",
        "confidential",
        "private",
        "sensitive",
        "secret",
        "personal",
        "urgent",
        "critical",
        "emergency",
    }

    DECISION_PHRASES = [
        r"should\s+we",
        r"which\s+option",
        r"need\s+to\s+decide",
        r"what\s+do\s+you\s+think",
        r"your\s+thoughts",
        r"approve",
        r"sign\s+off",
    ]

    def detect_complexity(msg, body: str):
        """Detect email complexity. Returns (score, reasons)."""
        reasons = []
        score = 0.0

        # Check length
        word_count = len(body.split())
        if word_count > 500:
            reasons.append(f"long email ({word_count} words)")
            score += 0.2

        # Check paragraphs
        paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
        if len(paragraphs) > 5:
            reasons.append(f"many paragraphs ({len(paragraphs)})")
            score += 0.15

        # Check questions
        questions = body.count("?")
        if questions > 3:
            reasons.append(f"multiple questions ({questions})")
            score += 0.25

        # Check sensitive keywords
        body_lower = body.lower()
        found_sensitive = [kw for kw in SENSITIVE_KEYWORDS if kw in body_lower]
        if found_sensitive:
            reasons.append(f"sensitive: {', '.join(found_sensitive[:2])}")
            score += 0.3

        # Check decision phrases
        if any(re.search(pattern, body_lower) for pattern in DECISION_PHRASES):
            reasons.append("requires decision")
            score += 0.25

        # Check multiple recipients
        to_addrs = msg.get_all("to", [])
        cc_addrs = msg.get_all("cc", [])
        total = len(to_addrs) + len(cc_addrs)
        if total > 2:
            reasons.append(f"multiple recipients ({total})")
            score += 0.15

        return min(score, 1.0), reasons

    # Get unreplied emails
    unreplied = email.get_unreplied_emails()

    if not unreplied:
        click.echo("No unreplied emails found.")
        return

    click.echo(f"\nComplexity Analysis (threshold: {threshold}):\n")
    click.echo(f"{'Sender':<30} | {'Subject':<40} | {'Score':<6} | {'Status':<10}")
    click.echo("-" * 100)

    complex_count = 0
    simple_count = 0

    for msg_id, subject, sender in unreplied:
        # Read email to get message object and body
        message_data = email.read_message(msg_id)

        # Parse the raw email to get EmailMessage object
        from email import message_from_string

        msg = message_from_string(message_data)

        # Extract plain text body
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload and isinstance(payload, bytes):
                        body = payload.decode("utf-8", errors="replace")
                    break
        else:
            payload = msg.get_payload(decode=True)
            if payload and isinstance(payload, bytes):
                body = payload.decode("utf-8", errors="replace")

        # Detect complexity
        complexity_score, reasons = detect_complexity(msg, body)
        is_complex = complexity_score > threshold

        status = "COMPLEX" if is_complex else "simple"
        if is_complex:
            complex_count += 1

            # Optionally mark as no-reply-needed
            if mark_complex:
                email._mark_no_reply_needed(msg_id, f"complex email - {', '.join(reasons[:2])}")
                status += " (marked)"
        else:
            simple_count += 1

        # Truncate sender/subject for display
        sender_short = sender[:28] + ".." if len(sender) > 30 else sender
        subject_short = subject[:38] + ".." if len(subject) > 40 else subject

        click.echo(
            f"{sender_short:<30} | {subject_short:<40} | {complexity_score:<6.2f} | {status:<10}"
        )

        if reasons:
            click.echo(f"  Reasons: {', '.join(reasons)}")

    click.echo(f"\nSummary: {simple_count} simple, {complex_count} complex")

    if mark_complex and complex_count > 0:
        click.echo(f"\nMarked {complex_count} complex emails as no-reply-needed.")


@cli.command()
@click.option(
    "--status",
    type=click.Choice(["all", "replied", "no_reply_needed"]),
    default="all",
    help="Filter by completion status",
)
def list_completed(status: str) -> None:
    """List completed emails (replied or marked as no reply needed)."""
    workspace_dir = get_workspace_dir()
    email = AgentEmail(workspace_dir)

    import json

    try:
        replies_data = json.loads(email.replies_state_file.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        click.echo("No completed emails found.")
        return

    if not replies_data:
        click.echo("No completed emails found.")
        return

    filtered_data = {}
    if status == "all":
        filtered_data = replies_data
    else:
        for msg_id, data in replies_data.items():
            if data.get("status") == status:
                filtered_data[msg_id] = data

    if not filtered_data:
        click.echo(f"No emails with status '{status}' found.")
        return

    click.echo(f"Found {len(filtered_data)} completed emails:")
    click.echo(f"{'Status':<15} | {'Completed At':<20} | {'Details':<40} | Message ID")
    click.echo("-" * 100)

    for msg_id, data in filtered_data.items():
        status_val = data.get("status", "unknown")
        completed_at = data.get("completed_at", data.get("replied_at", "unknown"))[
            :19
        ]  # Truncate timestamp

        if status_val == "replied":
            details = f"Reply ID: {data.get('reply_id', 'unknown')}"
        elif status_val == "no_reply_needed":
            details = f"Reason: {data.get('reason', 'no reason given')}"
        else:
            details = "Unknown"

        click.echo(f"{status_val:<15} | {completed_at:<20} | {details[:40]:<40} | {msg_id}")


@cli.command()
@click.argument("folder", default="all")
def sync_maildir(folder: str) -> None:
    """Sync messages between markdown and maildir formats.

    If folder is 'all', syncs inbox and sent folders.
    Currently only supports syncing from maildir to markdown.
    """
    workspace_dir = get_workspace_dir()
    email = AgentEmail(workspace_dir)

    folders = ["inbox", "sent"] if folder == "all" else [folder]

    for f in folders:
        click.echo(f"Syncing {f}...")
        try:
            email.sync_from_maildir(f)
            click.echo(f"Synced {f} from maildir to markdown")
        except ValueError as e:
            click.echo(f"Error syncing {f}: {e}", err=True)

    click.echo("Sync complete")


@cli.command()
@click.argument("folder", type=str)
@click.argument("dest_maildir", type=str)
def export_maildir(folder: str, dest_maildir: str) -> None:
    """Export messages from markdown to maildir format.

    FOLDER: Folder to export (inbox, sent, drafts, archive, or 'all')
    DEST_MAILDIR: Destination maildir directory path

    Example:
        ./cli.py export-maildir inbox ~/test-maildir
        ./cli.py export-maildir all ~/test-maildir
    """
    workspace_dir = get_workspace_dir()
    email = AgentEmail(workspace_dir)

    dest_path = Path(dest_maildir)
    folders = ["inbox", "sent", "drafts", "archive"] if folder == "all" else [folder]

    for f in folders:
        click.echo(f"Exporting {f} to {dest_path}...")
        try:
            result = email.export_to_maildir(f, dest_path)
            if "error" in result:
                click.echo(f"Error: {result['error']}", err=True)
            else:
                click.echo(
                    f"Exported {f}: {result['success']} succeeded, "
                    f"{result['failed']} failed, {result['skipped']} skipped"
                )
        except ValueError as e:
            click.echo(f"Error exporting {f}: {e}", err=True)

    click.echo("Export complete")


@cli.command()
def check_unreplied() -> None:
    """Check for unreplied emails from allowlisted senders."""
    workspace_dir = get_workspace_dir()
    email = AgentEmail(workspace_dir)

    unreplied = email.get_unreplied_emails()

    if not unreplied:
        click.echo("No unreplied emails found.")
        return

    click.echo(f"Found {len(unreplied)} unreplied emails:")
    click.echo(f"{'Sender':<30} | {'Subject':<40} | Message ID")
    click.echo("-" * 90)

    for message_id, subject, sender in unreplied:
        click.echo(f"{sender:<30} | {subject[:40]:<40} | {message_id}")

    # Exit with code 1 to indicate emails were found
    sys.exit(1)


@cli.command()
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be processed without actually doing it",
)
def process_unreplied(dry_run: bool) -> None:
    """Process unreplied emails with gptme (same as watcher but on-demand)."""
    workspace_dir = get_workspace_dir()
    email = AgentEmail(workspace_dir)

    def process_email(message_id: str, subject: str, sender: str) -> None:
        if dry_run:
            click.echo(f"Would process: {sender} - {subject}")
            return

        # Find the email file
        email_file = None
        inbox_dir = workspace_dir / "email" / "inbox"
        for f in inbox_dir.glob("*.md"):
            content = f.read_text()
            if message_id in content:
                email_file = f
                break

        if not email_file:
            click.echo(f"Could not find email file for {message_id}", err=True)
            return

        # Use gptme to process the email
        import subprocess
        import time

        gptme_cmd = [
            "gptme",
            "--no-confirm",
            "--non-interactive",
            "--name",
            f"email-{sender.replace('@', '_at_')}-at-{int(time.time())}",
            f"I received an email that needs a response. Please read `{email_file}`, and if there is anything you need to do or act on, do that. Then generate an appropriate reply using `./cli.py reply {message_id} <your_reply_content>` and send it using `./cli.py send <draft_id>`. It's important to use the 'reply' command (not 'compose') to maintain email threading. Format the message as markdown and include links if appropriate (it will be rendered as HTML). Only reply when appropriate.",
        ]

        try:
            result = subprocess.run(
                gptme_cmd,
                cwd=workspace_dir,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode == 0:
                click.echo(f"Successfully processed email from {sender}")
            else:
                click.echo(f"Failed to process email from {sender}: {result.stderr}", err=True)

        except subprocess.TimeoutExpired:
            click.echo(f"Timeout processing email from {sender}", err=True)
        except Exception as e:
            click.echo(f"Error processing email from {sender}: {e}", err=True)

    processed = email.process_unreplied_emails(process_email)

    if dry_run:
        click.echo(f"Dry run complete. Would have processed {processed} emails.")
    else:
        click.echo(f"Processed {processed} unreplied emails.")


@cli.command()
@click.argument("message_id")
def check_completion_status(message_id: str) -> None:
    """Check if a specific message ID is marked as completed."""
    workspace_dir = get_workspace_dir()
    email = AgentEmail(workspace_dir)

    import json

    # Show various formats to help debug
    normalized_id = message_id.strip("<>")
    with_brackets = f"<{normalized_id}>"

    click.echo(f"Checking completion status for: {message_id}")
    click.echo(f"Normalized (no brackets): {normalized_id}")
    click.echo(f"With brackets: {with_brackets}")
    click.echo()

    is_completed = email._is_completed(message_id)
    click.echo(f"Is completed: {is_completed}")

    # Show what's actually in the replies state file
    try:
        replies_data = json.loads(email.replies_state_file.read_text())
        click.echo("\nEntries in replies_state.json:")
        matching_entries = []
        for stored_id, data in replies_data.items():
            if stored_id == message_id or stored_id == normalized_id or stored_id == with_brackets:
                matching_entries.append((stored_id, data))

        if matching_entries:
            for stored_id, data in matching_entries:
                click.echo(f"  {stored_id}: {data}")
        else:
            click.echo("  No matching entries found")
            click.echo(f"  Total entries in file: {len(replies_data)}")

    except (json.JSONDecodeError, FileNotFoundError):
        click.echo("No replies_state.json file found or invalid JSON")


if __name__ == "__main__":
    cli()
