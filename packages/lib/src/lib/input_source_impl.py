"""Concrete implementations of input sources.

This module provides specific implementations for GitHub issues,
email messages, and other external input sources.
"""

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .input_sources import (
    InputSource,
    InputSourceType,
    TaskCreationResult,
    TaskRequest,
)


class GitHubInputSource(InputSource):
    """Input source for GitHub issues.

    Configuration keys:
        - repo: Repository in owner/name format
        - label: Label to filter issues (e.g., "task-request")
        - workspace_path: Path to Bob's workspace
    """

    def _get_source_type(self) -> InputSourceType:
        return InputSourceType.GITHUB

    async def poll_for_inputs(self) -> List[TaskRequest]:
        """Poll GitHub for issues with task-request label.

        Returns:
            List of TaskRequest objects from GitHub issues
        """
        repo = self.config.get("repo", "ErikBjare/bob")
        label = self.config.get("label", "task-request")

        # Fetch issues with label using gh CLI
        cmd = [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            label,
            "--state",
            "open",
            "--json",
            "number,title,body,author,createdAt,labels",
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            raise ConnectionError(f"Failed to fetch GitHub issues: {e.stderr}")

        issues = json.loads(result.stdout)
        requests = []

        for issue in issues:
            # Extract priority from labels
            priority = self._extract_priority(issue.get("labels", []))

            # Extract tags from labels
            tags = [label["name"] for label in issue.get("labels", [])]

            request = TaskRequest(
                source_type=InputSourceType.GITHUB,
                source_id=f"issue-{issue['number']}",
                title=issue["title"],
                description=issue.get("body", ""),
                created_at=datetime.fromisoformat(
                    issue["createdAt"].replace("Z", "+00:00")
                ),
                author=issue["author"]["login"] if issue.get("author") else None,
                priority=priority,
                tags=tags,
                metadata={
                    "issue_number": issue["number"],
                    "repo": repo,
                    "url": f"https://github.com/{repo}/issues/{issue['number']}",
                },
            )
            requests.append(request)

        return requests

    def _extract_priority(self, labels: List[Dict[str, Any]]) -> Optional[str]:
        """Extract priority from GitHub labels.

        Args:
            labels: List of label dictionaries

        Returns:
            Priority string ("high", "medium", "low") or None
        """
        for label in labels:
            name = str(label["name"]).lower()
            if "priority:" in name:
                return name.split(":")[1].strip()
        return None

    def _is_duplicate(self, request: TaskRequest) -> bool:
        """Check if GitHub issue already has a corresponding task.

        Args:
            request: TaskRequest to check

        Returns:
            True if task already exists for this issue
        """
        workspace_path = Path(self.config.get("workspace_path", "/home/bob/bob"))
        tasks_dir = workspace_path / "tasks"

        # Check for task file containing this issue number
        issue_number = request.metadata.get("issue_number")
        if not issue_number:
            return False

        # Search for existing tasks mentioning this issue
        try:
            cmd = ["grep", "-r", "-l", f"#{issue_number}", str(tasks_dir)]
            result = subprocess.run(cmd, capture_output=True, text=True)
            # grep returns 0 if found, 1 if not found
            return result.returncode == 0
        except Exception:
            # If grep fails, assume not duplicate
            return False

    async def create_task(self, request: TaskRequest) -> TaskCreationResult:
        """Create a task file from GitHub issue.

        Args:
            request: Validated TaskRequest

        Returns:
            Result of task creation
        """
        workspace_path = Path(self.config.get("workspace_path", "/home/bob/bob"))
        tasks_dir = workspace_path / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)

        # Generate task filename from title
        task_filename = self._generate_task_filename(request.title)
        task_path = tasks_dir / task_filename

        # Check if file already exists
        if task_path.exists():
            return TaskCreationResult(
                success=False, error=f"Task file already exists: {task_filename}"
            )

        # Create task content
        content = self._generate_task_content(request)

        # Write task file
        try:
            task_path.write_text(content)
            return TaskCreationResult(
                success=True,
                task_path=task_path,
                task_id=task_filename.replace(".md", ""),
            )
        except Exception as e:
            return TaskCreationResult(
                success=False, error=f"Failed to write task file: {e}"
            )

    def _generate_task_filename(self, title: str) -> str:
        """Generate task filename from title.

        Args:
            title: Task title

        Returns:
            Filename (e.g., "implement-feature.md")
        """
        # Convert to lowercase, replace spaces with hyphens
        filename = title.lower()
        filename = "".join(c if c.isalnum() or c in " -" else "" for c in filename)
        filename = filename.replace(" ", "-")
        # Limit length
        filename = filename[:50]
        return f"{filename}.md"

    def _generate_task_content(self, request: TaskRequest) -> str:
        """Generate task file content from request.

        Args:
            request: TaskRequest with all data

        Returns:
            Markdown content for task file
        """
        # Generate frontmatter
        frontmatter = [
            "---",
            "state: new",
            f"created: {datetime.now().isoformat()}",
        ]

        if request.priority:
            frontmatter.append(f"priority: {request.priority}")

        if request.tags:
            tags_str = ", ".join(request.tags)
            frontmatter.append(f"tags: [{tags_str}]")

        if request.author:
            frontmatter.append(f"assigned_to: {request.author}")

        frontmatter.append("---")

        # Generate body
        body = [
            f"# {request.title}",
            "",
            request.description,
            "",
            "## Source",
            "- **Type**: GitHub Issue",
            f"- **Issue**: #{request.metadata['issue_number']}",
            f"- **URL**: {request.metadata['url']}",
            f"- **Created**: {request.created_at.isoformat()}",
        ]

        if request.author:
            body.append(f"- **Author**: @{request.author}")

        return "\n".join(frontmatter + [""] + body)

    async def acknowledge_input(self, request: TaskRequest) -> None:
        """Add comment to GitHub issue acknowledging task creation.

        Args:
            request: The processed TaskRequest
        """
        repo = request.metadata.get("repo")
        issue_number = request.metadata.get("issue_number")

        if not repo or not issue_number:
            return

        comment = (
            "✅ Task created from this issue.\n\n"
            "I'll work on this and update when complete."
        )

        try:
            subprocess.run(
                [
                    "gh",
                    "issue",
                    "comment",
                    str(issue_number),
                    "--repo",
                    repo,
                    "--body",
                    comment,
                ],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            # Acknowledgment is best-effort
            pass


class EmailInputSource(InputSource):
    """Input source for email messages.

    Configuration keys:
        - maildir_path: Path to maildir (e.g., ~/.local/share/mail/inbox)
        - allowlist: List of allowed sender emails
        - workspace_path: Path to Bob's workspace
    """

    def _get_source_type(self) -> InputSourceType:
        return InputSourceType.EMAIL

    async def poll_for_inputs(self) -> List[TaskRequest]:
        """Poll maildir for unread emails from allowlisted senders.

        Returns:
            List of TaskRequest objects from emails
        """
        maildir_path = Path(
            self.config.get("maildir_path", "~/.local/share/mail/inbox")
        ).expanduser()
        allowlist = self.config.get("allowlist", [])

        # Check new/ directory for unread emails
        new_dir = maildir_path / "new"
        if not new_dir.exists():
            return []

        requests = []
        for email_file in new_dir.iterdir():
            try:
                content = email_file.read_text()

                # Parse email headers (simple implementation)
                from_addr = self._extract_header(content, "From")
                subject = self._extract_header(content, "Subject")
                date_str = self._extract_header(content, "Date")

                # Check allowlist
                if not any(allowed in from_addr for allowed in allowlist):
                    continue

                # Extract body
                body = self._extract_body(content)

                # Parse date
                created_at = self._parse_email_date(date_str)

                request = TaskRequest(
                    source_type=InputSourceType.EMAIL,
                    source_id=email_file.name,
                    title=subject,
                    description=body,
                    created_at=created_at,
                    author=from_addr,
                    priority=self._extract_priority_from_email(subject, body),
                    tags=["email", "external-request"],
                    metadata={
                        "email_file": str(email_file),
                        "from": from_addr,
                        "subject": subject,
                    },
                )
                requests.append(request)

            except Exception:
                # Skip malformed emails
                continue

        return requests

    def _extract_header(self, content: str, header_name: str) -> str:
        """Extract email header value.

        Args:
            content: Full email content
            header_name: Header to extract

        Returns:
            Header value or empty string
        """
        for line in content.split("\n"):
            if line.startswith(f"{header_name}:"):
                return line.split(":", 1)[1].strip()
        return ""

    def _extract_body(self, content: str) -> str:
        """Extract email body after headers.

        Args:
            content: Full email content

        Returns:
            Email body text
        """
        # Find double newline marking end of headers
        parts = content.split("\n\n", 1)
        if len(parts) > 1:
            return parts[1].strip()
        return ""

    def _parse_email_date(self, date_str: str) -> datetime:
        """Parse email date header.

        Args:
            date_str: Date header value

        Returns:
            Parsed datetime
        """
        # Simplified - in real implementation use email.utils.parsedate_to_datetime
        try:
            return datetime.now()  # Placeholder
        except Exception:
            return datetime.now()

    def _extract_priority_from_email(self, subject: str, body: str) -> Optional[str]:
        """Extract priority from email content.

        Args:
            subject: Email subject
            body: Email body

        Returns:
            Priority string or None
        """
        text = (subject + " " + body).lower()
        if "urgent" in text or "asap" in text:
            return "high"
        elif "low priority" in text or "when you can" in text:
            return "low"
        return "medium"

    def _is_duplicate(self, request: TaskRequest) -> bool:
        """Check if email already has a corresponding task.

        Args:
            request: TaskRequest to check

        Returns:
            True if task already exists for this email
        """
        workspace_path = Path(self.config.get("workspace_path", "/home/bob/bob"))
        tasks_dir = workspace_path / "tasks"

        # Check for task mentioning this email subject
        subject = request.metadata.get("subject", "")
        if not subject:
            return False

        # Search for tasks with similar title
        try:
            cmd = [
                "grep",
                "-r",
                "-l",
                subject[:30],  # First 30 chars of subject
                str(tasks_dir),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            return result.returncode == 0
        except Exception:
            return False

    async def create_task(self, request: TaskRequest) -> TaskCreationResult:
        """Create a task file from email.

        Args:
            request: Validated TaskRequest

        Returns:
            Result of task creation
        """
        # Similar to GitHubInputSource.create_task
        workspace_path = Path(self.config.get("workspace_path", "/home/bob/bob"))
        tasks_dir = workspace_path / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)

        task_filename = self._generate_task_filename(request.title)
        task_path = tasks_dir / task_filename

        if task_path.exists():
            return TaskCreationResult(
                success=False, error=f"Task file already exists: {task_filename}"
            )

        content = self._generate_task_content(request)

        try:
            task_path.write_text(content)
            return TaskCreationResult(
                success=True,
                task_path=task_path,
                task_id=task_filename.replace(".md", ""),
            )
        except Exception as e:
            return TaskCreationResult(
                success=False, error=f"Failed to write task file: {e}"
            )

    def _generate_task_filename(self, title: str) -> str:
        """Generate task filename from email subject."""
        filename = title.lower()
        filename = "".join(c if c.isalnum() or c in " -" else "" for c in filename)
        filename = filename.replace(" ", "-")
        filename = filename[:50]
        return f"email-{filename}.md"

    def _generate_task_content(self, request: TaskRequest) -> str:
        """Generate task file content from email."""
        frontmatter = [
            "---",
            "state: new",
            f"created: {datetime.now().isoformat()}",
        ]

        if request.priority:
            frontmatter.append(f"priority: {request.priority}")

        if request.tags:
            tags_str = ", ".join(request.tags)
            frontmatter.append(f"tags: [{tags_str}]")

        frontmatter.append("---")

        body = [
            f"# {request.title}",
            "",
            request.description,
            "",
            "## Source",
            "- **Type**: Email",
            f"- **From**: {request.author}",
            f"- **Date**: {request.created_at.isoformat()}",
        ]

        return "\n".join(frontmatter + [""] + body)

    async def acknowledge_input(self, request: TaskRequest) -> None:
        """Move email to processed folder.

        Args:
            request: The processed TaskRequest
        """
        email_file = Path(request.metadata.get("email_file", ""))
        if not email_file.exists():
            return

        # Move from new/ to cur/ (mark as read)
        new_path = email_file
        cur_path = email_file.parent.parent / "cur" / (email_file.name + ":2,S")

        try:
            new_path.rename(cur_path)
        except Exception:
            # Best-effort
            pass


class WebhookInputSource(InputSource):
    """Input source for webhook requests.

    Webhooks store payloads as JSON files in a queue directory.
    This source polls the queue and creates tasks from webhook data.

    Configuration keys:
        - webhook_queue_path: Path to webhook queue directory
        - workspace_path: Path to Bob's workspace
        - require_auth_token: Optional authentication token
    """

    def _get_source_type(self) -> InputSourceType:
        return InputSourceType.WEBHOOK

    async def poll_for_inputs(self) -> List[TaskRequest]:
        """Poll webhook queue directory for new webhook payloads.

        Returns:
            List of TaskRequest objects from webhook payloads
        """
        queue_path = Path(
            self.config.get("webhook_queue_path", "~/.local/share/webhook-queue")
        ).expanduser()

        if not queue_path.exists():
            queue_path.mkdir(parents=True, exist_ok=True)
            return []

        requests = []
        for webhook_file in queue_path.glob("*.json"):
            try:
                payload = json.loads(webhook_file.read_text())

                # Validate required fields
                if not all(k in payload for k in ["title", "description"]):
                    continue

                # Extract optional fields
                priority = payload.get("priority", "medium")
                tags = payload.get("tags", ["webhook"])
                author = payload.get("author")

                # Parse creation time
                created_at_str = payload.get("created_at")
                if created_at_str:
                    created_at = datetime.fromisoformat(created_at_str)
                else:
                    # Use file modification time
                    created_at = datetime.fromtimestamp(webhook_file.stat().st_mtime)

                request = TaskRequest(
                    source_type=InputSourceType.WEBHOOK,
                    source_id=webhook_file.stem,
                    title=payload["title"],
                    description=payload["description"],
                    created_at=created_at,
                    author=author,
                    priority=priority,
                    tags=tags,
                    metadata={
                        "webhook_file": str(webhook_file),
                        "webhook_source": payload.get("source", "unknown"),
                        "original_payload": payload,
                    },
                )
                requests.append(request)

            except Exception:
                # Skip malformed webhook files
                continue

        return requests

    def _is_duplicate(self, request: TaskRequest) -> bool:
        """Check if webhook already has a corresponding task.

        Args:
            request: TaskRequest to check

        Returns:
            True if task already exists for this webhook
        """
        workspace_path = Path(self.config.get("workspace_path", "/home/bob/bob"))
        tasks_dir = workspace_path / "tasks"

        # Check for task mentioning this webhook ID
        webhook_id = request.source_id
        if not webhook_id:
            return False

        # Search for tasks with this webhook source_id
        try:
            cmd = ["grep", "-r", "-l", f"webhook-{webhook_id}", str(tasks_dir)]
            result = subprocess.run(cmd, capture_output=True, text=True)
            return result.returncode == 0
        except Exception:
            return False

    async def create_task(self, request: TaskRequest) -> TaskCreationResult:
        """Create a task file from webhook payload.

        Args:
            request: Validated TaskRequest

        Returns:
            Result of task creation
        """
        workspace_path = Path(self.config.get("workspace_path", "/home/bob/bob"))
        tasks_dir = workspace_path / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)

        task_filename = self._generate_task_filename(request.title)
        task_path = tasks_dir / task_filename

        if task_path.exists():
            return TaskCreationResult(
                success=False, error=f"Task file already exists: {task_filename}"
            )

        content = self._generate_task_content(request)

        try:
            task_path.write_text(content)
            return TaskCreationResult(
                success=True,
                task_path=task_path,
                task_id=task_filename.replace(".md", ""),
            )
        except Exception as e:
            return TaskCreationResult(
                success=False, error=f"Failed to write task file: {e}"
            )

    def _generate_task_filename(self, title: str) -> str:
        """Generate task filename from webhook title."""
        filename = title.lower()
        filename = "".join(c if c.isalnum() or c in " -" else "" for c in filename)
        filename = filename.replace(" ", "-")
        filename = filename[:50]
        return f"webhook-{filename}.md"

    def _generate_task_content(self, request: TaskRequest) -> str:
        """Generate task file content from webhook."""
        frontmatter = [
            "---",
            "state: new",
            f"created: {datetime.now().isoformat()}",
        ]

        if request.priority:
            frontmatter.append(f"priority: {request.priority}")

        if request.tags:
            tags_str = ", ".join(request.tags)
            frontmatter.append(f"tags: [{tags_str}]")

        if request.author:
            frontmatter.append(f"assigned_to: {request.author}")

        frontmatter.append("---")

        body = [
            f"# {request.title}",
            "",
            request.description,
            "",
            "## Source",
            "- **Type**: Webhook",
            f"- **Webhook ID**: {request.source_id}",
            f"- **Source**: {request.metadata.get('webhook_source', 'unknown')}",
            f"- **Received**: {request.created_at.isoformat()}",
        ]

        if request.author:
            body.append(f"- **Author**: {request.author}")

        return "\n".join(frontmatter + [""] + body)

    async def acknowledge_input(self, request: TaskRequest) -> None:
        """Delete processed webhook file from queue.

        Args:
            request: The processed TaskRequest
        """
        webhook_file = Path(request.metadata.get("webhook_file", ""))
        if webhook_file.exists():
            try:
                webhook_file.unlink()
            except Exception:
                # Best-effort
                pass


