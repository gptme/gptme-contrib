#!/usr/bin/env python3
# /// script
# dependencies = ["httpx", "python-dotenv"]
# ///
"""
Linear Activity CLI - Emit thoughts/responses to Linear agent sessions.

Usage:
    ./linear-activity.py thought <session_id> <message>
    ./linear-activity.py response <session_id> <message>
    ./linear-activity.py error <session_id> <message>
    ./linear-activity.py refresh                          # Refresh OAuth token
    ./linear-activity.py token-status                     # Check token status
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

# Load environment from .env file in script directory
ENV_FILE = Path(__file__).parent / ".env"
TOKENS_FILE = Path(__file__).parent / ".tokens.json"
load_dotenv(ENV_FILE)

# Linear endpoints
LINEAR_API = "https://api.linear.app/graphql"
LINEAR_OAUTH_TOKEN = "https://api.linear.app/oauth/token"


def load_oauth_credentials() -> tuple[str, str] | None:
    """Load OAuth client credentials from environment (loaded via python-dotenv)."""
    client_id = os.environ.get("LINEAR_CLIENT_ID")
    client_secret = os.environ.get("LINEAR_CLIENT_SECRET")
    if client_id and client_secret:
        return client_id, client_secret
    return None


def is_token_expired() -> bool:
    """Check if the current token is expired."""
    if not TOKENS_FILE.exists():
        return True

    try:
        tokens = json.loads(TOKENS_FILE.read_text())
        expires_at = float(tokens.get("expiresAt", 0))
        # Convert from milliseconds if needed
        if expires_at > 1e12:
            expires_at = expires_at / 1000
        # Add 5 minute buffer
        return bool(time.time() > (expires_at - 300))
    except Exception:
        return True


def refresh_token() -> bool:
    """Refresh the OAuth token."""
    if not TOKENS_FILE.exists():
        print(f"Error: No tokens file found at {TOKENS_FILE}", file=sys.stderr)
        return False

    credentials = load_oauth_credentials()
    if not credentials:
        print("Error: No OAuth credentials found", file=sys.stderr)
        print(
            f"Set LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET in {ENV_FILE}",
            file=sys.stderr,
        )
        return False

    client_id, client_secret = credentials

    try:
        tokens = json.loads(TOKENS_FILE.read_text())
        refresh_tok = tokens.get("refreshToken") or tokens.get("refresh_token")
        if not refresh_tok:
            print("Error: No refresh token in tokens file", file=sys.stderr)
            return False

        response = httpx.post(
            LINEAR_OAUTH_TOKEN,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_tok,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=30.0,
        )

        new_tokens = response.json()

        if "access_token" not in new_tokens:
            print(f"Error refreshing token: {new_tokens}", file=sys.stderr)
            return False

        # Save in our format
        save_tokens = {
            "accessToken": new_tokens["access_token"],
            "refreshToken": new_tokens.get("refresh_token", refresh_tok),
            "tokenType": new_tokens.get("token_type", "Bearer"),
            "scope": new_tokens.get("scope", tokens.get("scope")),
            "actorType": "application",
            "expiresAt": int(
                (time.time() + new_tokens.get("expires_in", 86400)) * 1000
            ),
        }
        TOKENS_FILE.write_text(json.dumps(save_tokens, indent=2))
        print(
            f"✓ Token refreshed, expires in {new_tokens.get('expires_in', 0) // 3600}h"
        )
        return True

    except Exception as e:
        print(f"Error refreshing token: {e}", file=sys.stderr)
        return False


def token_status() -> None:
    """Print token status information."""
    if not TOKENS_FILE.exists():
        print(f"No tokens file found at {TOKENS_FILE}")
        return

    try:
        tokens = json.loads(TOKENS_FILE.read_text())
        expires_at = tokens.get("expiresAt", 0)
        if expires_at > 1e12:
            expires_at = expires_at / 1000

        from datetime import datetime

        expiry = datetime.fromtimestamp(expires_at)
        now = datetime.now()

        print(f"Tokens file: {TOKENS_FILE}")
        print(f"Expires: {expiry}")
        print(f"Expired: {now > expiry}")
        if now < expiry:
            remaining = expiry - now
            hours = remaining.seconds // 3600
            minutes = (remaining.seconds % 3600) // 60
            print(f"Remaining: {hours}h {minutes}m")
        print(f"Scope: {tokens.get('scope', 'unknown')}")
    except Exception as e:
        print(f"Error reading tokens: {e}")


def get_access_token() -> str:
    """Get Linear access token from tokens file or environment."""
    # Check environment first
    if token := os.environ.get("LINEAR_ACCESS_TOKEN"):
        return token

    # Check tokens file
    if TOKENS_FILE.exists():
        try:
            tokens = json.loads(TOKENS_FILE.read_text())
            # Support both camelCase and snake_case keys
            if access_token := (
                tokens.get("accessToken") or tokens.get("access_token")
            ):
                return str(access_token)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Failed to read tokens file: {e}", file=sys.stderr)

    # Check for personal API key
    if token := os.environ.get("LINEAR_API_KEY"):
        return token

    print("Error: No Linear access token found.", file=sys.stderr)
    print(
        "Set LINEAR_ACCESS_TOKEN environment variable or create .tokens.json",
        file=sys.stderr,
    )
    sys.exit(1)


def emit_activity(
    session_id: str,
    content: str,
    activity_type: str = "thought",
    ephemeral: bool = False,
    signal: str | None = None,
) -> bool:
    """Emit an activity to a Linear agent session.

    Args:
        session_id: The Linear agent session ID
        content: The message content
        activity_type: One of: thought, action, response, elicitation, error, prompt
        ephemeral: If True, activity disappears after the next one (good for progress)
        signal: Optional signal: stop, continue, auth, select
    """
    token = get_access_token()

    # All valid activity types
    valid_types = {"thought", "action", "response", "elicitation", "error", "prompt"}
    if activity_type not in valid_types:
        print(
            f"Warning: Unknown activity type '{activity_type}', using 'thought'",
            file=sys.stderr,
        )
        activity_type = "thought"

    # GraphQL mutation for creating agent activity
    mutation = """
    mutation CreateAgentActivity($input: AgentActivityCreateInput!) {
      agentActivityCreate(input: $input) {
        success
        agentActivity {
          id
        }
      }
    }
    """

    # Content is a JSON object with type and body
    content_obj = {
        "type": activity_type,
        "body": content,
    }

    # Build input
    input_obj: dict[str, Any] = {
        "agentSessionId": session_id,
        "content": content_obj,
    }

    # Add optional fields
    if ephemeral:
        input_obj["ephemeral"] = True

    if signal:
        valid_signals = {"stop", "continue", "auth", "select"}
        if signal in valid_signals:
            input_obj["signal"] = signal
        else:
            print(f"Warning: Unknown signal '{signal}', ignoring", file=sys.stderr)

    variables = {"input": input_obj}

    try:
        response = httpx.post(
            LINEAR_API,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"query": mutation, "variables": variables},
            timeout=30.0,
        )
        response.raise_for_status()

        result = response.json()

        if errors := result.get("errors"):
            print(f"GraphQL errors: {errors}", file=sys.stderr)
            return False

        data = result.get("data", {}).get("agentActivityCreate", {})
        if data.get("success"):
            activity_id = data.get("agentActivity", {}).get("id", "unknown")
            print(
                f"✓ Emitted {activity_type} to session {session_id} (activity: {activity_id})"
            )
            return True
        else:
            print(f"Failed to emit activity: {result}", file=sys.stderr)
            return False

    except httpx.HTTPError as e:
        print(f"HTTP error: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return False


# ============================================================================
# Linear API Wrapper Functions
# ============================================================================


def _graphql_request(query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL request against Linear API."""
    token = get_access_token()
    response = httpx.post(
        LINEAR_API,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=30.0,
    )
    response.raise_for_status()
    result = response.json()
    if errors := result.get("errors"):
        print(f"GraphQL errors: {errors}", file=sys.stderr)
        return {}
    data = result.get("data", {})
    return dict(data) if data else {}


