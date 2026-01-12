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
    ./linear-activity.py auth                             # Initial OAuth flow
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
LINEAR_OAUTH_AUTHORIZE = "https://linear.app/oauth/authorize"


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


def do_auth() -> bool:
    """Perform initial OAuth authorization flow."""
    creds = load_oauth_credentials()
    if not creds:
        print("Error: LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET required in .env")
        print("Create scripts/linear/.env with these values from Linear OAuth app")
        return False

    client_id, client_secret = creds

    # Get callback URL from env if set, otherwise use a placeholder
    env_file = find_env_file()
    callback_url = "https://your-ngrok-domain/oauth/callback"
    if env_file:
        content = env_file.read_text()
        for line in content.strip().split("\n"):
            if line.startswith("LINEAR_CALLBACK_URL="):
                callback_url = line.split("=", 1)[1].strip()
                break

    # Build authorization URL
    scopes = "read,write,app:mentionable,app:assignable"
    auth_url = (
        f"{LINEAR_OAUTH_AUTHORIZE}?"
        f"client_id={client_id}&"
        f"redirect_uri={callback_url}&"
        f"scope={scopes}&"
        f"response_type=code&"
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
        print("\nAborted")
        return False

    # Extract code from URL
    if "code=" not in redirect_url:
        print("Error: No authorization code found in URL")
        return False

    code = redirect_url.split("code=")[1].split("&")[0]
    print("\nExchanging code for tokens...")

    # Exchange code for tokens
    try:
        with httpx.Client() as client:
            response = client.post(
                LINEAR_OAUTH_TOKEN,
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": callback_url,
                    "code": code,
                },
            )

            if response.status_code != 200:
                print(f"Error: {response.status_code} - {response.text}")
                return False

            data = response.json()
            tokens = {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token"),
                "expiresAt": int(time.time() + data.get("expires_in", 36000)) * 1000,
            }

            # Save tokens
            tokens_file = find_tokens_file()
            if not tokens_file:
                # Create in same directory as env file
                env_file = find_env_file()
                if env_file:
                    tokens_file = env_file.parent / ".tokens.json"
                else:
                    tokens_file = Path.cwd() / ".tokens.json"

            tokens_file.write_text(json.dumps(tokens, indent=2))
            print(f"✓ Tokens saved to {tokens_file}")
            return True

    except Exception as e:
        print(f"Error exchanging code: {e}")
        return False


def is_token_expired() -> bool:
    """Check if the current token is expired."""
    tokens_file = find_tokens_file()
    if not tokens_file:
        return True

    try:
        tokens = json.loads(tokens_file.read_text())
        expires_at: float = float(tokens.get("expiresAt", 0))
        # Convert from milliseconds if needed
        if expires_at > 1e12:
            expires_at = expires_at / 1000
        # Add 5 minute buffer
        return bool(time.time() > (expires_at - 300))
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
        print(
            "Set LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET in environment or .env file",
            file=sys.stderr,
        )
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
            "expiresAt": int(
                (time.time() + new_tokens.get("expires_in", 86400)) * 1000
            ),
        }
        tokens_file.write_text(json.dumps(save_tokens, indent=2))
        print(
            f"✓ Token refreshed, expires in {new_tokens.get('expires_in', 0) // 3600}h"
        )
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
            total_seconds = int(remaining.total_seconds())
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
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
            access_token: str | None = tokens.get("accessToken") or tokens.get(
                "access_token"
            )
            if access_token:
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
    session_id: str, content: str, activity_type: str = "thought"
) -> bool:
    """Emit an activity to a Linear agent session."""
    token = get_access_token()

    # Map activity types to Linear's expected format
    type_map = {
        "thought": "thought",
        "response": "response",
        "error": "error",
    }
    linear_type = type_map.get(activity_type, "thought")

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
        "type": linear_type,
        "body": content,
    }

    variables = {
        "input": {
            "agentSessionId": session_id,
            "content": content_obj,
        }
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
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nCommands:")
        print("  thought <session_id> <message>  - Emit a thinking/progress update")
        print("  response <session_id> <message> - Emit a final response")
        print("  error <session_id> <message>    - Emit an error message")
        print("  auth                            - Initial OAuth authorization flow")
        print("  refresh                         - Refresh OAuth token")
        print("  token-status                    - Check token status")
        sys.exit(1)

    command = sys.argv[1]

    # Token management commands
    if command == "auth":
        success = do_auth()
        sys.exit(0 if success else 1)

    if command == "refresh":
        success = refresh_token()
        sys.exit(0 if success else 1)

    if command == "token-status":
        token_status()
        sys.exit(0)

    # Activity commands require session_id and message
    if len(sys.argv) < 4:
        print(f"Usage: {command} <session_id> <message>", file=sys.stderr)
        sys.exit(1)

    session_id = sys.argv[2]
    message = " ".join(sys.argv[3:])

    if command not in ("thought", "response", "error"):
        print(f"Unknown command: {command}", file=sys.stderr)
        print(
            "Use: thought, response, error, refresh, or token-status", file=sys.stderr
        )
        sys.exit(1)

    # Auto-refresh if token is expired
    if is_token_expired():
        print("Token expired, attempting refresh...", file=sys.stderr)
        if not refresh_token():
            print(
                "Warning: Could not refresh token, proceeding anyway", file=sys.stderr
            )

    success = emit_activity(session_id, message, command)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
