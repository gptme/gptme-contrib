---
match:
  keywords:
    - "Linear"
    - "linear api"
    - "linear graphql"
    - "linear-activity"
    - "LINEAR_API_KEY"
    - "list linear issues"
    - "query linear"
category: tools
---
# Linear API Integration

## Rule
**ALWAYS filter by state when listing issues** to exclude completed/canceled work. Use `linear-activity.py` CLI for common operations; fall back to GraphQL API for listing issues with filters.

## Critical: State Filtering is MANDATORY

⚠️ **When listing Linear issues, ALWAYS filter out completed/canceled states.**

Without state filtering, you will recommend work that's already done, wasting everyone's time.

```python
# MANDATORY: Include state filter in ALL issue queries
"filter": {
    "state": {"type": {"nin": ["completed", "canceled"]}}
}
```

## Context
When fetching issues, teams, or other data from Linear.

## Detection
Observable signals indicating Linear API work:
- Need to read Linear issues or comments
- Checking Linear issue state
- Syncing tasks with Linear
- Any mention of Linear team (ENG, SUDO, etc.)

## Pattern: Preferred - Use CLI

For common operations, use the CLI wrapper:

```bash
# Get issue details
uv run scripts/linear/linear-activity.py get-issue SUDO-123

# Get issue comments
uv run scripts/linear/linear-activity.py get-comments SUDO-123

# Get workflow states
uv run scripts/linear/linear-activity.py get-states --team=SUDO

# Get unread notifications
uv run scripts/linear/linear-activity.py get-notifications

# Update issue state
uv run scripts/linear/linear-activity.py update-issue SUDO-123 --state=<state-id>

# Add comment
uv run scripts/linear/linear-activity.py add-comment SUDO-123 "Comment text"
```

## Pattern: Fallback - GraphQL API

For operations not supported by CLI, use GraphQL directly:
```python
import os
import json
import urllib.request

# Token from environment
token = os.environ.get("LOFTY_LINEAR_TOKEN") or os.environ.get("LINEAR_API_KEY")
if not token:
    raise ValueError("No Linear API token found")

# GraphQL query
query = """
query($teamKey: String!, $first: Int!) {
    team(key: $teamKey) {
        issues(first: $first, orderBy: updatedAt) {
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

# Make request
req = urllib.request.Request(
    "https://api.linear.app/graphql",
    data=json.dumps({
        "query": query,
        "variables": {"teamKey": "SUDO", "first": 10}
    }).encode(),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",  # OAuth tokens require Bearer prefix
    },
)

with urllib.request.urlopen(req, timeout=30) as response:
    data = json.loads(response.read().decode())

if "errors" in data:
    print(f"GraphQL errors: {data['errors']}")
else:
    issues = data["data"]["team"]["issues"]["nodes"]
    for issue in issues:
        print(f"{issue['identifier']}: {issue['title']}")
```

## Key Points

1. **Token format**: Token already includes "Bearer " prefix - use directly in Authorization header
2. **Team key**: Use team key (e.g., "SUDO", "ENG") not team ID
3. **State filtering**: Use `filter` parameter with state type (completed, canceled, etc.)
4. **Rate limits**: Linear has generous rate limits, but add timeout for safety
5. **User mentions**: `@username` does NOT work via API - use full profile links

## Mentioning Users in Comments

**Critical**: When mentioning users via the API, `@username` syntax does NOT work.
You must use the full profile link format:

```markdown
[User Name](https://linear.app/<workspace>/settings/account/<user-id>)
```

Example:
```markdown
[Erik Bjäreholt](https://linear.app/superuserlabs/settings/account/ace04b67-c8dc-432f-a00d-85953cc14e13) can you review this?
```

## Listing Issues with Proper Filtering

**⚠️ The CLI does not have a `list-issues` command. Use GraphQL for listing issues.**

```python
import json
from pathlib import Path
import urllib.request

# Load OAuth token (same as CLI uses)
tokens_file = Path("scripts/linear/.tokens.json")
tokens = json.loads(tokens_file.read_text())
access_token = tokens.get("accessToken") or tokens.get("access_token")

# ALWAYS include state filter to exclude completed/canceled
query = """
query($first: Int!, $filter: IssueFilter) {
    issues(first: $first, filter: $filter, orderBy: updatedAt) {
        nodes {
            identifier
            title
            state { name type }
            assignee { name email }
        }
    }
}
"""

variables = {
    "first": 50,
    "filter": {
        "team": {"key": {"eq": "SUDO"}},
        "assignee": {"email": {"eq": "someone@example.com"}},  # Optional
        "state": {"type": {"nin": ["completed", "canceled"]}}  # MANDATORY!
    }
}

req = urllib.request.Request(
    "https://api.linear.app/graphql",
    data=json.dumps({"query": query, "variables": variables}).encode(),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    },
)

with urllib.request.urlopen(req, timeout=30) as response:
    data = json.loads(response.read().decode())
    issues = data["data"]["issues"]["nodes"]
    for issue in issues:
        print(f"{issue['identifier']}: {issue['title']} ({issue['state']['name']})")
```

## Warning: Linear/GitHub State Sync

⚠️ **Linear issue states may be out of sync with GitHub PRs.**

An issue might show "In Review" in Linear even after its PR was merged in GitHub.
When recommending work:
1. Filter by state (excludes explicitly closed issues)
2. Cross-check with GitHub PRs if task mentions a specific PR
3. Note any discrepancies in your recommendations

## Quick State Filter Reference

```python
# Open issues only (ALWAYS use this for recommendations)
"filter": {"state": {"type": {"nin": ["completed", "canceled"]}}}

# Closed issues only
"filter": {"state": {"type": {"in": ["completed", "canceled"]}}}

# Specific states (use state IDs from get-states command)
"filter": {"state": {"id": {"eq": "state-uuid-here"}}}
```

## Outcome
Following this pattern enables:
- Reliable Linear API access
- Proper authentication handling
- Structured data retrieval
- State filtering for issues

## Related
- [gptme-contrib gptodo import command](https://github.com/gptme/gptme-contrib) - Uses this pattern
- [Linear GraphQL API docs](https://developers.linear.app/docs/graphql/working-with-the-graphql-api)
