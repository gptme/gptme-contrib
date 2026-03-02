"""Email processing run loop implementation.

Includes consecutive-failure backoff: if the same set of unreplied emails
keeps appearing after execution (indicating the LLM couldn't process them,
e.g. due to auth errors), we progressively skip runs to avoid burning tokens.
"""

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from gptme_runloops.base import BaseRunLoop
from gptme_runloops.utils.execution import ExecutionResult
from gptme_runloops.utils.prompt import generate_base_prompt, get_agent_name

# Backoff schedule: after N consecutive failures, skip runs
# Maps threshold -> (skip_probability, description)
_BACKOFF_SCHEDULE = [
    (8, 7, 8, "skip 7/8 runs (~40 min between attempts)"),
    (5, 3, 4, "skip 3/4 runs (~20 min between attempts)"),
    (3, 1, 2, "skip 1/2 runs (~10 min between attempts)"),
]


class EmailRun(BaseRunLoop):
    """Email processing run loop.

    Implements email workflow:
    - Sync emails via mbsync (in has_work, before lock)
    - Check for unreplied emails (in has_work, before lock)
    - Process with gptme if emails found (after lock acquired)

    Includes failure backoff: tracks consecutive runs where the same
    unreplied emails persist after execution, and progressively skips
    runs to avoid wasting tokens on unresolvable emails.
    """

    def __init__(
        self,
        workspace: Path,
        model: str | None = None,
        tool_format: str | None = None,
    ):
        """Initialize email run.

        Args:
            workspace: Path to workspace directory
            model: Model override (e.g. "openai-subscription/gpt-5.3-codex")
            tool_format: Tool format override (markdown/xml/tool)
        """
        super().__init__(
            workspace=workspace,
            run_type="email",
            timeout=1200,  # 20 minutes
            lock_wait=False,  # Don't wait for lock
            model=model,
            tool_format=tool_format,
        )
        self._state_file = workspace / "state" / "email-backoff.json"
        self._current_email_hash: str | None = None

    def _load_backoff_state(self) -> dict[str, Any]:
        """Load backoff state from disk."""
        if self._state_file.exists():
            try:
                data: dict[str, Any] = json.loads(self._state_file.read_text())
                return data
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_backoff_state(self, state: dict[str, Any]) -> None:
        """Save backoff state to disk."""
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(state))

    def _hash_email_set(self, description: str) -> str:
        """Create a hash of the unreplied email set for change detection."""
        return hashlib.sha256(description.encode()).hexdigest()[:16]

    def _should_skip_backoff(self) -> bool:
        """Check if we should skip this run due to consecutive failures.

        Returns:
            True if this run should be skipped
        """
        import random

        state = self._load_backoff_state()
        failures = state.get("consecutive_failures", 0)

        for threshold, skip_n, out_of, desc in _BACKOFF_SCHEDULE:
            if failures >= threshold:
                if random.randint(1, out_of) <= skip_n:
                    self.logger.info(
                        f"Email backoff: skipping ({failures} consecutive failures, {desc})"
                    )
                    return True
                else:
                    self.logger.info(
                        f"Email backoff: running despite {failures} failures (lucky draw)"
                    )
                    return False

        return False

    def _sync_emails(self) -> None:
        """Sync emails from server via mbsync."""
        self.logger.info("Syncing emails via mbsync...")
        try:
            result = subprocess.run(
                ["mbsync", "-a"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                self.logger.info("Successfully synced emails")
            else:
                self.logger.warning(
                    f"mbsync failed with exit code {result.returncode}: {result.stderr}"
                )
        except subprocess.TimeoutExpired:
            self.logger.error("mbsync timed out after 60s")
        except FileNotFoundError:
            self.logger.error("mbsync not found - is it installed?")
        except Exception as e:
            self.logger.error(f"Error running mbsync: {e}")

    def _sync_workspace_email(self) -> None:
        """Sync workspace email directory."""
        self.logger.info("Syncing workspace email directory...")
        try:
            result = subprocess.run(
                ["uv", "run", "python3", "-m", "gptmail", "sync-maildir"],
                capture_output=True,
                text=True,
                timeout=120,  # Increased from 30s - large mailboxes (11k+ emails) need more time
                cwd=self.workspace,
            )
            if result.returncode == 0:
                self.logger.info("Successfully synced workspace email directory")
            else:
                self.logger.warning(f"Email sync failed: {result.stderr}")
        except Exception as e:
            self.logger.error(f"Error syncing workspace email: {e}")

    def has_work(self) -> bool:
        """Check if there are unreplied emails BEFORE acquiring lock.

        This syncs emails and checks for unreplied ones without taking
        a lock, so runs with no emails don't create calendar entries.

        Also checks backoff state: if the same emails have been seen
        repeatedly without being resolved, progressively skips runs.

        Returns:
            True if there are unreplied emails to process
        """
        # Sync emails first
        self._sync_emails()
        self._sync_workspace_email()

        # Check if there are unreplied emails
        self.logger.info("Checking for unreplied emails...")
        try:
            result = subprocess.run(
                ["uv", "run", "python3", "-m", "gptmail", "check-unreplied"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self.workspace,
            )

            # Exit codes: 0=no emails, 1=has emails, 2=error
            if result.returncode == 0:
                self.logger.info("No unreplied emails found")
                # Reset backoff on success (emails were resolved)
                state = self._load_backoff_state()
                if state.get("consecutive_failures", 0) > 0:
                    self.logger.info("Email backoff: reset (no unreplied emails)")
                    self._save_backoff_state(
                        {"consecutive_failures": 0, "last_hash": ""}
                    )
                return False
            elif result.returncode == 1:
                # Parse output to get email count and first email info
                output = result.stdout.strip()
                lines = output.split("\n")
                # First line has count, data rows start after header (line 2) and separator (line 3)
                count_line = lines[0] if lines else "emails found"
                # Get first data row (skip header and separator)
                first_email = lines[3].strip() if len(lines) > 3 else ""
                # Extract sender and subject from the data row
                if first_email and "|" in first_email:
                    parts = [p.strip() for p in first_email.split("|")]
                    sender = parts[0] if parts else ""
                    subject = parts[1][:30] if len(parts) > 1 else ""
                    desc = f"{count_line} - {sender}: {subject}..."
                else:
                    desc = count_line

                # Check if this is the same email set we've been failing on
                self._current_email_hash = self._hash_email_set(output)
                state = self._load_backoff_state()
                if state.get("last_hash") == self._current_email_hash:
                    # Same emails as last time — check backoff
                    if self._should_skip_backoff():
                        return False
                else:
                    # New email set — reset backoff
                    if state.get("consecutive_failures", 0) > 0:
                        self.logger.info("Email backoff: reset (new emails detected)")
                    self._save_backoff_state(
                        {
                            "consecutive_failures": 0,
                            "last_hash": self._current_email_hash,
                        }
                    )

                self._work_description = desc
                self.logger.info(f"Found {desc}")
                return True
            else:
                self.logger.warning(f"Unexpected exit code: {result.returncode}")
                return False

        except Exception as e:
            self.logger.error(f"Error checking for emails: {e}")
            return False

    def pre_run(self) -> bool:
        """Perform pre-run checks (git pull only since sync done in has_work).

        Returns:
            True if pre-run succeeded
        """
        # Just do git pull, email sync already done in has_work()
        return super().pre_run()

    def generate_prompt(self) -> str:
        """Generate prompt for email processing.

        Returns:
            Email processing prompt
        """
        # Get agent name from workspace config
        agent_name = get_agent_name(self.workspace)

        return generate_base_prompt(
            run_type="email",
            agent_name=agent_name,
            additional_sections=f"""
## Email Processing Task

Check for unreplied emails and respond appropriately:

```shell
# Check for unreplied emails
cd {self.workspace} && uv run python3 -m gptmail check-unreplied
```

After reviewing emails:
1. Read full thread: `uv run python3 -m gptmail read <message-id> --thread`
2. Compose thoughtful reply
3. Use reply command to create draft
4. Send the draft

Available commands:
- Check: `uv run python3 -m gptmail check-unreplied`
- Read: `uv run python3 -m gptmail read <message-id> --thread`
- Reply: `uv run python3 -m gptmail reply <message-id> "Reply text"`
- Send: `uv run python3 -m gptmail send <draft-message-id>`

Mark emails as no-reply-needed if they don't require response.

**Git Safety**:
- Create journal entries naturally to document your work
- Before committing: check git status is clean
- Only commit journals if no unrelated changes exist
- If git status is dirty: create journal but don't commit

Complete when all emails are addressed.
""",
        )

    def execute(self, prompt: str) -> ExecutionResult:
        """Execute email processing with gptme.

        Note: has_work() already confirmed there are emails to process,
        so we can proceed directly to execution.

        Args:
            prompt: Prompt text

        Returns:
            ExecutionResult with exit code and status
        """
        # has_work() already confirmed there are unreplied emails
        self.logger.info("Processing emails with gptme...")
        return super().execute(prompt)

    def post_run(self, result: ExecutionResult) -> None:
        """Track whether emails were actually resolved.

        If the same emails are still unreplied after execution,
        increment the failure counter to trigger backoff.
        """
        # Quick re-check: are the same emails still unreplied?
        try:
            check = subprocess.run(
                ["uv", "run", "python3", "-m", "gptmail", "check-unreplied"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self.workspace,
            )

            state = self._load_backoff_state()

            if check.returncode == 0:
                # All emails resolved — reset backoff
                self.logger.info("Post-run: all emails resolved, resetting backoff")
                self._save_backoff_state({"consecutive_failures": 0, "last_hash": ""})
            elif check.returncode == 1:
                # Emails still unreplied — increment failure counter
                post_hash = self._hash_email_set(check.stdout.strip())
                if post_hash == self._current_email_hash:
                    failures = state.get("consecutive_failures", 0) + 1
                    self.logger.warning(
                        f"Post-run: same emails still unreplied "
                        f"(consecutive failures: {failures})"
                    )
                    self._save_backoff_state(
                        {"consecutive_failures": failures, "last_hash": post_hash}
                    )
                else:
                    # Different set (some resolved, new ones appeared)
                    self.logger.info("Post-run: email set changed, resetting backoff")
                    self._save_backoff_state(
                        {"consecutive_failures": 0, "last_hash": post_hash}
                    )
        except Exception as e:
            self.logger.error(f"Post-run email check failed: {e}")
