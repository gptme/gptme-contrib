"""
Logging configuration for cross-platform communication.

Provides consistent logging setup with platform-specific loggers,
structured logging support, and configurable output formats.
"""

import logging
import sys
from pathlib import Path
from typing import Optional


class PlatformLogger:
    """
    Platform-specific logger with consistent formatting.

    Provides structured logging with platform context,
    timing information, and error tracking.
    """

    def __init__(self, name: str, platform: str, log_dir: Optional[Path] = None):
        """
        Initialize platform logger.

        Args:
            name: Logger name
            platform: Platform identifier (email, twitter, discord, etc.)
            log_dir: Optional directory for log files
        """
        self.logger = logging.getLogger(f"{name}.{platform}")
        self.platform = platform
        self.log_dir = log_dir

    def info(self, message: str, **context) -> None:
        """Log info message with context."""
        self._log(logging.INFO, message, context)

    def warning(self, message: str, **context) -> None:
        """Log warning message with context."""
        self._log(logging.WARNING, message, context)

    def error(self, message: str, **context) -> None:
        """Log error message with context."""
        self._log(logging.ERROR, message, context)

    def debug(self, message: str, **context) -> None:
        """Log debug message with context."""
        self._log(logging.DEBUG, message, context)

    def _log(self, level: int, message: str, context: dict) -> None:
        """Internal logging with context."""
        context_str = " ".join(f"{k}={v}" for k, v in context.items())
        full_message = f"[{self.platform}] {message}"
        if context_str:
            full_message += f" | {context_str}"

        self.logger.log(level, full_message)


def configure_logging(
    level: str = "INFO",
    log_file: Optional[Path] = None,
    format_string: Optional[str] = None,
) -> None:
    """
    Configure logging for communication systems.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_file: Optional file path for log output
        format_string: Optional custom format string
    """
    if format_string is None:
        format_string = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # Create formatter
    formatter = logging.Formatter(format_string)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper()))

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler if specified
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


def get_logger(
    name: str, platform: str, log_dir: Optional[Path] = None
) -> PlatformLogger:
    """
    Get platform-specific logger.

    Args:
        name: Logger name
        platform: Platform identifier
        log_dir: Optional directory for log files

    Returns:
        Configured PlatformLogger instance
    """
    return PlatformLogger(name, platform, log_dir)
