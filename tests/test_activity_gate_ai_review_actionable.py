"""PM must act on our own AI reviewer's findings, not silence them as self-chatter.

`has_actionable_update` skips an update when the last actor is $AUTHOR, to stop
the agent re-triggering itself. Bob's self-hosted AI reviewer (ErikBjare/bob#1122)
posts through Bob's *user* account rather than a GitHub App, so its reviews hit
that guard and were dropped: PM listened to Greptile (~70 references in the gate)
and ignored the replacement we actually run (zero references).

The distinguishing signal is the machine marker on the comment body. Bob's
genuine human-directed comments carry no marker and must STILL be silenced —
that is the real self-trigger loop this guard exists to stop.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "scripts" / "github" / "activity-gate.sh"
BOT = "TimeToBuildBob"
MARKER = "<!-- bob-ai-review"


def _extract_function(name: str) -> str:
    src = GATE.read_text()
    start = src.index(f"{name}() {{")
    rest = src[start:]
    return rest[: rest.index("\n}\n") + len("\n}\n")]


def _actionable(body: str, login: str) -> bool:
    """Run has_actionable_update against a PR whose latest comment is (login, body)."""
    pr = {
        "comments": [
            {
                "author": {"login": login},
                "createdAt": "2026-08-08T10:00:00Z",
                "body": body,
            }
        ],
        "latestReviews": [],
    }
    script = f"""
set -uo pipefail
AUTHOR={BOT}
AI_REVIEW_MARKER_RE='^<!-- bob-ai-review(-finding| \\{{.*\\}}) -->$'
{_extract_function("has_actionable_update")}
if has_actionable_update '{json.dumps(pr)}'; then echo YES; else echo NO; fi
"""
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )
    return "YES" in proc.stdout


def test_ai_review_summary_is_actionable():
    """The regression: our reviewer's findings must reach PM."""
    assert _actionable(f'3 findings\n\n{MARKER} {{"sha": "abc"}} -->', BOT)


def test_ai_review_inline_finding_is_actionable():
    assert _actionable(f"{MARKER}-finding -->\nP1 — the hash ignores .state", BOT)


def test_bobs_own_plain_comment_is_still_silenced():
    """The loop guard must survive — this is why we match the marker, not the login."""
    assert not _actionable("Pushed a fix, rerunning CI.", BOT)


def test_bobs_reply_quoting_marker_inline_is_still_silenced():
    assert not _actionable(f"Fixed the parsing of `{MARKER}` and pushed.", BOT)


def test_bobs_reply_quoting_marker_line_is_still_silenced():
    quoted = f"> {MARKER}-finding -->\n> P1 — stale state\n\nFixed in abc123."
    assert not _actionable(quoted, BOT)


def test_similar_standalone_marker_is_still_silenced():
    assert not _actionable(f"Status update\n\n{MARKER}-manual -->", BOT)


def test_human_comment_still_actionable():
    assert _actionable("this looks wrong to me", "ErikBjare")


def test_greptile_review_still_actionable():
    """Do not regress the reviewer PM already listened to."""
    assert _actionable("<h3>Security Review</h3> looks fine", "greptile-apps[bot]")
