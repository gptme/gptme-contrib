"""Core library for the email-based message system.

This module provides the AgentEmail class which handles all email operations for
AI agent communication, including:
- Composing, sending, and receiving emails
- Managing email folders (inbox, sent, archive, drafts)
- Tracking conversation threads and reply states
- Syncing with external maildir sources (Gmail via mbsync)
- Rate limiting and duplicate detection
- Converting between markdown and MIME formats

The email system uses markdown files for storage, making emails version-controllable
and human-readable while supporting full MIME features including HTML rendering.

Example:
    Initialize and send an email::

        from gptmail.lib import AgentEmail

        agent = AgentEmail("/path/to/workspace", "agent@example.com")
        msg_id = agent.compose(
            to="user@example.com",
            subject="Test",
            content="Hello world!"
        )
        agent.send(msg_id)

    Check for unreplied emails::

        unreplied = agent.get_unreplied_emails()
        for msg_id, subject, sender in unreplied:
            print(f"Need to reply to: {subject} from {sender}")
"""

import email.charset
import logging
from typing import Dict, Tuple
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import Header
from email.message import EmailMessage, Message
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.policy import default
from email.utils import format_datetime, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Optional

import markdown

from gptmail.communication_utils.rate_limiting.limiters import RateLimiter
from gptmail.communication_utils.state.locks import FileLock, LockError
from gptmail.communication_utils.state.tracking import ConversationTracker, MessageState

logger = logging.getLogger(__name__)

# Use quoted-printable instead of base64 for UTF-8 email parts.
# Base64-encoded multipart emails are more likely to trigger spam filters.
_utf8_qp = email.charset.Charset("utf-8")
_utf8_qp.body_encoding = email.charset.QP


def _is_html(text: str) -> bool:
    """Detect if text content is already HTML."""
    stripped = text.strip()
    return stripped.startswith(("<html", "<!DOCTYPE", "<HTML", "<!doctype"))


def fix_list_spacing(markdown_text: str) -> str:
    """
    Add blank lines before lists if missing.

    This ensures the markdown library recognizes lists properly.
    Only adds blank line before the first item of each list.

    Args:
        markdown_text: The markdown text to process

    Returns:
        The markdown text with proper list spacing
    """
    lines = markdown_text.split("\n")
    result = []
    prev_was_list = False

    for i, line in enumerate(lines):
        # Check if this line is a list item
        is_list_item = bool(
            re.match(r"^\s*[-*+]\s+", line)  # Unordered list
            or re.match(r"^\s*\d+\.\s+", line)
        )  # Numbered list

        # Check if previous line is blank
        prev_is_blank = i == 0 or (i > 0 and lines[i - 1].strip() == "")

        # If this is a list item, previous line is not blank,
        # and we're not continuing a list, add blank line
        if is_list_item and not prev_is_blank and not prev_was_list and i > 0:
            result.append("")  # Add blank line before first list item

        result.append(line)
        prev_was_list = is_list_item

    return "\n".join(result)


