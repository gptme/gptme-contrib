#!/usr/bin/env python3
# /// script
# dependencies = ["flask", "httpx", "python-dotenv"]
# ///
"""
Linear Webhook Server - Handle Linear agent session webhooks.

Receives webhooks from Linear, creates worktrees, and spawns gptme sessions.

Usage:
    ./linear-webhook.py                    # Start server on default port
    PORT=8081 ./linear-webhook.py          # Start on specific port
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, request, jsonify
import httpx

# Load environment from .env file
ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

# Configuration
PORT = int(os.environ.get("PORT", 8081))
WEBHOOK_SECRET = os.environ.get("LINEAR_WEBHOOK_SECRET")
NOTIFICATIONS_DIR = Path(
    os.environ.get("NOTIFICATIONS_DIR", "/tmp/lofty-linear-notifications")
)
LOFTY_WORKSPACE = Path(
    os.environ.get("LOFTY_WORKSPACE", Path.home() / "repos" / "lofty")
)
LOGS_DIR = LOFTY_WORKSPACE / "logs" / "linear-sessions"
WORKTREE_BASE = Path(
    os.environ.get("WORKTREE_BASE", Path.home() / "repos" / "lofty-worktrees")
)
GPTME_TIMEOUT = 30 * 60  # 30 minutes

# Linear API
LINEAR_API = "https://api.linear.app/graphql"
TOKENS_FILE = Path(__file__).parent / ".tokens.json"

# Session tracking for deduplication
processed_sessions: set[str] = set()
SESSION_DEDUP_TTL = 60 * 60  # 1 hour

# Session locks to prevent race conditions
session_locks: dict[str, threading.Lock] = {}

app = Flask(__name__)

# Ensure directories exist
NOTIFICATIONS_DIR.mkdir(parents=True, exist_ok=True)
WORKTREE_BASE.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Path to the linear-activity.py CLI
LINEAR_ACTIVITY_CLI = Path(__file__).parent / "linear-activity.py"


def ensure_valid_token() -> bool:
    """Ensure we have a valid token, refreshing if necessary.

    Uses the linear-activity.py CLI to check and refresh tokens.
    Returns True if we have a valid token, False otherwise.
    """
    try:
        # Check token status using existing CLI
        result = subprocess.run(
            ["uv", "run", str(LINEAR_ACTIVITY_CLI), "token-status"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        # If token is valid (exit code 0 and "Expired: False" in output)
        if result.returncode == 0 and "Expired: False" in result.stdout:
            return True

        # Token is expired, try to refresh
        print("Token expired, attempting refresh...", file=sys.stderr)
        refresh_result = subprocess.run(
            ["uv", "run", str(LINEAR_ACTIVITY_CLI), "refresh"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if refresh_result.returncode == 0:
            print("✓ Token refreshed successfully", file=sys.stderr)
            return True

        print(f"Failed to refresh token: {refresh_result.stderr}", file=sys.stderr)
        return False

    except subprocess.TimeoutExpired:
        print("Token check/refresh timed out", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Error checking/refreshing token: {e}", file=sys.stderr)
        return False


def get_access_token() -> str | None:
    """Get Linear access token from tokens file or environment."""
    if token := os.environ.get("LINEAR_ACCESS_TOKEN"):
        return token

    if TOKENS_FILE.exists():
        try:
            tokens = json.loads(TOKENS_FILE.read_text())
            # Support both camelCase and snake_case keys
            access_token: str | None = tokens.get("accessToken") or tokens.get(
                "access_token"
            )
            return access_token
        except (json.JSONDecodeError, IOError):
            pass

    return os.environ.get("LINEAR_API_KEY")


def verify_signature(payload: bytes, signature: str) -> bool:
    """Verify webhook signature using HMAC-SHA256."""
    if not signature or not WEBHOOK_SECRET:
        print("Missing signature or webhook secret", file=sys.stderr)
        return False

    expected = hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()

    return hmac.compare_digest(signature, expected)


def store_notification(payload: dict) -> Path:
    """Store webhook payload for logging."""
    timestamp = datetime.utcnow().isoformat()
    event_type = payload.get("type", "unknown")
    random_suffix = os.urandom(4).hex()
    filename = f"{int(time.time() * 1000)}-{random_suffix}-{event_type}.json"
    filepath = NOTIFICATIONS_DIR / filename

    notification = {
        "timestamp": timestamp,
        "payload": payload,
        "processed": False,
    }

    filepath.write_text(json.dumps(notification, indent=2))
    print(f"Stored notification: {filename}")
    return filepath


def emit_activity(
    session_id: str, content: str, activity_type: str = "thought"
) -> bool:
    """Emit an activity to a Linear agent session."""
    token = get_access_token()
    if not token:
        print("No access token available", file=sys.stderr)
        return False

    mutation = """
    mutation CreateAgentActivity($input: AgentActivityCreateInput!) {
      agentActivityCreate(input: $input) {
        success
        agentActivity { id }
      }
    }
    """

    # Content is a JSON object with type and body
    content_obj = {
        "type": activity_type,
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
        result = response.json()

        if result.get("data", {}).get("agentActivityCreate", {}).get("success"):
            print(f"Emitted {activity_type} activity to session {session_id}")
            return True

        print(f"Failed to emit activity: {result}", file=sys.stderr)
        return False

    except Exception as e:
        print(f"Error emitting activity: {e}", file=sys.stderr)
        return False


def create_worktree(session_id: str) -> Path:
    """Create a git worktree for the session."""
    worktree_name = f"linear-session-{session_id}"
    worktree_path = WORKTREE_BASE / worktree_name
    branch_name = worktree_name

    # Remove existing worktree if present
    if worktree_path.exists():
        print(f"Removing existing worktree: {worktree_path}")
        subprocess.run(
            ["git", "worktree", "remove", "-f", str(worktree_path)],
            cwd=LOFTY_WORKSPACE,
            capture_output=True,
        )

    # Fetch and update local main branch
    subprocess.run(
        ["git", "fetch", "origin", "main:main"],
        cwd=LOFTY_WORKSPACE,
        capture_output=True,
    )

    # Create worktree
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            str(worktree_path),
            "-B",
            branch_name,
            "origin/main",
        ],
        cwd=LOFTY_WORKSPACE,
        check=True,
    )

    # Initialize submodules
    print("Initializing submodules...")
    subprocess.run(
        ["git", "submodule", "update", "--init", "--recursive"],
        cwd=worktree_path,
        check=True,
    )

    print(f"✓ Created worktree: {worktree_path}")
    return worktree_path


def try_merge_worktree(session_id: str, worktree_path: Path) -> bool:
    """Try to merge worktree branch to main. Returns True if successful."""
    branch_name = f"linear-session-{session_id}"

    try:
        # Check if there are uncommitted changes
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )

        if status.stdout.strip():
            # Commit any uncommitted changes as WIP
            print(f"Committing uncommitted changes in {branch_name}...")
            subprocess.run(["git", "add", "-A"], cwd=worktree_path, check=True)
            subprocess.run(
                ["git", "commit", "-m", f"WIP: auto-commit from session {session_id}"],
                cwd=worktree_path,
                capture_output=True,
            )

        # Fetch latest main
        subprocess.run(
            ["git", "fetch", "origin", "main"],
            cwd=LOFTY_WORKSPACE,
            capture_output=True,
            check=True,
        )

        # Try to rebase worktree branch onto main
        rebase_result = subprocess.run(
            ["git", "rebase", "origin/main"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )

        if rebase_result.returncode != 0:
            # Rebase failed (conflict) - abort and return False
            subprocess.run(
                ["git", "rebase", "--abort"], cwd=worktree_path, capture_output=True
            )
            print(f"⚠ Merge conflict in {branch_name}, leaving for manual resolution")
            return False

        # Switch to main and merge
        subprocess.run(["git", "checkout", "main"], cwd=LOFTY_WORKSPACE, check=True)
        subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"],
            cwd=LOFTY_WORKSPACE,
            check=True,
        )

        merge_result = subprocess.run(
            [
                "git",
                "merge",
                "--no-ff",
                branch_name,
                "-m",
                f"feat(linear): merge session {session_id}",
            ],
            cwd=LOFTY_WORKSPACE,
            capture_output=True,
            text=True,
        )

        if merge_result.returncode != 0:
            # Merge failed - abort
            subprocess.run(
                ["git", "merge", "--abort"], cwd=LOFTY_WORKSPACE, capture_output=True
            )
            print(f"⚠ Merge to main failed for {branch_name}")
            return False

        # Push to origin
        push_result = subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=LOFTY_WORKSPACE,
            capture_output=True,
            text=True,
        )

        if push_result.returncode != 0:
            print(f"⚠ Push failed, will retry: {push_result.stderr}")
            # Pull and retry once
            subprocess.run(
                ["git", "pull", "--rebase", "origin", "main"], cwd=LOFTY_WORKSPACE
            )
            push_result = subprocess.run(
                ["git", "push", "origin", "main"],
                cwd=LOFTY_WORKSPACE,
                capture_output=True,
                text=True,
            )
            if push_result.returncode != 0:
                return False

        print(f"✓ Successfully merged {branch_name} to main")
        return True

    except Exception as e:
        print(f"Error during merge: {e}", file=sys.stderr)
        return False


def cleanup_worktree(session_id: str, worktree_path: Path):
    """Try to merge worktree to main, then clean up if successful."""
    branch_name = f"linear-session-{session_id}"

    # Try to merge first
    merge_success = try_merge_worktree(session_id, worktree_path)

    if not merge_success:
        print(f"⚠ Leaving worktree {worktree_path} for scheduled cleanup")
        emit_activity(
            session_id,
            f"⚠ Could not auto-merge branch {branch_name}. Left for manual review.",
            "error",
        )
        return

    # Merge succeeded - safe to remove worktree
    try:
        if worktree_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", str(worktree_path)],
                cwd=LOFTY_WORKSPACE,
                capture_output=True,
            )
            # Also delete the branch
            subprocess.run(
                ["git", "branch", "-d", branch_name],
                cwd=LOFTY_WORKSPACE,
                capture_output=True,
            )
            print(f"✓ Cleaned up worktree and branch for session {session_id}")
    except Exception as e:
        print(f"Error cleaning up worktree: {e}", file=sys.stderr)


def build_gptme_prompt(session_data: dict) -> str:
    """Build the prompt for gptme."""
    session_id = session_data["session_id"]
    action = session_data["action"]
    issue = session_data.get("issue", {})
    prompt_context = session_data.get("prompt_context", "")
    linear_activity_path = LINEAR_ACTIVITY_CLI

    return f"""# Linear Agent Session: {session_id}

