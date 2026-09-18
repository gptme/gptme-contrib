"""Black-box tests for scripts/check-claude-usage-parser.py.

Verifies that an auth/HTTP failure in the raw TUI output is reported as the
real cause (auth/network) rather than the misleading version error, and that a
genuine parse failure still reports the version hint.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PARSER = REPO_ROOT / "scripts" / "check-claude-usage-parser.py"


def run_parser(text: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(PARSER), "--json"],
        input=text,
        capture_output=True,
        text=True,
    )


def test_auth_failure_reports_auth_not_version() -> None:
    """An auth-failure banner must name auth/network, not the version."""
    # Simulates the /usage TUI when the credential can't authenticate.
    raw = (
        "Error: Not signed in. Please sign in to continue.\n"
        "Run /login to authenticate with your Claude account.\n"
    )
    proc = run_parser(raw)
    assert proc.returncode == 1
    assert "Could not authenticate" in proc.stderr
    assert "not a version problem" in proc.stderr
    # The misleading version hint must NOT appear.
    assert "requires Claude Code v2.1.183" not in proc.stderr
    # The --raw instruction is printed once (by main), not duplicated.
    assert proc.stderr.count("--raw") == 1


def test_http_failure_reports_network() -> None:
    """A transport failure must name network, not the version."""
    raw = "Network error: failed to connect to the usage endpoint.\n"
    proc = run_parser(raw)
    assert proc.returncode == 1
    assert "Could not authenticate" in proc.stderr
    assert "network failure" in proc.stderr
    assert "requires Claude Code v2.1.183" not in proc.stderr


def test_unable_to_authenticate_wording_detected() -> None:
    """'unable to authenticate' (common CC wording) must be detected."""
    raw = "Error: unable to authenticate with the API.\n"
    proc = run_parser(raw)
    assert proc.returncode == 1
    assert "Could not authenticate" in proc.stderr
    assert "requires Claude Code v2.1.183" not in proc.stderr


def test_timeout_failure_reports_network() -> None:
    """A timeout must name network, not the version."""
    raw = "Request timed out while fetching usage data.\n"
    proc = run_parser(raw)
    assert proc.returncode == 1
    assert "Could not authenticate" in proc.stderr
    assert "requires Claude Code v2.1.183" not in proc.stderr


def test_genuine_parse_failure_still_reports_version() -> None:
    """A real parse failure (no auth signals) keeps the version hint."""
    raw = "Some unrelated TUI text with no usage bars and no auth signals.\n"
    proc = run_parser(raw)
    assert proc.returncode == 1
    assert "Could not parse usage data" in proc.stderr
    assert "requires Claude Code v2.1.183" in proc.stderr


def test_normal_usage_parses() -> None:
    """A normal usage TUI still parses to JSON successfully."""
    raw = (
        "Current session\n"
        "  ██████████████████████████████ 100% used\n"
        "  Resets 9pm\n"
        "Current week (all models)\n"
        "  ██████████████░░░░░░░░░░░░░░░░ 45% used\n"
        "  Resets Thu, 12am\n"
    )
    proc = run_parser(raw)
    assert proc.returncode == 0
    assert '"five_hour"' in proc.stdout
    assert '"seven_day"' in proc.stdout
