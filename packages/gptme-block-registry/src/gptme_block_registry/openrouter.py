"""OpenRouter key-limit window classification and re-probe deadline math.

Ports the semantics of the shell breaker so callers in any language get the same
answer:

* ``daily``   → next UTC midnight (the key cap resets on the UTC day boundary).
* ``weekly`` / ``monthly`` → now + 24h. OpenRouter's weekly/monthly windows are
  RELATIVE to the key's own rolling period, which the 403 body does not
  disclose, so there is no calendar boundary to target. Blocking to a guessed
  boundary would idle a healthy key for up to a week; 24h is a conservative
  re-probe interval.
* ``credits`` → now + 4h (account credit exhaustion has no scheduled reset; an
  operator top-up should be noticed within hours).
* anything unrecognized → ``daily``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_WINDOW_RE = re.compile(
    r"key limit exceeded[^)]*\((daily|weekly|monthly) limit\)", re.IGNORECASE
)


def limit_window_from_text(text: str) -> str | None:
    """Classify the limit window named in an OpenRouter 403 body.

    Returns ``daily``/``weekly``/``monthly``, or ``daily`` when the phrase is
    present without a window (the historical default), or ``None`` when the
    text does not report a key-limit at all.
    """
    if "key limit exceeded" not in text.lower():
        return None
    match = _WINDOW_RE.search(text)
    return match.group(1).lower() if match else "daily"


def block_deadline(window: str, now: datetime | None = None) -> datetime:
    """Absolute re-probe deadline for a limit window (always UTC-aware)."""
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    if window == "weekly" or window == "monthly":
        return now + timedelta(hours=24)
    if window == "credits":
        return now + timedelta(hours=4)
    # daily and every unrecognized window
    next_midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return next_midnight
