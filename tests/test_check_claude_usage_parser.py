"""Black-box tests for scripts/check-claude-usage-parser.py.

Verifies that an auth/HTTP failure in the raw TUI output is reported as the
real cause (auth/network) rather than the misleading version error, and that a
genuine parse failure still reports the version hint.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PARSER = REPO_ROOT / "scripts" / "check-claude-usage-parser.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cc-usage"


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


def test_auth_signal_substring_inside_word_is_not_auth_failure() -> None:
    """A parse failure must not be misread as auth just because a signal word
    appears inside another word (e.g. "tls" inside "results")."""
    raw = "No usage bars found. Compiling results; retry later.\n"
    proc = run_parser(raw)
    assert proc.returncode == 1
    assert "Could not parse usage data" in proc.stderr
    assert "requires Claude Code v2.1.183" in proc.stderr
    assert "Could not authenticate" not in proc.stderr


def test_ambiguous_transport_words_do_not_false_positive() -> None:
    """Realistic parse-failure text that merely contains ambiguous words
    ("timeout", a number embedding "502", a gateway hostname, "results") must
    keep the version hint, not be misreported as auth/network."""
    for raw in (
        "Usage data loading; retry on timeout. No usage bars found.\n",
        "Session 1502 active, 503 events seen, no usage bars.\n",
        "Proxy api-gateway.internal unreachable from this shell.\n",
        "Compiling results...\n",
        "Response HTTP 200 OK but no usage bars found.\n",
    ):
        proc = run_parser(raw)
        assert proc.returncode == 1, raw
        assert "requires Claude Code v2.1.183" in proc.stderr, raw
        assert "Could not authenticate" not in proc.stderr, raw


def test_tls_word_detected_as_auth_failure() -> None:
    """A standalone TLS-failure word is still classified as auth/network."""
    raw = "Error: TLS handshake failed while contacting the usage endpoint.\n"
    proc = run_parser(raw)
    assert proc.returncode == 1
    assert "Could not authenticate" in proc.stderr
    assert "requires Claude Code v2.1.183" not in proc.stderr


def test_gateway_word_detected_as_auth_failure() -> None:
    """A standalone gateway error is still classified as auth/network."""
    raw = "Error: Bad Gateway while fetching usage data.\n"
    proc = run_parser(raw)
    assert proc.returncode == 1
    assert "Could not authenticate" in proc.stderr
    assert "requires Claude Code v2.1.183" not in proc.stderr


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


def test_cc_2_1_319_weekly_only_pane_is_valid_and_names_the_absent_window() -> None:
    """CC >= 2.1.318 renders only weekly windows; that is not a parse error.

    The fixture is a verbatim 2.1.319 capture: `Current week (all models)` and
    `Current week (Fable)`, no `Current session` block. The 5h absence must be
    stamped explicitly, never reported as the "requires v2.1.183" version error.
    """
    proc = run_parser((FIXTURES / "usage-pane-2.1.319.pane").read_text())
    assert proc.returncode == 0, proc.stderr
    assert "requires Claude Code v2.1.183" not in proc.stderr
    data = json.loads(proc.stdout)
    assert data["seven_day"]["utilization"] == 0.09
    assert data["seven_day_fable"]["utilization"] == 0.0
    assert "seven_day_sonnet" not in data
    assert "five_hour" not in data
    assert data["_window_state"] == {
        "five_hour": "absent_from_pane",
        "weekly_windows": ["seven_day", "seven_day_fable"],
    }
    assert "five_hour window absent" in proc.stderr


def test_pane_with_session_window_is_stamped_present() -> None:
    """Older panes keep five_hour and are stamped present, with no warning."""
    raw = (
        "Current session\n"
        "  ██████████████░░░░░░░░░░░░░░░░ 45% used\n"
        "  Resets 9pm\n"
        "Current week (all models)\n"
        "  ██████████████░░░░░░░░░░░░░░░░ 45% used\n"
        "  Resets Thu, 12am\n"
        "Current week (Sonnet only)\n"
        "  ██████░░░░░░░░░░░░░░░░░░░░░░░░ 20% used\n"
        "  Resets Thu, 12am\n"
    )
    proc = run_parser(raw)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["seven_day_sonnet"]["utilization"] == 0.2
    assert data["_window_state"]["five_hour"] == "present"
    assert data["_window_state"]["weekly_windows"] == [
        "seven_day",
        "seven_day_sonnet",
    ]
    assert "absent" not in proc.stderr


def test_lone_non_weekly_header_is_still_a_parse_failure() -> None:
    """A single stray session header, with no weekly window, is not usage data."""
    raw = "Current session\n  ████ 45% used\n  Resets 9pm\n"
    proc = run_parser(raw)
    assert proc.returncode == 1
    assert "Could not parse usage data" in proc.stderr


def test_incomplete_weekly_total_does_not_borrow_fable_percentage() -> None:
    raw = (
        "Current week (all models)\n"
        "Loading...\n"
        "Current week (Fable)\n"
        "0% used\n"
        "Resets Nov 13, 8am (UTC)\n"
    )
    proc = run_parser(raw)
    assert proc.returncode == 1
    assert "Could not parse usage data" in proc.stderr


def test_weekly_total_does_not_borrow_fable_reset() -> None:
    raw = (
        "Current week (all models)\n"
        "90% used\n"
        "Current week (Fable)\n"
        "0% used\n"
        "Resets Nov 13, 8am (UTC)\n"
    )
    proc = run_parser(raw)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["seven_day"]["utilization"] == 0.9
    assert data["seven_day"]["resets"] == "unknown"
    assert data["seven_day_fable"]["resets"] == "Nov 13, 8am (UTC)"


def test_low_usage_fallback_does_not_claim_observed_windows() -> None:
    proc = run_parser("Nothing over 10%\n")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    # Preserve the legacy estimates without mislabelling them as pane data.
    assert data["five_hour"]["utilization"] == 0.0
    assert data["seven_day"]["utilization"] == 0.0
    assert data["_window_state"] == {
        "five_hour": "absent_from_pane",
        "weekly_windows": [],
    }
    assert "five_hour window absent" in proc.stderr
    assert "synthetic" in proc.stderr
    assert "reporting weekly windows only" not in proc.stderr
