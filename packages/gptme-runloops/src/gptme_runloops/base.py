"""Base class for all run loop types."""

import json
import random
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from gptme_runloops.utils.execution import ExecutionResult, execute_gptme
from gptme_runloops.utils.git import git_pull_with_retry
from gptme_runloops.utils.lock import RunLoopLock
from gptme_runloops.utils.logging import (
    get_logger,
    log_execution_end,
    log_execution_start,
)

# Backoff schedule for consecutive-failure detection.
# Each entry: (failure_threshold, skip_n, out_of, description)
# "skip_n out_of out_of" means skip the run with probability skip_n/out_of.
# Entries are checked from most severe to least severe.
BACKOFF_SCHEDULE = [
    (8, 7, 8, "skip 7/8 runs (~40 min between attempts)"),
    (5, 3, 4, "skip 3/4 runs (~20 min between attempts)"),
    (3, 1, 2, "skip 1/2 runs (~10 min between attempts)"),
]


class BaseRunLoop(ABC):
    """Base class for all run loop types.

    Provides common infrastructure:
    - Lock management to prevent concurrent runs
    - Structured logging
    - Git operations with retry
    - gptme execution with timeout
    - Cleanup and error handling
    - Consecutive-failure backoff (opt-in via work_hash in subclasses)

    Subclasses override:
    - generate_prompt(): Create run-specific prompt
    - pre_run(): Run-specific preparation
    - post_run(): Run-specific cleanup

    To enable backoff, subclasses call:
    - _check_backoff(work_hash) in has_work() — returns True if run should be skipped
    - _record_backoff_success() in post_run() when work was resolved
    - _record_backoff_failure(work_hash) in post_run() when same work persists
    """

    def __init__(
        self,
        workspace: Path,
        run_type: str,
        timeout: int = 3000,
        lock_wait: bool = False,
        model: str | None = None,
        tool_format: str | None = None,
    ):
        """Initialize run loop.

        Args:
            workspace: Path to workspace directory
            run_type: Type of run (for logging and locking)
            timeout: Maximum execution time in seconds
            lock_wait: Whether to wait for lock or fail immediately
            model: Model override (e.g. "openai-subscription/gpt-5.3-codex")
            tool_format: Tool format override (markdown/xml/tool)
        """
        self.workspace = Path(workspace)
        self.run_type = run_type
        self.timeout = timeout
        self.lock_wait = lock_wait
        self.model = model
        self.tool_format = tool_format

        # Initialize utilities
        lock_dir = self.workspace / "logs"
        self.lock = RunLoopLock(lock_dir, run_type)
        self.logger = get_logger(run_type)

        self._start_time: float | None = None
        self._work_description: str | None = None  # Description of work found

    # --- Backoff infrastructure ---

    @property
    def _backoff_state_file(self) -> Path:
        """Path to the backoff state file for this run type."""
        return self.workspace / "state" / f"{self.run_type}-backoff.json"

    def _load_backoff_state(self) -> dict[str, Any]:
        """Load backoff state from disk."""
        if self._backoff_state_file.exists():
            try:
                data: dict[str, Any] = json.loads(self._backoff_state_file.read_text())
                return data
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_backoff_state(self, state: dict[str, Any]) -> None:
        """Save backoff state to disk."""
        self._backoff_state_file.parent.mkdir(parents=True, exist_ok=True)
        self._backoff_state_file.write_text(json.dumps(state))

    def _check_backoff(self, work_hash: str) -> bool:
        """Check if this run should be skipped due to consecutive failures.

        Call this in has_work() after computing a hash of the current work.
        If the same work hash has been seen repeatedly without resolution,
        progressively skips runs to avoid wasting tokens.

        Also resets the backoff counter when a new/different work set is
        detected (new hash means the situation changed, so retry eagerly).

        Args:
            work_hash: Hash identifying the current work set. Should change
                       when the underlying work is resolved or changes.

        Returns:
            True if this run should be skipped (backed off)
        """
        state = self._load_backoff_state()
        failures = state.get("consecutive_failures", 0)

        if state.get("last_hash") != work_hash:
            # New/different work — reset backoff and proceed
            if failures > 0:
                self.logger.info(
                    f"Backoff: reset (new work detected, was {failures} failures)"
                )
            self._save_backoff_state(
                {"consecutive_failures": 0, "last_hash": work_hash}
            )
            return False

        # Same work as last time — apply schedule
        for threshold, skip_n, out_of, desc in BACKOFF_SCHEDULE:
            if failures >= threshold:
                if random.randint(1, out_of) <= skip_n:
                    self.logger.info(
                        f"Backoff: skipping ({failures} consecutive failures, {desc})"
                    )
                    return True
                else:
                    self.logger.info(
                        f"Backoff: running despite {failures} failures (lucky draw)"
                    )
                    return False

        return False

    def _record_backoff_success(self) -> None:
        """Record that work was successfully resolved — reset backoff counter."""
        state = self._load_backoff_state()
        if state.get("consecutive_failures", 0) > 0:
            self.logger.info("Backoff: reset (work resolved)")
        self._save_backoff_state({"consecutive_failures": 0, "last_hash": ""})

    def _record_backoff_failure(self, work_hash: str) -> None:
        """Record that the same work persists after execution — increment counter.

        Args:
            work_hash: Hash of the work set that is still unresolved.
        """
        state = self._load_backoff_state()
        failures = state.get("consecutive_failures", 0) + 1
        self.logger.warning(
            f"Backoff: same work still unresolved (consecutive failures: {failures})"
        )
        self._save_backoff_state(
            {"consecutive_failures": failures, "last_hash": work_hash}
        )

    # --- Run loop lifecycle ---

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

        To opt into backoff, call self._check_backoff(work_hash) and return
        False if it returns True.

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
            model=self.model,
            tool_format=self.tool_format,
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
