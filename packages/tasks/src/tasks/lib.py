"""Core business logic for the tasks package.

This module contains core business logic that sits between
the CLI layer (cli.py) and utility functions (utils.py).

Architecture:
- cli.py: Click commands, output formatting, user interaction
- lib.py: Core business logic, external API integrations
- utils.py: Pure utility functions, data structures, helpers

cli.py imports from both lib.py and utils.py as needed.
"""

import json
import logging
import subprocess
from datetime import date
from typing import Any, Dict, List, Optional


def fetch_github_issues(
    repo: str, state: str, labels: List[str], assignee: Optional[str], limit: int
) -> List[Dict[str, Any]]:
    """Fetch issues from GitHub using gh CLI.

    Args:
        repo: Repository in owner/repo format
        state: Issue state filter (open, closed, all)
        labels: List of labels to filter by
        assignee: Filter by assignee (use 'me' for authenticated user)
        limit: Maximum number of issues to fetch

    Returns:
        List of issue dicts with keys: number, title, state, labels, url, body, tracking_ref, source
    """
    cmd = [
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--limit",
        str(limit),
        "--json",
        "number,title,state,labels,url,body",
    ]

    if state != "all":
        cmd.extend(["--state", state])

    for label in labels:
        cmd.extend(["--label", label])

    if assignee:
        cmd.extend(["--assignee", assignee])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logging.error(f"GitHub CLI failed: {result.stderr}")
            return []

        issues_data = json.loads(result.stdout)
        issues = []
        for issue in issues_data:
            issues.append(
                {
                    "number": issue["number"],
                    "title": issue["title"],
                    "state": issue["state"].lower(),
                    "labels": [lbl["name"] for lbl in issue.get("labels", [])],
                    "url": issue["url"],
                    "body": issue.get("body", "")[:500] if issue.get("body") else "",
                    "tracking_ref": issue["url"],  # Use full URL
                    "source": "github",
                }
            )
        return issues
    except subprocess.TimeoutExpired:
        logging.error("GitHub CLI timed out")
        return []
    except json.JSONDecodeError as e:
        logging.error(f"Failed to parse GitHub response: {e}")
        return []
    except Exception as e:
        logging.error(f"Error fetching GitHub issues: {e}")
        return []


def fetch_linear_issues(team: str, state: str, limit: int) -> List[Dict[str, Any]]:
    """Fetch issues from Linear using GraphQL API.

    Requires LINEAR_API_KEY environment variable.

    Args:
        team: Linear team key (e.g., 'ENG', 'SUDO')
        state: Issue state filter (open, closed, all)
        limit: Maximum number of issues to fetch

    Returns:
        List of issue dicts with keys: number, title, state, labels, url, body, tracking_ref, source
    """
    import os
    import urllib.request

    token = os.environ.get("LINEAR_API_KEY")
    if not token:
        logging.warning("LINEAR_API_KEY not set")
        return []

    # Build state filter
    state_filter = {}
    if state == "open":
        state_filter = {"state": {"type": {"nin": ["completed", "canceled"]}}}
    elif state == "closed":
        state_filter = {"state": {"type": {"in": ["completed", "canceled"]}}}

    query = """
    query($teamKey: String!, $first: Int!, $filter: IssueFilter) {
        team(key: $teamKey) {
            issues(first: $first, filter: $filter) {
                nodes {
                    identifier
                    title
                    state { name type }
                    labels { nodes { name } }
                    url
                    description
                }
            }
        }
    }
    """

    variables = {"teamKey": team, "first": limit}
    if state_filter:
        variables["filter"] = state_filter

    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")

    req = urllib.request.Request(
        "https://api.linear.app/graphql",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": token,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))

        if "errors" in data:
            logging.error(f"Linear API errors: {data['errors']}")
            return []

        team_data = data.get("data", {}).get("team")
        if not team_data:
            logging.error(f"Team '{team}' not found in Linear")
            return []

        issues = []
        for issue in team_data.get("issues", {}).get("nodes", []):
            state_info = issue.get("state", {})
            state_type = state_info.get("type", "").lower()
            # Map Linear state types to simple open/closed
            issue_state = (
                "closed" if state_type in ["completed", "canceled"] else "open"
            )

            issues.append(
                {
                    "number": issue["identifier"],
                    "title": issue["title"],
                    "state": issue_state,
                    "labels": [
                        lbl["name"] for lbl in issue.get("labels", {}).get("nodes", [])
                    ],
                    "url": issue["url"],
                    "body": (issue.get("description") or "")[:500],
                    "tracking_ref": issue["url"],
                    "source": "linear",
                }
            )
        return issues

    except urllib.error.URLError as e:
        logging.error(f"Linear API request failed: {e}")
        return []
    except json.JSONDecodeError as e:
        logging.error(f"Failed to parse Linear response: {e}")
        return []
    except Exception as e:
        logging.error(f"Error fetching Linear issues: {e}")
        return []


def generate_task_filename(title: str, number: str | int, source: str) -> str:
    """Generate a filename for a task from issue title.

    Args:
        title: Issue title
        number: Issue number/identifier
        source: Source system (github, linear)

    Returns:
        Filename in format: slug-number.md
    """
    import re

    # Convert to lowercase and replace non-alphanumeric with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower())
    # Remove leading/trailing hyphens and limit length
    slug = slug.strip("-")[:40].rstrip("-")
    # Add number and extension
    return f"{slug}-{number}.md"


def map_priority_from_labels(labels: List[str]) -> Optional[str]:
    """Map priority from issue labels.

    Args:
        labels: List of label names

    Returns:
        Priority string (high, medium, low) or None if no priority label found
    """
    labels_lower = [lbl.lower() for lbl in labels]
    if any(
        "high" in lbl or "critical" in lbl or "urgent" in lbl for lbl in labels_lower
    ):
        return "high"
    if any("medium" in lbl for lbl in labels_lower):
        return "medium"
    if any("low" in lbl for lbl in labels_lower):
        return "low"
    return None


def generate_task_content(
    issue: Dict[str, Any], source: str, priority: Optional[str]
) -> str:
    """Generate task file content from issue data.

    Args:
        issue: Issue data dict with number, title, tracking_ref, labels, url, body
        source: Source system (github, linear)
        priority: Priority string or None

    Returns:
        Complete task file content with frontmatter and body
    """
    frontmatter_lines = [
        "---",
        "state: new",
        f"created: {date.today().isoformat()}",
        f"tracking: ['{issue['tracking_ref']}']",
    ]

    if priority:
        frontmatter_lines.append(f"priority: {priority}")

    # Add tags from labels (limit to 5)
    if issue.get("labels"):
        tags = [lbl.lower().replace(" ", "-") for lbl in issue["labels"][:5]]
        tags.append(source)  # Add source as tag
        frontmatter_lines.append(f"tags: {json.dumps(tags)}")

    frontmatter_lines.append("---")

    # Build body
    title = issue["title"]
    url = issue["url"]
    body = issue.get("body", "")

    body_lines = [
        "",
        f"# {title}",
        "",
        f"**Source**: [{source.title()} #{issue['number']}]({url})",
        "",
    ]

    if body:
        body_lines.extend(
            [
                "## Description",
                "",
                body,
                "",
            ]
        )

    body_lines.extend(
        [
            "## Notes",
            "",
            "*Imported from external tracker. See source link for full context.*",
            "",
        ]
    )

    return "\n".join(frontmatter_lines) + "\n" + "\n".join(body_lines)