class SchedulerInputSource(InputSource):
    """Input source for scheduled task creation.

    Reads a schedule configuration file and creates tasks based on
    time-based triggers (one-time or recurring).

    Configuration keys:
        - schedule_config_path: Path to schedule YAML/JSON file
        - workspace_path: Path to Bob's workspace
        - state_file_path: Path to store scheduling state

    Schedule format (YAML):
    ```yaml
    scheduled_tasks:
      - id: daily-standup
        title: "Daily standup reflection"
        description: "Reflect on progress and plan next steps"
        schedule:
          type: recurring
          pattern: daily
          time: "09:00"
        priority: medium
        tags: [daily, reflection]

      - id: weekly-review
        title: "Weekly review"
        description: "Review week's progress and learnings"
        schedule:
          type: recurring
          pattern: weekly
          day: friday
          time: "16:00"
        priority: high
        tags: [weekly, review]

      - id: one-time-task
        title: "Prepare presentation"
        description: "Prepare slides for conference"
        schedule:
          type: once
          datetime: "2024-02-15T14:00:00"
        priority: high
    ```
    """

    def _get_source_type(self) -> InputSourceType:
        return InputSourceType.SCHEDULER

    async def poll_for_inputs(self) -> List[TaskRequest]:
        """Poll schedule configuration for due tasks.

        Returns:
            List of TaskRequest objects for tasks that are due
        """
        import yaml

        schedule_config_path = Path(
            self.config.get("schedule_config_path", "~/.config/bob/schedule.yaml")
        ).expanduser()

        if not schedule_config_path.exists():
            return []

        try:
            with schedule_config_path.open() as f:
                schedule_data = yaml.safe_load(f)
        except Exception:
            return []

        scheduled_tasks = schedule_data.get("scheduled_tasks", [])
        state = self._load_state()
        current_time = datetime.now()
        requests = []

        for task in scheduled_tasks:
            task_id = task.get("id")
            if not task_id:
                continue

            schedule = task.get("schedule", {})
            schedule_type = schedule.get("type")

            # Check if task is due
            if schedule_type == "once":
                # One-time scheduled task
                if self._is_one_time_due(task, state, current_time):
                    requests.append(self._create_task_request(task))
                    # Mark as processed
                    state[task_id] = {"last_run": current_time.isoformat()}

            elif schedule_type == "recurring":
                # Recurring scheduled task
                if self._is_recurring_due(task, state, current_time):
                    requests.append(self._create_task_request(task))
                    # Update last run time
                    state[task_id] = {"last_run": current_time.isoformat()}

        # Save updated state
        self._save_state(state)

        return requests

    def _is_one_time_due(
        self, task: Dict[str, Any], state: Dict[str, Any], current_time: datetime
    ) -> bool:
        """Check if one-time scheduled task is due.

        Args:
            task: Task configuration
            state: Scheduling state
            current_time: Current datetime

        Returns:
            True if task is due and not yet run
        """
        task_id = task.get("id")

        # Check if already run
        if task_id in state:
            return False

        # Check if time has come
        datetime_str = task.get("schedule", {}).get("datetime")
        if not datetime_str:
            return False

        scheduled_time = datetime.fromisoformat(datetime_str)
        return current_time >= scheduled_time

    def _is_recurring_due(
        self, task: Dict[str, Any], state: Dict[str, Any], current_time: datetime
    ) -> bool:
        """Check if recurring scheduled task is due.

        Args:
            task: Task configuration
            state: Scheduling state
            current_time: Current datetime

        Returns:
            True if task is due based on recurrence pattern
        """
        task_id = str(task.get("id", ""))
        schedule = task.get("schedule", {})
        pattern = schedule.get("pattern", "")  # daily, weekly, monthly
        time_str = schedule.get("time", "09:00")  # HH:MM format

        # Parse scheduled time
        hour, minute = map(int, time_str.split(":"))

        # Check last run
        last_run_str = state.get(task_id, {}).get("last_run")
        if last_run_str:
            last_run = datetime.fromisoformat(last_run_str)
        else:
            last_run = None

        # Check if it's time based on pattern
        if pattern == "daily":
            # Run once per day at specified time
            # Use time window approach: check if current time >= scheduled time
            # and we haven't run since scheduled time
            scheduled_time = current_time.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if current_time >= scheduled_time:
                # Check if already run since scheduled time today
                if (
                    last_run
                    and last_run >= scheduled_time
                    and last_run.date() == current_time.date()
                ):
                    return False
                return True

        elif pattern == "weekly":
            # Run once per week on specified day
            day = schedule.get("day", "monday").lower()
            weekday_map = {
                "monday": 0,
                "tuesday": 1,
                "wednesday": 2,
                "thursday": 3,
                "friday": 4,
                "saturday": 5,
                "sunday": 6,
            }
            target_weekday = weekday_map.get(day, 0)

            # Use time window approach: check if current time >= scheduled time
            if current_time.weekday() == target_weekday:
                scheduled_time = current_time.replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                if current_time >= scheduled_time:
                    # Check if already run since scheduled time this week
                    if last_run and last_run >= scheduled_time:
                        days_since_last_run = (current_time - last_run).days
                        if days_since_last_run < 7:
                            return False
                    return True

        elif pattern == "monthly":
            # Run once per month on specified day
            # Use time window approach: check if current time >= scheduled time
            day_of_month = schedule.get("day_of_month", 1)
            if current_time.day == day_of_month:
                scheduled_time = current_time.replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                if current_time >= scheduled_time:
                    # Check if already run since scheduled time this month
                    if (
                        last_run
                        and last_run >= scheduled_time
                        and last_run.month == current_time.month
                    ):
                        return False
                    return True

        return False

    def _create_task_request(self, task: Dict[str, Any]) -> TaskRequest:
        """Create TaskRequest from scheduled task configuration.

        Args:
            task: Task configuration dictionary

        Returns:
            TaskRequest object
        """
        return TaskRequest(
            source_type=InputSourceType.SCHEDULER,
            source_id=f"scheduled-{task['id']}",
            title=task.get("title", "Scheduled Task"),
            description=task.get("description", ""),
            created_at=datetime.now(),
            priority=task.get("priority", "medium"),
            tags=task.get("tags", ["scheduled"]),
            metadata={
                "schedule_id": task.get("id"),
                "schedule_type": task.get("schedule", {}).get("type"),
                "schedule_pattern": task.get("schedule", {}).get("pattern"),
            },
        )

    def _load_state(self) -> Dict[str, Any]:
        """Load scheduling state from file.

        Returns:
            Dictionary of task states
        """
        state_file = Path(
            self.config.get("state_file_path", "~/.local/share/bob/schedule-state.json")
        ).expanduser()

        if not state_file.exists():
            return {}

        try:
            result = json.loads(state_file.read_text())
            return dict(result) if isinstance(result, dict) else {}
        except Exception:
            return {}

    def _save_state(self, state: Dict[str, Any]) -> None:
        """Save scheduling state to file.

        Args:
            state: Dictionary of task states to save
        """
        state_file = Path(
            self.config.get("state_file_path", "~/.local/share/bob/schedule-state.json")
        ).expanduser()

        state_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            state_file.write_text(json.dumps(state, indent=2))
        except Exception:
            # Best-effort
            pass

    async def create_task(self, request: TaskRequest) -> TaskCreationResult:
        """Create a task file from scheduled task.

        Args:
            request: Validated TaskRequest

        Returns:
            Result of task creation
        """
        workspace_path = Path(self.config.get("workspace_path", "/home/bob/bob"))
        tasks_dir = workspace_path / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)

        task_filename = self._generate_task_filename(request.title)
        task_path = tasks_dir / task_filename

        if task_path.exists():
            return TaskCreationResult(
                success=False, error=f"Task file already exists: {task_filename}"
            )

        content = self._generate_task_content(request)

        try:
            task_path.write_text(content)
            return TaskCreationResult(
                success=True,
                task_path=task_path,
                task_id=task_filename.replace(".md", ""),
            )
        except Exception as e:
            return TaskCreationResult(
                success=False, error=f"Failed to write task file: {e}"
            )

    def _generate_task_filename(self, title: str) -> str:
        """Generate task filename from scheduled task title."""
        filename = title.lower()
        filename = "".join(c if c.isalnum() or c in " -" else "" for c in filename)
        filename = filename.replace(" ", "-")
        filename = filename[:50]

        # Add timestamp to make unique for recurring tasks
        timestamp = datetime.now().strftime("%Y%m%d")
        return f"scheduled-{timestamp}-{filename}.md"

    def _generate_task_content(self, request: TaskRequest) -> str:
        """Generate task file content from scheduled task."""
        frontmatter = [
            "---",
            "state: new",
            f"created: {datetime.now().isoformat()}",
        ]

        if request.priority:
            frontmatter.append(f"priority: {request.priority}")

        if request.tags:
            tags_str = ", ".join(request.tags)
            frontmatter.append(f"tags: [{tags_str}]")

        frontmatter.append("---")

        body = [
            f"# {request.title}",
            "",
            request.description,
            "",
            "## Source",
            "- **Type**: Scheduled Task",
            f"- **Schedule ID**: {request.metadata.get('schedule_id')}",
            f"- **Schedule Type**: {request.metadata.get('schedule_type')}",
        ]

        pattern = request.metadata.get("schedule_pattern")
        if pattern:
            body.append(f"- **Recurrence**: {pattern}")

        body.append(f"- **Created**: {request.created_at.isoformat()}")

        return "\n".join(frontmatter + [""] + body)
