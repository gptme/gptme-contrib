"""
Agent Workspace Validator

A tool for validating that directories conform to the gptme agent workspace structure.
"""

from .validate import (
    RECOMMENDED_DIRS,
    RECOMMENDED_FILES,
    REQUIRED_DIRS,
    REQUIRED_FILES,
    ValidationResult,
    check_fork_script,
    check_gptme_toml,
    check_required_dirs,
    check_required_files,
    validate_workspace,
)

__all__ = [
    "ValidationResult",
    "validate_workspace",
    "check_required_files",
    "check_required_dirs",
    "check_gptme_toml",
    "check_fork_script",
    "REQUIRED_FILES",
    "REQUIRED_DIRS",
    "RECOMMENDED_FILES",
    "RECOMMENDED_DIRS",
]