def get_issue(identifier: str) -> bool:
    """Get issue details by identifier (e.g., SUDO-3)."""
    query = """
    query GetIssue($identifier: String!) {
      issue(id: $identifier) {
        id
        identifier
        title
        description
        url
        priority
        state { id name type }
        assignee { id name displayName }
        team { id name key }
        labels { nodes { id name } }
      }
    }
    """
    data = _graphql_request(query, {"identifier": identifier})
    issue = data.get("issue")

    if not issue:
        print(f"Issue {identifier} not found", file=sys.stderr)
        return False

    print(f"Issue: {issue['identifier']} - {issue['title']}")
    print(f"URL: {issue['url']}")
    print(f"State: {issue['state']['name']} ({issue['state']['type']})")
    print(f"Priority: {issue.get('priority', 'None')}")
    if issue.get("assignee"):
        print(f"Assignee: {issue['assignee']['displayName']}")
    if issue.get("team"):
        print(f"Team: {issue['team']['name']} ({issue['team']['key']})")
    if issue.get("labels", {}).get("nodes"):
        labels = [label["name"] for label in issue["labels"]["nodes"]]
        print(f"Labels: {', '.join(labels)}")
    if issue.get("description"):
        print(f"\nDescription:\n{issue['description'][:500]}...")

    return True


