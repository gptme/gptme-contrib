#!/usr/bin/env python3
# /// script
# dependencies = ["httpx", "python-dotenv"]
# ///
"""
Linear Activity CLI - Emit activities to Linear agent sessions.

Usage:
    ./linear-activity.py thought <session_id> <message>       # Show reasoning/progress
    ./linear-activity.py action <session_id> --action=<name> --parameter=<param>
                                                              # Tool invocation
    ./linear-activity.py response <session_id> <message>      # Final answer (closes session)
    ./linear-activity.py elicitation <session_id> <message>   # Request info from user
    ./linear-activity.py error <session_id> <message>         # Report error
    ./linear-activity.py refresh                              # Refresh OAuth token
    ./linear-activity.py token-status                         # Check token status

Common flags:
    --ephemeral    Activity disappears after next one (good for progress)
    --signal=X     Send signal: stop, continue, auth, select

Examples:
    # Progress update (ephemeral)
    ./linear-activity.py thought <session_id> --ephemeral "Reading files..."

    # Tool invocation
    ./linear-activity.py action <session_id> --action="shell" --parameter="git status"

    # Final response (closes the session)
    ./linear-activity.py response <session_id> "Done! Fixed the bug in commit abc123."

API Commands:
    ./linear-activity.py get-issue <identifier>           # Get issue details
    ./linear-activity.py get-comments <identifier>        # Get issue comments
    ./linear-activity.py get-states [--team=KEY]          # Get workflow states
    ./linear-activity.py get-notifications                # Get unread notifications
    ./linear-activity.py update-issue <id> --state=ID     # Update issue state
    ./linear-activity.py add-comment <id> <body>          # Add comment to issue
    ./linear-activity.py auth                             # Run OAuth flow
"""

import json
import os
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


# ============================================================================
# Custom Exceptions
# ============================================================================


class LinearCLIError(Exception):
    """Base exception for Linear CLI errors."""

    exit_code: int = 1

    def __init__(self, message: str, exit_code: int | None = None):
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class AuthenticationError(LinearCLIError):
    """Raised when authentication fails or credentials are missing."""

    exit_code: int = 2


class TokenExpiredError(AuthenticationError):
    """Raised when the OAuth token is expired and cannot be refreshed."""

    exit_code: int = 3


class APIError(LinearCLIError):
    """Raised when a Linear API request fails."""

    exit_code: int = 4

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        graphql_errors: list | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.graphql_errors = graphql_errors


class NotFoundError(APIError):
    """Raised when a requested resource is not found."""

    exit_code: int = 5


class ValidationError(LinearCLIError):
    """Raised when input validation fails."""

    exit_code: int = 6


# ============================================================================
# Constants and Configuration
# ============================================================================


class ActivityType(Enum):
    """Valid activity types for Linear agent sessions."""

    THOUGHT = "thought"
    ACTION = "action"
    RESPONSE = "response"
    ELICITATION = "elicitation"
    ERROR = "error"
    PROMPT = "prompt"


# Load environment from .env file in script directory
ENV_FILE = Path(__file__).parent / ".env"
TOKENS_FILE = Path(__file__).parent / ".tokens.json"
load_dotenv(ENV_FILE)

# Linear endpoints
LINEAR_API_URL = "https://api.linear.app/graphql"
LINEAR_OAUTH_TOKEN_URL = "https://api.linear.app/oauth/token"
LINEAR_OAUTH_AUTHORIZE_URL = "https://linear.app/oauth/authorize"


# ============================================================================
# Authentication Functions
# ============================================================================


def validate_auth_environment() -> dict[str, str]:
    """Validate all required environment variables for OAuth flow.

    Returns:
        Dict with validated credentials: client_id, client_secret, callback_url

    Raises:
        AuthenticationError: If any required credentials are missing
    """
    required_vars = {
        "LINEAR_CLIENT_ID": "From Linear OAuth Application settings (Settings > API > OAuth Applications)",
        "LINEAR_CLIENT_SECRET": "From Linear OAuth Application settings (Settings > API > OAuth Applications)",
        "LINEAR_CALLBACK_URL": "Your ngrok URL + /oauth/callback (e.g., https://abc123.ngrok-free.app/oauth/callback)",
    }

    values = {}
    missing = []

    for var, description in required_vars.items():
        value = os.environ.get(var)
        if value:
            values[var] = value
        else:
            missing.append(f"  - {var}: {description}")

    if missing:
        raise AuthenticationError(
            "Missing required environment variables for OAuth:\n"
            + "\n".join(missing)
            + f"\n\nSet these in {ENV_FILE} (see .env.template for examples)"
        )

    return values


