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

Detection is by explicit marker, not by author login: the login is shared with
Bob's genuine human-directed review comments, which must keep blocking.
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


def _count(threads: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = smc.fetch_unresolved_human_threads(
        "o/r", 1, review_data=([], threads)
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

    This is why detection keys on the marker rather than the author login.
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
        {"isResolved": False, "comments": {"nodes": [{"author": {"login": "x"}}]}}
    ]
    assert _count(threads)["unresolved"] == 1


def test_graphql_query_requests_comment_body() -> None:
    """The marker check is inert unless the query actually fetches `body`."""
    src = MODULE_PATH.read_text()
    threads_block = src.split("reviewThreads(", 1)[1].split("checks_green", 1)[0]
    assert "body" in threads_block, "reviewThreads query must select comment body"
