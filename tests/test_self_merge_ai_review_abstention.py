"""Our AI reviewer can decline to review. The gate must treat that as a blocker.

The self-hosted reviewer (ErikBjare/bob#1122) posts a normal review for most
diffs, but on a submodule-pointer bump it abstains outright — the diff is a SHA
and no source, so there is nothing in it to assess:

    ℹ️ **Submodule pointer change only — not reviewed.**
    ...Treat this as *not reviewed*, not as approved.

The gate could not see that. gptme/gptme-cloud#850 self-merged carrying exactly
that comment, pinning prod's submodule to a commit 5 ahead of gptme master — a
merge of two still-open PRs' heads, reviewed by nobody.

Scope, deliberately: this is a NEGATIVE signal only. An abstention blocks; a
clean AI review grants nothing. Making our own reviewer a merge credential in
place of Greptile is a separate, open decision — the tests below pin that an
ordinary AI review (and no AI review at all) leaves eligibility untouched.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "github"
    / "self-merge-check.py"
)
spec = importlib.util.spec_from_file_location("self_merge_check", MODULE_PATH)
if spec is None or spec.loader is None:
    pytest.skip(f"Could not load module from {MODULE_PATH}", allow_module_level=True)
self_merge_check = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = self_merge_check
spec.loader.exec_module(self_merge_check)


ABSTENTION_BODY = """## 🤖 AI code review

ℹ️ **Submodule pointer change only — not reviewed.**

This diff moves a submodule SHA and contains no source changes, so there is
nothing here for me to assess. Whether the bump is safe depends on the
submodule's commit range, which this diff does not include. Treat this as
*not reviewed*, not as approved.

<!-- bob-ai-review {"sha": "c096e25e50f8", "score": null, "engine": "llm", "history": []} -->
"""

ORDINARY_REVIEW_BODY = """## 🤖 AI code review

**2 finding(s)** — ⚠️ **2** P2

<!-- bob-ai-review {"sha": "deadbeef1234", "score": 3, "engine": "llm", "history": []} -->
"""

CLEAN_REVIEW_BODY = """## 🤖 AI code review

✅ **No findings.** The diff looks correct to me on this pass.

<!-- bob-ai-review {"sha": "deadbeef1234", "score": 5, "engine": "llm", "history": []} -->
"""

ABSTENTION_REASON = "AI review abstained — not reviewed"


REVIEWER = "TimeToBuildBob"


def _gh_returning(*bodies: str, author: str = REVIEWER):
    """Stand in for the comments fetch, in the shared `{author, body}` JSONL form.

    The abstention check reads markers through the same author-validated fetch as
    the positive path, so the stub returns what `gh` actually emits rather than a
    pre-selected body. That keeps the selection logic under test instead of
    hard-coding its answer.
    """

    def _run_gh(args: list[str], **kwargs: Any) -> str:
        return "\n".join(
            json.dumps({"author": author, "body": body}) for body in bodies
        )

    return _run_gh


def _abstained(*bodies: str, author: str = REVIEWER) -> bool | None:
    with patch.object(
        self_merge_check, "run_gh_checked", _gh_returning(*bodies, author=author)
    ):
        return self_merge_check.ai_review_abstained("o/r", 1, expected_author=REVIEWER)


# ---------------------------------------------------------------------------
# ai_review_abstained
# ---------------------------------------------------------------------------


def test_abstention_is_detected() -> None:
    assert _abstained(ABSTENTION_BODY) is True


@pytest.mark.parametrize("body", [ORDINARY_REVIEW_BODY, CLEAN_REVIEW_BODY])
def test_ordinary_review_is_not_an_abstention(body: str) -> None:
    assert _abstained(body) is False


def test_no_ai_review_at_all_is_not_an_abstention() -> None:
    assert _abstained() is False


def test_legacy_marker_without_score_is_not_an_abstention() -> None:
    legacy = (
        "## 🤖 AI code review\n\n"
        '<!-- bob-ai-review {"sha": "deadbeef1234", "engine": "llm"} -->'
    )
    assert _abstained(legacy) is False


def test_only_the_latest_review_counts() -> None:
    """An abstention superseded by a real review must not keep blocking.

    A submodule-only PR that grows source commits gets re-reviewed for real;
    the stale abstention above it is not a verdict on the current diff.
    """
    assert _abstained(ABSTENTION_BODY, ORDINARY_REVIEW_BODY) is False


def test_latest_abstention_after_an_earlier_real_review_blocks() -> None:
    assert _abstained(ORDINARY_REVIEW_BODY, ABSTENTION_BODY) is True


def test_a_human_comment_quoting_the_abstention_does_not_count() -> None:
    """Selection is on the HTML marker, not the visible heading."""
    quoted = (
        "> ℹ️ **Submodule pointer change only — not reviewed.**\n\n"
        "Discussing this, but I am not the reviewer.\n"
    )
    assert _abstained(quoted) is False


def test_failed_lookup_is_unknown() -> None:
    """A failed call is not evidence. It must never read as 'did not abstain'."""
    with patch.object(self_merge_check, "run_gh_checked", lambda *a, **k: None):
        assert (
            self_merge_check.ai_review_abstained("o/r", 1, expected_author=REVIEWER)
            is None
        )


def test_unknown_identity_is_unknown() -> None:
    """Without an identity no marker can be attributed, so nothing is concluded."""
    with patch.object(
        self_merge_check, "run_gh_checked", _gh_returning(ABSTENTION_BODY)
    ):
        assert (
            self_merge_check.ai_review_abstained("o/r", 1, expected_author="") is None
        )


def test_corrupt_marker_from_our_reviewer_is_unknown() -> None:
    """A marker we cannot parse is corruption, not absence.

    Reading it as "no marker" would let a mangled abstention pass as
    "did not abstain" — the failure this gate exists to prevent.
    """
    assert _abstained("<!-- bob-ai-review {bad json} -->") is None


@pytest.mark.parametrize("body", ["", "   \n", "not a marker"])
def test_comments_without_any_marker_are_not_an_abstention(body: str) -> None:
    assert _abstained(body) is False


def test_marker_from_another_account_is_ignored() -> None:
    """The marker is plain text and invisible in the rendered view.

    Without an author check, anyone able to comment could paste a `score: null`
    marker and block the PR from ever merging.
    """
    assert _abstained(ABSTENTION_BODY, author="mallory") is False


def test_fetch_is_author_scoped_and_paginated() -> None:
    captured: list[str] = []

    def _capture(args: list[str], **kwargs: Any) -> str:
        captured.extend(args)
        return json.dumps({"author": REVIEWER, "body": CLEAN_REVIEW_BODY})

    with patch.object(self_merge_check, "run_gh_checked", _capture):
        assert (
            self_merge_check.ai_review_abstained("o/r", 42, expected_author=REVIEWER)
            is False
        )

    assert "repos/o/r/issues/42/comments" in captured
    assert "--paginate" in captured
    # The author is carried in the response and filtered in Python, so the query
    # must actually request it rather than selecting on body alone.
    query = captured[captured.index("--jq") + 1]
    assert "author" in query
    assert "body" in query


# ---------------------------------------------------------------------------
# evaluate_pr wiring
# ---------------------------------------------------------------------------


def _evaluate(*comment_bodies: str) -> Any:
    """Run evaluate_pr on a PR that passes every other gate."""
    pr_data: dict[str, object] = {
        "number": 999,
        "author": {"login": "TimeToBuildBob"},
        "title": "Bump submodule",
        "url": "https://github.com/gptme/gptme-cloud/pull/999",
        "files": [{"path": "tests/test_example.py"}],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "isDraft": False,
        "state": "OPEN",
        "reviewDecision": None,
        "headRefOid": "abc123",
        "mergeStateStatus": "CLEAN",
    }
    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(self_merge_check, "merge_permission", return_value=True),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": True, "unresolved": 0, "total": 1},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=None),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "authors": []},
        ),
        patch.object(self_merge_check, "run_gh", _gh_returning(*comment_bodies)),
        patch.object(
            self_merge_check, "run_gh_checked", _gh_returning(*comment_bodies)
        ),
    ):
        return self_merge_check.evaluate_pr(
            "gptme/gptme-cloud", 999, workspace_repos=["gptme/gptme-cloud"]
        )


def test_evaluate_pr_blocks_on_abstention() -> None:
    """The regression: gptme-cloud#850 merged with exactly this comment."""
    result = _evaluate(ABSTENTION_BODY)
    assert not result.eligible
    assert ABSTENTION_REASON in result.reasons