class AgentEmail:
    """Handles email operations for agent communication.

    This class provides a complete email management system for AI agents, with support
    for composing, sending, receiving, and organizing emails. It uses a markdown-based
    storage format for easy version control and human readability.

    Attributes:
        workspace (Path): Path to the workspace directory.
        email_dir (Path): Path to the email storage directory (workspace/email).
        own_email (str): The agent's own email address (used to filter self-replies).
        external_maildir_inbox (Path): Path to external inbox maildir (for syncing).
        external_maildir_sent (Path): Path to external sent maildir (for syncing).
        processed_state_file (Path): File tracking processed email IDs.
        locks_dir (Path): Directory for lock files (prevents concurrent processing).
        rate_limiter (RateLimiter): Rate limiter for email sending operations.
        tracker (ConversationTracker): Tracks reply states and conversation threads.

    Example:
        >>> agent = AgentEmail("/home/user", "agent@example.com")
        >>> msg_id = agent.compose("user@example.com", "Hello", "Test message")
        >>> agent.send(msg_id)
    """

    def __init__(
        self,
        workspace_dir: str | Path,
        own_email: str | None = None,
        own_email_name: str | None = None,
    ):
        """Initialize with workspace directory.

        Args:
            workspace_dir: Path to the workspace directory
            own_email: The agent's own email address (used to avoid replying to self).
                      If None, will use AGENT_EMAIL environment variable (required)
            own_email_name: Display name for the agent's email (e.g. "Thomas, Michael's Assistant").
                      If None, will use AGENT_EMAIL_NAME environment variable (optional)
        """
        # Clear message index cache at start of sync
        # This ensures we rebuild the index with current state
        self._message_index_cache = {}

        self.workspace = Path(workspace_dir)
        self.email_dir = self.workspace / "email"
        # Ensure own_email is always a string - require AGENT_EMAIL env var if not provided
        self.own_email: str = own_email if own_email is not None else os.getenv("AGENT_EMAIL", "")
        if not self.own_email:
            raise ValueError(
                "own_email must be provided or AGENT_EMAIL environment variable must be set"
            )
        # Optional display name for the agent's email
        self.own_email_name: str | None = (
            own_email_name if own_email_name is not None else os.getenv("AGENT_EMAIL_NAME")
        )

        # External maildir paths (from mbsync)
        # Use environment variables with fallback to defaults for backward compatibility
        self.external_maildir_inbox = Path(
            os.getenv("MAILDIR_INBOX", str(Path.home() / ".local/share/mail/gmail/INBOX"))
        )
        self.external_maildir_sent = Path(
            os.getenv("MAILDIR_SENT", str(Path.home() / ".local/share/mail/gmail/Sent"))
        )

        # Build folder mappings from environment variables
        # Supports MAILDIR_INBOX, MAILDIR_SENT, MAILDIR_ARCHIVE, MAILDIR_<NAME>, etc.
        self.external_maildir_folders: dict[str, Path] = {
            "inbox": self.external_maildir_inbox,
            "sent": self.external_maildir_sent,
        }

        # Add any additional MAILDIR_* folders from environment
        for key, value in os.environ.items():
            if key.startswith("MAILDIR_") and key not in ("MAILDIR_INBOX", "MAILDIR_SENT"):
                folder_name = key[8:].lower()  # MAILDIR_ARCHIVE -> archive
                self.external_maildir_folders[folder_name] = Path(value)

        # State files for tracking
        self.processed_state_file = self.email_dir / "processed_state.txt"
        self.locks_dir = self.email_dir / "locks"

        # Rate limiting for email sending (1 request per second)
        self.rate_limiter = RateLimiter.for_platform("email")

        # Initialize conversation tracker for reply state management
        self.tracker = ConversationTracker(self.locks_dir)

        self._validate_structure()
        self._ensure_processed_state()

    def _validate_structure(self) -> None:
        """Ensure email directory structure exists.

        Raises:
            RuntimeError: If required directories (inbox, sent, archive, drafts, filters) are missing.
        """
        # Main folders for markdown messages
        required_dirs = ["inbox", "sent", "archive", "drafts", "filters"]
        for dir_name in required_dirs:
            dir_path = self.email_dir / dir_name
            if not dir_path.exists():
                raise RuntimeError(f"Required directory missing: {dir_path}")

    def _ensure_processed_state(self) -> None:
        """Ensure processed state file exists (tracker handles reply state)."""
        # Create locks directory (also used by ConversationTracker)
        self.locks_dir.mkdir(exist_ok=True)

        # Initialize processed state file if it doesn't exist
        if not self.processed_state_file.exists():
            self.processed_state_file.write_text("")

    def _is_replied(self, message_id: str) -> bool:
        """Check if we've already replied to this message.

        Args:
            message_id: The message ID to check (with or without angle brackets).

        Returns:
            True if the message has been replied to, False otherwise.
        """
        # Normalize message ID format
        normalized_id = message_id.strip("<>")
        conversation_id = "email"  # Single conversation for all emails

        msg_info = self.tracker.get_message_state(conversation_id, normalized_id)
        return msg_info is not None and msg_info.state == MessageState.COMPLETED

    def _mark_replied(self, original_message_id: str, reply_message_id: str) -> None:
        """Mark a message as replied to.

        Args:
            original_message_id: The message ID of the original message.
            reply_message_id: The message ID of the reply message.
        """
        # Normalize message ID format
        normalized_id = original_message_id.strip("<>")
        conversation_id = "email"  # Single conversation for all emails

        # Set state to COMPLETED with reply info
        self.tracker.set_message_state(
            conversation_id=conversation_id,
            message_id=normalized_id,
            state=MessageState.COMPLETED,
        )

    def _mark_no_reply_needed(self, message_id: str, reason: str = "no reply needed") -> None:
        """Mark a message as processed but no reply needed.

        Args:
            message_id: The message ID to mark.
            reason: Optional reason for not replying (default: "no reply needed").
        """
        # Normalize message ID format
        normalized_id = message_id.strip("<>")
        conversation_id = "email"  # Single conversation for all emails

        # Set state to NO_REPLY_NEEDED
        self.tracker.set_message_state(
            conversation_id=conversation_id,
            message_id=normalized_id,
            state=MessageState.NO_REPLY_NEEDED,
        )

    def _is_completed(self, message_id: str) -> bool:
        """Check if message has been completed (replied or marked as no reply needed).

        Args:
            message_id: The message ID to check.

        Returns:
            True if the message is completed (replied or no reply needed), False otherwise.
        """
        # Normalize message ID format
        normalized_id = message_id.strip("<>")
        conversation_id = "email"  # Single conversation for all emails

        msg_info = self.tracker.get_message_state(conversation_id, normalized_id)
        return msg_info is not None and msg_info.state in (
            MessageState.COMPLETED,
            MessageState.NO_REPLY_NEEDED,
        )

    # TODO: this should probably return some list[Message] or list[EmailMessage] type instead of a list of tuples
    # TODO: this should probably sort the emails by timestamp in some suitable order (or make it easy for consumers to sort by using an Message type)
    def get_unreplied_emails(self, folders: list[str] | None = None) -> list[tuple[str, str, str]]:
        """Get list of emails that haven't been replied to.

        Args:
            folders: List of folders to scan (default: ["inbox"])
                     Pass ["inbox", "archive"] to also check archived emails.

        Returns:
            List of (message_id, subject, sender) tuples for unreplied emails
        """
        if folders is None:
            # Default to inbox only - caller can pass ["inbox", "archive"] if needed
            folders = ["inbox"]

        unreplied = []
        seen_message_ids: set[str] = set()  # Avoid duplicates across folders

        for folder in folders:
            folder_dir = self.email_dir / folder
            if not folder_dir.exists():
                continue

            for email_file in folder_dir.glob("*.md"):
                try:
                    content = email_file.read_text()

                    # Extract message details
                    message_id_match = re.search(r"Message-ID: (<[^>]+>)", content)
                    subject_match = re.search(r"Subject: (.+)", content)
                    from_match = re.search(r"From: (.+)", content)
                    to_match = re.search(r"To: (.+)", content)

                    if not (message_id_match and subject_match and from_match):
                        continue

                    message_id = message_id_match.group(1)

                    # Skip if we've already seen this message in another folder
                    if message_id in seen_message_ids:
                        continue
                    seen_message_ids.add(message_id)

                    subject = subject_match.group(1)
                    from_line = from_match.group(1)

                    # Skip if not addressed to the agent's email
                    # (only process emails sent TO the agent, not CC'd or other recipients)
                    if to_match and self.own_email:
                        to_line = to_match.group(1).lower()
                        if self.own_email.lower() not in to_line:
                            continue

                    # Extract email from "Name <email>" format
                    sender = from_line
                    if "<" in from_line and ">" in from_line:
                        start = from_line.find("<")
                        end = from_line.find(">", start)
                        if start != -1 and end != -1:
                            sender = from_line[start + 1 : end].strip()

                    # Skip if already completed (replied or marked as no reply needed)
                    if self._is_completed(message_id):
                        continue

                    # Skip if not from allowlisted sender
                    if not self._is_allowlisted_sender(sender):
                        continue

                    # Skip notification-type emails that don't need replies
                    if self._is_notification_email(subject, content):
                        continue

                    # Skip if we already replied to this email
                    # (check sent folder for In-Reply-To pointing to this message)
                    sent_dir = self.email_dir / "sent"
                    already_replied = False
                    for sent_file in sent_dir.glob("*.md"):
                        try:
                            sent_content = sent_file.read_text()
                            if f"In-Reply-To: {message_id}" in sent_content:
                                already_replied = True
                                break
                        except Exception:
                            continue
                    if already_replied:
                        continue

                    unreplied.append((message_id, subject, sender))

                except Exception as e:
                    print(f"Error processing {email_file}: {e}")
                    continue

        return unreplied

    def _is_allowlisted_sender(self, sender: str) -> bool:
        """Check if sender is allowlisted for auto-responses.

        Normalizes email addresses by removing +tags and checks against
        allowlist from EMAIL_ALLOWLIST env var. Self-emails are automatically excluded.

        Args:
            sender: The email address to check.

        Returns:
            True if the sender is allowlisted and not self, False otherwise.
        """
        # Read from environment variable, fall back to defaults
        env_allowlist = os.getenv("EMAIL_ALLOWLIST", "")
        if env_allowlist == "*":
            # Wildcard - allow all senders (except self)
            pass  # Will check self below
        elif env_allowlist:
            allowlisted = [e.strip() for e in env_allowlist.split(",") if e.strip()]
        else:
            # Default allowlist if not configured
            allowlisted = [
                "erik@bjareho.lt",
                "erik.bjareholt@gmail.com",
                "filip.harald@gmail.com",
                "rickard.edic@gmail.com",
            ]

        # Remove +tag from email address for comparison
        clean_sender = sender.lower()
        if "+" in clean_sender and "@" in clean_sender:
            local, domain = clean_sender.split("@", 1)
            if "+" in local:
                local = local.split("+")[0]
                clean_sender = f"{local}@{domain}"

        # Don't reply to emails from ourselves
        clean_own_email = self.own_email.lower()
        if "+" in clean_own_email and "@" in clean_own_email:
            local, domain = clean_own_email.split("@", 1)
            if "+" in local:
                local = local.split("+")[0]
                clean_own_email = f"{local}@{domain}"

        if clean_sender == clean_own_email:
            return False

        # Check wildcard mode
        if os.getenv("EMAIL_ALLOWLIST", "") == "*":
            return True

        # Check if sender's domain matches any allowlisted domain
        sender_domain = clean_sender.split("@")[-1] if "@" in clean_sender else ""
        for allowed in allowlisted:
            allowed_lower = allowed.lower()
            # Match full email OR domain
            if clean_sender == allowed_lower or sender_domain == allowed_lower:
                return True
        return False

    def _is_notification_email(self, subject: str, content: str) -> bool:
        """Check if email is a notification that doesn't need a reply.

        Matches against common notification patterns like security alerts,
        verification codes, GitHub notifications, etc.

        Args:
            subject: The email subject line.
            content: The full email content.

        Returns:
            True if the email matches notification patterns, False otherwise.
        """
        notification_patterns = [
            r"New login to",
            r"security alert",
            r"verification code",
            r"password reset",
            r"account activity",
            r"github.*notification",
            r"accepted your.*invite",
            r"no-?reply",
            r"automated.*message",
        ]

        subject_lower = subject.lower()
        content_lower = content.lower()

        for pattern in notification_patterns:
            if re.search(pattern, subject_lower) or re.search(pattern, content_lower):
                return True

        return False

    def process_unreplied_emails(self, callback_func, folders: list[str] | None = None) -> int:
        """Process unreplied emails with locking to prevent duplicates.

        Args:
            callback_func: Function to call for each unreplied email,
                          should accept (message_id, subject, sender)
            folders: List of folders to scan (default: ["inbox"])

        Returns:
            Number of emails processed
        """
        unreplied = self.get_unreplied_emails(folders=folders)
        processed_count = 0

        for message_id, subject, sender in unreplied:
            # Try to acquire lock (non-blocking with timeout=0)
            lock_file = self.locks_dir / f"{self._format_filename(message_id)}.lock"
            try:
                with FileLock(lock_file, timeout=0):
                    print(f"Processing unreplied email from {sender}: {subject}")
                    callback_func(message_id, subject, sender)
                    processed_count += 1
            except LockError:
                print(f"Skipping {message_id} (already being processed)")
            except Exception as e:
                print(f"Error processing {message_id}: {e}")

        return processed_count

    def _generate_message_id(self) -> str:
        """Generate a unique message ID.

        Returns:
            A unique message ID in the format <uuid>.
        """
        unique_id = str(uuid.uuid4())
        return f"<{unique_id}>"

    def _format_filename(self, message_id: str) -> str:
        """Convert message ID to filename.

        Strips angle brackets and replaces problematic characters
        to create a filesystem-safe filename.

        Args:
            message_id: The message ID to convert (with or without angle brackets).

        Returns:
            A filesystem-safe filename with .md extension.
        """
        # Strip < > and replace problematic characters
        filename = message_id.strip("<>").replace("@", "_at_").replace("/", "_")
        return f"{filename}.md"

    def compose(
        self,
        to: str,
        subject: str,
        content: str,
        from_address: Optional[str] = None,
        reply_to: Optional[str] = None,
        references: Optional[list[str]] = None,
    ) -> str:
        """Create new email in drafts directory.

        Args:
            to: Recipient email address
            subject: Email subject
            content: Message content in Markdown
            from_address: Optional custom sender address (uses AGENT_EMAIL env var)
            reply_to: Optional message ID being replied to
            references: Optional list of referenced message IDs

        Returns:
            Message ID of the created draft

        Raises:
            ValueError: If content is empty or invalid
        """
        if not content or not content.strip():
            raise ValueError("Message content cannot be empty")

        message_id = self._generate_message_id()
        now = datetime.now(timezone.utc)

        # Use custom from_address or default
        sender_email = from_address or self.own_email
        # Format sender with display name if available
        # Display name must be quoted if it contains special characters (RFC 5322)
        if self.own_email_name and not from_address:
            # Always quote the display name to handle commas, apostrophes, etc.
            sender = f'"{self.own_email_name}" <{sender_email}>'
        else:
            sender = sender_email

        # Build headers
        headers = [
            "MIME-Version: 1.0",
            f"From: {sender}",
            f"To: {to}",
            f"Date: {format_datetime(now)}",
            f"Subject: {subject}",
            f"Message-ID: {message_id}",
            "Content-Type: text/html; charset=utf-8",
        ]

        # Add threading headers if needed
        if reply_to:
            # Ensure reply_to has angle brackets
            reply_to_formatted = reply_to if reply_to.startswith("<") else f"<{reply_to}>"
            headers.append(f"In-Reply-To: {reply_to_formatted}")
        if references:
            # Ensure each reference has angle brackets
            formatted_refs = []
            for ref in references:
                ref_formatted = ref if ref.startswith("<") else f"<{ref}>"
                formatted_refs.append(ref_formatted)
            headers.append(f"References: {' '.join(formatted_refs)}")

        # Ensure content has no leading/trailing whitespace
        content = content.strip()

        # Combine headers and content with proper separators
        message = "\n".join(headers) + "\n\n" + content + "\n"

        # Validate format
        try:
            # Test that we can parse it back
            test_headers, test_body = self._markdown_to_email(message)
            if not test_headers or not test_body:
                raise ValueError("Failed to validate message format")
        except Exception as e:
            raise ValueError(f"Invalid message format: {e}")

        # Save to drafts
        filename = self._format_filename(message_id)
        draft_path = self.email_dir / "drafts" / filename
        draft_path.write_text(message)

        return message_id

    def send(self, message_id: str) -> None:
        """Send email (move from drafts to sent and deliver).

        Args:
            message_id: ID of message to send
        """
        filename = self._format_filename(message_id)
        draft_path = self.email_dir / "drafts" / filename
        sent_path = self.email_dir / "sent" / filename

        if not draft_path.exists():
            raise ValueError(f"Draft not found: {message_id}")

        # Read content before moving
        content = draft_path.read_text()

        # Actually send via msmtp first
        try:
            # Validate msmtp is available
            if not self._validate_msmtp_config():
                raise RuntimeError("msmtp is not properly installed or configured")

            # Extract headers and body for sending
            headers, body = self._markdown_to_email(content)
            recipient = headers.get("To", "")
            sender = headers.get("From") or self.own_email

            if not recipient:
                raise ValueError("No recipient found in email headers")

            # Detect if body is already HTML (e.g. from a script that
            # generates HTML directly). If so, send as simple text/html
            # without multipart/alternative or Content-Transfer-Encoding.
            # This matches what works with raw msmtp and avoids garbling
            # HTML through markdown.markdown().
            body_is_html = _is_html(body)

            # Build common header values
            from_header = headers.get("From") or sender
            from_name, from_email = parseaddr(from_header)
            if from_name:
                if from_name.isascii():
                    from_value = f'"{from_name}" <{from_email}>'
                else:
                    encoded_name = Header(from_name, "utf-8").encode()
                    from_value = f"{encoded_name} <{from_email}>"
            else:
                from_value = from_email

            subject = headers.get("Subject", "")
            date_value = headers.get("Date", format_datetime(datetime.now(timezone.utc)))
            message_id = headers.get("Message-ID", "")

            if body_is_html:
                # Build a simple text/html message with raw UTF-8 body.
                # No multipart, no Content-Transfer-Encoding header.
                # This is the format that reliably passes spam filters.
                header_lines = [
                    "MIME-Version: 1.0",
                    f"From: {from_value}",
                    f"To: {recipient}",
                    f"Date: {date_value}",
                    f"Subject: {subject}",
                    f"Message-ID: {message_id}",
                    "Content-Type: text/html; charset=utf-8",
                ]
                if "In-Reply-To" in headers:
                    header_lines.append(f"In-Reply-To: {headers['In-Reply-To']}")
                if "References" in headers:
                    header_lines.append(f"References: {headers['References']}")

                raw_message = "\r\n".join(header_lines) + "\r\n\r\n" + body
                mime_bytes = raw_message.encode("utf-8")
            else:
                # Markdown body — create multipart/alternative with both
                # plain text and HTML versions.
                msg = MIMEMultipart("alternative")
                msg["From"] = from_value
                msg["To"] = recipient
                if subject.isascii():
                    msg["Subject"] = subject
                else:
                    msg["Subject"] = Header(subject, "utf-8").encode()
                msg["Date"] = date_value
                msg["Message-ID"] = message_id
                if "In-Reply-To" in headers:
                    msg["In-Reply-To"] = headers["In-Reply-To"]
                if "References" in headers:
                    msg["References"] = headers["References"]

                plain_text = body
                html_body = markdown.markdown(
                    fix_list_spacing(body),
                    extensions=["extra", "codehilite", "sane_lists"],
                )
                # Use quoted-printable instead of base64 to reduce spam score.
                # Note: MIMEText._charset accepts Charset objects at runtime
                # despite type stubs declaring str | None.
                msg.attach(MIMEText(plain_text, "plain", _charset=_utf8_qp))  # type: ignore[arg-type]
                msg.attach(MIMEText(html_body, "html", _charset=_utf8_qp))  # type: ignore[arg-type]
                mime_bytes = msg.as_string().encode("utf-8")

            # Get appropriate msmtp account
            # Extract just email address from "Name <email>" format for account lookup
            _, sender_email = parseaddr(sender)
            account = self._get_msmtp_account_for_address(sender_email or sender)

            # Build msmtp command
            # Extract just the email address from "Name <email>" format
            _, recipient_email = parseaddr(recipient)
            if not recipient_email:
                recipient_email = recipient  # Fallback if parsing fails

            msmtp_cmd = ["msmtp"]
            if account:
                msmtp_cmd.extend(["-a", account])
            msmtp_cmd.append(recipient_email)

            # Check rate limit before sending
            if not self.rate_limiter.can_proceed():
                wait_time = self.rate_limiter.time_until_ready()
                print(f"Rate limit reached. Waiting {wait_time:.1f}s...")
                if not self.rate_limiter.wait_if_needed(max_wait=60):
                    raise RuntimeError("Rate limit timeout - could not send within 60s")

            # Send via msmtp
            subprocess.run(
                msmtp_cmd,
                input=mime_bytes,
                capture_output=True,
                check=True,
                timeout=30,
            )
            print(f"Email delivered via SMTP to {recipient} (from {sender}) as HTML")
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.decode() if e.stderr else str(e)
            print(f"Failed to send {message_id}: {error_msg}")
            raise
        except subprocess.TimeoutExpired:
            print(f"Timeout sending {message_id}: SMTP server took too long to respond")
            raise
        except Exception as e:
            print(f"Error sending {message_id}: {e}")
            raise

        # Move markdown file to sent folder (only after successful sending)
        draft_path.rename(sent_path)

        # If this is a reply, mark the original message as replied to
        headers, _ = self._markdown_to_email(content)
        in_reply_to = headers.get("In-Reply-To")
        if in_reply_to:
            try:
                self._mark_replied(in_reply_to, message_id)
                print(f"Marked original message {in_reply_to} as replied to by {message_id}")
            except Exception as e:
                print(f"Warning: Failed to mark original message as replied: {e}")

        print(f"Message {message_id} sent successfully and archived")

    def receive(self, message_data: str) -> str:
        """Process received email to inbox.

        Args:
            message_data: Full email message content

        Returns:
            Message ID of received message
        """
        # Extract message ID from headers
        match = re.search(r"Message-ID: (<[^>]+>)", message_data)
        if not match:
            raise ValueError("Message-ID header missing")
        message_id = match.group(1)

        # Save markdown version to inbox
        filename = self._format_filename(message_id)
        inbox_path = self.email_dir / "inbox" / filename
        inbox_path.write_text(message_data)

        return message_id

    def archive(self, message_id: str) -> None:
        """Move message to archive.

        Args:
            message_id: ID of message to archive
        """
        filename = self._format_filename(message_id)

        # Check inbox, sent, and drafts folders
        source_paths = [
            self.email_dir / "inbox" / filename,
            self.email_dir / "sent" / filename,
            self.email_dir / "drafts" / filename,
        ]

        for source_path in source_paths:
            if source_path.exists():
                archive_path = self.email_dir / "archive" / filename
                source_path.rename(archive_path)
                print(f"Message {message_id} archived")
                return

        raise ValueError(f"Message not found: {message_id}")

    def list_messages(self, folder: str = "inbox") -> list[tuple[str, str, datetime]]:
        """List messages in specified folder.

        Args:
            folder: Folder to list (inbox, sent, archive, drafts)

        Returns:
            List of (message_id, subject, date) tuples
        """
        folder_path = self.email_dir / folder
        if not folder_path.exists():
            raise ValueError(f"Invalid folder: {folder}")

        messages = []
        for file_path in folder_path.glob("*.md"):
            content = file_path.read_text()

            # Extract headers
            subject_match = re.search(r"Subject: (.+)", content)
            date_match = re.search(r"Date: (.+)", content)
            id_match = re.search(r"Message-ID: (<[^>]+>)", content)

            if subject_match and date_match and id_match:
                # Parse date, looks like: Fri, 07 Feb 2025 16:14:03 -0800
                date = self._parse_email_date(date_match.group(1))
                messages.append((id_match.group(1), subject_match.group(1), date))

        return sorted(messages, key=lambda m: m[2])  # Sort by date

    def read_message(self, message_id: str, include_thread: bool = False) -> str:
        """Read message content.

        Args:
            message_id: ID of message to read
            include_thread: If True, include full conversation thread

        Returns:
            Full message content, optionally with thread context
        """
        filename = self._format_filename(message_id)

        # Check all folders - all received emails are now in inbox
        folders = ["inbox", "sent", "archive", "drafts"]
        message_content = None
        for folder in folders:
            path = self.email_dir / folder / filename
            if path.exists():
                message_content = path.read_text()
                break

        if not message_content:
            raise ValueError(f"Message not found: {message_id}")

        if not include_thread:
            return message_content

        # Build and display the full thread
        return self._format_thread_display(message_id)

    def get_thread_messages(self, message_id: str) -> list[dict]:
        """Get all messages in the same thread as the given message.

        Args:
            message_id: Any message ID in the thread

        Returns:
            List of message dicts with id, headers, body, timestamp, and folder
        """
        # Find the root of the thread by following In-Reply-To chains backward
        thread_root = self._find_thread_root(message_id)

        # Collect all messages in the thread
        thread_messages: list[dict] = []
        self._collect_thread_messages(thread_root, thread_messages, set())

        # Sort by timestamp
        thread_messages.sort(key=lambda m: m["timestamp"])

        return thread_messages

    def _find_thread_root(self, message_id: str) -> str:
        """Find the root message of a thread by following In-Reply-To chains."""
        current_id = message_id
        last_valid_id = message_id  # Keep track of last valid message
        seen_ids = set()

        while current_id and current_id not in seen_ids:
            seen_ids.add(current_id)

            try:
                content = self.read_message(current_id)
                headers, _ = self._markdown_to_email(content)

                # Update last valid ID since we successfully read this message
                last_valid_id = current_id

                # If there's an In-Reply-To, follow it backward
                in_reply_to = headers.get("In-Reply-To")
                if in_reply_to:
                    # Clean up the message ID format
                    in_reply_to = in_reply_to.strip().strip("<>")
                    if not in_reply_to.startswith("<"):
                        in_reply_to = f"<{in_reply_to}>"
                    current_id = in_reply_to
                else:
                    # No parent found, this is the root
                    break

            except ValueError:
                # Message not found, return last valid message we found
                break

        return last_valid_id

    def _collect_thread_messages(
        self, message_id: str, collected: list[dict], seen: set[str]
    ) -> None:
        """Recursively collect all messages in a thread."""
        if message_id in seen:
            return

        seen.add(message_id)

        try:
            content = self.read_message(message_id)
            headers, body = self._markdown_to_email(content)

            # Parse timestamp for sorting
            timestamp = self._parse_email_date(headers.get("Date", ""))

            # Find which folder this message is in
            folder = self._find_message_folder(message_id)

            message_info = {
                "id": message_id,
                "headers": headers,
                "body": body,
                "timestamp": timestamp,
                "folder": folder,
                "content": content,
            }
            collected.append(message_info)

            # Find all replies to this message
            replies = self._find_replies_to(message_id)
            for reply_id in replies:
                self._collect_thread_messages(reply_id, collected, seen)

        except ValueError:
            # Message not found, skip
            pass

    def _find_replies_to(self, message_id: str) -> list[str]:
        """Find all messages that reply to the given message ID."""
        replies = []

        # Search all folders for messages with In-Reply-To pointing to message_id
        folders = ["inbox", "sent", "archive", "drafts"]
        for folder in folders:
            folder_path = self.email_dir / folder
            if not folder_path.exists():
                continue

            for email_file in folder_path.glob("*.md"):
                try:
                    content = email_file.read_text()
                    headers, _ = self._markdown_to_email(content)

                    in_reply_to = headers.get("In-Reply-To", "").strip().strip("<>")
                    clean_message_id = message_id.strip("<>")

                    if in_reply_to == clean_message_id:
                        reply_id = headers.get("Message-ID", "")
                        if reply_id:
                            replies.append(reply_id)

                except Exception:
                    logger.warning(f"Error processing {email_file} for replies")
                    continue

        return replies

    def _find_message_folder(self, message_id: str) -> str:
        """Find which folder a message is stored in."""
        filename = self._format_filename(message_id)
        folders = ["inbox", "sent", "archive", "drafts"]

        for folder in folders:
            path = self.email_dir / folder / filename
            if path.exists():
                return folder

        return "unknown"

    def _parse_email_date(self, date_str: str) -> datetime:
        """Parse email date string to datetime object."""
        if not date_str:
            return datetime.min.replace(tzinfo=timezone.utc)

        try:
            # Try parsing RFC 2822 format

            return parsedate_to_datetime(date_str)
        except Exception:
            # Fallback to current time if parsing fails
            return datetime.now(timezone.utc)

    def _format_thread_display(self, message_id: str) -> str:
        """Format a complete thread for display."""
        thread_messages = self.get_thread_messages(message_id)

        if not thread_messages:
            return self.read_message(message_id)

        output = []
        output.append("=" * 80)
        output.append(f"CONVERSATION THREAD ({len(thread_messages)} messages)")
        output.append("=" * 80)
        output.append("")

        for i, msg in enumerate(thread_messages):
            # Header for each message
            headers = msg["headers"]
            is_current = msg["id"] == message_id

            marker = ">>> CURRENT MESSAGE <<<" if is_current else ""
            output.append(f"[{i + 1}/{len(thread_messages)}] {marker}")
            output.append(f"From: {headers.get('From', 'Unknown')}")
            output.append(f"To: {headers.get('To', 'Unknown')}")
            output.append(f"Date: {headers.get('Date', 'Unknown')}")
            output.append(f"Subject: {headers.get('Subject', 'No Subject')}")
            output.append(f"Folder: {msg['folder']}")
            output.append("-" * 60)

            # Message body
            body = msg["body"].strip()
            if body:
                output.append(body)
            else:
                output.append("(Empty message)")

            output.append("")
            output.append("=" * 60)
            output.append("")

        return "\n".join(output)

    def _markdown_to_email(self, content: str) -> tuple[dict[str, str], str]:
        """Convert markdown format to email headers and body.

        Args:
            content: Full markdown message content

        Returns:
            Tuple of (headers dict, body string)

        Raises:
            ValueError: If message format is invalid
        """
        if not content or not content.strip():
            raise ValueError("Empty message")

        # Split headers and body
        parts = content.split("\n\n", 1)
        if len(parts) != 2:
            print(f"Warning: Message missing body separator, content: {content[:100]}...")
            # Try to handle messages with just headers
            if "\n" in content:
                return self._parse_headers(content), ""
            raise ValueError("Invalid message format")

        headers_text, body = parts
        return self._parse_headers(headers_text), body

    def _parse_headers(self, headers_text: str) -> dict[str, str]:
        """Parse email headers from text.

        Args:
            headers_text: Raw header text

        Returns:
            Dict of parsed headers
        """
        headers = {}
        current_key = None
        current_value = []

        for line in headers_text.split("\n"):
            if not line:
                continue

            # Handle header continuation
            if line[0].isspace():
                if current_key:
                    current_value.append(line.strip())
                continue

            # Save previous header if any
            if current_key and current_value:
                headers[current_key] = " ".join(current_value)
                current_value = []

            # Parse new header
            if ": " in line:
                current_key, value = line.split(": ", 1)
                current_value = [value]
            else:
                print(f"Warning: Invalid header line: {line}")

        # Save last header
        if current_key and current_value:
            headers[current_key] = " ".join(current_value)

        return headers

    def _validate_msmtp_config(self) -> bool:
        """Check if msmtp is properly configured and available.

        Returns:
            True if msmtp is available and configured, False otherwise
        """
        try:
            # Check if msmtp is installed
            subprocess.run(["msmtp", "--version"], capture_output=True, check=True, timeout=10)
            return True
        except (
            subprocess.CalledProcessError,
            FileNotFoundError,
            subprocess.TimeoutExpired,
        ):
            return False

    def _get_msmtp_account_for_address(self, from_address: str) -> Optional[str]:
        """Get the appropriate msmtp account for a given from address.

        Args:
            from_address: The sender email address

        Returns:
            The msmtp account name to use, or None for default account
        """
        # For now, use simple domain-based mapping
        # This could be enhanced to read msmtp config file
        if "gmail.com" in from_address:
            return "gmail"
        return None  # Use default account

    def _build_message_index(self, folder: str) -> Dict[str, Dict]:
        """Build an index of existing messages for fast duplicate detection.

        This eliminates the O(n²) problem by reading all existing messages once
        and building an in-memory index for O(1) lookups.

        Args:
            folder: The folder to index (inbox or sent)

        Returns:
            Dict mapping message keys to metadata for comparison
        """
        index = {}
        folder_path = self.email_dir / folder

        for existing_file in folder_path.glob("*.md"):
            try:
                content = existing_file.read_text()
                headers, existing_body = self._markdown_to_email(content)

                # Extract key metadata for comparison
                in_reply_to = headers.get("In-Reply-To", "").strip().strip("<>")
                references = headers.get("References", "").strip()
                subject = headers.get("Subject", "").strip()
                to_addr = headers.get("To", "").strip()
                from_addr = headers.get("From", "").strip()
                date_str = headers.get("Date", "").strip()

                # Parse date
                try:
                    msg_date = self._parse_email_date(date_str)
                except Exception:
                    msg_date = None

                # Get body snippet (first 200 chars, normalized)
                body_snippet = "".join(existing_body[:200].split()) if existing_body else ""

                # Create composite key from strong identifiers
                # Use In-Reply-To + Subject as primary key (most reliable for matching)
                key = f"{in_reply_to}|{subject}|{to_addr}"

                # Store metadata for comparison
                index[key] = {
                    "in_reply_to": in_reply_to,
                    "references": references,
                    "subject": subject,
                    "to": to_addr,
                    "from": from_addr,
                    "date": msg_date,
                    "body_snippet": body_snippet,
                    "file": existing_file,
                }

                # Also index by subject+from for inbox messages
                if folder == "inbox":
                    alt_key = f"{subject}|{from_addr}"
                    if alt_key not in index:  # Don't overwrite if exists
                        index[alt_key] = index[key]

            except Exception as e:
                logger.debug(f"Error indexing {existing_file}: {e}")
                continue

        logger.debug(f"Built message index for {folder}: {len(index)} entries")
        return index

    def _get_message_key(self, email_msg: EmailMessage, folder: str) -> Tuple[str, str]:
        """Get indexing keys for a message.

        Args:
            email_msg: The message to get keys for
            folder: The folder context (inbox or sent)

        Returns:
            Tuple of (primary_key, alt_key)
        """
        in_reply_to = email_msg.get("In-Reply-To", "").strip().strip("<>")
        subject = email_msg.get("Subject", "").strip()
        to_addr = email_msg.get("To", "").strip()
        from_addr = email_msg.get("From", "").strip()

        primary_key = f"{in_reply_to}|{subject}|{to_addr}"
        alt_key = f"{subject}|{from_addr}" if folder == "inbox" else None
        return primary_key, alt_key

    def _is_duplicate_message(self, email_msg: EmailMessage, folder: str) -> bool:
        """Check if a message is a duplicate based on content/metadata.

        This prevents duplicates when Gmail assigns a different Message-ID to sent emails.

        Args:
            email_msg: The email message to check
            folder: The folder to check in (inbox or sent)

        Returns:
            True if a duplicate is found, False otherwise
        """
        # Build index if not already built (happens once per sync)
        if not hasattr(self, "_message_index_cache"):
            self._message_index_cache = {}

        if folder not in self._message_index_cache:
            self._message_index_cache[folder] = self._build_message_index(folder)

        index = self._message_index_cache[folder]

        # Extract key metadata for comparison
        in_reply_to = email_msg.get("In-Reply-To", "").strip().strip("<>")
        references = email_msg.get("References", "").strip()
        subject = email_msg.get("Subject", "").strip()
        to_addr = email_msg.get("To", "").strip()
        from_addr = email_msg.get("From", "").strip()
        date_str = email_msg.get("Date", "").strip()

        # Parse date for comparison (allow 2 minute window for clock differences)
        try:
            msg_date = self._parse_email_date(date_str)
        except Exception:
            msg_date = None

        # Get message body for content comparison
        if email_msg.is_multipart():
            body = ""
            for part in email_msg.walk():
                if part.get_content_type() == "text/plain":
                    content = (
                        part.get_content()
                        if isinstance(part, EmailMessage)
                        else part.get_payload(decode=True)
                    )
                    if isinstance(content, bytes):
                        content = content.decode("utf-8", errors="replace")
                    body = content
                    break
        else:
            body = (
                email_msg.get_content()
                if isinstance(email_msg, EmailMessage)
                else email_msg.get_payload(decode=True)
            )
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="replace")

        # Get first 200 chars of body for comparison (ignore whitespace differences)
        body_snippet = "".join(body[:200].split()) if body else ""

        # Check index for potential duplicates using keys
        primary_key, alt_key = self._get_message_key(email_msg, folder)

        candidates = []
        if primary_key in index:
            candidates.append(index[primary_key])
        if alt_key and alt_key in index:
            candidates.append(index[alt_key])

        # Check each candidate for actual duplicate
        for existing in candidates:
            # Compare key fields
            matches = []

            # For sent emails, In-Reply-To and References are strong indicators
            if in_reply_to and existing["in_reply_to"] == in_reply_to:
                matches.append("in_reply_to")

            if references and existing["references"] == references:
                matches.append("references")

            # Subject match (exact)
            if subject and existing["subject"] == subject:
                matches.append("subject")

            # To/From match (normalize by removing quotes and extra whitespace)
            def normalize_addr(addr):
                """Normalize email address by removing quotes and extra whitespace."""
                return addr.strip().strip('"').strip()

            if to_addr and normalize_addr(existing["to"]) == normalize_addr(to_addr):
                matches.append("to")

            if from_addr and normalize_addr(existing["from"]) == normalize_addr(from_addr):
                matches.append("from")

            # Date match (within 2 minutes)
            if msg_date and existing["date"]:
                time_diff = abs((msg_date - existing["date"]).total_seconds())
                if time_diff < 120:  # 2 minutes
                    matches.append("date")

            # Body content match (first 200 chars, normalized)
            if (
                body_snippet
                and existing["body_snippet"]
                and body_snippet == existing["body_snippet"]
            ):
                matches.append("body")

            # Consider it a duplicate if we have strong matches
            # For sent emails: In-Reply-To + Subject + To + Date is very strong indicator
            #                  (body may differ due to Gmail processing)
            # For inbox emails: need stricter matching including body
            if folder == "sent":
                # Strong match: In-Reply-To + Date + (Subject or Body)
                if (
                    "in_reply_to" in matches
                    and "date" in matches
                    and ("subject" in matches or "body" in matches)
                ):
                    return True
                # Alternative: Subject + To + Date (for emails without In-Reply-To)
                if "subject" in matches and "to" in matches and "date" in matches:
                    return True
            else:  # inbox
                if "in_reply_to" in matches and "body" in matches:
                    return True
                if (
                    "subject" in matches
                    and "from" in matches
                    and "body" in matches
                    and "date" in matches
                ):
                    return True

        return False

    def _get_sync_state_path(self, folder: str) -> Path:
        """Get path to sync state file for a folder."""
        return self.email_dir / f".sync_state_{folder}.json"

    def _load_sync_state(self, folder: str) -> set[str]:
        """Load set of already-processed maildir filenames."""
        state_path = self._get_sync_state_path(folder)
        if state_path.exists():
            import json

            try:
                with open(state_path) as f:
                    data = json.load(f)
                    return set(data.get("processed_files", []))
            except (json.JSONDecodeError, KeyError, OSError):
                return set()
        return set()

    def _prune_sync_state(
        self, folder: str, processed_files: set[str], maildir_folder: Path
    ) -> set[str]:
        """Prune stale entries from sync state.

        Removes entries for maildir files that no longer exist, preventing
        unbounded state growth. Returns the pruned set.
        """
        if not processed_files:
            return processed_files

        # Build set of currently existing maildir filenames (normalized)
        existing_maildir_files: set[str] = set()
        for subdir in ["cur", "new"]:
            subdir_path = maildir_folder / subdir
            if subdir_path.exists():
                for msg_path in subdir_path.glob("*"):
                    if msg_path.name != ".gitkeep":
                        # Normalize filename by stripping flags
                        normalized = msg_path.name.split(":")[0]
                        existing_maildir_files.add(normalized)

        # Remove entries that no longer exist
        stale_entries = processed_files - existing_maildir_files
        if stale_entries:
            pruned = processed_files - stale_entries
            print(
                f"Pruned {len(stale_entries)} stale entries from sync state "
                "(files no longer in maildir)"
            )
            return pruned
        return processed_files

    def _save_sync_state(self, folder: str, processed_files: set[str]) -> None:
        """Save set of processed maildir filenames."""
        import json

        state_path = self._get_sync_state_path(folder)
        try:
            with open(state_path, "w") as f:
                json.dump({"processed_files": sorted(processed_files)}, f)
        except OSError as e:
            print(f"Warning: Could not save sync state: {e}")

    def sync_from_maildir(self, folder: str) -> None:
        """Sync messages from external maildir to markdown format.

        This imports emails from the external maildir (e.g., Gmail via mbsync)
        into the workspace storage as markdown files.

        Optimized for incremental sync: tracks which maildir files have been
        processed to avoid re-reading unchanged files on subsequent syncs.
        For large mailboxes (10k+ emails), this reduces sync time from 60-90s
        to under 5s when no new mail.

        Args:
            folder: Folder to sync - supports "inbox" and "sent" (configure via MAILDIR_INBOX/MAILDIR_SENT env vars)
        """

        # Choose appropriate external maildir from folder mappings
        if folder not in self.external_maildir_folders:
            available = ", ".join(sorted(self.external_maildir_folders.keys()))
            raise ValueError(
                f"Unknown folder '{folder}'. Available folders: {available}. "
                f"Add MAILDIR_{folder.upper()} environment variable to configure."
            )
        maildir_folder = self.external_maildir_folders[folder]

        if not maildir_folder.exists():
            print(f"Warning: Maildir folder does not exist: {maildir_folder}")
            return

        success = 0
        failed = 0
        skipped = 0
        already_processed = 0

        # OPTIMIZATION 1: Load sync state to skip already-processed files
        processed_files = self._load_sync_state(folder)
        initial_processed_count = len(processed_files)

        # OPTIMIZATION 2: Pre-load existing markdown filenames into a set
        # This avoids N filesystem existence checks (O(1) set lookup instead)
        save_dir = self.email_dir / folder
        save_dir.mkdir(parents=True, exist_ok=True)
        existing_files = {f.name for f in save_dir.glob("*.md")}

        # Process messages in both cur/ (read) and new/ (unread) directories
        for subdir in ["cur", "new"]:
            subdir_path = maildir_folder / subdir
            if not subdir_path.exists():
                continue

            for msg_path in subdir_path.glob("*"):
                if msg_path.name == ".gitkeep":
                    continue

                # OPTIMIZATION 1: Skip already-processed maildir files entirely
                # This avoids reading/parsing 11k files on subsequent syncs
                # Normalize filename by stripping flags (files move from new/ to cur/ with flags like :2,S)
                maildir_filename = msg_path.name.split(":")[0]
                if maildir_filename in processed_files:
                    already_processed += 1
                    continue

                try:
                    # Parse flags from filename (only relevant for cur/)
                    flags = ""
                    if subdir == "cur" and ":" in msg_path.name:
                        _, flags = msg_path.name.split(":", 1)

                    # Read message with proper policy
                    msg_bytes = msg_path.read_bytes()
                    email_msg = message_from_bytes(msg_bytes, policy=default)

                    # Get Message-ID
                    message_id = email_msg["Message-ID"]
                    if not message_id:
                        print(f"Warning: No Message-ID in {msg_path}")
                        failed += 1
                        # Still mark as processed to avoid re-trying
                        processed_files.add(maildir_filename)
                        continue

                    # Check if we already have this message in any folder
                    filename = self._format_filename(message_id)
                    filename = filename.replace("/", "_")

                    # OPTIMIZATION 2: Use pre-loaded set instead of filesystem check
                    md_path = save_dir / filename

                    if filename in existing_files:
                        skipped += 1
                        # Mark as processed so we don't re-check next time
                        processed_files.add(maildir_filename)
                        continue

                    # Check for duplicates by content/metadata (prevents Gmail Message-ID reassignment duplicates)
                    if self._is_duplicate_message(email_msg, folder):
                        skipped += 1
                        # Mark as processed to avoid re-checking on next sync
                        processed_files.add(maildir_filename)
                        print(
                            f"Skipping duplicate message (different Message-ID): {message_id[:50]}..."
                        )
                        continue

                    # Convert to markdown format
                    headers = []
                    for key in [
                        "MIME-Version",
                        "From",
                        "To",
                        "Date",
                        "Subject",
                        "Message-ID",
                        "In-Reply-To",
                        "References",
                        "Content-Type",
                    ]:
                        if key in email_msg:
                            headers.append(f"{key}: {email_msg[key]}")

                    # Handle multipart messages
                    if email_msg.is_multipart():
                        # Try to find text/plain part first
                        text_part: Message | None = None
                        for part in email_msg.walk():
                            if part.get_content_type() == "text/plain":
                                text_part = part
                                break

                        # If no text/plain, try text/html
                        if not text_part:
                            for part in email_msg.walk():
                                if part.get_content_type() == "text/html":
                                    text_part = part
                                    break

                        if text_part:
                            if isinstance(text_part, EmailMessage):
                                body = text_part.get_content()
                            else:
                                body = text_part.get_payload(decode=True)
                                if isinstance(body, bytes):
                                    body = body.decode("utf-8", errors="replace")
                        else:
                            print(f"Warning: No text content found in {message_id}")
                            body = ""
                    else:
                        # Single part message
                        if isinstance(email_msg, EmailMessage):
                            body = email_msg.get_content()
                        else:
                            body = email_msg.get_payload(decode=True)
                            if isinstance(body, bytes):
                                body = body.decode("utf-8", errors="replace")

                    # Ensure body is a string
                    if isinstance(body, bytes):
                        body = body.decode("utf-8", errors="replace")
                    elif not isinstance(body, str):
                        body = str(body)

                    # Remove null bytes and normalize newlines
                    body = body.replace("\0", "").replace("\r\n", "\n")

                    # Combine into markdown format
                    content = "\n".join(headers) + "\n\n" + body

                    # Ensure the directory exists
                    save_dir.mkdir(parents=True, exist_ok=True)

                    # Save the file
                    md_path = save_dir / filename
                    md_path.write_text(content)
                    success += 1

                    # Track successful sync for incremental optimization
                    processed_files.add(maildir_filename)
                    existing_files.add(filename)

                    # Store flags for future use
                    if flags:
                        print(f"Found flags for {message_id}: {flags}")

                    # For sent emails, extract In-Reply-To to mark original as replied
                    if folder == "sent" and "In-Reply-To" in email_msg:
                        in_reply_to = email_msg["In-Reply-To"]
                        if in_reply_to:
                            self._mark_replied(in_reply_to, message_id)
                            print(f"Marked {in_reply_to} as replied by {message_id}")

                except Exception as e:
                    print(f"Error processing {msg_path}: {e}")
                    failed += 1
                    # Still mark as processed to avoid re-trying on next sync
                    processed_files.add(maildir_filename)
                    continue

        # Prune stale entries and save sync state for incremental optimization
        processed_files = self._prune_sync_state(folder, processed_files, maildir_folder)
        if len(processed_files) != initial_processed_count:
            self._save_sync_state(folder, processed_files)

        # Report results with optimization stats
        if already_processed > 0:
            print(
                f"Synced {folder}: {success} new, {failed} failed, {skipped} skipped (already exist), "
                f"{already_processed} skipped (previously synced)"
            )
        else:
            print(
                f"Synced {folder}: {success} succeeded, {failed} failed, {skipped} skipped (already exist)"
            )

    def export_to_maildir(self, folder: str, dest_maildir: Path) -> dict[str, int | str]:
        """Export messages from markdown format to maildir.

        This exports emails from the workspace storage as markdown files
        to a maildir directory that can be read by mail clients like neomutt.

        Args:
            folder: Folder to export - supports "inbox", "sent", "drafts", "archive"
            dest_maildir: Path to destination maildir directory

        Returns:
            dict: Statistics with 'success', 'failed', 'skipped' counts
        """
        import socket
        import time

        # Map folder to source directory
        folder_map = {
            "inbox": self.email_dir / "inbox",
            "sent": self.email_dir / "sent",
            "drafts": self.email_dir / "drafts",
            "archive": self.email_dir / "archive",
        }

        if folder not in folder_map:
            raise ValueError(f"Unknown folder: {folder}. Supported: {list(folder_map.keys())}")

        source_dir = folder_map[folder]

        if not source_dir.exists():
            return {
                "success": 0,
                "failed": 0,
                "skipped": 0,
                "error": f"Source folder does not exist: {source_dir}",
            }

        # Create maildir structure
        for subdir in ["cur", "new", "tmp"]:
            (dest_maildir / subdir).mkdir(parents=True, exist_ok=True)

        success = 0
        failed = 0
        skipped = 0

        hostname = socket.gethostname()

        # Export each markdown file to maildir format
        for md_file in source_dir.glob("*.md"):
            try:
                # Read markdown content
                content = md_file.read_text()

                # Generate unique maildir filename
                # Format: <timestamp>.<unique>.<hostname>:2,<flags>
                # :2,S means Seen (read) flag
                timestamp = int(time.time() * 1000)  # milliseconds for uniqueness
                unique_id = md_file.stem
                filename = f"{timestamp}.{unique_id}.{hostname}:2,S"

                # Write to maildir cur/ directory (for read messages)
                dest_file = dest_maildir / "cur" / filename
                dest_file.write_text(content)

                success += 1

            except Exception as e:
                print(f"Failed to export {md_file.name}: {e}")
                failed += 1
                continue

        print(f"Exported {folder}: {success} succeeded, {failed} failed, {skipped} skipped")

        return {
            "success": success,
            "failed": failed,
            "skipped": skipped,
        }

    def import_from_maildir(self, source_maildir: Path, folder: str) -> dict[str, int | str]:
        """Import messages from maildir format to markdown.

        This imports emails from a maildir directory (as used by mail clients)
        to the workspace storage as markdown files.

        Args:
            source_maildir: Path to source maildir directory
            folder: Destination folder - supports "inbox", "sent", "drafts", "archive"

        Returns:
            dict: Statistics with 'success', 'failed', 'skipped' counts
        """
        # Map folder to destination directory
        folder_map = {
            "inbox": self.email_dir / "inbox",
            "sent": self.email_dir / "sent",
            "drafts": self.email_dir / "drafts",
            "archive": self.email_dir / "archive",
        }

        if folder not in folder_map:
            raise ValueError(f"Unknown folder: {folder}. Supported: {list(folder_map.keys())}")

        dest_dir = folder_map[folder]
        dest_dir.mkdir(parents=True, exist_ok=True)

        success = 0
        failed = 0
        skipped = 0

        # Import from both cur/ (read) and new/ (unread) directories
        for subdir in ["cur", "new"]:
            maildir_subdir = source_maildir / subdir

            if not maildir_subdir.exists():
                continue

            # Process each maildir file
            for maildir_file in maildir_subdir.iterdir():
                if not maildir_file.is_file():
                    continue

                try:
                    # Read maildir content
                    content = maildir_file.read_text()

                    # Parse to extract Message-ID
                    # Split headers and body
                    if "\n\n" not in content:
                        print(f"Invalid format (no header/body separator): {maildir_file.name}")
                        failed += 1
                        continue

                    headers_part, _ = content.split("\n\n", 1)

                    # Extract Message-ID
                    message_id = None
                    for line in headers_part.split("\n"):
                        if line.lower().startswith("message-id:"):
                            message_id = line.split(":", 1)[1].strip()
                            # Remove angle brackets if present
                            message_id = message_id.strip("<>")
                            break

                    if not message_id:
                        print(f"No Message-ID found: {maildir_file.name}")
                        failed += 1
                        continue

                    # Generate filename from message ID
                    filename = self._format_filename(message_id)
                    dest_file = dest_dir / filename

                    # Skip if already exists
                    if dest_file.exists():
                        skipped += 1
                        continue

                    # Write to markdown storage
                    dest_file.write_text(content)
                    success += 1

                except Exception as e:
                    print(f"Failed to import {maildir_file.name}: {e}")
                    failed += 1
                    continue

        print(f"Imported {folder}: {success} succeeded, {failed} failed, {skipped} skipped")

        return {
            "success": success,
            "failed": failed,
            "skipped": skipped,
        }
