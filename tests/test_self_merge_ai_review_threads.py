"""AI review findings must not count as unresolved *human* review threads.

Bob's self-hosted AI reviewer (ErikBjare/bob#1122, the Greptile replacement)
posts each inline finding as a review comment through Bob's **user** account,
not a GitHub App. `_is_bot_author` only recognises `*[bot]` logins and a fixed
name list, so every finding looked like a human opening a review thread.

The effect was the opposite of the reviewer's purpose: posting a review made a
PR *less* mergeable. Observed live on gptme/gptme-contrib#1383 —

    Eligible: NO
    - 4 unresolved human review thread(s) from: TimeToBuildBob

where all four "human" threads were the AI reviewer's own findings, on a PR
authored by the same account.

Detection requires both the reviewer's login and the finding marker on the
root. Unmarked feedback from the shared account still counts as human.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "github" / "self-merge-check.py"
)
spec = importlib.util.spec_from_file_location("self_merge_check_ai", MODULE_PATH)
if spec is None or spec.loader is None:
    pytest.skip(f"Could not load {MODULE_PATH}", allow_module_level=True)
smc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smc
spec.loader.exec_module(smc)

MARKER = smc._AI_REVIEW_FINDING_MARKER


def _thread(author: str, body: str, resolved: bool = False) -> dict[str, Any]:
    return {
        "isResolved": resolved,
        "comments": {"nodes": [{"author": {"login": author}, "body": body}]},
    }


REVIEWER = "TimeToBuildBob"


def _count(
    threads: list[dict[str, Any]], expected_author: str | None = REVIEWER
) -> dict[str, Any]:
    result: dict[str, Any] = smc.fetch_unresolved_human_threads(
        "o/r", 1, review_data=([], threads), expected_author=expected_author
    )
    return result


def test_ai_findings_do_not_block() -> None:
    """The regression: four AI findings must not read as human feedback."""
    threads = [
        _thread("TimeToBuildBob", f"{MARKER}\n\nP1 — the hash ignores .state")
        for _ in range(4)
    ]
    result = _count(threads)
    assert result["unresolved"] == 0
    assert result["total"] == 0
    assert result["authors"] == []


def test_genuine_human_thread_still_blocks() -> None:
    """A real reviewer must still stop a self-merge — the point of the gate."""
    result = _count([_thread("ErikBjare", "this looks wrong to me")])
    assert result["unresolved"] == 1
    assert result["authors"] == ["ErikBjare"]


def test_same_account_human_comment_still_blocks() -> None:
    """Unmarked comments from Bob's account are human-directed and still count.

    The expected author's login alone is not sufficient for the exemption.
    """
    result = _count([_thread("TimeToBuildBob", "Erik — should we drop this flag?")])
    assert result["unresolved"] == 1
    assert result["authors"] == ["TimeToBuildBob"]


def test_mixed_threads_count_only_the_human_one() -> None:
    result = _count(
        [
            _thread("TimeToBuildBob", f"{MARKER}\n\nP2 — substring match"),
            _thread("ErikBjare", "please add a test"),
            _thread("greptile-apps[bot]", "bot finding"),
        ]
    )
    assert result["unresolved"] == 1
    assert result["authors"] == ["ErikBjare"]


def test_resolved_ai_thread_is_also_excluded_from_total() -> None:
    result = _count([_thread("TimeToBuildBob", f"{MARKER}\n\nP1", resolved=True)])
    assert result["total"] == 0


def test_missing_body_is_treated_as_human() -> None:
    """Fail closed: if the body is absent we cannot prove it is machine-authored."""
    threads = [
        {"isResolved": False, "comments": {"nodes": [{"author": {"login": REVIEWER}}]}}
    ]
    assert _count(threads)["unresolved"] == 1


def test_graphql_query_requests_comment_body() -> None:
    """The marker check is inert unless the query actually fetches `body`."""
    src = MODULE_PATH.read_text()
    threads_block = src.split("reviewThreads(", 1)[1].split("checks_green", 1)[0]
    assert "body" in threads_block, "reviewThreads query must select comment body"


def test_marker_on_human_thread_does_not_hide_it() -> None:
    """gptme-contrib#2346 item 4: the marker is a label, not a signature.

    A human who prefixes an unresolved review thread with the finding marker
    must not make it vanish from the gate — only the reviewer's own account
    can open an exempt AI finding thread.
    """
    result = _count([_thread("ErikBjare", f"{MARKER}\n\nplease fix this")])
    assert result["unresolved"] == 1
    assert result["authors"] == ["ErikBjare"]


def test_marker_thread_without_known_reviewer_fails_closed() -> None:
    """Without the reviewer identity, nothing is exempted from the gate."""
    result = _count(
        [_thread(REVIEWER, f"{MARKER}\n\nP1 — finding")], expected_author=None
    )
    assert result["unresolved"] == 1