@pytest.mark.parametrize("body", [ORDINARY_REVIEW_BODY, CLEAN_REVIEW_BODY])
def test_evaluate_pr_unchanged_by_an_ordinary_ai_review(body: str) -> None:
    """A clean AI review must grant nothing and block nothing.

    Pins the scope: this change adds a blocker, it does not turn our own
    reviewer into a merge credential.
    """
    result = _evaluate(body)
    assert not any(ABSTENTION_REASON in r for r in result.reasons)
    assert result.eligible


def test_evaluate_pr_unchanged_when_there_is_no_ai_review() -> None:
    result = _evaluate()
    assert not any(ABSTENTION_REASON in r for r in result.reasons)
    assert result.eligible


def test_unverifiable_greptile_state_refuses_the_ai_fallback() -> None:
    """A clean AI review must not rescue a PR whose Greptile state cannot be read.

    `_fetch_greptile_review_data` returning None means the lookup failed, not that
    Greptile is absent. Treating it as absence would open the AI-review fallback
    on any transient API error, so the gate fails closed instead.
    """
    pr_data: dict[str, object] = {
        "number": 999,
        "author": {"login": "TimeToBuildBob"},
        "title": "Bump submodule",
        "url": "https://github.com/gptme/gptme-cloud/pull/999",
        "files": [{"path": "tests/test_example.py"}],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "isDraft": False,
        "state": "OPEN",
        "reviewDecision": None,
        "headRefOid": "abc123",
        "mergeStateStatus": "CLEAN",
    }
    with (
        patch.object(self_merge_check, "fetch_pr", return_value=pr_data),
        patch.object(self_merge_check, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(self_merge_check, "merge_permission", return_value=True),
        patch.object(
            self_merge_check, "_fetch_greptile_review_data", return_value=None
        ),
        patch.object(
            self_merge_check,
            "fetch_greptile_status",
            return_value={"has_review": False, "unresolved": 0, "total": 0},
        ),
        patch.object(self_merge_check, "greptile_summary_score", return_value=None),
        patch.object(
            self_merge_check,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "authors": []},
        ),
        # A clean AI review does not rescue a PR whose Greptile state is unknown.
        patch.object(self_merge_check, "run_gh", _gh_returning(CLEAN_REVIEW_BODY)),
        patch.object(
            self_merge_check, "run_gh_checked", _gh_returning(CLEAN_REVIEW_BODY)
        ),
    ):
        result = self_merge_check.evaluate_pr(
            "gptme/gptme-cloud", 999, workspace_repos=["gptme/gptme-cloud"]
        )
    assert not result.eligible
    assert any("Could not verify Greptile review state" in r for r in result.reasons)
