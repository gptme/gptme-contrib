"""Shared helpers for Linear OAuth state persistence and validation."""

from __future__ import annotations

import secrets
from pathlib import Path

OAUTH_STATE_FILE = Path(__file__).parent / ".oauth-state"


def save_pending_oauth_state(state: str) -> None:
    """Persist the pending OAuth state for the next callback."""
    OAUTH_STATE_FILE.write_text(state)


def load_pending_oauth_state() -> str | None:
    """Load the pending OAuth state if it exists."""
    if not OAUTH_STATE_FILE.exists():
        return None

    value = OAUTH_STATE_FILE.read_text().strip()
    return value or None


def clear_pending_oauth_state() -> None:
    """Remove the pending OAuth state after a successful auth flow."""
    OAUTH_STATE_FILE.unlink(missing_ok=True)


def generate_and_save_oauth_state() -> str:
    """Generate a per-attempt OAuth state and persist it."""
    state = secrets.token_urlsafe(32)
    save_pending_oauth_state(state)
    return state


def get_oauth_state_error(
    received_state: str | None, *, expected_state: str | None
) -> str | None:
    """Return an error message when the OAuth state is missing or invalid."""
    if not expected_state:
        return "No pending OAuth state found. Start a new authorization attempt."
    if not received_state:
        return "No OAuth state found in callback URL."
    if received_state != expected_state:
        return "OAuth state mismatch. Start a new authorization attempt."
    return None