def load_oauth_credentials() -> tuple[str, str]:
    """Load OAuth client credentials from environment.

    Returns:
        Tuple of (client_id, client_secret)

    Raises:
        AuthenticationError: If credentials are not found in environment
    """
    client_id = os.environ.get("LINEAR_CLIENT_ID")
    client_secret = os.environ.get("LINEAR_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise AuthenticationError(
            f"OAuth credentials not found. "
            f"Set LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET in {ENV_FILE}"
        )

    return client_id, client_secret


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


def load_tokens() -> dict[str, Any]:
    """Load tokens from the tokens file.

    Returns:
        Dictionary containing token data

    Raises:
        AuthenticationError: If tokens file doesn't exist or is invalid
    """
    if not TOKENS_FILE.exists():
        raise AuthenticationError(f"No tokens file found at {TOKENS_FILE}")

    try:
        result: dict[str, Any] = json.loads(TOKENS_FILE.read_text())
        return result
    except json.JSONDecodeError as e:
        raise AuthenticationError(f"Invalid tokens file: {e}")


def refresh_token() -> None:
    """Refresh the OAuth token.

    Raises:
        AuthenticationError: If credentials are missing
        TokenExpiredError: If refresh fails
    """
    tokens = load_tokens()
    client_id, client_secret = load_oauth_credentials()

    refresh_tok = tokens.get("refreshToken") or tokens.get("refresh_token")
    if not refresh_tok:
        raise TokenExpiredError("No refresh token in tokens file")

    try:
        response = httpx.post(
            LINEAR_OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_tok,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=30.0,
        )
        response.raise_for_status()

        new_tokens = response.json()

        if "access_token" not in new_tokens:
            raise TokenExpiredError(f"Token refresh failed: {new_tokens}")

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

    except httpx.HTTPError as e:
        raise TokenExpiredError(f"HTTP error during token refresh: {e}")


