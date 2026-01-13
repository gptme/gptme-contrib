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
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, request, jsonify
import httpx

# Load environment from .env file
ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

# Configuration - Required environment variables (set in .env file)
PORT = int(os.environ.get("PORT", 8081))
WEBHOOK_SECRET = os.environ.get("LINEAR_WEBHOOK_SECRET")

# Required environment variables - use temp vars for validation
_agent_name = os.environ.get("AGENT_NAME")
_agent_workspace_str = os.environ.get("AGENT_WORKSPACE")
_default_branch = os.environ.get("DEFAULT_BRANCH")

# Validate required config at import time
_missing = []
if not _agent_name:
    _missing.append("AGENT_NAME")
if not _agent_workspace_str:
    _missing.append("AGENT_WORKSPACE")
if not _default_branch:
    _missing.append("DEFAULT_BRANCH")
if _missing:
    print(
        f"Error: Missing required environment variables: {', '.join(_missing)}",
        file=sys.stderr,
    )
    print(
        "Please set these in your .env file. See .env.template for examples.",
        file=sys.stderr,
    )
    sys.exit(1)

# Type narrowing for mypy - validated above
assert _agent_name is not None
assert _agent_workspace_str is not None
assert _default_branch is not None

# After validation, assign to typed constants
AGENT_NAME: str = _agent_name
AGENT_WORKSPACE: Path = Path(_agent_workspace_str)
DEFAULT_BRANCH: str = _default_branch

# Derived paths
LOGS_DIR = AGENT_WORKSPACE / "logs" / "linear-sessions"
NOTIFICATIONS_DIR = Path(
    os.environ.get(
        "NOTIFICATIONS_DIR", str(AGENT_WORKSPACE / "logs" / "linear-notifications")
    )
)
WORKTREE_BASE = Path(
    os.environ.get("WORKTREE_BASE", AGENT_WORKSPACE.parent / f"{AGENT_NAME}-worktrees")
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

# Track active issues to handle elicitation responses
# Maps issue_identifier -> (session_id, worktree_path)
active_issues: dict[str, tuple[str, Path]] = {}
active_issues_lock = threading.Lock()

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
            access_token = tokens.get("accessToken") or tokens.get("access_token")
            return str(access_token) if access_token else None
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
    timestamp = datetime.now(timezone.utc).isoformat()
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
            cwd=AGENT_WORKSPACE,
            capture_output=True,
        )

    # Fetch latest from origin
    subprocess.run(
        ["git", "fetch", "origin", DEFAULT_BRANCH],
        cwd=AGENT_WORKSPACE,
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
            f"origin/{DEFAULT_BRANCH}",
        ],
        cwd=AGENT_WORKSPACE,
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


