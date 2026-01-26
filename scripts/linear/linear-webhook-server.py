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
import html
import json
import logging
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


# Configure structured logging
def setup_logging() -> logging.Logger:
    """Set up structured logging with file and console handlers."""
    logger = logging.getLogger("linear-webhook")
    logger.setLevel(logging.DEBUG)

    # Console handler with concise format
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)
    console_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    return logger


# Initialize logger early
log = setup_logging()

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
LINEAR_OAUTH_TOKEN_URL = "https://api.linear.app/oauth/token"
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

# Add file handler now that LOGS_DIR is defined
_file_handler = logging.FileHandler(LOGS_DIR / "webhook-server.log")
_file_handler.setLevel(logging.DEBUG)
_file_fmt = logging.Formatter(
    "%(asctime)s [%(levelname)s] [%(funcName)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_file_handler.setFormatter(_file_fmt)
log.addHandler(_file_handler)

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
        log.warning("Token expired, attempting refresh...")
        refresh_result = subprocess.run(
            ["uv", "run", str(LINEAR_ACTIVITY_CLI), "refresh"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if refresh_result.returncode == 0:
            log.info("Token refreshed successfully")
            return True

        log.error(f"Failed to refresh token: {refresh_result.stderr}")
        return False

    except subprocess.TimeoutExpired:
        log.error("Token check/refresh timed out")
        return False
    except Exception as e:
        log.error(f"Error checking/refreshing token: {e}")
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

    return None


def verify_signature(payload: bytes, signature: str) -> bool:
    """Verify webhook signature using HMAC-SHA256."""
    if not signature or not WEBHOOK_SECRET:
        log.warning("Missing signature or webhook secret")
        return False

    expected = hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()

    return hmac.compare_digest(signature, expected)


def store_notification(payload: dict) -> Path:
    """Store webhook payload for logging."""
    timestamp = datetime.now(timezone.utc).isoformat()
    event_type = payload.get("type", "unknown")
    action = payload.get("action", "unknown")
    random_suffix = os.urandom(4).hex()
    filename = f"{int(time.time() * 1000)}-{random_suffix}-{event_type}.json"
    filepath = NOTIFICATIONS_DIR / filename

    # Extract key identifiers for logging
    session_id = payload.get("agentSession", {}).get("id", "no-session")
    issue_id = (
        payload.get("agentSession", {}).get("issue", {}).get("identifier", "no-issue")
    )

    notification = {
        "timestamp": timestamp,
        "payload": payload,
        "processed": False,
    }

    filepath.write_text(json.dumps(notification, indent=2))
    log.info(
        f"WEBHOOK_STORED | type={event_type} action={action} session={session_id} issue={issue_id} file={filename}"
    )
    return filepath


def emit_activity(
    session_id: str, content: str, activity_type: str = "thought"
) -> bool:
    """Emit an activity to a Linear agent session."""
    token = get_access_token()
    if not token:
        log.error(
            f"SESSION_ACTIVITY_FAILED | session={session_id} type={activity_type} reason=no_access_token"
        )
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
            activity_id = (
                result.get("data", {})
                .get("agentActivityCreate", {})
                .get("agentActivity", {})
                .get("id", "unknown")
            )
            log.info(
                f"SESSION_ACTIVITY_SENT | session={session_id} type={activity_type} activity_id={activity_id}"
            )
            return True

        log.error(
            f"SESSION_ACTIVITY_FAILED | session={session_id} type={activity_type} result={result}"
        )
        return False

    except Exception as e:
        log.error(
            f"SESSION_ACTIVITY_ERROR | session={session_id} type={activity_type} error={e}"
        )
        return False


def create_worktree(session_id: str) -> Path:
    """Create a git worktree for the session."""
    worktree_name = f"linear-session-{session_id}"
    worktree_path = WORKTREE_BASE / worktree_name
    branch_name = worktree_name

    log.debug(f"SESSION_WORKTREE_START | session={session_id} path={worktree_path}")

    # Remove existing worktree if present
    if worktree_path.exists():
        log.info(
            f"SESSION_WORKTREE_CLEANUP | session={session_id} path={worktree_path}"
        )
        subprocess.run(
            ["git", "worktree", "remove", "-f", str(worktree_path)],
            cwd=AGENT_WORKSPACE,
            capture_output=True,
        )

    # Fetch latest from origin
    fetch_result = subprocess.run(
        ["git", "fetch", "origin", DEFAULT_BRANCH],
        cwd=AGENT_WORKSPACE,
        capture_output=True,
        text=True,
    )
    fetch_failed = fetch_result.returncode != 0
    if fetch_failed:
        log.warning(
            f"SESSION_GIT_FETCH_FAILED | session={session_id} error={fetch_result.stderr.strip()}"
        )

    # Create worktree - use local branch if fetch failed, origin otherwise
    base_ref = DEFAULT_BRANCH if fetch_failed else f"origin/{DEFAULT_BRANCH}"
    worktree_result = subprocess.run(
        [
            "git",
            "worktree",
            "add",
            str(worktree_path),
            "-B",
            branch_name,
            base_ref,
        ],
        cwd=AGENT_WORKSPACE,
        capture_output=True,
        text=True,
    )

    if worktree_result.returncode != 0:
        log.error(
            f"SESSION_WORKTREE_FAILED | session={session_id} error={worktree_result.stderr.strip()}"
        )
        raise RuntimeError(f"git worktree add failed: {worktree_result.stderr}")

    # Verify worktree was actually created
    if not worktree_path.exists():
        log.error(
            f"SESSION_WORKTREE_MISSING | session={session_id} path={worktree_path}"
        )
        raise RuntimeError(
            f"git worktree add reported success but directory not created: {worktree_path}"
        )

    # Initialize submodules
    log.debug(f"SESSION_SUBMODULES_INIT | session={session_id}")
    subprocess.run(
        ["git", "submodule", "update", "--init", "--recursive"],
        cwd=str(worktree_path),  # Convert Path to string explicitly
        check=True,
    )

    log.info(
        f"SESSION_WORKTREE_CREATED | session={session_id} path={worktree_path} branch={branch_name}"
    )
    return worktree_path


def cleanup_worktree(session_id: str, worktree_path: Path):
    """Verify work is merged to upstream main, then clean up worktree.

    The agent is responsible for merging work. This function just verifies
    the commit is in origin/main before cleaning up.
    """
    branch_name = f"linear-session-{session_id}"

    log.debug(f"SESSION_CLEANUP_START | session={session_id} path={worktree_path}")

    # Fetch latest from origin
    subprocess.run(
        ["git", "fetch", "origin", DEFAULT_BRANCH],
        cwd=AGENT_WORKSPACE,
        capture_output=True,
    )

    # Get the latest commit from the worktree branch
    worktree_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(worktree_path),  # Convert Path to string explicitly
        capture_output=True,
        text=True,
    )

    if worktree_commit.returncode != 0:
        log.warning(f"SESSION_CLEANUP_NO_COMMIT | session={session_id}")
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
        log.info(
            f"SESSION_MERGE_VERIFIED | session={session_id} commit={commit_sha[:8]} branch=origin/{DEFAULT_BRANCH}"
        )
    else:
        log.warning(
            f"SESSION_NOT_MERGED | session={session_id} commit={commit_sha[:8]} branch={branch_name}"
        )
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
            log.info(
                f"SESSION_CLEANUP_COMPLETE | session={session_id} branch={branch_name}"
            )
    except Exception as e:
        log.error(f"SESSION_CLEANUP_ERROR | session={session_id} error={e}")


def build_gptme_prompt(session_data: dict) -> str:
    """Build the prompt for gptme."""
    session_id = session_data["session_id"]
    action = session_data["action"]
    issue = session_data.get("issue", {})
    prompt_context = session_data.get("prompt_context", "")
    linear_activity_path = LINEAR_ACTIVITY_CLI
    branch_name = f"linear-session-{session_id}"
    main_workspace = AGENT_WORKSPACE

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

**CRITICAL**: You MUST emit a `response` activity to close this Linear session.
Posting comments via GraphQL API does NOT close the session - only `response` does!

1. ✅ Emit progress updates during work (use --ephemeral for transient status)
2. ✅ Update journal with session summary
3. ✅ Commit and push your branch
4. ✅ Merge to main and push (from main workspace, not worktree):
   ```bash
   cd {main_workspace} && git fetch origin && git checkout {DEFAULT_BRANCH} && git pull && git merge origin/{branch_name} && git push
   ```
5. ✅ **EMIT RESPONSE** (this closes the Linear session):
   ```bash
   uv run {linear_activity_path} response {session_id} "Summary of what you accomplished"
   ```
6. ✅ Exit
"""


def spawn_gptme(worktree_path: Path, prompt: str, session_id: str) -> int:
    """Spawn gptme in the worktree with logging."""
    # Create log file for this session
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_file = LOGS_DIR / f"{timestamp}-{session_id}.log"

    log.info(
        f"SESSION_GPTME_SPAWN | session={session_id} worktree={worktree_path} log_file={log_file}"
    )

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
                cwd=str(worktree_path),  # Convert Path to string explicitly
                timeout=GPTME_TIMEOUT,
                stdout=f,
                stderr=subprocess.STDOUT,
            )

            f.write(f"\n{'=' * 50}\n")
            f.write(f"Finished: {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"Exit code: {result.returncode}\n")

        log.info(
            f"SESSION_GPTME_COMPLETE | session={session_id} exit_code={result.returncode} log_file={log_file}"
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        log.error(
            f"SESSION_GPTME_TIMEOUT | session={session_id} timeout={GPTME_TIMEOUT}s log_file={log_file}"
        )
        with open(log_file, "a") as f:
            f.write(f"\n{'=' * 50}\n")
            f.write(f"TIMEOUT after {GPTME_TIMEOUT} seconds\n")
        return 1
    except Exception as e:
        log.error(
            f"SESSION_GPTME_ERROR | session={session_id} error={e} log_file={log_file}"
        )
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
    issue = agent_session.get("issue", {})
    issue_identifier = issue.get("identifier", "unknown")
    issue_title = issue.get("title", "Unknown")

    if not session_id:
        log.error("SESSION_NO_ID | payload missing session ID")
        return

    log.info(
        f'SESSION_RECEIVED | session={session_id} action={action} issue={issue_identifier} title="{issue_title}"'
    )

    # CRITICAL: Validate token BEFORE doing any work
    # If we can't respond to Linear, there's no point spawning a session
    if not ensure_valid_token():
        log.error(
            f"SESSION_TOKEN_INVALID | session={session_id} issue={issue_identifier} - cannot proceed without valid token"
        )
        # Mark notification as failed for later retry
        try:
            notification = json.loads(filepath.read_text())
            notification["processed"] = False
            notification["error"] = "token_expired"
            notification["error_time"] = datetime.now(timezone.utc).isoformat()
            filepath.write_text(json.dumps(notification, indent=2))
        except Exception as e:
            log.error(
                f"SESSION_NOTIFICATION_UPDATE_FAILED | session={session_id} error={e}"
            )
        return

    # Deduplication
    if session_id in processed_sessions:
        log.info(
            f"SESSION_DUPLICATE | session={session_id} - already processed, skipping"
        )
        return

    processed_sessions.add(session_id)

    # Schedule cleanup after TTL
    def cleanup_session():
        time.sleep(SESSION_DEDUP_TTL)
        processed_sessions.discard(session_id)

    threading.Thread(target=cleanup_session, daemon=True).start()

    log.info(
        f"SESSION_PROCESSING | session={session_id} issue={issue_identifier} action={action}"
    )

    # Emit acknowledgment
    emit_activity(
        session_id,
        f"👋 Acknowledged! Starting work on {issue_identifier}...",
        "thought",
    )
    log.info(f"SESSION_ACKNOWLEDGED | session={session_id} issue={issue_identifier}")

    # Get or create session lock
    if session_id not in session_locks:
        session_locks[session_id] = threading.Lock()

    with session_locks[session_id]:
        worktree_path = None
        try:
            # Check if there's an existing worktree for this issue (e.g., from elicitation)
            with active_issues_lock:
                if issue_identifier in active_issues:
                    old_session_id, old_worktree_path = active_issues[issue_identifier]
                    if old_worktree_path.exists():
                        log.info(
                            f"SESSION_EXISTING_WORKTREE | session={session_id} issue={issue_identifier} old_session={old_session_id}"
                        )
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

            log.info(
                f"SESSION_SPAWNING | session={session_id} issue={issue_identifier} worktree={worktree_path}"
            )

            # Spawn gptme
            exit_code = spawn_gptme(worktree_path, prompt, session_id)

            # Log file path for error reference
            log_ref = f"logs/linear-sessions/*-{session_id}.log"

            if exit_code == 0:
                log.info(
                    f"SESSION_SUCCESS | session={session_id} issue={issue_identifier} exit_code={exit_code}"
                )
                # gptme should have sent its own response, but ensure session closes
            else:
                log.error(
                    f"SESSION_FAILED | session={session_id} issue={issue_identifier} exit_code={exit_code}"
                )
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
            log.exception(
                f"SESSION_EXCEPTION | session={session_id} issue={issue_identifier} error={e}"
            )
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
        log.warning(
            "WEBHOOK_SIGNATURE_INVALID | rejecting request with invalid signature"
        )
        return "Invalid signature", 401

    payload = request.get_json()

    if not payload:
        log.warning("WEBHOOK_INVALID_PAYLOAD | empty or invalid JSON payload")
        return "Invalid payload", 400

    event_type = payload.get("type", "unknown")
    action = payload.get("action", "unknown")
    session_id = payload.get("agentSession", {}).get("id", "no-session")
    issue_id = (
        payload.get("agentSession", {}).get("issue", {}).get("identifier", "no-issue")
    )

    log.info(
        f"WEBHOOK_RECEIVED | type={event_type} action={action} session={session_id} issue={issue_id}"
    )

    # Store notification
    filepath = store_notification(payload)

    # Process AgentSessionEvent asynchronously
    if event_type == "AgentSessionEvent":
        log.info(f"WEBHOOK_PROCESSING_ASYNC | session={session_id} spawning thread")
        thread = threading.Thread(
            target=process_agent_session_event,
            args=(payload, filepath),
            daemon=True,
        )
        thread.start()
    else:
        log.debug(
            f"WEBHOOK_IGNORED | type={event_type} action={action} - not an AgentSessionEvent"
        )

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


@app.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    """Handle OAuth callback from Linear.

    This endpoint receives the authorization code from Linear after the user
    authorizes the application, exchanges it for access/refresh tokens, and
    saves them to the tokens file.
    """
    # Get the authorization code from the query string
    code = request.args.get("code")
    _state = request.args.get("state")  # TODO: Implement CSRF validation with state
    error = request.args.get("error")

    if error:
        error_desc = request.args.get("error_description", "Unknown error")
        return (
            f"""
        <html>
        <head><title>Authorization Failed</title></head>
        <body style="font-family: sans-serif; padding: 40px; text-align: center;">
            <h1 style="color: #dc3545;">❌ Authorization Failed</h1>
            <p>Error: {html.escape(error)}</p>
            <p>{html.escape(error_desc)}</p>
            <p><a href="/">Back to home</a></p>
        </body>
        </html>
        """,
            400,
        )

    if not code:
        return (
            """
        <html>
        <head><title>Missing Code</title></head>
        <body style="font-family: sans-serif; padding: 40px; text-align: center;">
            <h1 style="color: #dc3545;">❌ Missing Authorization Code</h1>
            <p>No authorization code was provided in the callback.</p>
            <p><a href="/">Back to home</a></p>
        </body>
        </html>
        """,
            400,
        )

    # Load OAuth credentials
    client_id = os.environ.get("LINEAR_CLIENT_ID")
    client_secret = os.environ.get("LINEAR_CLIENT_SECRET")
    callback_url = os.environ.get("LINEAR_CALLBACK_URL")

    if not client_id or not client_secret:
        return (
            """
        <html>
        <head><title>Configuration Error</title></head>
        <body style="font-family: sans-serif; padding: 40px; text-align: center;">
            <h1 style="color: #dc3545;">❌ Configuration Error</h1>
            <p>OAuth credentials not configured.</p>
            <p>Set LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET in your .env file.</p>
            <p><a href="/">Back to home</a></p>
        </body>
        </html>
        """,
            500,
        )

    if not callback_url:
        return (
            """
        <html>
        <head><title>Configuration Error</title></head>
        <body style="font-family: sans-serif; padding: 40px; text-align: center;">
            <h1 style="color: #dc3545;">❌ Configuration Error</h1>
            <p>Callback URL not configured.</p>
            <p>Set LINEAR_CALLBACK_URL in your .env file.</p>
            <p><a href="/">Back to home</a></p>
        </body>
        </html>
        """,
            500,
        )

    # Exchange the code for tokens
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
            return (
                f"""
            <html>
            <head><title>Token Exchange Failed</title></head>
            <body style="font-family: sans-serif; padding: 40px; text-align: center;">
                <h1 style="color: #dc3545;">❌ Token Exchange Failed</h1>
                <p>Status: {response.status_code}</p>
                <p>Error: {html.escape(response.text)}</p>
                <p><a href="/">Back to home</a></p>
            </body>
            </html>
            """,
                500,
            )

        data = response.json()

        if "access_token" not in data:
            return (
                f"""
            <html>
            <head><title>Invalid Response</title></head>
            <body style="font-family: sans-serif; padding: 40px; text-align: center;">
                <h1 style="color: #dc3545;">❌ Invalid Token Response</h1>
                <p>Response: {html.escape(str(data))}</p>
                <p><a href="/">Back to home</a></p>
            </body>
            </html>
            """,
                500,
            )

        # Save tokens to file
        tokens = {
            "accessToken": data["access_token"],
            "refreshToken": data.get("refresh_token"),
            "expiresAt": time.time() * 1000 + data.get("expires_in", 315360000) * 1000,
            "scope": data.get("scope", ""),
            "tokenType": data.get("token_type", "Bearer"),
        }

        TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
        print(f"✓ OAuth tokens saved to {TOKENS_FILE}", file=sys.stderr)

        return """
        <html>
        <head><title>Authorization Successful</title></head>
        <body style="font-family: sans-serif; padding: 40px; text-align: center;">
            <h1 style="color: #28a745;">✅ Authorization Successful!</h1>
            <p>OAuth tokens have been saved successfully.</p>
            <p>You can now close this window.</p>
            <p>The webhook server is ready to receive Linear events.</p>
        </body>
        </html>
        """

    except httpx.TimeoutException:
        return (
            """
        <html>
        <head><title>Timeout</title></head>
        <body style="font-family: sans-serif; padding: 40px; text-align: center;">
            <h1 style="color: #dc3545;">❌ Request Timeout</h1>
            <p>The token exchange request timed out.</p>
            <p>Please try again.</p>
            <p><a href="/">Back to home</a></p>
        </body>
        </html>
        """,
            500,
        )
    except Exception as e:
        return (
            f"""
        <html>
        <head><title>Error</title></head>
        <body style="font-family: sans-serif; padding: 40px; text-align: center;">
            <h1 style="color: #dc3545;">❌ Error</h1>
            <p>{html.escape(f"{type(e).__name__}: {e}")}</p>
            <p><a href="/">Back to home</a></p>
        </body>
        </html>
        """,
            500,
        )


@app.route("/", methods=["GET"])
def index():
    """Root endpoint."""
    return jsonify(
        {
            "service": "Linear Webhook Server",
            "version": "2.1.0 (Python)",
            "endpoints": {
                "/webhook": "POST - Linear webhook handler",
                "/oauth/callback": "GET - OAuth callback handler",
                "/health": "GET - Health check",
            },
        }
    )


def main():
    if not WEBHOOK_SECRET:
        log.error(
            "STARTUP_FAILED | LINEAR_WEBHOOK_SECRET environment variable required"
        )
        sys.exit(1)

    log.info(f"STARTUP | port={PORT} agent={AGENT_NAME}")
    log.info(f"STARTUP | workspace={AGENT_WORKSPACE}")
    log.info(f"STARTUP | worktree_base={WORKTREE_BASE}")
    log.info(f"STARTUP | notifications_dir={NOTIFICATIONS_DIR}")
    log.info(f"STARTUP | logs_dir={LOGS_DIR}")
    log.info(f"STARTUP | log_file={LOGS_DIR / 'webhook-server.log'}")
    log.info(f"STARTUP | webhook_endpoint=http://localhost:{PORT}/webhook")

    app.run(host="0.0.0.0", port=PORT, debug=False)


if __name__ == "__main__":
    main()
