"""Structured logging for run loops."""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


def get_logger(
    name: str, log_file: Optional[Path] = None, level: int = logging.INFO
) -> logging.Logger:
    """Get a configured logger for run loops.

    Args:
        name: Logger name (typically run loop type)
        log_file: Optional file to write logs to
        level: Logging level

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    logger.handlers.clear()

    # Create formatter with timestamp
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler if specified
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def log_execution_start(logger: logging.Logger, run_type: str) -> None:
    """Log the start of a run loop execution.

    Args:
        logger: Logger instance
        run_type: Type of run (autonomous, email, etc.)
    """
    logger.info(f"Starting {run_type} run")
    logger.info(f"Time: {datetime.now().isoformat()}")


def log_execution_end(
    logger: logging.Logger, run_type: str, exit_code: int, duration_seconds: float
) -> None:
    """Log the end of a run loop execution.

    Args:
        logger: Logger instance
        run_type: Type of run
        exit_code: Exit code from execution
        duration_seconds: Duration in seconds
    """
    status = (
        "completed successfully"
        if exit_code == 0
        else f"failed (exit code: {exit_code})"
    )
    logger.info(f"{run_type} run {status}")
    logger.info(f"Duration: {duration_seconds:.1f} seconds")