def get_comments(identifier: str) -> bool:
    """Get comments on an issue."""
    query = """
    query GetComments($identifier: String!) {
      issue(id: $identifier) {
        identifier
        title
        comments(first: 20) {
          nodes {
            id
            body
            createdAt
            user { displayName }
          }
        }
      }
    }
    """
    data = _graphql_request(query, {"identifier": identifier})
    issue = data.get("issue")

    if not issue:
        print(f"Issue {identifier} not found", file=sys.stderr)
        return False

    comments = issue.get("comments", {}).get("nodes", [])
    print(f"Comments on {issue['identifier']} - {issue['title']}:\n")

    if not comments:
        print("No comments")
        return True

    for c in comments:
        user = c.get("user", {}).get("displayName", "Unknown")
        print(f"--- {user} ({c['createdAt'][:10]}) ---")
        print(c.get("body", "")[:500])
        print()

    return True


def get_states(team_key: str | None = None) -> bool:
    """Get workflow states, optionally filtered by team."""
    if team_key:
        query = """
        query GetTeamStates($teamKey: String!) {
          team(id: $teamKey) {
            name
            states {
              nodes {
                id
                name
                type
                position
              }
            }
          }
        }
        """
        data = _graphql_request(query, {"teamKey": team_key})
        team = data.get("team")
        if not team:
            print(f"Team {team_key} not found", file=sys.stderr)
            return False
        states = team.get("states", {}).get("nodes", [])
        print(f"Workflow states for {team['name']}:")
    else:
        query = """
        query GetAllStates {
          workflowStates(first: 50) {
            nodes {
              id
              name
              type
              team { key name }
            }
          }
        }
        """
        data = _graphql_request(query)
        states = data.get("workflowStates", {}).get("nodes", [])
        print("Workflow states:")

    for s in sorted(
        states, key=lambda x: (x.get("team", {}).get("key", ""), x.get("position", 0))
    ):
        team_info = f" [{s['team']['key']}]" if s.get("team") else ""
        print(f"  {s['id']}: {s['name']} ({s['type']}){team_info}")

    return True


def get_notifications() -> bool:
    """Get unread notifications."""
    query = """
    query GetNotifications {
      notifications(first: 20) {
        nodes {
          id
          type
          readAt
          createdAt
          ... on IssueNotification {
            issue { identifier title url }
            comment { body }
          }
        }
      }
    }
    """
    data = _graphql_request(query)
    notifications = data.get("notifications", {}).get("nodes", [])

    unread = [n for n in notifications if not n.get("readAt")]
    print(f"Unread notifications: {len(unread)}\n")

    for n in unread[:10]:
        issue = n.get("issue", {})
        if issue:
            print(
                f"- [{n['type']}] {issue.get('identifier', 'N/A')}: {issue.get('title', 'N/A')}"
            )
            if n.get("comment"):
                print(f"  Comment: {n['comment']['body'][:100]}...")
        else:
            print(f"- [{n['type']}] {n.get('createdAt', '')[:10]}")

    return True