You have been {action} in Linear issue {issue.get('identifier', 'unknown')}: {issue.get('title', 'Unknown')}

## Context from Linear
{prompt_context}

## Your Tools

### Emit Activities to Linear
Use Python script to show progress:
```bash
uv run {linear_activity_path} thought {session_id} "your thinking"
uv run {linear_activity_path} response {session_id} "final answer"
uv run {linear_activity_path} error {session_id} "error details"
```

## COMPLETION PROTOCOL (Required before exit)

1. ✅ Submit final response via linear-activity.py
2. ✅ Update journal with session summary
3. ✅ Commit and merge to main
4. ✅ Exit
"""


def spawn_gptme(worktree_path: Path, prompt: str, session_id: str) -> int:
    """Spawn gptme in the worktree with logging."""
    print(f"Spawning gptme in {worktree_path}...")

    # Create log file for this session
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    log_file = LOGS_DIR / f"{timestamp}-{session_id}.log"

    try:
        with open(log_file, "w") as f:
            f.write(f"=== Linear Session: {session_id} ===\n")
            f.write(f"Started: {datetime.utcnow().isoformat()}\n")
            f.write(f"Worktree: {worktree_path}\n")
            f.write(f"Prompt: {prompt}\n")
            f.write("=" * 50 + "\n\n")
            f.flush()

            result = subprocess.run(
                ["./run.sh", "--non-interactive", prompt],
                cwd=worktree_path,
                timeout=GPTME_TIMEOUT,
                stdout=f,
                stderr=subprocess.STDOUT,
            )

            f.write(f"\n{'=' * 50}\n")
            f.write(f"Finished: {datetime.utcnow().isoformat()}\n")
            f.write(f"Exit code: {result.returncode}\n")

        print(f"Session log: {log_file}")
        return result.returncode
    except subprocess.TimeoutExpired:
        print("gptme timed out", file=sys.stderr)
        with open(log_file, "a") as f:
            f.write(f"\n{'=' * 50}\n")
            f.write(f"TIMEOUT after {GPTME_TIMEOUT} seconds\n")
        return 1
    except Exception as e:
        print(f"Error spawning gptme: {e}", file=sys.stderr)
        with open(log_file, "a") as f:
            f.write(f"\n{'=' * 50}\n")
            f.write(f"ERROR: {e}\n")
        return 1


def process_agent_session_event(payload: dict, filepath: Path):
    """Process an AgentSessionEvent webhook."""
    agent_session = payload.get("agentSession", {})
    session_id = agent_session.get("id")
    action = payload.get("action")
    prompt_context = payload.get("promptContext", "")

    if not session_id:
        print("No session ID in payload", file=sys.stderr)
        return

    # CRITICAL: Validate token BEFORE doing any work
    # If we can't respond to Linear, there's no point spawning a session
    if not ensure_valid_token():
        print(
            f"ERROR: Cannot process session {session_id} - no valid token available!",
            file=sys.stderr,
        )
        print(
            "Webhook stored but session NOT spawned. Fix token and restart service.",
            file=sys.stderr,
        )
        # Mark notification as failed for later retry
        try:
            notification = json.loads(filepath.read_text())
            notification["processed"] = False
            notification["error"] = "token_expired"
            notification["error_time"] = datetime.utcnow().isoformat()
            filepath.write_text(json.dumps(notification, indent=2))
        except Exception as e:
            print(f"Failed to update notification file: {e}", file=sys.stderr)
        return

    # Deduplication
    if session_id in processed_sessions:
        print(f"Session {session_id} already processed, skipping")
        return

    processed_sessions.add(session_id)

    # Schedule cleanup after TTL
    def cleanup_session():
        time.sleep(SESSION_DEDUP_TTL)
        processed_sessions.discard(session_id)

    threading.Thread(target=cleanup_session, daemon=True).start()

    print(f"Processing AgentSessionEvent: {action} for session {session_id}")

    # Emit acknowledgment
    issue = agent_session.get("issue", {})
    emit_activity(
        session_id,
        f"👋 Acknowledged! Starting work on {issue.get('identifier', 'this issue')}...",
        "thought",
    )
    print(f"✓ Emitted acknowledgment for session {session_id}")

    # Get or create session lock
    if session_id not in session_locks:
        session_locks[session_id] = threading.Lock()

    with session_locks[session_id]:
        worktree_path = None
        try:
            # Create worktree
            worktree_path = create_worktree(session_id)

            # Build prompt
            session_data = {
                "session_id": session_id,
                "action": action,
                "issue": issue,
                "prompt_context": prompt_context,
            }
            prompt = build_gptme_prompt(session_data)

            # Spawn gptme
            exit_code = spawn_gptme(worktree_path, prompt, session_id)

            # Log file path for error reference
            log_ref = f"logs/linear-sessions/*-{session_id}.log"

            if exit_code == 0:
                print(f"✓ Session {session_id} completed successfully")
                # gptme should have sent its own response, but ensure session closes
            else:
                print(f"✗ Session {session_id} failed with exit code {exit_code}")
                emit_activity(
                    session_id,
                    f"Session failed with exit code {exit_code}. Log: {log_ref}",
                    "error",
                )
                # Send response to close the session
                emit_activity(
                    session_id,
                    f"❌ Session ended with errors (exit code {exit_code}). Check {log_ref} for details.",
                    "response",
                )

        except Exception as e:
            print(f"Error processing session: {e}", file=sys.stderr)
            log_ref = f"logs/linear-sessions/*-{session_id}.log"
            emit_activity(session_id, f"Fatal error: {str(e)}", "error")
            # Send response to close the session
            emit_activity(
                session_id,
                f"❌ Session crashed: {str(e)}. Check {log_ref} for details.",
                "response",
            )

        finally:
            if worktree_path:
                cleanup_worktree(session_id, worktree_path)


@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming webhooks from Linear."""
    signature = request.headers.get("Linear-Signature", "")
    raw_body = request.get_data()

    # Verify signature
    if not verify_signature(raw_body, signature):
        print("Invalid webhook signature", file=sys.stderr)
        return "Invalid signature", 401

    payload = request.get_json()

    if not payload:
        return "Invalid payload", 400

    event_type = payload.get("type", "unknown")
    action = payload.get("action", "unknown")

    print(f"Received webhook: {event_type} - {action}")

    # Store notification
    filepath = store_notification(payload)

    # Process AgentSessionEvent asynchronously
    if event_type == "AgentSessionEvent":
        thread = threading.Thread(
            target=process_agent_session_event,
            args=(payload, filepath),
            daemon=True,
        )
        thread.start()

    return "OK", 200


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify(
        {
            "status": "healthy",
            "port": PORT,
            "notifications_dir": str(NOTIFICATIONS_DIR),
        }
    )


@app.route("/", methods=["GET"])
def index():
    """Root endpoint."""
    return jsonify(
        {
            "service": "Linear Webhook Server",
            "version": "2.0.0 (Python)",
            "endpoints": {
                "/webhook": "POST - Linear webhook handler",
                "/health": "GET - Health check",
            },
        }
    )


def main():
    if not WEBHOOK_SECRET:
        print(
            "Error: LINEAR_WEBHOOK_SECRET environment variable required",
            file=sys.stderr,
        )
        print("Set it in .env file or environment", file=sys.stderr)
        sys.exit(1)

    print(f"Linear webhook server running on port {PORT}")
    print(f"Notifications directory: {NOTIFICATIONS_DIR}")
    print(f"Webhook endpoint: http://localhost:{PORT}/webhook")

    app.run(host="0.0.0.0", port=PORT, debug=False)


if __name__ == "__main__":
    main()
