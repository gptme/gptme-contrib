"""Core library for the email-based message system."""

import re
import subprocess
import uuid
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import EmailMessage, Message
from email.policy import default
from email.utils import format_datetime
from pathlib import Path
from typing import Optional

import markdown


class AgentEmail:
    """Handles email operations for agent communication."""

    def __init__(self, workspace_dir: str | Path, own_email: str | None = None):
        """Initialize with workspace directory.

        Args:
            workspace_dir: Path to the workspace directory
            own_email: The agent's own email address (used to avoid replying to self).
                      If None, will use AGENT_EMAIL environment variable or default to "bob@superuserlabs.org"
        """
        import os

        self.workspace = Path(workspace_dir)
        self.email_dir = self.workspace / "email"
        # Ensure own_email is always a string (never None due to fallback)
        self.own_email = own_email if own_email is not None else os.getenv("AGENT_EMAIL", "bob@superuserlabs.org")

        # External maildir paths (from mbsync)
        self.external_maildir_bob = Path.home() / ".local/share/mail/gmail/Bob"
        self.external_maildir_sent = Path.home() / ".local/share/mail/gmail/Bob/Sent"

        # State files for tracking
        self.replies_state_file = self.email_dir / "replies_state.json"
        self.processed_state_file = self.email_dir / "processed_state.txt"
        self.locks_dir = self.email_dir / "locks"

        self._validate_structure()
        self._ensure_state_files()

    def _validate_structure(self) -> None:
        """Ensure email directory structure exists."""
        # Main folders for markdown messages
        required_dirs = ["inbox", "sent", "archive", "drafts", "filters"]
        for dir_name in required_dirs:
            dir_path = self.email_dir / dir_name
            if not dir_path.exists():
                raise RuntimeError(f"Required directory missing: {dir_path}")

    def _ensure_state_files(self) -> None:
        """Ensure state tracking files and directories exist."""
        import json

        # Create locks directory
        self.locks_dir.mkdir(exist_ok=True)

        # Initialize replies state file if it doesn't exist
        if not self.replies_state_file.exists():
            self.replies_state_file.write_text(json.dumps({}))

        # Initialize processed state file if it doesn't exist
        if not self.processed_state_file.exists():
            self.processed_state_file.write_text("")

    def _acquire_lock(self, message_id: str, timeout: int = 300) -> bool:
        """Acquire lock for processing an email.

        Args:
            message_id: ID of message to lock
            timeout: Lock timeout in seconds

        Returns:
            True if lock acquired, False if already locked
        """
        import time

        lock_file = self.locks_dir / f"{self._format_filename(message_id)}.lock"

        # Check for existing lock
        if lock_file.exists():
            try:
                lock_time = float(lock_file.read_text().strip())
                if time.time() - lock_time < timeout:
                    return False  # Still locked
                else:
                    # Stale lock, remove it
                    lock_file.unlink()
            except (ValueError, FileNotFoundError):
                # Invalid lock file, remove it
                lock_file.unlink(missing_ok=True)

        # Create lock
        lock_file.write_text(str(time.time()))
        return True

    def _release_lock(self, message_id: str) -> None:
        """Release lock for an email."""
        lock_file = self.locks_dir / f"{self._format_filename(message_id)}.lock"
        lock_file.unlink(missing_ok=True)

    def _is_replied(self, message_id: str) -> bool:
        """Check if we've already replied to this message."""
        import json

        try:
            replies_data = json.loads(self.replies_state_file.read_text())
            return message_id in replies_data
        except (json.JSONDecodeError, FileNotFoundError):
            return False

    def _mark_replied(self, original_message_id: str, reply_message_id: str) -> None:
        """Mark a message as replied to."""
        import json

        try:
            replies_data = json.loads(self.replies_state_file.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            replies_data = {}

        # Normalize message ID format - always store with angle brackets for consistency
        normalized_id = original_message_id.strip("<>")
        with_brackets = f"<{normalized_id}>"

        replies_data[with_brackets] = {
            "status": "replied",
            "reply_id": reply_message_id,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

        self.replies_state_file.write_text(json.dumps(replies_data, indent=2))

    def _mark_no_reply_needed(self, message_id: str, reason: str = "no reply needed") -> None:
        """Mark a message as processed but no reply needed."""
        import json

        try:
            replies_data = json.loads(self.replies_state_file.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            replies_data = {}

        # Normalize message ID format - always store with angle brackets for consistency
        normalized_id = message_id.strip("<>")
        with_brackets = f"<{normalized_id}>"

        replies_data[with_brackets] = {
            "status": "no_reply_needed",
            "reason": reason,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

        self.replies_state_file.write_text(json.dumps(replies_data, indent=2))

    def _is_completed(self, message_id: str) -> bool:
        """Check if message has been completed (replied or marked as no reply needed)."""
        import json

        try:
            replies_data = json.loads(self.replies_state_file.read_text())
            # Normalize message ID format - try both with and without angle brackets
            normalized_id = message_id.strip("<>")
            with_brackets = f"<{normalized_id}>"

            return message_id in replies_data or normalized_id in replies_data or with_brackets in replies_data
        except (json.JSONDecodeError, FileNotFoundError):
            return False

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
            "bob@superuserlabs.org",
            "alice@superuserlabs.org",
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
        unreplied = self.get_unreplied_emails()
        processed_count = 0

        for message_id, subject, sender in unreplied:
            # Try to acquire lock
            if not self._acquire_lock(message_id):
                print(f"Skipping {message_id} (already being processed)")
                continue

            try:
                print(f"Processing unreplied email from {sender}: {subject}")
                callback_func(message_id, subject, sender)
                processed_count += 1
            except Exception as e:
                print(f"Error processing {message_id}: {e}")
            finally:
                self._release_lock(message_id)

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
            from_address: Optional custom sender address (defaults to bob@superuserlabs.org)
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
            html_body = markdown.markdown(body, extensions=["extra", "codehilite"])
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            # Get appropriate msmtp account
            account = self._get_msmtp_account_for_address(sender)

            # Build msmtp command
            msmtp_cmd = ["msmtp"]
            if account:
                msmtp_cmd.extend(["-a", account])
            msmtp_cmd.append(recipient)

            # Send MIME message via msmtp
            subprocess.run(
                msmtp_cmd,
                input=msg.as_string().encode("utf-8"),
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

    def _collect_thread_messages(self, message_id: str, collected: list[dict], seen: set[str]) -> None:
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
        if "gmail.com" in from_address or "+bob@gmail.com" in from_address:
            return "gmail"
        return None  # Use default account

    def sync_from_maildir(self, folder: str) -> None:
        """Sync messages from external maildir to markdown format.

        This imports emails from the external maildir (e.g., Gmail via mbsync)
        into the workspace storage as markdown files.

        Args:
            folder: Folder to sync - supports "inbox" (Bob label) and "sent" (Bob-sent label)
        """

        # Choose appropriate external maildir
        if folder == "inbox":
            maildir_folder = self.external_maildir_bob
        elif folder == "sent":
            maildir_folder = self.external_maildir_sent
        else:
            raise ValueError(f"sync_from_maildir supports 'inbox' and 'sent' folders, got: {folder}")

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
                    email_msg = message_from_bytes(msg_bytes, policy=default)

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

        print(f"Synced {folder}: {success} succeeded, {failed} failed, {skipped} skipped (already exist)")
