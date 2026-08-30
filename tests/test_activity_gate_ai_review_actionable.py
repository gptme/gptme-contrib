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


def _gate_source() -> str:
    return GATE.read_text()


def _extract_function(name: str) -> str:
    src = _gate_source()
    start = src.index(f"{name}() {{")
    rest = src[start:]
    return rest[: rest.index("\n}\n") + len("\n}\n")]


def _extract_shell_assignment(name: str) -> str:
    prefix = f"{name}="
    line = next(line for line in _gate_source().splitlines() if line.startswith(prefix))
    return line


def _actionable(
    body: str,
    login: str,
    *,
    activity_type: str = "comment",
) -> bool:
    """Run has_actionable_update with the supplied comment or review activity."""
    activity = {
        "author": {"login": login},
        "body": body,
    }
    if activity_type == "comment":
        activity["createdAt"] = "2026-08-08T10:00:00Z"
        comments = [activity]
        reviews = []
    elif activity_type == "review":
        activity["submittedAt"] = "2026-08-08T10:00:00Z"
        comments = []
        reviews = [activity]
    else:
        raise ValueError(f"unsupported activity type: {activity_type}")

    pr = {"comments": comments, "latestReviews": reviews}
    script = f"""
set -uo pipefail
AUTHOR={BOT}
{_extract_shell_assignment("AI_REVIEW_MARKER_RE")}
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


def test_ai_review_finding_in_latest_review_is_actionable():
    assert _actionable(
        f"{MARKER}-finding -->\nP1 — the hash ignores .state",
        BOT,
        activity_type="review",
    )


def test_ai_review_mentioning_maintainer_is_still_actionable():
    assert _actionable(
        f"{MARKER}-finding -->\nP1 — @ErikBjare should verify this path", BOT
    )


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


# --- 2026-08-29 regressions: gptme/gptme#3638 and #3669 -------------------
#
# #3638: the reviewer upserts its summary comment IN PLACE, so a re-review
# with standing findings moves nothing in createdAt order — the marker JSON
# is the only trace. Non-empty findings[] at the current head = inbound.
# #3669: on a Bob-authored PR the reviewer's whole pass (empty-body review +
# marked inline comments) is invisible in pr_data (latestReviews omits the
# PR author's own reviews); the newest inline review comment must be checked
# before silencing a plain self comment.

HEAD = "7bb89e6c544c12c15fbfd224ab40f690e7a11401"
MARKER_WITH_FINDINGS = (
    "## AI code review\n\n"
    f'{MARKER} {{"sha": "7bb89e6c544c", "score": 3, '
    f'"findings": [{{"fp": "071a049d9d84", "severity": "P1"}}]}} -->'
)


def _actionable_pr(pr: dict, repo: str | None = None, gh_stdout: str = "") -> bool:
    """Run has_actionable_update on a full pr_data dict, optionally with a
    repo argument and a stubbed `gh` binary whose stdout is canned."""
    import os
    import tempfile

    repo_arg = f" '{repo}'" if repo else ""
    with tempfile.TemporaryDirectory() as td:
        gh_stub = Path(td) / "gh"
        gh_stub.write_text("#!/bin/bash\ncat <<'GHEOF'\n" + gh_stdout + "\nGHEOF\n")
        gh_stub.chmod(0o755)
        script = f"""
