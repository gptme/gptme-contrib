"""
Fetch real GitHub activity metrics using `gh` CLI and `git log`.

Provides actual commit counts, merged PRs, and closed issues
instead of relying on LLM guessing.
"""

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import date, timedelta

logger = logging.getLogger(__name__)

DEFAULT_REPOS = [
    "ErikBjare/gptme-bob",
    "gptme/gptme",
    "gptme/gptme-contrib",
]


@dataclass
class RepoActivity:
    """Activity for a single repository."""

    repo: str
    commits: int = 0
    merged_prs: list[dict[str, str]] = field(default_factory=list)
    closed_issues: list[dict[str, str]] = field(default_factory=list)


@dataclass
class PRReview:
    """A PR review received from someone."""

    repo: str
    pr_number: int
    pr_title: str
    reviewer: str
    url: str = ""


@dataclass
class CrossRepoPR:
    """A PR authored in a repo outside DEFAULT_REPOS."""

    repo: str
    number: int
    title: str
    state: str = ""  # "open", "merged", "closed"
    url: str = ""


@dataclass
class GitHubActivity:
    """Aggregated GitHub activity across repos."""

    start_date: date
    end_date: date
    repos: list[RepoActivity] = field(default_factory=list)
    reviews_received: list[PRReview] = field(default_factory=list)
    cross_repo_prs: list[CrossRepoPR] = field(default_factory=list)

    @property
    def total_commits(self) -> int:
        return sum(r.commits for r in self.repos)

    @property
    def total_prs_merged(self) -> int:
        return sum(len(r.merged_prs) for r in self.repos)

    @property
    def total_issues_closed(self) -> int:
        return sum(len(r.closed_issues) for r in self.repos)


