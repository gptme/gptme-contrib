#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "click",
#   "markdown",
# ]
# [tool.uv]
# exclude-newer = "2025-05-26T00:00:00Z"
# ///
"""Simple email sync and response daemon."""

import logging
import subprocess
import sys
import time
from pathlib import Path

# Add parent directory to path for shared communication_utils
sys.path.insert(0, str(Path(__file__).parent.parent))

from communication_utils.state.locks import FileLock, LockError
from email.lib import AgentEmail

# Configuration
SCRIPT_DIR = Path(__file__).parent
GPTME_CONTRIB_DIR = SCRIPT_DIR.parent.parent
WORKSPACE_DIR = GPTME_CONTRIB_DIR.parent
EMAIL_DIR = WORKSPACE_DIR / "email"
INBOX_DIR = EMAIL_DIR / "inbox"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(EMAIL_DIR / "watcher.log"), logging.StreamHandler()],
)


def run_mbsync():
    """Fetch new emails from external provider (both inbox and sent labels)."""
    success = True

    for label in ["gmail-agent", "gmail-agent-sent"]:
        try:
            logging.info(f"Running mbsync for {label}...")
            result = subprocess.run(["mbsync", label], capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                logging.info(f"mbsync {label} completed successfully")
            else:
                logging.error(f"mbsync {label} failed: {result.stderr}")
                success = False
        except Exception as e:
            logging.error(f"mbsync {label} error: {e}")
            success = False

    return success


def sync_emails():
    """Import emails from maildir to workspace (both inbox and sent)."""
    try:
        logging.info("Syncing emails...")
        result = subprocess.run(
            [str(SCRIPT_DIR / "cli.py"), "sync-maildir", "all"],
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logging.info("Email sync completed")
            return True
        else:
            logging.error(f"Email sync failed: {result.stderr}")
            return False
    except Exception as e:
        logging.error(f"Email sync error: {e}")
        return False


def find_email_file(message_id: str) -> Path | None:
    """Find email file by message ID."""
    for f in INBOX_DIR.glob("*.md"):
        if message_id in f.read_text():
            return f
    return None


def run_gptme_for_email(message_id: str, subject: str, sender: str, email_file: Path) -> bool:
    """Run gptme to process an email. Returns True if successful."""
    gptme_cmd = [
        "gptme",
        "--no-confirm",
        "--non-interactive",
        "--name",
        f"email-{sender.replace('@', '_at_')}-at-{int(time.time())}",
        f"I received an email that needs a response. Please read `{email_file}`, and decide if it needs a reply. If it does, generate an appropriate reply using `./cli.py reply {message_id} <your_reply_content>` and send it using `./cli.py send <draft_id>`. If it doesn't need a reply (e.g., it's just informational, already resolved, or not actionable), mark it as processed using `./cli.py mark-no-reply {message_id} --reason '<brief_reason>'`. It's important to use the 'reply' command (not 'compose') to maintain email threading. Format replies as markdown and include links if appropriate (it will be rendered as HTML). Always complete the task by either replying or marking as no reply needed.",
    ]

    logging.info(f"Starting gptme for email from {sender}")
    print(f"\n{'=' * 60}")
    print(f"🤖 PROCESSING EMAIL FROM: {sender}")
    print(f"📧 SUBJECT: {subject}")
    print(f"{'=' * 60}\n")

    try:
        result = subprocess.run(gptme_cmd, cwd=WORKSPACE_DIR, text=True, timeout=300)
        success = result.returncode == 0

        print(f"\n{'=' * 60}")
        if success:
            print(f"✅ SUCCESS: Email from {sender} processed successfully")
            logging.info(f"Successfully processed email from {sender}")
        else:
            print(f"❌ FAILED: Email from {sender} failed (exit code {result.returncode})")
            logging.error(f"Failed to process email from {sender} (exit code {result.returncode})")
        print(f"{'=' * 60}\n")

        return success

    except subprocess.TimeoutExpired:
        print(f"\n⏰ TIMEOUT: Processing email from {sender} took too long")
        logging.error(f"Timeout processing email from {sender}")
        return False
    except Exception as e:
        print(f"\n💥 ERROR: {e}")
        logging.error(f"Error processing email from {sender}: {e}")
        return False


def process_single_email(message_id: str, subject: str, sender: str) -> None:
    """Process a single email with error handling."""
    logging.info(f"Processing email from {sender}: {subject}")

    email_file = find_email_file(message_id)
    if not email_file:
        logging.error(f"Could not find email file for {message_id}")
        return

    run_gptme_for_email(message_id, subject, sender, email_file)


def process_unreplied_emails(limit: int | None = None):
    """Process unreplied emails using the AgentEmail class.

    Args:
        limit: If provided, only process this many emails
    """
    try:
        logging.info("Checking for unreplied emails...")
        email = AgentEmail(WORKSPACE_DIR)

        if limit == 1:
            # For single email processing, get the list and process just the first one
            unreplied = email.get_unreplied_emails()
            if not unreplied:
                logging.info("No unreplied emails found")
                return False

            message_id, subject, sender = unreplied[0]
            logging.info(f"Processing single email from {sender}: {subject}")

            lock_file = email.locks_dir / f"{email._format_filename(message_id)}.lock"
            try:
                with FileLock(lock_file, timeout=0):
                    process_single_email(message_id, subject, sender)
                    logging.info("Processed 1 unreplied email")
                    return True
            except LockError:
                logging.info(f"Email {message_id} is already being processed")
                return False
        else:
            # Process all unreplied emails
            processed = email.process_unreplied_emails(process_single_email)
            if processed > 0:
                logging.info(f"Processed {processed} unreplied emails")
            return processed > 0

    except Exception as e:
        logging.error(f"Error checking unreplied emails: {e}")
        return False


def run_sync_cycle(email_limit: int | None = None):
    """Run a complete sync cycle: fetch emails, sync to workspace, process unreplied.

    Args:
        email_limit: If provided, only process this many emails
    """
    try:
        # Step 1: Fetch emails from Gmail
        if not run_mbsync():
            logging.warning("mbsync failed, skipping this cycle")
            return False

        # Step 2: Import emails to workspace
        if not sync_emails():
            logging.warning("Email sync failed, skipping unreplied check")
            return False

        # Step 3: Process unreplied emails (with optional limit)
        process_unreplied_emails(limit=email_limit)

        return True
    except Exception as e:
        logging.error(f"Error in sync cycle: {e}")
        return False


def run_daemon():
    """Run the email daemon with periodic sync cycles."""
    logging.info("Starting email daemon...")

    try:
        while True:
            logging.info("Starting sync cycle...")
            run_sync_cycle()

            # Wait 30 seconds between cycles
            logging.info("Sync cycle complete, waiting 30 seconds...")
            time.sleep(30)

    except KeyboardInterrupt:
        logging.info("Stopping email daemon...")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        command = sys.argv[1]
        if command == "once":
            # Run sync once and exit
            logging.info("Running single sync cycle...")
            run_sync_cycle()
        elif command == "one":
            # Run sync and process just one unreplied email
            logging.info("Running sync cycle and processing one email...")
            run_sync_cycle(email_limit=1)
        else:
            print("Usage: watcher.py [once|one]")
            print("  once: Run single sync cycle (process all unreplied emails)")
            print("  one:  Run sync cycle and process just one unreplied email")
            print("  (no args): Run continuous daemon")
            sys.exit(1)
    else:
        # Start daemon
        run_daemon()
