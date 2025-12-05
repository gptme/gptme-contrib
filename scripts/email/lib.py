"""Core library for the email-based message system."""

import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.policy import default
from email.utils import format_datetime
from pathlib import Path
from typing import Optional

# Add parent directory to path for shared communication_utils
sys.path.insert(0, str(Path(__file__).parent.parent))

from communication_utils.state.locks import FileLock, LockError
from communication_utils.state.tracking import ConversationTracker, MessageState
from communication_utils.rate_limiting.limiters import RateLimiter
from communication_utils.monitoring import get_logger, MetricsCollector

import markdown


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
    """Handles email operations for agent communication."""

    def __init__(self, workspace_dir: str | Path, own_email: str | None = None):
        """Initialize with workspace directory.

        Args:
            workspace_dir: Path to the workspace directory
            own_email: The agent's own email address (used to avoid replying to self).
                      If None, will use AGENT_EMAIL environment variable or default to "agent@example.org"
        """
        import os

        self.workspace = Path(workspace_dir)
        self.email_dir = self.workspace / "email"
        # Ensure own_email is always a string (never None due to fallback)
        self.own_email = (
            own_email
            if own_email is not None
            else os.getenv("AGENT_EMAIL", "agent@example.org")
        )

        # External maildir paths (from mbsync)
        # Use environment variables with fallback to defaults for backward compatibility
        self.external_maildir = Path(
            os.getenv("MAILDIR_INBOX", str(Path.home() / ".local/share/mail/gmail/agent"))
        )
        self.external_maildir_sent = Path(
            os.getenv("MAILDIR_SENT", str(Path.home() / ".local/share/mail/gmail/agent/Sent"))
        )

        # State files for tracking
        self.processed_state_file = self.email_dir / "processed_state.txt"
        self.locks_dir = self.email_dir / "locks"

        # Rate limiting for email sending (1 request per second)
        self.rate_limiter = RateLimiter.for_platform("email")

        # Initialize conversation tracker for reply state management
        self.tracker = ConversationTracker(self.locks_dir)

        # Initialize monitoring
        self.logger = get_logger("email_system", "email", log_dir=self.email_dir / "logs")
        self.metrics = MetricsCollector()

        self._validate_structure()
        self._ensure_processed_state()

    def _validate_structure(self) -> None:
        """Ensure email directory structure exists."""
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
        """Check if we've already replied to this message."""
        # Normalize message ID format
        normalized_id = message_id.strip("<>")
        conversation_id = "email"  # Single conversation for all emails

        msg_info = self.tracker.get_message_state(conversation_id, normalized_id)
        return msg_info is not None and msg_info.state == MessageState.COMPLETED

    def _mark_replied(self, original_message_id: str, reply_message_id: str) -> None:
        """Mark a message as replied to."""
        # Ensure message tracked with universal ID
        universal_id = self._ensure_message_tracked(original_message_id)
        conversation_id = "email"

        # Set state to COMPLETED
        self.tracker.set_message_state(
            conversation_id=conversation_id, message_id=universal_id, state=MessageState.COMPLETED
        )

    def _mark_no_reply_needed(self, message_id: str, reason: str = "no reply needed") -> None:
        """Mark a message as processed but no reply needed."""
        # Ensure message tracked with universal ID
        universal_id = self._ensure_message_tracked(message_id)
        conversation_id = "email"

        # Set state to NO_REPLY_NEEDED
        self.tracker.set_message_state(
            conversation_id=conversation_id,
            message_id=universal_id,
            state=MessageState.NO_REPLY_NEEDED,
        )

    def _is_completed(self, message_id: str) -> bool:
        """Check if message has been completed (replied or marked as no reply needed)."""
        # Normalize message ID format
        normalized_id = message_id.strip("<>")
        conversation_id = "email"

        # Check via platform ID lookup
        msg_info = self.tracker.get_message_by_platform_id(
            conversation_id, platform="email", platform_message_id=normalized_id
        )
        return msg_info is not None and msg_info.state in (
            MessageState.COMPLETED,
            MessageState.NO_REPLY_NEEDED,
        )

    def _ensure_message_tracked(
        self,
        message_id: str,
        from_user: str | None = None,
        to_user: str | None = None,
        subject: str | None = None,
    ) -> str:
        """
        Ensure message exists in tracking system with universal ID.

        Args:
            message_id: Platform message ID (email Message-ID)
            from_user: Optional sender email
            to_user: Optional recipient email
            subject: Optional message subject

        Returns:
            Universal message ID
        """
        normalized_id = message_id.strip("<>")
        conversation_id = "email"

        # Check if already tracked
        msg_info = self.tracker.get_message_by_platform_id(
            conversation_id, platform="email", platform_message_id=normalized_id
        )

        if msg_info:
            return str(msg_info.message_id)

        # Create new unified message
        msg_info = self.tracker.create_unified_message(
            conversation_id=conversation_id,
            platform="email",
            platform_message_id=normalized_id,
            from_user=from_user,
            to_user=to_user,
            subject=subject,
        )

        return str(msg_info.message_id)

    def get_unreplied_emails(self) -> list[tuple[str, str, str]]:
        """Get list of emails that haven't been replied to.

        Returns:
            List of (message_id, subject, sender) tuples for unreplied emails
        """
        unreplied = []

        # Scan inbox for emails from allowlisted senders
        inbox_dir = self.email_dir / "inbox"
        for email_file in inbox_dir.glob("*.md"):
            try:
                content = email_file.read_text()

                # Extract message details
                message_id_match = re.search(r"Message-ID: (<[^>]+>)", content)
                subject_match = re.search(r"Subject: (.+)", content)
                from_match = re.search(r"From: (.+)", content)

                if not (message_id_match and subject_match and from_match):
                    continue

                message_id = message_id_match.group(1)
                subject = subject_match.group(1)
                from_line = from_match.group(1)

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

                unreplied.append((message_id, subject, sender))

            except Exception as e:
                print(f"Error processing {email_file}: {e}")
                continue

        return unreplied

    def _is_allowlisted_sender(self, sender: str) -> bool:
        """Check if sender is allowlisted for auto-responses."""
        # TODO: set in config or env variable
        allowlisted = [
            "erik@bjareho.lt",
            "erik.bjareholt@gmail.com",
            "filip.harald@gmail.com",
            "agent@example.org",
            "other@example.org",
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

        return clean_sender in [s.lower() for s in allowlisted]

    def _is_notification_email(self, subject: str, content: str) -> bool:
        """Check if email is a notification that doesn't need a reply."""
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

    def process_unreplied_emails(self, callback_func) -> int:
        """Process unreplied emails with locking to prevent duplicates.

        Args:
            callback_func: Function to call for each unreplied email,
                          should accept (message_id, subject, sender)

        Returns:
            Number of emails processed
        """
        # Start metrics tracking
        op = self.metrics.start_operation("process_unreplied", "email")

        unreplied = self.get_unreplied_emails()
        self.logger.info("Processing unreplied emails", unreplied_count=len(unreplied))

        processed_count = 0
        skipped_count = 0
        error_count = 0

        for message_id, subject, sender in unreplied:
            # Try to acquire lock (non-blocking with timeout=0)
            lock_file = self.locks_dir / f"{self._format_filename(message_id)}.lock"
            try:
                with FileLock(lock_file, timeout=0):
                    print(f"Processing unreplied email from {sender}: {subject}")
                    self.logger.info(
                        "Processing email",
                        message_id=message_id,
                        sender=sender,
                        subject=subject[:50],
                    )
                    callback_func(message_id, subject, sender)
                    processed_count += 1
            except LockError:
                print(f"Skipping {message_id} (already being processed)")
                self.logger.debug("Email locked, skipping", message_id=message_id)
                skipped_count += 1
            except Exception as e:
                print(f"Error processing {message_id}: {e}")
                self.logger.error("Email processing error", message_id=message_id, error=str(e))
                error_count += 1

        self.logger.info(
            "Unreplied email processing complete",
            processed=processed_count,
            skipped=skipped_count,
            errors=error_count,
            total=len(unreplied),
        )
        op.complete(success=True)
        return processed_count

    def _generate_message_id(self) -> str:
        """Generate a unique message ID."""
        unique_id = str(uuid.uuid4())
        return f"<{unique_id}>"

    def _format_filename(self, message_id: str) -> str:
        """Convert message ID to filename."""
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
            from_address: Optional custom sender address (defaults to agent@example.org)
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
        sender = from_address or self.own_email

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
        # Start metrics tracking
        op = self.metrics.start_operation("send_email", "email")

        filename = self._format_filename(message_id)
        draft_path = self.email_dir / "drafts" / filename
        sent_path = self.email_dir / "sent" / filename

        if not draft_path.exists():
            error_msg = f"Draft not found: {message_id}"
            self.logger.error("Send failed", message_id=message_id, error=error_msg)
            op.complete(success=False, error=error_msg)
            raise ValueError(error_msg)

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
                error_msg = "No recipient found in email headers"
                self.logger.error("Send failed", message_id=message_id, error=error_msg)
                op.complete(success=False, error=error_msg)
                raise ValueError(error_msg)

            # Log send operation start
            self.logger.info(
                "Sending email",
                message_id=message_id,
                recipient=recipient,
                sender=sender,
                content_length=len(content),
            )

            # Create proper MIME multipart message
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText

            # Create multipart message
            msg = MIMEMultipart("alternative")

            # Set headers from extracted metadata
            msg["From"] = headers.get("From") or sender
            msg["To"] = recipient
            msg["Subject"] = headers.get("Subject", "")
            msg["Date"] = headers.get("Date", format_datetime(datetime.now(timezone.utc)))
            msg["Message-ID"] = headers.get("Message-ID", "")
            if "In-Reply-To" in headers:
                msg["In-Reply-To"] = headers["In-Reply-To"]
            if "References" in headers:
                msg["References"] = headers["References"]

            # Add plain text version (markdown as fallback)
            plain_text = body
            msg.attach(MIMEText(plain_text, "plain", "utf-8"))

            # Add HTML version (converted markdown)
            # Use sane_lists extension for better nested list handling
            # Note: Removed nl2br extension as it breaks proper list formatting
            # by converting single newlines to <br> tags, which prevents proper
            # paragraph and list rendering in HTML
            html_body = markdown.markdown(
                fix_list_spacing(body),
                extensions=["extra", "codehilite", "sane_lists"],
            )
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            # Get appropriate msmtp account
            account = self._get_msmtp_account_for_address(sender)

            # Build msmtp command
            msmtp_cmd = ["msmtp"]
            if account:
                msmtp_cmd.extend(["-a", account])
            msmtp_cmd.append(recipient)

            # Check rate limit before sending
            if not self.rate_limiter.can_proceed():
                wait_time = self.rate_limiter.time_until_ready()
                print(f"Rate limit reached. Waiting {wait_time:.1f}s...")
                if not self.rate_limiter.wait_if_needed(max_wait=60):
                    raise RuntimeError("Rate limit timeout - could not send within 60s")

            # Send MIME message via msmtp
            subprocess.run(
                msmtp_cmd,
                input=msg.as_string().encode("utf-8"),
                capture_output=True,
                check=True,
                timeout=30,
            )
            print(f"Email delivered via SMTP to {recipient} (from {sender}) as HTML")
            self.logger.info("SMTP send successful", message_id=message_id, recipient=recipient)
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.decode() if e.stderr else str(e)
            print(f"Failed to send {message_id}: {error_msg}")
            self.logger.error("SMTP send failed", message_id=message_id, error=error_msg)
            op.complete(success=False, error=error_msg)
            raise
        except subprocess.TimeoutExpired:
            error_msg = "SMTP server timeout"
            print(f"Timeout sending {message_id}: SMTP server took too long to respond")
            self.logger.error("SMTP send timeout", message_id=message_id, error=error_msg)
            op.complete(success=False, error=error_msg)
            raise
        except Exception as e:
            error_msg = str(e)
            print(f"Error sending {message_id}: {e}")
            self.logger.error("SMTP send error", message_id=message_id, error=error_msg)
            op.complete(success=False, error=error_msg)
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
                self.logger.warning(
                    "Failed to mark as replied",
                    message_id=message_id,
                    original_id=in_reply_to,
                    error=str(e),
                )

        print(f"Message {message_id} sent successfully and archived")
        self.logger.info("Email send complete", message_id=message_id, was_reply=bool(in_reply_to))
        op.complete(success=True)

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
            from email.utils import parsedate_to_datetime

            return parsedate_to_datetime(date_str)
        except Exception:
            # Fallback to current time if parsing fails
            return datetime.now(timezone.utc)

    def _format_thread_display(self, message_id: str) -> str:
        """Format a complete thread for display."""
        thread_messages = self.get_thread_messages(message_id)

        if not thread_messages:
            return self.read_message(message_id)

        # Count my replies
        my_replies = sum(
            1 for msg in thread_messages if msg["folder"].lower() in ["sent", "agent-sent"]
        )
        output = []
        output.append("=" * 80)
        output.append(f"CONVERSATION THREAD ({len(thread_messages)} messages)")
        output.append(f"🤖 Your replies: {my_replies} of {len(thread_messages)} messages")
        output.append("=" * 80)
        output.append("")

        for i, msg in enumerate(thread_messages):
            # Header for each message
            headers = msg["headers"]
            is_current = msg["id"] == message_id
            is_my_reply = msg["folder"].lower() in ["sent", "agent-sent"]

            # Check replied status for inbox messages
            replied_status = ""
            if not is_my_reply:
                is_replied = self._is_completed(msg["id"])
                replied_status = " ✅ [REPLIED]" if is_replied else " ⏸️  [PENDING]"

            # Build marker showing message type
            markers = []
            if is_current:
                markers.append(">>> CURRENT MESSAGE <<<")
            if is_my_reply:
                markers.append("🤖 [MY REPLY]")

            marker = " ".join(markers) + replied_status
            output.append(f"[{i + 1}/{len(thread_messages)}] {marker}")
            output.append(f"From: {headers.get('From', 'Unknown')}")
            output.append(f"To: {headers.get('To', 'Unknown')}")
            output.append(f"Date: {headers.get('Date', 'Unknown')}")
            output.append(f"Subject: {headers.get('Subject', 'No Subject')}")
            output.append(f"Folder: {msg['folder']}")
            output.append(f"Message-ID: {msg['id'][:40]}...")  # Truncate for readability
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
        if "gmail.com" in from_address or "+agent@gmail.com" in from_address:
            return "gmail"
        return None  # Use default account

    def sync_from_maildir(self, folder: str) -> None:
        """Sync messages from external maildir to markdown format.

        This imports emails from the external maildir (e.g., Gmail via mbsync)
        into the workspace storage as markdown files.

        Args:
            folder: Folder to sync - supports "inbox" (agent label) and "sent" (agent-sent label)
        """

        # Choose appropriate external maildir
        if folder == "inbox":
            maildir_folder = self.external_maildir
        elif folder == "sent":
            maildir_folder = self.external_maildir_sent
        else:
            raise ValueError(
                f"sync_from_maildir supports 'inbox' and 'sent' folders, got: {folder}"
            )

        if not maildir_folder.exists():
            print(f"Warning: Maildir folder does not exist: {maildir_folder}")
            return

        success = 0
        failed = 0
        skipped = 0

        # Process messages in both cur/ (read) and new/ (unread) directories
        for subdir in ["cur", "new"]:
            subdir_path = maildir_folder / subdir
            if not subdir_path.exists():
                continue

            for msg_path in subdir_path.glob("*"):
                if msg_path.name == ".gitkeep":
                    continue

                try:
                    # Parse flags from filename (only relevant for cur/)
                    flags = ""
                    if subdir == "cur" and ":" in msg_path.name:
                        _, flags = msg_path.name.split(":", 1)

                    # Read message with proper policy
                    msg_bytes = msg_path.read_bytes()
                    email_msg = BytesParser(policy=default).parsebytes(msg_bytes)

                    # Get Message-ID
                    message_id = email_msg["Message-ID"]
                    if not message_id:
                        print(f"Warning: No Message-ID in {msg_path}")
                        failed += 1
                        continue

                    # Check if we already have this message in any folder
                    filename = self._format_filename(message_id)
                    filename = filename.replace("/", "_")

                    # For sent emails, check if it already exists in sent folder
                    # For inbox emails, check if it already exists in inbox folder
                    save_dir = self.email_dir / folder
                    md_path = save_dir / filename

                    if md_path.exists():
                        skipped += 1
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
                    continue

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
