"""Email processing run loop implementation."""

import subprocess
from pathlib import Path

from run_loops.base import BaseRunLoop
from run_loops.utils.execution import ExecutionResult
from run_loops.utils.prompt import generate_base_prompt


class EmailRun(BaseRunLoop):
    """Email processing run loop.

    Implements email workflow:
    - Sync emails via mbsync (in has_work, before lock)
    - Check for unreplied emails (in has_work, before lock)
    - Process with gptme if emails found (after lock acquired)
    """

    def __init__(self, workspace: Path):
        """Initialize email run.

        Args:
            workspace: Path to workspace directory
        """
        super().__init__(
            workspace=workspace,
            run_type="email",
            timeout=1200,  # 20 minutes
            lock_wait=False,  # Don't wait for lock
        )

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
                timeout=30,
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
                return False
            elif result.returncode == 1:
                # Parse output to get email count for description
                output = result.stdout.strip()
                self._work_description = f"unreplied emails: {output[:100]}"
                self.logger.info(f"Found unreplied emails: {output[:100]}")
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
        return generate_base_prompt(
            run_type="email",
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