set -uo pipefail
export PATH="{td}:$PATH"
AUTHOR={BOT}
{_extract_shell_assignment("AI_REVIEW_MARKER_RE")}
{_extract_function("has_actionable_update")}
if has_actionable_update '{json.dumps(pr)}'{repo_arg}; then echo YES; else echo NO; fi
"""
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ},
        )
    return "YES" in proc.stdout


def _pr_3638_shape(head: str = HEAD) -> dict:
    """Marker comment with standing findings, then two plain self replies."""
    return {
        "number": 3638,
        "headRefOid": head,
        "comments": [
            {
                "author": {"login": BOT},
                "createdAt": "2026-08-26T16:25:07Z",
                "body": MARKER_WITH_FINDINGS,
            },
            {
                "author": {"login": BOT},
                "createdAt": "2026-08-26T19:56:40Z",
                "body": "## Greptile Convergence Adjudication\n\nAll resolved.",
            },
        ],
        "latestReviews": [],
    }


def test_standing_marker_findings_at_head_actionable():
    """gptme#3638: standing P1 in the in-place-edited marker, last actors all
    self -> inbound work."""
    assert _actionable_pr(_pr_3638_shape())


def test_quoted_standing_marker_reply_is_silenced():
    """A later self-reply that blockquotes the standing marker is not itself
    reviewer state. Without a complete-line marker comment, last-actor=self
    stays silenced (P2 on gptme-contrib#1549)."""
    quoted = (
        "Looked at this.\n"
        f'> {MARKER} {{"sha": "7bb89e6c544c", "score": 3, '
        f'"findings": [{{"fp": "x", "severity": "P1"}}]}} -->\n'
        "Waiting on the next pass."
    )
    pr = {
        "number": 1549,
        "headRefOid": HEAD,
        "comments": [
            {
                "author": {"login": BOT},
                "createdAt": "2026-08-30T10:00:00Z",
                "body": quoted,
            },
        ],
        "latestReviews": [],
    }
    assert not _actionable_pr(pr)


def test_real_marker_still_actionable_when_later_reply_quotes_it():
    """The jq selector must keep the real complete-line marker even if a
    later self-reply quotes it. Otherwise the #3638 standing-findings
    check would go blind the moment anyone quotes the marker."""
    quoted = (
        "Looked at this.\n"
        f'> {MARKER} {{"sha": "7bb89e6c544c", "score": 3, '
        f'"findings": [{{"fp": "x", "severity": "P1"}}]}} -->\n'
        "Waiting on the next pass."
    )
    pr = _pr_3638_shape()
    pr["comments"].append(
        {
            "author": {"login": BOT},
            "createdAt": "2026-08-30T10:00:00Z",
            "body": quoted,
        }
    )
    assert _actionable_pr(pr)


def test_standing_marker_findings_stale_sha_silenced():
    """A marker recorded against an older head does not count — the push
    since the review is fresh activity and the sweep re-reviews it."""
    pr = _pr_3638_shape(head="deadbeef00000000000000000000000000000000")
    assert not _actionable_pr(pr)


def _pr_3669_shape() -> dict:
    """Plain self bot-invocation comment is the only visible activity."""
    return {
        "number": 3669,
        "headRefOid": "a0eb591ec0ea0b11812fa7285d24448cf7dcad72",
        "comments": [
            {
                "author": {"login": BOT},
                "createdAt": "2026-08-29T14:37:34Z",
                "body": "@greptileai review",
            },
        ],
        "latestReviews": [],
    }


def test_newer_inline_finding_is_actionable():
    """gptme#3669: the reviewer's pass is invisible in pr_data; the newest
    inline review comment carries the finding marker -> inbound work."""
    inline = json.dumps(
        {
            "login": BOT,
            "time": "2026-08-29T15:58:39Z",
            "body": f"{MARKER}-finding -->\nP1 — file write not under flock",
        }
    )
    assert _actionable_pr(_pr_3669_shape(), repo="gptme/gptme", gh_stdout=inline)


def test_newer_inline_self_reply_is_silenced():
    """Bob's own inline thread replies (self login, no marker) must stay
    silenced — that is the self-trigger loop the guard exists to stop."""
    inline = json.dumps(
        {
            "login": BOT,
            "time": "2026-08-29T15:58:39Z",
            "body": "Fixed in abc123, thanks.",
        }
    )
    assert not _actionable_pr(_pr_3669_shape(), repo="gptme/gptme", gh_stdout=inline)


def test_older_inline_finding_is_silenced():
    """An inline finding OLDER than the last visible self activity does not
    flip the verdict — the agent already responded after it."""
    inline = json.dumps(
        {
            "login": BOT,
            "time": "2026-08-29T10:00:00Z",
            "body": f"{MARKER}-finding -->\nP1 — already handled",
        }
    )
    assert not _actionable_pr(_pr_3669_shape(), repo="gptme/gptme", gh_stdout=inline)