def do_auth() -> None:
    """Perform initial OAuth authorization flow.

    Raises:
        AuthenticationError: If credentials are missing or auth fails
    """
    # Validate all required env vars upfront (shows all missing at once)
    env = validate_auth_environment()
    client_id = env["LINEAR_CLIENT_ID"]
    client_secret = env["LINEAR_CLIENT_SECRET"]
    callback_url = env["LINEAR_CALLBACK_URL"]

    # Build authorization URL
    scopes = "read,write,app:mentionable,app:assignable,initiative:read,initiative:write,issues:create,comments:create"
    auth_url = (
        f"{LINEAR_OAUTH_AUTHORIZE_URL}?"
        f"client_id={client_id}&"
        f"redirect_uri={callback_url}&"
        f"scope={scopes}&"
        f"response_type=code&"
        f"actor=app&"
        f"state=auth"
    )

    print("=== Linear OAuth Authorization ===\n")
    print("1. Open this URL in your browser:\n")
    print(f"   {auth_url}\n")
    print("2. Authorize the application in Linear")
    print("3. After redirect, copy the FULL redirect URL (with ?code=...)")
    print("4. Paste the URL here:\n")

    try:
        redirect_url = input("Redirect URL: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise AuthenticationError("Authorization aborted")

    # Extract code from URL
    if "code=" not in redirect_url:
        raise AuthenticationError("No authorization code found in URL")

    code = redirect_url.split("code=")[1].split("&")[0]
    print("\nExchanging code for tokens...")

    # Exchange code for tokens
    try:
        response = httpx.post(
            LINEAR_OAUTH_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": callback_url,
                "code": code,
            },
            timeout=30.0,
        )

        if response.status_code != 200:
            raise AuthenticationError(
                f"Token exchange failed: {response.status_code} - {response.text}"
            )

        data = response.json()
        if "access_token" not in data:
            raise AuthenticationError(f"Token exchange failed: {data}")

        tokens = {
            "accessToken": data["access_token"],
            "refreshToken": data.get("refresh_token"),
            "tokenType": data.get("token_type", "Bearer"),
            "scope": data.get("scope"),
            "actorType": "application",
            "expiresAt": int((time.time() + data.get("expires_in", 36000)) * 1000),
        }

        TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
        print(f"✓ Tokens saved to {TOKENS_FILE}")
        print(f"  Expires in {data.get('expires_in', 0) // 3600}h")

    except httpx.HTTPError as e:
        raise AuthenticationError(f"HTTP error during token exchange: {e}")


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
            total_seconds = int(remaining.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            print(f"Remaining: {hours}h {minutes}m")
        print(f"Scope: {tokens.get('scope', 'unknown')}")
    except Exception as e:
        print(f"Error reading tokens: {e}")


def get_access_token() -> str:
    """Get Linear access token from tokens file or environment.

    Returns:
        Access token string

    Raises:
        AuthenticationError: If no token is available
    """
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
            raise AuthenticationError(f"Failed to read tokens file: {e}")

    raise AuthenticationError(
        "No Linear access token found. "
        "Set LINEAR_ACCESS_TOKEN environment variable or run 'auth' command"
    )


def ensure_valid_token() -> None:
    """Ensure we have a valid token, refreshing if needed.

    Raises:
        TokenExpiredError: If token is expired and cannot be refreshed
    """
    if is_token_expired():
        print("Token expired, attempting refresh...", file=sys.stderr)
        refresh_token()


# ============================================================================
# GraphQL API Functions
# ============================================================================


def _graphql_request(query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL request against Linear API.

    Args:
        query: GraphQL query string
        variables: Optional query variables

    Returns:
        Response data dictionary

    Raises:
        APIError: If the request fails or returns errors
    """
    token = get_access_token()

    try:
        response = httpx.post(
            LINEAR_API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": variables or {}},
            timeout=30.0,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise APIError(
            f"HTTP {e.response.status_code}: {e.response.text}",
            status_code=e.response.status_code,
        )
    except httpx.HTTPError as e:
        raise APIError(f"HTTP error: {e}")

    result = response.json()

    if errors := result.get("errors"):
        raise APIError(
            f"GraphQL errors: {errors}",
            graphql_errors=errors,
        )

    data = result.get("data", {})
    return dict(data) if data else {}


# ============================================================================
# Activity Functions
# ============================================================================


def emit_activity(
    session_id: str,
    content: str,
    activity_type: str | ActivityType = ActivityType.THOUGHT,
    ephemeral: bool = False,
    signal: str | None = None,
    action_name: str | None = None,
    action_param: str | None = None,
) -> str:
    """Emit an activity to a Linear agent session.

    Args:
        session_id: The Linear agent session ID
        content: The message content (used as 'body' for most types)
        activity_type: ActivityType enum or string
        ephemeral: If True, activity disappears after the next one
        signal: Optional signal: stop, continue, auth, select
        action_name: Required for 'action' type - the action being performed
        action_param: Required for 'action' type - the parameter for the action

    Returns:
        Activity ID on success

    Raises:
        ValidationError: If required parameters are missing
        APIError: If the API request fails
    """
    # Normalize activity_type to string value
    if isinstance(activity_type, ActivityType):
        activity_type_str = activity_type.value
    else:
        # Validate string against enum values
        valid_values = {t.value for t in ActivityType}
        if activity_type not in valid_values:
            print(
                f"Warning: Unknown activity type '{activity_type}', using 'thought'",
                file=sys.stderr,
            )
            activity_type_str = ActivityType.THOUGHT.value
        else:
            activity_type_str = activity_type

    # Content structure depends on activity type
    if activity_type_str == "action":
        if not action_name or not action_param:
            raise ValidationError(
                "'action' type requires --action and --parameter flags"
            )
        content_obj = {
            "type": activity_type_str,
            "action": action_name,
            "parameter": action_param,
        }
    else:
        content_obj = {
            "type": activity_type_str,
            "body": content,
        }

    # Build input
    input_obj: dict[str, Any] = {
        "agentSessionId": session_id,
        "content": content_obj,
    }

    if ephemeral:
        input_obj["ephemeral"] = True

    if signal:
        valid_signals = {"stop", "continue", "auth", "select"}
        if signal not in valid_signals:
            print(f"Warning: Unknown signal '{signal}', ignoring", file=sys.stderr)
        else:
            input_obj["signal"] = signal

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

    data = _graphql_request(mutation, {"input": input_obj})
    result = data.get("agentActivityCreate", {})

    if not result.get("success"):
        raise APIError(f"Failed to emit activity: {data}")

    activity_id: str = result.get("agentActivity", {}).get("id", "unknown")
    print(
        f"✓ Emitted {activity_type_str} to session {session_id} (activity: {activity_id})"
    )
    return activity_id


# ============================================================================
# Linear API Wrapper Functions
# ============================================================================


def get_issue(identifier: str) -> dict[str, Any]:
    """Get issue details by identifier (e.g., SUDO-3).

    Args:
        identifier: Issue identifier (e.g., SUDO-3)

    Returns:
        Issue data dictionary

    Raises:
        NotFoundError: If the issue is not found
    """
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
        raise NotFoundError(f"Issue {identifier} not found")

    # Print formatted output
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

    return dict(issue)


def get_comments(identifier: str) -> list[dict[str, Any]]:
    """Get comments on an issue.

    Args:
        identifier: Issue identifier

    Returns:
        List of comment dictionaries

    Raises:
        NotFoundError: If the issue is not found
    """
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
        raise NotFoundError(f"Issue {identifier} not found")

    comments = issue.get("comments", {}).get("nodes", [])
    print(f"Comments on {issue['identifier']} - {issue['title']}:\n")

    if not comments:
        print("No comments")
    else:
        for c in comments:
            user = c.get("user", {}).get("displayName", "Unknown")
            print(f"--- {user} ({c['createdAt'][:10]}) ---")
            print(c.get("body", "")[:500])
            print()

    return list(comments)


def get_states(team_key: str | None = None) -> list[dict[str, Any]]:
    """Get workflow states, optionally filtered by team.

    Args:
        team_key: Optional team key to filter by

    Returns:
        List of state dictionaries

    Raises:
        NotFoundError: If the team is not found
    """
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
            raise NotFoundError(f"Team {team_key} not found")
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

    return list(states)


def get_notifications() -> list[dict[str, Any]]:
    """Get unread notifications.

    Returns:
        List of unread notification dictionaries
    """
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

    return unread


def update_issue(
    identifier: str, state_id: str | None = None, assignee_id: str | None = None
) -> dict[str, Any]:
    """Update an issue's state or assignee.

    Args:
        identifier: Issue identifier
        state_id: Optional new state ID
        assignee_id: Optional new assignee ID

    Returns:
        Updated issue data

    Raises:
        NotFoundError: If the issue is not found
        ValidationError: If no updates are specified
    """
    if not state_id and not assignee_id:
        raise ValidationError("No updates specified (use --state or --assignee)")

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
        raise NotFoundError(f"Issue {identifier} not found")

    issue_id = issue["id"]

    # Build update input
    update_input = {}
    if state_id:
        update_input["stateId"] = state_id
    if assignee_id:
        update_input["assigneeId"] = assignee_id

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

    if not result.get("success"):
        raise APIError("Failed to update issue")

    updated = result.get("issue", {})
    print(f"✓ Updated {updated.get('identifier', identifier)}")
    if state_id:
        print(f"  State: {updated.get('state', {}).get('name', 'unknown')}")
    if assignee_id:
        print(
            f"  Assignee: {updated.get('assignee', {}).get('displayName', 'unassigned')}"
        )

    return dict(updated) if updated else {}


def add_comment(identifier: str, body: str) -> dict[str, Any]:
    """Add a comment to an issue.

    Args:
        identifier: Issue identifier
        body: Comment body text

    Returns:
        Created comment data

    Raises:
        NotFoundError: If the issue is not found
    """
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
        raise NotFoundError(f"Issue {identifier} not found")

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

    if not result.get("success"):
        raise APIError("Failed to add comment")

    print(f"✓ Added comment to {identifier}")
    comment: dict[str, Any] = result.get("comment", {})
    return comment


# ============================================================================
# CLI Main
# ============================================================================


def print_help() -> None:
    """Print help message."""
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
    print("  --ephemeral                         - Activity disappears after next one")
    print(
        "  --signal=<signal>                   - Add signal: stop, continue, auth, select"
    )
    print("\nAPI Commands:")
    print("  get-issue <identifier>              - Get issue details (e.g., SUDO-3)")
    print("  get-comments <identifier>           - Get comments on an issue")
    print("  get-states [--team=KEY]             - Get workflow states")
    print("  get-notifications                   - Get unread notifications")
    print("  update-issue <id> --state=ID        - Update issue state")
    print("  add-comment <identifier> <body>     - Add comment to issue")
    print("\nToken Commands:")
    print("  auth                                - Initial OAuth authorization")
    print("  refresh                             - Refresh OAuth token")
    print("  token-status                        - Check token status")
    print("\nExamples:")
    print("  thought abc123 'Analyzing code...'")
    print("  action abc123 --ephemeral 'Running tests...'")
    print("  get-issue SUDO-3")
    print("  add-comment SUDO-3 'Fixed in PR #42'")


def main() -> int:
    """Main entry point.

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
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

    # Show help if no args or -h/--help requested
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print_help()
        # Exit 0 for explicit --help, 1 for missing args
        return 0 if len(sys.argv) >= 2 and sys.argv[1] in ("-h", "--help") else 1

    command = sys.argv[1]

    try:
        # Token management commands
        if command == "auth":
            do_auth()
            return 0

        if command == "refresh":
            refresh_token()
            return 0

        if command == "token-status":
            token_status()
            return 0

        # Auto-refresh for API commands if token expired
        if command in api_commands or command in activity_types:
            ensure_valid_token()

        # API wrapper commands
        if command == "get-issue":
            if len(sys.argv) < 3:
                raise ValidationError("Usage: get-issue <identifier>")
            get_issue(sys.argv[2])
            return 0

        if command == "get-comments":
            if len(sys.argv) < 3:
                raise ValidationError("Usage: get-comments <identifier>")
            get_comments(sys.argv[2])
            return 0

        if command == "get-states":
            team_key = None
            for arg in sys.argv[2:]:
                if arg.startswith("--team="):
                    team_key = arg.split("=", 1)[1]
            get_states(team_key)
            return 0

        if command == "get-notifications":
            get_notifications()
            return 0

        if command == "update-issue":
            if len(sys.argv) < 3:
                raise ValidationError("Usage: update-issue <identifier> --state=ID")
            identifier = sys.argv[2]
            state_id = None
            assignee_id = None
            for arg in sys.argv[3:]:
                if arg.startswith("--state="):
                    state_id = arg.split("=", 1)[1]
                elif arg.startswith("--assignee="):
                    assignee_id = arg.split("=", 1)[1]
            update_issue(identifier, state_id=state_id, assignee_id=assignee_id)
            return 0

        if command == "add-comment":
            if len(sys.argv) < 4:
                raise ValidationError("Usage: add-comment <identifier> <body>")
            identifier = sys.argv[2]
            body = " ".join(sys.argv[3:])
            add_comment(identifier, body)
            return 0

        # Activity commands require session_id and message
        if command not in activity_types:
            raise ValidationError(
                f"Unknown command: {command}\n"
                f"Use one of: {', '.join(sorted(activity_types | api_commands))}"
            )

        if len(sys.argv) < 4:
            if command == "action":
                raise ValidationError(
                    f"Usage: {command} <session_id> --action=<name> --parameter=<param> [--ephemeral] [--signal=X]"
                )
            else:
                raise ValidationError(
                    f"Usage: {command} <session_id> [--ephemeral] [--signal=X] <message>"
                )

        session_id = sys.argv[2]

        # Parse flags and message from remaining args
        ephemeral = False
        signal = None
        action_name = None
        action_param = None
        message_parts = []

        for arg in sys.argv[3:]:
            if arg == "--ephemeral":
                ephemeral = True
            elif arg.startswith("--signal="):
                signal = arg.split("=", 1)[1]
            elif arg.startswith("--action="):
                action_name = arg.split("=", 1)[1]
            elif arg.startswith("--parameter="):
                action_param = arg.split("=", 1)[1]
            else:
                message_parts.append(arg)

        message = " ".join(message_parts)

        # For 'action' type, message is optional but action/parameter are required
        if command != "action" and not message:
            raise ValidationError("Message is required")

        emit_activity(
            session_id,
            message,
            command,
            ephemeral=ephemeral,
            signal=signal,
            action_name=action_name,
            action_param=action_param,
        )
        return 0

    except LinearCLIError as e:
        print(f"Error: {e}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        print("\nAborted", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