def update_issue(
    identifier: str, state_id: str | None = None, assignee_id: str | None = None
) -> bool:
    """Update an issue's state or assignee."""
    # First get the issue ID from identifier
    get_query = """
    query GetIssueId($identifier: String!) {
      issue(id: $identifier) {
        id
        identifier
      }
    }
    """
    data = _graphql_request(get_query, {"identifier": identifier})
    issue = data.get("issue")

    if not issue:
        print(f"Issue {identifier} not found", file=sys.stderr)
        return False

    issue_id = issue["id"]

    # Build update input
    update_input = {}
    if state_id:
        update_input["stateId"] = state_id
    if assignee_id:
        update_input["assigneeId"] = assignee_id

    if not update_input:
        print("No updates specified", file=sys.stderr)
        return False

    mutation = """
    mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {
      issueUpdate(id: $id, input: $input) {
        success
        issue {
          identifier
          title
          state { name }
          assignee { displayName }
        }
      }
    }
    """
    data = _graphql_request(mutation, {"id": issue_id, "input": update_input})
    result = data.get("issueUpdate", {})

    if result.get("success"):
        updated = result.get("issue", {})
        print(f"✓ Updated {updated.get('identifier', identifier)}")
        if state_id:
            print(f"  State: {updated.get('state', {}).get('name', 'unknown')}")
        if assignee_id:
            print(
                f"  Assignee: {updated.get('assignee', {}).get('displayName', 'unassigned')}"
            )
        return True
    else:
        print("Failed to update issue", file=sys.stderr)
        return False


def add_comment(identifier: str, body: str) -> bool:
    """Add a comment to an issue."""
    # First get the issue ID
    get_query = """
    query GetIssueId($identifier: String!) {
      issue(id: $identifier) {
        id
        identifier
      }
    }
    """
    data = _graphql_request(get_query, {"identifier": identifier})
    issue = data.get("issue")

    if not issue:
        print(f"Issue {identifier} not found", file=sys.stderr)
        return False

    mutation = """
    mutation AddComment($input: CommentCreateInput!) {
      commentCreate(input: $input) {
        success
        comment {
          id
          body
        }
      }
    }
    """
    data = _graphql_request(mutation, {"input": {"issueId": issue["id"], "body": body}})
    result = data.get("commentCreate", {})

    if result.get("success"):
        print(f"✓ Added comment to {identifier}")
        return True
    else:
        print("Failed to add comment", file=sys.stderr)
        return False