def cleanup_worktree(session_id: str, worktree_path: Path):
    """Verify work is merged to upstream main, then clean up worktree.

    The agent is responsible for merging work. This function just verifies
    the commit is in origin/main before cleaning up.
    """
    branch_name = f"linear-session-{session_id}"

    # Fetch latest from origin
    subprocess.run(
        ["git", "fetch", "origin", DEFAULT_BRANCH],
        cwd=AGENT_WORKSPACE,
        capture_output=True,
    )

    # Get the latest commit from the worktree branch
    worktree_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )

    if worktree_commit.returncode != 0:
        print("⚠ Could not get commit from worktree", file=sys.stderr)
        return

    commit_sha = worktree_commit.stdout.strip()

    # Check if this commit is in origin/main (i.e., work was merged and pushed)
    check_merged = subprocess.run(
        ["git", "branch", "-r", "--contains", commit_sha],
        cwd=AGENT_WORKSPACE,
        capture_output=True,
        text=True,
    )

    if f"origin/{DEFAULT_BRANCH}" in check_merged.stdout:
        print(f"✓ Commit {commit_sha[:8]} verified in origin/{DEFAULT_BRANCH}")
    else:
        print(f"⚠ Commit {commit_sha[:8]} NOT found in origin/{DEFAULT_BRANCH}")
        emit_activity(
            session_id,
            f"⚠ Work not merged to {DEFAULT_BRANCH}. Branch `{branch_name}` commit {commit_sha[:8]} not in origin/{DEFAULT_BRANCH}. Please merge and push manually.",
            "thought",  # Use thought, not error - session work may still be valid
        )
        return

    # Work verified - safe to remove worktree
    try:
        if worktree_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", str(worktree_path)],
                cwd=AGENT_WORKSPACE,
                capture_output=True,
            )
            # Also delete the branch
            subprocess.run(
                ["git", "branch", "-d", branch_name],
                cwd=AGENT_WORKSPACE,
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

You have been {action} in Linear issue {issue.get("identifier", "unknown")}: {issue.get("title", "Unknown")}

## Context from Linear
{prompt_context}

## Your Tools

### Emit Activities to Linear

Use the linear-activity.py script to communicate with Linear. The user can see your progress in real-time!

**Activity Types:**
| Type | Purpose | Session Effect |
|------|---------|----------------|
| `thought` | Show reasoning/progress | Stays active |
| `action` | Tool invocation (e.g., "Running tests...") | Stays active |
| `response` | Final answer | **Closes session** |
| `elicitation` | Request info from user | Stays active |
| `error` | Error occurred | Marks as errored |

**Flags:**
- `--ephemeral` - Activity disappears after next one (great for progress updates)

**Examples:**
```bash
# Show progress (ephemeral - disappears when next update comes)
uv run {linear_activity_path} action {session_id} --ephemeral "Reading codebase..."
uv run {linear_activity_path} action {session_id} --ephemeral "Running tests..."
uv run {linear_activity_path} thought {session_id} --ephemeral "Found 3 issues, fixing..."

# Permanent updates (stay visible)
uv run {linear_activity_path} thought {session_id} "Analysis complete: found authentication bug in login.py"

# Final response (CLOSES the session)
uv run {linear_activity_path} response {session_id} "Fixed the bug. See commit abc123."

# Report error
uv run {linear_activity_path} error {session_id} "Failed to access repository"
```

### Progress Updates Best Practice

**IMPORTANT**: Keep the Linear user informed of your progress! Use ephemeral activities to show what you're doing:

1. When starting work: `action --ephemeral "Starting analysis..."`
2. During work: `action --ephemeral "Checking file X..."`
3. Key findings: `thought "Found issue: ..."` (non-ephemeral for important info)
4. Before completion: `thought "Preparing final response..."`
5. Final: `response "Done! Here's what I did..."`

## COMPLETION PROTOCOL (Required before exit)

1. ✅ Emit progress updates during work (use --ephemeral for transient status)
2. ✅ Submit final response via `response` command (this closes the session)
3. ✅ Update journal with session summary
4. ✅ Commit your changes (the webhook server will merge after you exit)
5. ✅ Exit
"""


def spawn_gptme(worktree_path: Path, prompt: str, session_id: str) -> int:
    """Spawn gptme in the worktree with logging."""
    print(f"Spawning gptme in {worktree_path}...")

    # Create log file for this session
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_file = LOGS_DIR / f"{timestamp}-{session_id}.log"

    try:
        with open(log_file, "w") as f:
            f.write(f"=== Linear Session: {session_id} ===\n")
            f.write(f"Started: {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"Worktree: {worktree_path}\n")
            f.write(f"Prompt: {prompt}\n")
            f.write("=" * 50 + "\n\n")
            f.flush()

            result = subprocess.run(
                ["gptme", "--non-interactive", prompt],
                cwd=worktree_path,
                timeout=GPTME_TIMEOUT,
                stdout=f,
                stderr=subprocess.STDOUT,
            )

            f.write(f"\n{'=' * 50}\n")
            f.write(f"Finished: {datetime.now(timezone.utc).isoformat()}\n")
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
            notification["error_time"] = datetime.now(timezone.utc).isoformat()
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

    # Get issue identifier for tracking
    issue_identifier = issue.get("identifier", "unknown")

    with session_locks[session_id]:
        worktree_path = None
        try:
            # Check if there's an existing worktree for this issue (e.g., from elicitation)
            with active_issues_lock:
                if issue_identifier in active_issues:
                    old_session_id, old_worktree_path = active_issues[issue_identifier]
                    if old_worktree_path.exists():
                        print(
                            f"Found existing worktree for {issue_identifier} from session {old_session_id}"
                        )
                        print("Checking if previous work was merged...")

                        # Verify and clean up old worktree (no auto-merge - agent is responsible)
                        cleanup_worktree(old_session_id, old_worktree_path)

                    # Remove from tracking
                    del active_issues[issue_identifier]

            # Create worktree
            worktree_path = create_worktree(session_id)

            # Track this session for the issue
            with active_issues_lock:
                active_issues[issue_identifier] = (session_id, worktree_path)

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
            # Clean up issue tracking
            with active_issues_lock:
                if issue_identifier in active_issues:
                    tracked_session, _ = active_issues[issue_identifier]
                    if tracked_session == session_id:
                        del active_issues[issue_identifier]

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
