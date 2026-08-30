"""Transient-401 / auth-death classifier for gptodo spawn.

Sourced from Bob's canonical ``scripts/lib/auth_401.py`` (task
``harden-401-auth-everywhere``, ErikBjare/bob#968). Kept as a small,
self-contained copy so gptodo does not depend on the agent workspace.

Public API
----------
``is_transient_401(text)``
    Does arbitrary text (stderr, captured stdout) carry a 401/auth-failure
    signature?  No size gate — for classifying a captured error string.

``is_auth_death(output, max_bytes)``
    The spawn-output case: tiny output *and* an auth signature.  Both
    conditions required so a large, genuinely-completed session whose diff
    merely mentions "401" is not misclassified.
"""

from __future__ import annotations

import re

# Auth-failure markers emitted by CC / gptme on a dead auth token.
# Kept deliberately broad — the tiny-output size gate prevents false positives
# in the spawn-output case.
_AUTH_PATTERNS = [
    r"\b401\b",
    r"\b403\b",
    r"unauthorized",
    r"invalid bearer token",
    r"authentication_error",
    r"authentication_failed",
    r"invalid[_ ]api[_ ]key",
    r"oauth\b.{0,40}\bexpired",
    r"oauth\b.{0,40}\b(fail|error|invalid)",
    r"please run /login",
    r"credit balance is too low",
    r"disabled.*subscription",
]

_AUTH_RE = re.compile("|".join(_AUTH_PATTERNS), re.IGNORECASE)

# Smallest real worker output observed (2026-06-24) was ~46 KB; 2 KB has a
# >20× safety margin against false positives from completed sessions.
DEFAULT_MAX_BYTES = 2_000


def is_transient_401(text: str) -> bool:
    """Return True iff *text* carries a 401 / auth-failure signature.

    No size gate — call this on a captured stderr/stdout string.  For the
    size-gated spawn-output case use :func:`is_auth_death`.
    """
    return bool(_AUTH_RE.search(text))


def is_auth_death(output: str, max_bytes: int = DEFAULT_MAX_BYTES) -> bool:
    """Return True iff *output* is small **and** carries an auth-failure signature.

    The spawn case: a session that died instantly on a 401 produces a tiny
    output. Both conditions are required so a large, genuinely-completed
    session whose diff merely mentions "401" is not flagged.
    """
    if len(output.encode("utf-8", errors="replace")) > max_bytes:
        return False
    return is_transient_401(output)
