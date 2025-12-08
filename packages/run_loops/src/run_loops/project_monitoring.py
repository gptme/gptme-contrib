"""Project monitoring run loop implementation."""

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from run_loops.base import BaseRunLoop
from run_loops.utils.execution import ExecutionResult
from run_loops.utils.github import (
    CommentLoopDetector,
    has_unresolved_bot_reviews,
)


@dataclass
class WorkItem:
    """Represents a discovered work item."""

    repo: str
    item_type: str  # "pr_update", "ci_failure", "assigned_issue"
    number: int
    title: str
    url: str
    details: str


class ProjectMonitoringRun(BaseRunLoop):
    """Project monitoring run loop.

    Implements project monitoring workflow:
    - Repository discovery in organization
    - State tracking for PR updates, CI failures, assigned issues
    - Work classification (GREEN vs RED)
    - Hot-loop coordination with autonomous runs
    - Execution-focused processing
    """

    def __init__(
        self,
        workspace: Path,
        target_org: str = "gptme",
        author: str = "",
        agent_name: str = "Agent",
    ):
        """Initialize project monitoring run.

        Args:
            workspace: Path to workspace directory
            target_org: GitHub organization to monitor
            author: GitHub username for filtering (GitHub handle)
            agent_name: Name of the agent for prompts
        """
        super().__init__(
            workspace=workspace,
            run_type="project-monitoring",
            timeout=1800,  # 30 minutes
            lock_wait=False,  # Don't wait for lock
        )

        self.target_org = target_org
        self.author = author
        self.agent_name = agent_name
        self.state_dir = workspace / "logs/.project-monitoring-state"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Initialize loop detector for comment spam prevention
        self.loop_detector = CommentLoopDetector(self.state_dir)

        # Cache discovered work between has_work() and generate_prompt()
        self._discovered_work: list[WorkItem] = []

    def has_work(self) -> bool:
        """Check if there is work to do BEFORE acquiring lock.

        Discovers all work items and caches them for generate_prompt().
        Only returns True if actionable work exists, preventing calendar
        entries for check-only runs.

        Returns:
            True if there is work to process
        """
        self.logger.info("Checking for project monitoring work...")

        # Discover work (this is the expensive operation)
        self._discovered_work = self.discover_work()

        if not self._discovered_work:
            self.logger.info("No project monitoring work found")
            return False

        # Set work description for calendar
        work_summary = ", ".join(
            f"{item.item_type}#{item.number}"
            for item in self._discovered_work[:5]  # First 5 items
        )
        if len(self._discovered_work) > 5:
            work_summary += f" +{len(self._discovered_work) - 5} more"
        self._work_description = f"project work: {work_summary}"

        self.logger.info(
            f"Found {len(self._discovered_work)} work items: {work_summary}"
        )
        return True

    def discover_repositories(self) -> list[str]:
        """Discover repositories in organization.

        Returns:
            List of repository names (owner/repo format)
        """
        self.logger.info(f"Discovering repositories in {self.target_org}...")

        try:
            result = subprocess.run(
                [
                    "gh",
                    "repo",
                    "list",
                    self.target_org,
                    "--limit",
                    "100",
                    "--json",
                    "nameWithOwner",
                    "-q",
                    ".[].nameWithOwner",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                self.logger.error(f"Failed to discover repositories: {result.stderr}")
                return []

            repos = [
                line.strip()
                for line in result.stdout.strip().split("\n")
                if line.strip()
            ]
            self.logger.info(f"Found {len(repos)} repositories")
            return repos

        except Exception as e:
            self.logger.error(f"Error discovering repositories: {e}")
            return []

    def check_pr_updates(self, repo: str) -> list[WorkItem]:
        """Check for updated PRs using state tracking.

        Args:
            repo: Repository name (owner/repo)

        Returns:
            List of WorkItem for updated PRs
        """
        work_items = []

        try:
            # Get open PRs by author
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--author",
                    self.author,
                    "--state",
                    "open",
                    "--json",
                    "number,title,updatedAt,url,headRefName",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0 or not result.stdout.strip():
                return []

            prs = json.loads(result.stdout)

            for pr in prs:
                pr_number = pr["number"]
                updated_at = pr["updatedAt"]
                state_file = (
                    self.state_dir / f"{repo.replace('/', '-')}-pr-{pr_number}.state"
                )

                # Check if PR updated since last check
                is_new = False
                if state_file.exists():
                    last_check = state_file.read_text().strip()
                    if updated_at > last_check:
                        is_new = True
                else:
                    is_new = True

                if is_new:
                    # Check spam prevention before adding work item
                    if self.should_post_comment(repo, pr_number, "update"):
                        # Update state file
                        state_file.write_text(updated_at)

                        branch_name = pr.get("headRefName", "unknown")
                        work_items.append(
                            WorkItem(
                                repo=repo,
                                item_type="pr_update",
                                number=pr_number,
                                title=pr["title"],
                                url=pr["url"],
                                details=f"PR #{pr_number} updated: {updated_at}\n  - **Branch**: `{branch_name}` (push to this branch!)",
                            )
                        )

        except Exception as e:
            self.logger.error(f"Error checking PRs in {repo}: {e}")

        return work_items

    def check_ci_failures(self, repo: str) -> list[WorkItem]:
        """Check for CI failures using state tracking.

        Args:
            repo: Repository name (owner/repo)

        Returns:
            List of WorkItem for CI failures
        """
        work_items = []
        state_file = self.state_dir / f"{repo.replace('/', '-')}-ci-failures.state"

        try:
            # Get PRs with CI status
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--author",
                    self.author,
                    "--state",
                    "open",
                    "--json",
                    "number,title,statusCheckRollup,url",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0 or not result.stdout.strip():
                return []

            prs = json.loads(result.stdout)
            current_failures = []

            for pr in prs:
                pr_number = pr["number"]
                checks = pr.get("statusCheckRollup") or []

                # Check for failures
                has_failure = any(
                    check.get("conclusion") == "FAILURE" for check in checks
                )

                if has_failure:
                    current_failures.append(pr_number)

            # Read previous failures
            prev_failures = []
            if state_file.exists():
                prev_failures = [
                    int(line.strip())
                    for line in state_file.read_text().strip().split("\n")
                    if line.strip()
                ]

            # Find new failures
            new_failures = set(current_failures) - set(prev_failures)

            for pr_number in new_failures:
                pr_data = next((p for p in prs if p["number"] == pr_number), None)
                if pr_data:
                    # Check spam prevention before adding work item
                    if self.should_post_comment(repo, pr_number, "ci_failure"):
                        work_items.append(
                            WorkItem(
                                repo=repo,
                                item_type="ci_failure",
                                number=pr_number,
                                title=pr_data["title"],
                                url=pr_data["url"],
                                details=f"PR #{pr_number} CI failing (NEW)",
                            )
                        )

            # Update state file
            state_file.write_text("\n".join(str(n) for n in current_failures))

        except Exception as e:
            self.logger.error(f"Error checking CI in {repo}: {e}")

        return work_items

    def _is_last_activity_by_self(self, repo: str, pr_number: int) -> bool:
        """Check if the last activity on the PR was by the agent.

        Args:
            repo: Repository name (owner/repo)
            pr_number: PR number

        Returns:
            True if last comment/activity was by self (the configured author)
        """
        try:
            # Get last comment on PR
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    repo,
                    "--json",
                    "comments",
                    "--jq",
                    ".comments | sort_by(.createdAt) | last | .author.login",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0 or not result.stdout.strip():
                # No comments or error - assume not by self
                return False

            last_author = result.stdout.strip()
            return last_author == self.author

        except Exception as e:
            self.logger.warning(f"Error checking last activity author: {e}")
            return False

    def _check_for_bot_reviews(
        self, repo: str, pr_number: int
    ) -> tuple[bool, list[str]]:
        """Check if PR has unresolved bot reviews.

        Bot reviews cannot be resolved via API (only maintainers can resolve them).
        This method detects them to avoid infinite loops trying to resolve.

        Args:
            repo: Repository name (owner/repo)
            pr_number: PR number

        Returns:
            Tuple of (has_bot_reviews: bool, bot_usernames: list)
        """
        has_bots, bot_users = has_unresolved_bot_reviews(repo, pr_number)
        if has_bots:
            self.logger.info(
                f"PR {repo}#{pr_number} has unresolved bot reviews from: {bot_users}"
            )
            self.logger.info(
                "Skipping resolution attempts - bot reviews can only be resolved by maintainers"
            )
        return has_bots, bot_users

    def should_post_comment(
        self, repo: str, pr_number: int, comment_type: str, comment_content: str = ""
    ) -> bool:
        """Check if we should post a comment to avoid spam.

        Logic (extended from original):
        0. If loop detected: SKIP (prevent spam loops)
        1. If no previous comment: POST (first time)
        2. If PR updated since last comment: POST (new commits/reviews)
        3. If comment type changed: POST (different kind of work)
        4. If comment stale (24+ hours): POST (periodic reminder)
        5. Otherwise: SKIP (avoid spam)

        Args:
            repo: Repository name (owner/repo)
            pr_number: PR number
            comment_type: Type of comment (e.g., "update", "ci_failure")
            comment_content: Optional content for loop hash detection

        Returns:
            True if we should post comment, False to skip
        """
        # Rule 0: Check for comment loops (Issue #188 fix)
        if comment_content:
            should_post, reason = self.loop_detector.check_and_record(
                repo, pr_number, comment_content, comment_type
            )
            if not should_post:
                self.logger.warning(f"Loop prevention: {reason} for {repo}#{pr_number}")
                return False

        state_file = (
            self.state_dir / f"{repo.replace('/', '-')}-pr-{pr_number}-comment.state"
        )

        try:
            # Get PR's current update time
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    repo,
                    "--json",
                    "updatedAt",
                    "--jq",
                    ".updatedAt",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0 or not result.stdout.strip():
                self.logger.warning(
                    f"Could not get PR update time for {repo}#{pr_number}"
                )
                return False

            current_updated = result.stdout.strip()

            # Check previous comment state
            if state_file.exists():
                state_data = state_file.read_text().strip().split()
                if len(state_data) >= 3:
                    prev_type, prev_time, pr_updated = (
                        state_data[0],
                        state_data[1],
                        state_data[2],
                    )

                    # Rule 1: PR updated since last comment (new commits/reviews)
                    if current_updated > pr_updated:
                        # Check if last activity was by agent (skip own comments)
                        if self._is_last_activity_by_self(repo, pr_number):
                            self.logger.info(
                                f"PR {repo}#{pr_number} updated by self, skipping"
                            )
                            # Update state to avoid repeated checks
                            state_file.write_text(
                                f"{comment_type} {datetime.now().isoformat()} {current_updated}"
                            )
                            return False

                        self.logger.info(
                            f"PR {repo}#{pr_number} updated since last comment"
                        )
                        state_file.write_text(
                            f"{comment_type} {datetime.now().isoformat()} {current_updated}"
                        )
                        return True

                    # Rule 2: Comment type changed
                    if comment_type != prev_type:
                        self.logger.info(
                            f"Comment type changed for {repo}#{pr_number}: {prev_type} -> {comment_type}"
                        )
                        state_file.write_text(
                            f"{comment_type} {datetime.now().isoformat()} {current_updated}"
                        )
                        return True

                    # Rule 3: Comment stale (24+ hours)
                    prev_time_dt = datetime.fromisoformat(prev_time)
                    age = datetime.now() - prev_time_dt
                    if age > timedelta(hours=24):
                        self.logger.info(
                            f"Comment stale for {repo}#{pr_number} (age: {age})"
                        )
                        state_file.write_text(
                            f"{comment_type} {datetime.now().isoformat()} {current_updated}"
                        )
                        return True

                    # Don't post duplicate
                    self.logger.info(
                        f"Skipping duplicate comment on {repo}#{pr_number} (type: {comment_type})"
                    )
                    return False

            # No previous comment: post it (Rule 0)
            self.logger.info(f"First comment for {repo}#{pr_number}")
            state_file.write_text(
                f"{comment_type} {datetime.now().isoformat()} {current_updated}"
            )
            return True

        except Exception as e:
            self.logger.error(
                f"Error checking comment state for {repo}#{pr_number}: {e}"
            )
            return False

    def check_assigned_issues(self, repo: str) -> list[WorkItem]:
        """Check for assigned issues using state tracking.

        Args:
            repo: Repository name (owner/repo)

        Returns:
            List of WorkItem for assigned issues
        """
        work_items = []
        state_file = self.state_dir / f"{repo.replace('/', '-')}-assigned-issues.state"

        try:
            # Get assigned issues
            result = subprocess.run(
                [
                    "gh",
                    "issue",
                    "list",
                    "--repo",
                    repo,
                    "--assignee",
                    self.author,
                    "--state",
                    "open",
                    "--json",
                    "number,title,url",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0 or not result.stdout.strip():
                return []

            issues = json.loads(result.stdout)
            current_issues = [issue["number"] for issue in issues]

            # Read previous issues
            prev_issues = []
            if state_file.exists():
                prev_issues = [
                    int(line.strip())
                    for line in state_file.read_text().strip().split("\n")
                    if line.strip()
                ]

            # Find new issues
            new_issues = set(current_issues) - set(prev_issues)

            for issue_number in new_issues:
                issue_data = next(
                    (i for i in issues if i["number"] == issue_number), None
                )
                if issue_data:
                    work_items.append(
                        WorkItem(
                            repo=repo,
                            item_type="assigned_issue",
                            number=issue_number,
                            title=issue_data["title"],
                            url=issue_data["url"],
                            details=f"Issue #{issue_number} assigned (NEW)",
                        )
                    )

            # Update state file
            state_file.write_text("\n".join(str(n) for n in current_issues))

        except Exception as e:
            self.logger.error(f"Error checking issues in {repo}: {e}")

        return work_items

    def discover_work(self) -> list[WorkItem]:
        """Discover all work items across repositories.

        Returns:
            List of all discovered work items
        """
        all_work = []

        repos = self.discover_repositories()

        for repo in repos:
            self.logger.info(f"Checking {repo}...")

            # Check PR updates
            pr_work = self.check_pr_updates(repo)
            all_work.extend(pr_work)

            # Check CI failures
            ci_work = self.check_ci_failures(repo)
            all_work.extend(ci_work)

            # Check assigned issues
            issue_work = self.check_assigned_issues(repo)
            all_work.extend(issue_work)

        return all_work

    def generate_prompt(self) -> str:
        """Generate prompt for project monitoring.

        Returns:
            Project monitoring prompt with discovered work
        """
        # Use cached work from has_work() - don't re-discover
        work_items = self._discovered_work

        if not work_items:
            # Shouldn't happen if has_work() was called first, but handle gracefully
            self.logger.warning(
                "No cached work items (has_work() may not have been called)"
            )
            return ""  # Will be handled by execute()

        self.logger.info(f"Using {len(work_items)} cached work items")

        # Format work items
        work_description = "\n\n".join(
            f"**{item.item_type.replace('_', ' ').title()} in {item.repo}**:\n"
            f"- {item.details}\n"
            f"- URL: {item.url}"
            for item in work_items
        )

        return f"""You are {self.agent_name}, processing project-specific work via monitoring system.

**Current Time**: {datetime.now().strftime("%Y-%m-%d %H:%M UTC")}
**Context Budget**: 200k tokens (use ~160k for work, save ~40k margin)
**Time Limit**: {self.timeout}s (30 minutes) - wrap up by minute 25

**Work Found**:
{work_description}

## Required Workflow

Focus on EXECUTION over investigation. Three-step workflow like autonomous runs:

**Step 1**: Quick Investigation (5 min max)
- Read full PR/issue context (BOTH basic + comments views!)
- Understand what changed/what's needed
- Identify the actual problem

**Step 2**: Classification (2 min max)
Classify each work item:

**GREEN** (Do the work yourself):
- ✅ Bug fixes you can implement
- ✅ Type errors, linting issues
- ✅ Test failures you can fix
- ✅ Code review feedback application
- ✅ Documentation improvements
- ✅ Simple refactoring

**RED** (Comment with analysis only):
- ❌ Architectural decisions (needs maintainer)
- ❌ Infrastructure issues (CI config, deployment)
- ❌ Needs PR author input
- ❌ Breaking changes
- ❌ Missing credentials/access

**Step 3**: EXECUTION (20+ min - the main work!)
- For GREEN items: Implement fixes, test, commit, push
- For RED items: Post comprehensive analysis comment
- Don't stop early - make substantial progress
- Update PR/issue after completing work

## Git Workflow

**For External Repos** (gptme, gptme-webui, etc.):
- See **lessons/workflow/git-worktree-workflow.md**
- MUST use worktrees for PRs
- MUST update PR after fixing (don't just commit)
- Never commit directly to master/main

**Branch Verification (CRITICAL)**:
- Work item shows branch name: push to THAT branch
- Before pushing: `git branch -vv` → verify correct upstream
- If tracking wrong branch: `git branch --unset-upstream && git push -u origin <branch>`

**For Workspace Repo**:
- Can commit directly to master

## Critical File Operations

**Always use ABSOLUTE PATHS**:
- Correct: `{self.workspace}/journal/2025-11-25-topic.md`
- Wrong: `journal/2025-11-25-topic.md`

**Journal Location**:
- Create in workspace: `{self.workspace}/journal/YYYY-MM-DD-HHMMSS-description.md` (timestamp-based)

## Session Completion

**Brief Documentation** (2-5 min max):
1. Return to workspace: `cd {self.workspace}`
2. Create journal entry with timestamp: `journal/{datetime.now().strftime("%Y-%m-%d-%H%M%S")}-description.md`
3. Commit: `git add journal/*.md && git commit -m "docs(monitoring): session summary" && git push`
4. Use `complete` tool when finished

Begin processing the work now.
"""

    def execute(self, prompt: str) -> ExecutionResult:
        """Execute project monitoring.

        Args:
            prompt: Generated prompt

        Returns:
            ExecutionResult with exit code and status
        """
        # Check if work was found
        if not prompt:
            self.logger.info("No work found, skipping execution")
            return ExecutionResult(exit_code=0, timed_out=False)

        # Execute with gptme
        return super().execute(prompt)
