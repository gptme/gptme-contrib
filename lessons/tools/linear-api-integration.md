---
match:
  keywords:
    - "Linear"
    - "linear api"
    - "linear graphql"
    - "LOFTY_LINEAR_TOKEN"
    - "LINEAR_API_KEY"
category: tools
---
# Linear API Integration

## Rule
Access Linear via GraphQL API with `LOFTY_LINEAR_TOKEN` or `LINEAR_API_KEY` environment variable, using `urllib` for requests.

## Context
When fetching issues, teams, or other data from Linear in Python code.

## Detection
Observable signals indicating Linear API work:
- Need to read Linear issues or comments
- Checking Linear issue state
- Syncing tasks with Linear
- Any mention of Linear team (ENG, SUDO, etc.)

## Pattern
Standard Linear GraphQL query:
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
        "Authorization": token,  # Token already includes "Bearer" prefix
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
