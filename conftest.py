"""Repository-wide pytest guards (also loaded for package Makefile targets)."""

import os
import sys

import pytest


def pytest_sessionstart(session: pytest.Session) -> None:
    """Check in the test process, not just setup-python or a shell preflight."""
    expected = os.environ.get("CI_EXPECTED_PYTHON")
    actual = f"{sys.version_info.major}.{sys.version_info.minor}"
    if expected and actual != expected:
        raise pytest.UsageError(
            f"CI Python mismatch: expected {expected}, running {actual} ({sys.executable})"
        )


def pytest_report_header() -> str | None:
    expected = os.environ.get("CI_EXPECTED_PYTHON")
    if expected:
        return f"CI Python verified: {expected} ({sys.executable})"
    return None