def main():
    # Valid activity types
    activity_types = {"thought", "action", "response", "elicitation", "error", "prompt"}
    # API wrapper commands
    api_commands = {
        "get-issue",
        "get-comments",
        "get-states",
        "get-notifications",
        "update-issue",
        "add-comment",
    }

    if len(sys.argv) < 2:
        print(__doc__)
        print("\nActivity Commands:")
        print("  thought <session_id> <message>      - Emit thinking/progress update")
        print("  action <session_id> <message>       - Emit tool/action invocation")
        print(
            "  response <session_id> <message>     - Emit final response (closes session)"
        )
        print("  elicitation <session_id> <message>  - Request information from user")
        print("  error <session_id> <message>        - Emit error message")
        print("  prompt <session_id> <message>       - Emit prompt/instruction")
        print("\nFlags (add before message):")
        print(
            "  --ephemeral                         - Activity disappears after next one"
        )
        print(
            "  --signal=<signal>                   - Add signal: stop, continue, auth, select"
        )
        print("\nAPI Commands:")
        print(
            "  get-issue <identifier>              - Get issue details (e.g., SUDO-3)"
        )
        print("  get-comments <identifier>           - Get comments on an issue")
        print("  get-states [--team=KEY]             - Get workflow states")
        print("  get-notifications                   - Get unread notifications")
        print("  update-issue <id> --state=ID        - Update issue state")
        print("  add-comment <identifier> <body>     - Add comment to issue")
        print("\nToken Commands:")
        print("  refresh                             - Refresh OAuth token")
        print("  token-status                        - Check token status")
        print("\nExamples:")
        print("  thought abc123 'Analyzing code...'")
        print("  action abc123 --ephemeral 'Running tests...'")
        print("  get-issue SUDO-3")
        print("  add-comment SUDO-3 'Fixed in PR #42'")
        sys.exit(1)

    command = sys.argv[1]

    # Token management commands
    if command == "refresh":
        success = refresh_token()
        sys.exit(0 if success else 1)

    if command == "token-status":
        token_status()
        sys.exit(0)

    # Auto-refresh for API commands if token expired
    if command in api_commands or command in activity_types:
        if is_token_expired():
            print("Token expired, attempting refresh...", file=sys.stderr)
            if not refresh_token():
                print("Warning: Could not refresh token", file=sys.stderr)

    # API wrapper commands
    if command == "get-issue":
        if len(sys.argv) < 3:
            print("Usage: get-issue <identifier>", file=sys.stderr)
            sys.exit(1)
        success = get_issue(sys.argv[2])
        sys.exit(0 if success else 1)

    if command == "get-comments":
        if len(sys.argv) < 3:
            print("Usage: get-comments <identifier>", file=sys.stderr)
            sys.exit(1)
        success = get_comments(sys.argv[2])
        sys.exit(0 if success else 1)

    if command == "get-states":
        team_key = None
        for arg in sys.argv[2:]:
            if arg.startswith("--team="):
                team_key = arg.split("=", 1)[1]
        success = get_states(team_key)
        sys.exit(0 if success else 1)

    if command == "get-notifications":
        success = get_notifications()
        sys.exit(0 if success else 1)

    if command == "update-issue":
        if len(sys.argv) < 3:
            print("Usage: update-issue <identifier> --state=ID", file=sys.stderr)
            sys.exit(1)
        identifier = sys.argv[2]
        state_id = None
        assignee_id = None
        for arg in sys.argv[3:]:
            if arg.startswith("--state="):
                state_id = arg.split("=", 1)[1]
            elif arg.startswith("--assignee="):
                assignee_id = arg.split("=", 1)[1]
        success = update_issue(identifier, state_id=state_id, assignee_id=assignee_id)
        sys.exit(0 if success else 1)

    if command == "add-comment":
        if len(sys.argv) < 4:
            print("Usage: add-comment <identifier> <body>", file=sys.stderr)
            sys.exit(1)
        identifier = sys.argv[2]
        body = " ".join(sys.argv[3:])
        success = add_comment(identifier, body)
        sys.exit(0 if success else 1)

    # Activity commands require session_id and message
    if command not in activity_types:
        print(f"Unknown command: {command}", file=sys.stderr)
        print(
            f"Use one of: {', '.join(sorted(activity_types | api_commands))}",
            file=sys.stderr,
        )
        sys.exit(1)

    if len(sys.argv) < 4:
        print(
            f"Usage: {command} <session_id> [--ephemeral] [--signal=X] <message>",
            file=sys.stderr,
        )
        sys.exit(1)

    session_id = sys.argv[2]

    # Parse flags and message from remaining args
    ephemeral = False
    signal = None
    message_parts = []

    for arg in sys.argv[3:]:
        if arg == "--ephemeral":
            ephemeral = True
        elif arg.startswith("--signal="):
            signal = arg.split("=", 1)[1]
        else:
            message_parts.append(arg)

    message = " ".join(message_parts)

    if not message:
        print("Error: Message is required", file=sys.stderr)
        sys.exit(1)

    # Auto-refresh if token is expired
    if is_token_expired():
        print("Token expired, attempting refresh...", file=sys.stderr)
        if not refresh_token():
            print(
                "Warning: Could not refresh token, proceeding anyway", file=sys.stderr
            )

    success = emit_activity(
        session_id, message, command, ephemeral=ephemeral, signal=signal
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
