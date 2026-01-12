#!/usr/bin/env python3
# /// script
# dependencies = ["httpx"]
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

import httpx

# Linear endpoints
LINEAR_API = "https://api.linear.app/graphql"
LINEAR_OAUTH_TOKEN = "https://api.linear.app/oauth/token"


# Find tokens - check multiple locations
def find_tokens_file() -> Path | None:
    """Find .tokens.json in various locations."""
    candidates = [
        Path(__file__).parent / ".tokens.json",
        Path(__file__).parent / "linear-webhook" / ".tokens.json",  # Legacy path
        Path.home() / ".config" / "linear" / "tokens.json",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def find_env_file() -> Path | None:
    """Find .env file with OAuth credentials."""
    candidates = [
        Path(__file__).parent / ".env",
        Path(__file__).parent / "linear-webhook" / ".env",  # Legacy path
        Path.home() / ".config" / "linear" / ".env",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def load_oauth_credentials() -> tuple[str, str] | None:
    """Load OAuth client credentials from env file."""
    # Check environment first
    client_id = os.environ.get("LINEAR_CLIENT_ID")
    client_secret = os.environ.get("LINEAR_CLIENT_SECRET")
    if client_id and client_secret:
        return client_id, client_secret

    # Load from .env file
    env_file = find_env_file()
    if not env_file:
        return None

    try:
        content = env_file.read_text()
        creds = {}
        for line in content.strip().split("\n"):
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                creds[key.strip()] = value.strip()
        
        client_id = creds.get("LINEAR_CLIENT_ID")
        client_secret = creds.get("LINEAR_CLIENT_SECRET")
        if client_id and client_secret:
            return client_id, client_secret
    except Exception:
        pass
    return None


def is_token_expired() -> bool:
    """Check if the current token is expired."""
    tokens_file = find_tokens_file()
    if not tokens_file:
        return True
    
    try:
        tokens = json.loads(tokens_file.read_text())
        expires_at = tokens.get("expiresAt", 0)
        # Convert from milliseconds if needed
        if expires_at > 1e12:
            expires_at = expires_at / 1000
        # Add 5 minute buffer
        return time.time() > (expires_at - 300)
    except Exception:
        return True


def refresh_token() -> bool:
    """Refresh the OAuth token."""
    tokens_file = find_tokens_file()
    if not tokens_file:
        print("Error: No tokens file found", file=sys.stderr)
        return False

    credentials = load_oauth_credentials()
    if not credentials:
        print("Error: No OAuth credentials found", file=sys.stderr)
        print("Set LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET in environment or .env file", file=sys.stderr)
        return False

    client_id, client_secret = credentials

    try:
        tokens = json.loads(tokens_file.read_text())
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
            "expiresAt": int((time.time() + new_tokens.get("expires_in", 86400)) * 1000),
        }
        tokens_file.write_text(json.dumps(save_tokens, indent=2))
        print(f"✓ Token refreshed, expires in {new_tokens.get('expires_in', 0) // 3600}h")
        return True

    except Exception as e:
        print(f"Error refreshing token: {e}", file=sys.stderr)
        return False


def token_status() -> None:
    """Print token status information."""
    tokens_file = find_tokens_file()
    if not tokens_file:
        print("No tokens file found")
        return

    try:
        tokens = json.loads(tokens_file.read_text())
        expires_at = tokens.get("expiresAt", 0)
        if expires_at > 1e12:
            expires_at = expires_at / 1000
        
        from datetime import datetime
        expiry = datetime.fromtimestamp(expires_at)
        now = datetime.now()
        
        print(f"Tokens file: {tokens_file}")
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
    tokens_file = find_tokens_file()
    if tokens_file:
        try:
            tokens = json.loads(tokens_file.read_text())
            # Support both camelCase and snake_case keys
            if access_token := (
                tokens.get("accessToken") or tokens.get("access_token")
            ):
                return access_token
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
        print(f"Warning: Unknown activity type '{activity_type}', using 'thought'", file=sys.stderr)
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
    input_obj = {
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

    variables = {
        "input": input_obj
    }

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


def main():
    # Valid activity types
    activity_types = {"thought", "action", "response", "elicitation", "error", "prompt"}
    
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nActivity Commands:")
        print("  thought <session_id> <message>      - Emit thinking/progress update")
        print("  action <session_id> <message>       - Emit tool/action invocation")
        print("  response <session_id> <message>     - Emit final response (closes session)")
        print("  elicitation <session_id> <message>  - Request information from user")
        print("  error <session_id> <message>        - Emit error message")
        print("  prompt <session_id> <message>       - Emit prompt/instruction")
        print("\nFlags (add before message):")
        print("  --ephemeral                         - Activity disappears after next one")
        print("  --signal=<signal>                   - Add signal: stop, continue, auth, select")
        print("\nToken Commands:")
        print("  refresh                             - Refresh OAuth token")
        print("  token-status                        - Check token status")
        print("\nExamples:")
        print("  thought abc123 'Analyzing code...'")
        print("  action abc123 --ephemeral 'Running tests...'")
        print("  response abc123 'Done! See PR #42'")
        sys.exit(1)

    command = sys.argv[1]

    # Token management commands
    if command == "refresh":
        success = refresh_token()
        sys.exit(0 if success else 1)

    if command == "token-status":
        token_status()
        sys.exit(0)

    # Activity commands require session_id and message
    if len(sys.argv) < 4:
        print(f"Usage: {command} <session_id> [--ephemeral] [--signal=X] <message>", file=sys.stderr)
        sys.exit(1)

    if command not in activity_types:
        print(f"Unknown command: {command}", file=sys.stderr)
        print(f"Use one of: {', '.join(sorted(activity_types))}, refresh, token-status", file=sys.stderr)
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
            print("Warning: Could not refresh token, proceeding anyway", file=sys.stderr)

    success = emit_activity(session_id, message, command, ephemeral=ephemeral, signal=signal)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