def _run_command(cmd: list[str], timeout: int = 30) -> str | None:
    """Run a command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        logger.debug(
            "Command failed (rc=%d): %s\nstderr: %s", result.returncode, cmd, result.stderr
        )
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.debug("Command error: %s: %s", cmd, e)
        return None


def _gh_available() -> bool:
    """Check if the `gh` CLI is available and authenticated."""
    return _run_command(["gh", "auth", "status"]) is not None


def get_merged_prs(start: date, end: date, repo: str) -> list[dict[str, str]]:
    """Get merged PRs for a repo in a date range."""
    # gh search uses ISO dates; merged:YYYY-MM-DD..YYYY-MM-DD
    output = _run_command(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "merged",
            "--search",
            f"merged:{start.isoformat()}..{end.isoformat()}",
            "--json",
            "number,title,url,mergedAt",
            "--limit",
            "100",
        ]
    )
    if not output:
        return []
    try:
        prs = json.loads(output)
        return [
            {
                "number": str(pr.get("number", "")),
                "title": pr.get("title", ""),
                "url": pr.get("url", ""),
            }
            for pr in prs
        ]
    except (json.JSONDecodeError, TypeError):
        return []


def get_closed_issues(start: date, end: date, repo: str) -> list[dict[str, str]]:
    """Get closed issues for a repo in a date range."""
    output = _run_command(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "closed",
            "--search",
            f"closed:{start.isoformat()}..{end.isoformat()}",
            "--json",
            "number,title,url,closedAt",
            "--limit",
            "100",
        ]
    )
    if not output:
        return []
    try:
        issues = json.loads(output)
        return [
            {
                "number": str(issue.get("number", "")),
                "title": issue.get("title", ""),
                "url": issue.get("url", ""),
            }
            for issue in issues
        ]
    except (json.JSONDecodeError, TypeError):
        return []


def get_commit_count(start: date, end: date, repo_path: str | None = None) -> int:
    """Get commit count from git log for a date range."""
    cmd = ["git"]
    if repo_path:
        cmd.extend(["-C", repo_path])
    # --after is exclusive, so subtract 1 day; --before is exclusive, so add 1 day
    cmd.extend(
        [
            "log",
            f"--after={start - timedelta(days=1)}",
            f"--before={end + timedelta(days=1)}",
            "--oneline",
        ]
    )
    output = _run_command(cmd)
    if not output:
        return 0
    return len(output.strip().splitlines())


def get_reviews_received(start: date, end: date, repos: list[str]) -> list[PRReview]:
    """Get PR review comments received on our PRs in a date range."""
    reviews: list[PRReview] = []
    for repo in repos:
        output = _run_command(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "all",
                "--search",
                f"updated:{start.isoformat()}..{end.isoformat()}",
                "--json",
                "number,title,url,reviews",
                "--limit",
                "50",
            ]
        )
        if not output:
            continue
        try:
            prs = json.loads(output)
            for pr in prs:
                for review in pr.get("reviews", []):
                    author = review.get("author", {}).get("login", "")
                    if author and author not in ("ErikBjare", "bot"):
                        reviews.append(
                            PRReview(
                                repo=repo,
                                pr_number=pr.get("number", 0),
                                pr_title=pr.get("title", ""),
                                reviewer=author,
                                url=pr.get("url", ""),
                            )
                        )
        except (json.JSONDecodeError, TypeError):
            pass
    return reviews


def get_cross_repo_prs(start: date, end: date, author: str = "ErikBjare") -> list[CrossRepoPR]:
    """Get PRs authored in repos outside the default list."""
    output = _run_command(
        [
            "gh",
            "search",
            "prs",
            "--author",
            author,
            "--created",
            f"{start.isoformat()}..{end.isoformat()}",
            "--json",
            "repository,number,title,state,url",
            "--limit",
            "50",
        ]
    )
    if not output:
        return []
    try:
        prs = json.loads(output)
        results: list[CrossRepoPR] = []
        for pr in prs:
            repo_info = pr.get("repository", {})
            repo_name = repo_info.get("nameWithOwner", "")
            # Only include repos outside DEFAULT_REPOS
            if repo_name and repo_name not in DEFAULT_REPOS:
                results.append(
                    CrossRepoPR(
                        repo=repo_name,
                        number=pr.get("number", 0),
                        title=pr.get("title", ""),
                        state=pr.get("state", "").lower(),
                        url=pr.get("url", ""),
                    )
                )
        return results
    except (json.JSONDecodeError, TypeError):
        return []


def fetch_activity(
    start: date,
    end: date,
    repos: list[str] | None = None,
    workspace: str | None = None,
) -> GitHubActivity:
    """
    Fetch GitHub activity for a date range.

    Args:
        start: Start date (inclusive)
        end: End date (inclusive)
        repos: List of GitHub repos (owner/name). Defaults to DEFAULT_REPOS.
        workspace: Path to local git workspace for commit counting.

    Returns:
        GitHubActivity with data from all repos.
    """
    if repos is None:
        repos = DEFAULT_REPOS

    activity = GitHubActivity(start_date=start, end_date=end)
    has_gh = _gh_available()

    for repo in repos:
        repo_activity = RepoActivity(repo=repo)

        if has_gh:
            repo_activity.merged_prs = get_merged_prs(start, end, repo)
            repo_activity.closed_issues = get_closed_issues(start, end, repo)

        activity.repos.append(repo_activity)

    # Get commit count from local workspace if available
    if workspace:
        commit_count = get_commit_count(start, end, workspace)
        if activity.repos:
            activity.repos[0].commits = commit_count
        else:
            activity.repos.append(RepoActivity(repo="local", commits=commit_count))

    # Fetch reviews received and cross-repo PRs
    if has_gh:
        activity.reviews_received = get_reviews_received(start, end, repos)
        activity.cross_repo_prs = get_cross_repo_prs(start, end)

    return activity


def format_activity_for_prompt(activity: GitHubActivity) -> str:
    """
    Format GitHub activity as markdown for injection into LLM prompts.

    Returns empty string if no activity data.
    """
    lines: list[str] = []

    if (
        activity.total_commits == 0
        and activity.total_prs_merged == 0
        and activity.total_issues_closed == 0
    ):
        return ""

    lines.append("## GitHub Activity (Real Data)")
    lines.append(f"Period: {activity.start_date.isoformat()} to {activity.end_date.isoformat()}")
    lines.append(f"- **Total commits**: {activity.total_commits}")
    lines.append(f"- **PRs merged**: {activity.total_prs_merged}")
    lines.append(f"- **Issues closed**: {activity.total_issues_closed}")
    lines.append("")

    for repo in activity.repos:
        if not repo.merged_prs and not repo.closed_issues and repo.commits == 0:
            continue
        lines.append(f"### {repo.repo}")
        if repo.commits:
            lines.append(f"- Commits: {repo.commits}")
        if repo.merged_prs:
            lines.append("- Merged PRs:")
            for pr in repo.merged_prs:
                lines.append(f"  - #{pr['number']}: {pr['title']}")
        if repo.closed_issues:
            lines.append("- Closed Issues:")
            for issue in repo.closed_issues:
                lines.append(f"  - #{issue['number']}: {issue['title']}")
        lines.append("")

    return "\n".join(lines)
