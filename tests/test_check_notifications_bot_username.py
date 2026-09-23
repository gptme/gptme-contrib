"""The merge-ready suppression must filter on $BOT_USERNAME, not Bob's login.

gptme/gptme-contrib#1705: a hard-coded ``user.login == "TimeToBuildBob"`` jq
filter meant other agents never recognized their own maintainer-waiting
comments, so merge-ready PRs kept re-notifying.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "github" / "check-notifications.sh"


def test_merge_ready_filter_uses_bot_username() -> None:
    src = SCRIPT.read_text()
    assert 'select(.user.login == "TimeToBuildBob")' not in src
    assert "BOT_USERNAME" in src
    assert "${BOT_USERNAME:-TimeToBuildBob}" in src
