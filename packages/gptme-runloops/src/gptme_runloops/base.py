"""Base class for all run loop types."""

import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from gptme_runloops.utils.execution import ExecutionResult, execute_gptme
from gptme_runloops.utils.git import git_pull_with_retry
from gptme_runloops.utils.lock import RunLoopLock
from gptme_runloops.utils.logging import (
    get_logger,
    log_execution_end,
    log_execution_start,
)


class BaseRunLoop(ABC):
    """Base class for all run loop types.

    Provides common infrastructure:
    - Lock management to prevent concurrent runs
    - Structured logging
    - Git operations with retry
    - gptme execution with timeout
    - Cleanup and error handling

    Subclasses override:
    - generate_prompt(): Create run-specific prompt
    - pre_run(): Run-specific preparation
    - post_run(): Run-specific cleanup
    """

    def __init__(
        self,
        workspace: Path,
        run_type: str,
        timeout: int = 3000,
        lock_wait: bool = False,
    ):
        """Initialize run loop.

        Args:
            workspace: Path to workspace directory
            run_type: Type of run (for logging and locking)
            timeout: Maximum execution time in seconds
            lock_wait: Whether to wait for lock or fail immediately
        """
        self.workspace = Path(workspace)
        self.run_type = run_type
        self.timeout = timeout
        self.lock_wait = lock_wait

        # Initialize utilities
        lock_dir = self.workspace / "logs"
        self.lock = RunLoopLock(lock_dir, run_type)
        self.logger = get_logger(run_type)

        self._start_time: Optional[float] = None
        self._work_description: Optional[str] = None  # Description of work found

    def setup(self) -> bool:
        """Acquire lock and perform initial setup.

        Returns:
            True if setup succeeded, False otherwise
        """
        # Set work description before acquiring lock (for calendar entry)
        if self._work_description:
            self.lock.set_work_description(self._work_description)

        # Acquire lock
        self.logger.info(f"Acquiring lock for {self.run_type} run...")
        if not self.lock.acquire(wait=self.lock_wait):
            self.logger.warning(
                f"Failed to acquire lock (another {self.run_type} session running)"
            )
            return False

        self.logger.info("Lock acquired successfully")
        log_execution_start(self.logger, self.run_type)
        self._start_time = time.time()
        return True

    def has_work(self) -> bool:
        """Check if there is work to do BEFORE acquiring lock.

        Override in subclasses to check for actionable work.
        This runs before lock acquisition to avoid creating calendar
        entries for runs that don't actually do anything.

        If work is found, set self._work_description to describe what was found.

        Returns:
            True if there is work to do (default), False to skip this run
        """
        return True  # Default: assume there's always work

    def pre_run(self) -> bool:
        """Perform pre-run checks and preparation.

        Override in subclasses for run-specific preparation.

        Returns:
            True if pre-run succeeded, False otherwise
        """
        # Default: pull latest changes
        self.logger.info("Pulling latest changes from git...")
        return git_pull_with_retry(self.workspace, logger=self.logger)

    @abstractmethod
    def generate_prompt(self) -> str:
        """Generate prompt for gptme execution.

        Must be implemented by subclasses.

        Returns:
            Prompt text
        """
        pass

    def execute(self, prompt: str) -> ExecutionResult:
        """Execute gptme with the generated prompt.

        Args:
            prompt: Prompt text

        Returns:
            ExecutionResult with exit code and status
        """
        self.logger.info(f"Starting gptme execution (timeout: {self.timeout}s)...")

        result = execute_gptme(
            prompt=prompt,
            workspace=self.workspace,
            timeout=self.timeout,
            non_interactive=True,
            run_type=self.run_type,
        )

        if result.timed_out:
            self.logger.warning(f"Execution timed out after {self.timeout}s")
        elif result.success:
            self.logger.info("Execution completed successfully")
        else:
            self.logger.error(f"Execution failed with exit code {result.exit_code}")

        return result

    def post_run(self, result: ExecutionResult) -> None:
        """Perform post-run cleanup and logging.

        Override in subclasses for run-specific cleanup.

        Args:
            result: Result from execution
        """
        pass

    def cleanup(self) -> None:
        """Release lock and cleanup resources."""
        self.logger.info("Cleaning up...")
        self.lock.release()

        if self._start_time is not None:
            duration = time.time() - self._start_time
            log_execution_end(
                self.logger,
                self.run_type,
                0,  # We don't track exit code here
                duration,
            )

    def run(self) -> int:
        """Main run method - orchestrates the workflow.

        Returns:
            Exit code (0 for success, non-zero for failure)
        """
        try:
            # Check if there's work to do BEFORE acquiring lock
            # This prevents noisy calendar entries for check-only runs
            self.logger.info(f"Checking if {self.run_type} run has work to do...")
            if not self.has_work():
                self.logger.info(f"No work found for {self.run_type} run, skipping")
                return 0  # Success - nothing to do

            # Setup (acquires lock)
            if not self.setup():
                return 1

            # Pre-run checks
            if not self.pre_run():
                self.logger.warning("Pre-run checks failed, continuing anyway...")

            # Generate prompt
            prompt = self.generate_prompt()

            # Execute
            result = self.execute(prompt)

            # Post-run
            self.post_run(result)

            return result.exit_code

        except Exception as e:
            self.logger.exception(f"Unexpected error during {self.run_type} run: {e}")
            return 1

        finally:
            self.cleanup()
