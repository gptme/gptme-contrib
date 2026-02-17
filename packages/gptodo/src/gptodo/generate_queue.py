#!/usr/bin/env python3
"""Generate queue-generated.md from task files and high-priority GitHub issues.

This script reads task files and generates the queue-generated.md file
used by autonomous runs. It reads tasks from:
1. High-priority regular tasks from tasks/ directory
2. High-priority GitHub issues from current repository

The work queue format:
- Current Run: Brief summary of current session
- Planned Next: Top 5 tasks with details (priority, status, blockers)
- Last Updated: Timestamp

Configuration via environment variables:
- WORKSPACE_PATH: Path to agent workspace (default: current directory)
- GITHUB_USERNAME: GitHub username for assignee filtering (default: current user)
- JOURNAL_DIR: Journal directory name (default: journal)
- TASKS_DIR: Tasks directory name (default: tasks)
- STATE_DIR: State directory name (default: state)
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

try:
    import frontmatter
except ImportError:
    print("Error: python-frontmatter not installed")
    print("Install with: pip install python-frontmatter")
    sys.exit(1)


class Task:
    """Represents a task for the work queue."""

    def __init__(
        self,
        id: str,
        title: str,
        priority: str,
        state: str,
        source: str,
        details: Optional[str] = None,
        blockers: Optional[List[str]] = None,
        assigned: bool = False,
        requires: Optional[List[str]] = None,
    ):
        self.id = id
        self.title = title
        self.priority = priority
        self.state = state
        self.source = source  # "tasks" or "github"
        self.details = details or ""
        self.blockers = blockers or []
        self.assigned = assigned  # For GitHub issues: explicitly assigned
        self.requires = requires or []  # Task IDs this task depends on
        self.unblocking_power = 0  # Set by compute_unblocking_power()

    def priority_score(self) -> int:
        """Get numeric priority for sorting (higher = more important).

        Score components:
        - Base: urgent=4, high=3, medium=2, low=1
        - +1 if explicitly assigned (GitHub issues)
        - +unblocking_power (tasks that unblock more work rank higher)
        """
        priority = self.priority.lower()
        if priority == "urgent":
            score = 4
        elif priority == "high":
            score = 3
        elif priority == "medium":
            score = 2
        else:
            score = 1

        # Boost score if explicitly assigned (GitHub issues only)
        if self.assigned:
            score += 1

        # Boost by unblocking power (how many tasks this unblocks transitively)
        score += self.unblocking_power

        return score

    def to_work_queue_entry(self, rank: int) -> str:
        """Format as work queue entry."""
        # Build entry header
        priority_label = self.priority.upper() if self.priority != "medium" else ""
        header = f"{rank}. **{self.title}**"
        if priority_label:
            header += f" ({priority_label} priority)"

        # Build entry details
        lines = [header]

        # Add status
        if self.state == "active":
            lines.append("   - Status: 🏃 Active")
        elif self.state == "new":
            lines.append("   - Status: 🆕 New")

        # Add details if present
        if self.details:
            for line in self.details.split("\n"):
                if line.strip():
                    lines.append(f"   - {line.strip()}")

        # Add blockers if present
        if self.blockers:
            blocker_text = ", ".join(self.blockers)
            lines.append(f"   - Blocked on: {blocker_text}")

        # Show unblocking power if significant
        if self.unblocking_power > 0:
            lines.append(f"   - Unblocks: {self.unblocking_power} downstream task(s)")

        # Add source reference
        lines.append(f"   - Source: {self.source}/{self.id}")

        return "\n".join(lines)


class QueueGenerator:
    """Generate work queue from task files and GitHub issues."""

    def __init__(
        self,
        workspace_path: Path,
        github_username: Optional[str] = None,
        journal_dir: str = "journal",
        tasks_dir: str = "tasks",
        state_dir: str = "state",
        user: Optional[str] = None,
    ):
        self.workspace = workspace_path
        self.github_username = github_username
        self.tasks_dir = self.workspace / tasks_dir
        self.state_dir = self.workspace / state_dir
        self.journal_dir = self.workspace / journal_dir
        self.user = user  # Filter tasks by assigned_to field

        # Output filename includes user if specified
        if user:
            self.work_queue_file = self.state_dir / f"queue-generated-{user}.md"
        else:
            self.work_queue_file = self.state_dir / "queue-generated.md"

    def run_command(self, args: List[str]) -> str:
        """Run command and return output."""
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                cwd=self.workspace,
                timeout=30,
            )
            if result.returncode != 0:
                print(f"Warning: Command failed: {' '.join(args)}", file=sys.stderr)
                print(f"Error: {result.stderr}", file=sys.stderr)
                return ""
            return result.stdout.strip()
        except Exception as e:
            print(f"Warning: Failed to run command: {e}", file=sys.stderr)
            return ""

    def get_file_tasks(self) -> List[Task]:
        """Get high-priority tasks from tasks/*.md files (new or active state).

        Source: Local task files in tasks/ directory
        Filter: priority=high/urgent AND state=new/active
        Optional: Filter by assigned_to if --user is specified
        """
        tasks: List[Task] = []

        if not self.tasks_dir.exists():
            print(f"Warning: Tasks directory not found: {self.tasks_dir}")
            return tasks

        # Scan all task files for high-priority work
        for task_file in self.tasks_dir.glob("*.md"):
            if task_file.name.startswith("_"):
                continue

            try:
                post = frontmatter.load(task_file)

                # Filter by state (new or active only)
                state = post.metadata.get("state", "new")
                if state not in ("new", "active"):
                    continue

                # Filter by assigned_to if --user is specified
                if self.user:
                    assigned_to = post.metadata.get("assigned_to", "agent")
                    # Include if assigned to specified user or "both"
                    if assigned_to != self.user and assigned_to != "both":
                        continue

                # Filter by priority (high or urgent only) - skip if filtering by user
                # When filtering by user, include all priorities to show their full queue
                priority = post.metadata.get("priority", "medium")
                if not self.user and priority not in ("high", "urgent"):
                    continue

                # Extract title from content (first heading)
                title = task_file.stem  # fallback to filename
                content_lines = [line.strip() for line in post.content.split("\n") if line.strip()]
                for line in content_lines:
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break

                # Read dependency fields (requires is canonical, depends is deprecated alias)
                requires_list = post.metadata.get("requires", []) or post.metadata.get(
                    "depends", []
                )

                tasks.append(
                    Task(
                        id=task_file.stem,
                        title=title,
                        state=state,
                        priority=priority,
                        source="tasks",
                        requires=requires_list if isinstance(requires_list, list) else [],
                    )
                )
            except Exception as e:
                print(f"Warning: Failed to parse {task_file}: {e}")
                continue

        return tasks

    def get_github_issues(self) -> List[Task]:
        """Get high-priority issues from GitHub repository.

        Source: GitHub issues in current repository
        Filter: label=priority:high/urgent AND state=open
        Boost: +1 score if assigned to configured username

        Note: Skipped when --user filter is active, since GitHub uses
        a different assignment model (GitHub usernames via assignees)
        than local tasks (role-based assigned_to field).
        """
        tasks: List[Task] = []

        # Skip GitHub issues when filtering by user
        # GitHub issues use assignees (GitHub usernames) which don't map
        # to the role-based assigned_to field used in local tasks
        if self.user:
            return tasks

        try:
            # Get current repository name
            result = self.run_command(["gh", "repo", "view", "--json", "nameWithOwner"])
            if not result:
                return tasks

            repo_data = json.loads(result)
            repo = repo_data["nameWithOwner"]

            # Get all high/urgent priority issues
            result = self.run_command(
                [
                    "gh",
                    "issue",
                    "list",
                    "--repo",
                    repo,
                    "--state",
                    "open",
                    "--json",
                    "number,title,labels,assignees",
                    "--limit",
                    "50",
                ]
            )

            if not result:
                return tasks

            issues = json.loads(result)

            for issue in issues:
                # Extract priority from labels
                priority = None
                labels = issue.get("labels", [])

                for label in labels:
                    label_name = label.get("name", "")
                    if label_name == "priority:urgent":
                        priority = "urgent"
                        break
                    elif label_name == "priority:high":
                        priority = "high"

                # Only include high/urgent priority issues
                if priority not in ("high", "urgent"):
                    continue

                # Check if explicitly assigned to configured username
                assigned = False
                if self.github_username:
                    assignees = issue.get("assignees", [])
                    for assignee in assignees:
                        if assignee.get("login") == self.github_username:
                            assigned = True
                            break

                tasks.append(
                    Task(
                        id=f"issue-{issue['number']}",
                        title=issue["title"],
                        state="active",  # GitHub open issues treated as active
                        priority=priority,
                        source="github",
                        assigned=assigned,
                    )
                )

        except Exception as e:
            print(f"Warning: Failed to fetch GitHub issues: {e}")

        return tasks

    def get_current_run_summary(self) -> str:
        """Get current run summary from latest journal entry."""
        # Find latest journal entry for today
        today = datetime.now().strftime("%Y-%m-%d")

        if not self.journal_dir.exists():
            return f"Session {datetime.now().strftime('%Y%m%d-%H%M')}: Work in progress"

        # Find today's session files
        session_files = sorted(self.journal_dir.glob(f"{today}-session*.md"))

        if session_files:
            latest = session_files[-1]
            # Extract session number from filename
            # Format: YYYY-MM-DD-sessionXXX-description.md
            parts = latest.stem.split("-")
            if len(parts) >= 3 and parts[2].startswith("session"):
                session_num = parts[2].replace("session", "")
                # Read first line after title for summary
                content = latest.read_text()
                lines = [
                    line
                    for line in content.split("\n")
                    if line.strip() and not line.startswith("#")
                ]
                if lines:
                    summary = lines[0][:80]
                    return f"Session {session_num}: {summary}"

        # Fallback: generic message
        return f"Session {datetime.now().strftime('%Y%m%d-%H%M')}: Work in progress"

    def filter_blocked_tasks(self, tasks: List[Task]) -> List[Task]:
        """Filter out tasks whose dependencies are not yet resolved.

        A task is blocked if any of its requires references a task that is
        not in 'done' or 'cancelled' state. We check against all task files
        in the tasks directory (not just high-priority ones).
        """
        if not self.tasks_dir.exists():
            return tasks

        # Build a map of all task states (including done/cancelled ones)
        all_task_states: dict[str, str] = {}
        for task_file in self.tasks_dir.glob("*.md"):
            if task_file.name.startswith("_"):
                continue
            try:
                post = frontmatter.load(task_file)
                all_task_states[task_file.stem] = post.metadata.get("state", "new")
            except Exception:
                continue

        # Also check archive directory for completed tasks
        archive_dir = self.tasks_dir / "archive"
        if archive_dir.exists():
            for task_file in archive_dir.glob("*.md"):
                try:
                    post = frontmatter.load(task_file)
                    all_task_states[task_file.stem] = post.metadata.get("state", "done")
                except Exception:
                    continue

        ready_tasks = []
        for task in tasks:
            if not task.requires:
                ready_tasks.append(task)
                continue

            # Check if all task-based requires are resolved
            blocked = False
            for req in task.requires:
                # Skip URL-based requires (can't check without cache)
                if isinstance(req, str) and req.startswith("http"):
                    continue
                req_state = all_task_states.get(req)
                if req_state is None or req_state not in ("done", "cancelled"):
                    blocked = True
                    break

            if not blocked:
                ready_tasks.append(task)

        filtered_count = len(tasks) - len(ready_tasks)
        if filtered_count > 0:
            print(f"  Filtered {filtered_count} blocked task(s) with unmet dependencies")

        return ready_tasks

    def compute_unblocking_power(self, tasks: List[Task]) -> None:
        """Compute how many tasks each task transitively unblocks.

        For each task, count how many other tasks (across all task files)
        depend on it either directly or transitively. Tasks that unblock
        more work get higher priority scores.
        """
        if not self.tasks_dir.exists():
            return

        # Build a map of all task requires (including non-queue tasks)
        all_requires: dict[str, list[str]] = {}
        for task_file in self.tasks_dir.glob("*.md"):
            if task_file.name.startswith("_"):
                continue
            try:
                post = frontmatter.load(task_file)
                state = post.metadata.get("state", "new")
                # Only count non-terminal tasks as "things to unblock"
                if state in ("done", "cancelled"):
                    continue
                requires = post.metadata.get("requires", []) or post.metadata.get("depends", [])
                if requires and isinstance(requires, list):
                    # Filter to task-based requires only
                    task_requires = [
                        r for r in requires if isinstance(r, str) and not r.startswith("http")
                    ]
                    if task_requires:
                        all_requires[task_file.stem] = task_requires
            except Exception:
                continue

        for task in tasks:
            if task.source != "tasks":
                continue
            task.unblocking_power = self._count_transitive_dependents(task.id, all_requires, set())

    def _count_transitive_dependents(
        self,
        task_id: str,
        all_requires: dict[str, list[str]],
        visited: set[str],
    ) -> int:
        """Count tasks that directly or transitively depend on task_id."""
        if task_id in visited:
            return 0
        visited.add(task_id)

        count = 0
        for other_id, requires in all_requires.items():
            if task_id in requires and other_id not in visited:
                count += 1  # Direct dependent
                count += self._count_transitive_dependents(other_id, all_requires, visited)

        return count

    def generate_work_queue(self, tasks: List[Task], current_run: str) -> str:
        """Generate queue-generated.md content."""
        # Sort tasks by priority
        sorted_tasks = sorted(tasks, key=lambda t: t.priority_score(), reverse=True)

        # Take top 5 tasks
        top_tasks = sorted_tasks[:5]

        # Build work queue markdown
        lines = [
            "# Work Queue",
            "",
            "## Current Run",
            current_run,
            "",
            "## Planned Next",
            "",
        ]

        # Add top tasks
        if top_tasks:
            for rank, task in enumerate(top_tasks, 1):
                lines.append(task.to_work_queue_entry(rank))
                lines.append("")
        else:
            lines.append("No tasks currently queued")
            lines.append("")

        # Add timestamp
        lines.extend(
            [
                "## Last Updated",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            ]
        )

        return "\n".join(lines)

    def generate(self) -> None:
        """Generate work queue and write to file."""
        print("Generating work queue from task sources...")

        # Get tasks from both sources
        file_tasks = self.get_file_tasks()
        github_issues = self.get_github_issues()

        all_tasks = file_tasks + github_issues

        print(
            f"Found {len(file_tasks)} tasks from files, " f"{len(github_issues)} from GitHub issues"
        )

        # Filter out blocked tasks (unmet dependencies)
        all_tasks = self.filter_blocked_tasks(all_tasks)

        # Compute unblocking power for priority scoring
        self.compute_unblocking_power(all_tasks)

        # Get current run summary
        current_run = self.get_current_run_summary()

        # Generate work queue
        work_queue_content = self.generate_work_queue(all_tasks, current_run)

        # Write to file
        self.state_dir.mkdir(exist_ok=True)
        self.work_queue_file.write_text(work_queue_content)

        print(f"✓ Work queue generated: {self.work_queue_file}")
        print(f"  Current run: {current_run}")
        print(f"  Planned next: {min(len(all_tasks), 5)} tasks")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate work queue from task files and GitHub issues"
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Path to agent workspace (default: current directory)",
    )
    parser.add_argument(
        "--github-username",
        help="GitHub username for assignee filtering (default: from gh CLI)",
    )
    parser.add_argument(
        "--user",
        help="Filter tasks by assigned_to field (e.g., 'human', 'agent', 'both')",
    )
    parser.add_argument(
        "--journal-dir",
        default="journal",
        help="Journal directory name (default: journal)",
    )
    parser.add_argument(
        "--tasks-dir",
        default="tasks",
        help="Tasks directory name (default: tasks)",
    )
    parser.add_argument(
        "--state-dir",
        default="state",
        help="State directory name (default: state)",
    )

    args = parser.parse_args()

    # Get GitHub username if not provided
    github_username = args.github_username
    if not github_username:
        github_username = os.environ.get("GITHUB_USERNAME")

    # Create generator and run
    generator = QueueGenerator(
        workspace_path=args.workspace,
        github_username=github_username,
        journal_dir=args.journal_dir,
        tasks_dir=args.tasks_dir,
        state_dir=args.state_dir,
        user=args.user,
    )

    generator.generate()


if __name__ == "__main__":
    main()
