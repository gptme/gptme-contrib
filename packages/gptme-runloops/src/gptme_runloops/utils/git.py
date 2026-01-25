"""Git operations with retry logic."""

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional


def git_pull_with_retry(
    workspace: Path,
    max_retries: int = 3,
    retry_delay: int = 5,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """Pull latest changes from git with retry logic.

    Args:
        workspace: Git repository path
        max_retries: Maximum number of retry attempts
        retry_delay: Seconds to wait between retries
        logger: Optional logger for messages

    Returns:
        True if pull succeeded, False otherwise
    """

    def log(msg: str) -> None:
        if logger:
            logger.info(msg)
        else:
            print(msg)

    for attempt in range(1, max_retries + 1):
        try:
            subprocess.run(
                ["git", "pull"],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
            )
            log(f"Git pull successful (attempt {attempt}/{max_retries})")
            return True

        except subprocess.CalledProcessError:
            if attempt < max_retries:
                log(
                    f"WARNING: Git pull failed (attempt {attempt}/{max_retries}), "
                    f"retrying in {retry_delay}s..."
                )
                time.sleep(retry_delay)
            else:
                log(
                    f"ERROR: Git pull failed after {max_retries} attempts, "
                    "continuing with current state"
                )
                return False

    return False
