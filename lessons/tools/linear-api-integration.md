---
match:
  keywords:
    - "Linear"
    - "linear api"
    - "linear graphql"
    - "linear-activity"
    - "LINEAR_API_KEY"
category: tools
---
# Linear API Integration

## Rule
Use `linear-activity.py` CLI for common operations. Fall back to GraphQL API only when CLI doesn't support the operation.

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

## State Filter Example
```python
# Open issues only
variables = {
    "teamKey": team,
    "first": limit,
    "filter": {"state": {"type": {"nin": ["completed", "canceled"]}}}
}

# Closed issues only
variables = {
    "teamKey": team,
    "first": limit,
    "filter": {"state": {"type": {"in": ["completed", "canceled"]}}}
}
```

## Outcome
Following this pattern enables:
- Reliable Linear API access
- Proper authentication handling
- Structured data retrieval
- State filtering for issues

## Related
- [gptme-contrib tasks.py import command](https://github.com/gptme/gptme-contrib) - Uses this pattern
- [Linear GraphQL API docs](https://developers.linear.app/docs/graphql/working-with-the-graphql-api)
