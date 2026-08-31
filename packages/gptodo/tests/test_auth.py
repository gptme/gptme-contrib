"""Tests for the transient-401 / auth-death classifier."""

import pytest

from gptodo._auth import DEFAULT_MAX_BYTES, is_auth_death, is_transient_401


# ── is_transient_401 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Error: 401 unauthorized",
        "Invalid bearer token",
        "authentication_failed",
        "authentication_error",
        "invalid_api_key",
        "oauth expired",
        "Please run /login",
    ],
)
def test_is_transient_401_positive(text):
    assert is_transient_401(text)


@pytest.mark.parametrize(
    "text",
    [
        "Completed successfully",
        "Error: 500 server error",
        "timeout after 30s",
        # Persistent 403 / billing must not be treated as a transient 401.
        "403 Forbidden",
        "credit balance is too low",
        "disabled subscription",
        # Note: "PR #401 merged" DOES match \b401\b — that is by design.
        # is_transient_401 has no size gate. The size gate in is_auth_death is
        # what prevents false positives on large, legitimate outputs.
    ],
)
def test_is_transient_401_negative(text):
    assert not is_transient_401(text)


def test_is_transient_401_case_insensitive():
    assert is_transient_401("AUTHENTICATION_FAILED")
    assert is_transient_401("Unauthorized")


# ── is_auth_death ─────────────────────────────────────────────────────────────


def test_is_auth_death_tiny_output_with_signature():
    tiny = "Error: 401 unauthorized"
    assert is_auth_death(tiny)


def test_is_auth_death_large_output_with_signature():
    """A large output that mentions 401 must NOT be flagged."""
    large = "PR #401 was merged.\n" + "A" * DEFAULT_MAX_BYTES
    assert not is_auth_death(large)


def test_is_auth_death_tiny_output_no_signature():
    assert not is_auth_death("Some other error happened")


def test_is_auth_death_empty_output():
    assert not is_auth_death("")


def test_is_auth_death_custom_max_bytes():
    # Just over the default limit but under a custom one
    text = "401 unauthorized" + " " * (DEFAULT_MAX_BYTES + 1)
    assert not is_auth_death(text)  # default limit
    assert is_auth_death(text, max_bytes=DEFAULT_MAX_BYTES + 100)  # custom limit
