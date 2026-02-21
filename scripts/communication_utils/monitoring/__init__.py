"""
Logging and monitoring utilities for cross-platform communication.

Provides consistent logging, metrics tracking, and error reporting
across email, Twitter, Discord, and other platforms.
"""

from .loggers import PlatformLogger, configure_logging, get_logger
from .metrics import MetricsCollector, OperationMetrics

__all__ = [
    "get_logger",
    "configure_logging",
    "PlatformLogger",
    "MetricsCollector",
    "OperationMetrics",
]
