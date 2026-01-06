"""Template validation tools for gptme-agent-template."""

from .check_names import (
    validate_names,
    validate_agent_identity,
    ValidationResult,
    TEMPLATE_PATTERNS,
    FORK_PATTERNS,
)

__version__ = "0.1.0"

__all__ = [
    "validate_names",
    "validate_agent_identity",
    "ValidationResult",
    "TEMPLATE_PATTERNS",
    "FORK_PATTERNS",
]
