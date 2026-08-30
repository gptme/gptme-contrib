"""Provenance tests for scripts/github/greptile-merge-signal.py.

Greptile edits its summary comment in place, so "latest summary" says nothing
about WHICH head a score belongs to. gptme/gptme#3656: three commits postdated
the last review pass and a handoff comment attributed the old 5/5 to the new
head. The summary footer ("Last reviewed commit: [...](…/commit/<sha>)") is the
provenance that makes the score checkable; these tests pin its extraction and
the ``head_sha`` staleness gate built on it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).parent.parent / "scripts" / "github" / "greptile-merge-signal.py"
)
_spec = importlib.util.spec_from_file_location("greptile_merge_signal", _SCRIPT)
assert _spec is not None and _spec.loader is not None
gms = importlib.util.module_from_spec(_spec)
# @dataclass resolves the defining module through sys.modules; register before exec.
sys.modules["greptile_merge_signal"] = gms
_spec.loader.exec_module(gms)

HEAD = "62ec507d022bc1548201a874636742308b7453cc"
OLD = "aface374b55e2f818336156745eb503bcb132036"

FOOTER = (
    '<sub>Reviews (8): Last reviewed commit: ["fix(harness): gate dispatch..."]'
    "(https://github.com/gptme/gptme/commit/{sha}) | "
    "[Re-trigger Greptile](https://app.greptile.com/api/retrigger?id=1)</sub>"
)


def _summary(sha: str | None) -> dict:
    body = "<h3>Greptile Summary</h3>\n\nSafe to merge.\n\nConfidence Score: 5/5</h3>\n"
    if sha is not None:
        body += "\n" + FOOTER.format(sha=sha)
    return {
        "id": 1,
        "user": {"login": "greptile-apps[bot]"},
        "body": body,
        "created_at": "2026-08-28T00:18:09Z",
        "updated_at": "2026-08-29T14:10:08Z",
    }


def test_extract_reviewed_commit_from_footer() -> None:
    assert gms._extract_reviewed_commit(_summary(HEAD)["body"]) == HEAD


def test_extract_reviewed_commit_absent() -> None:
    assert gms._extract_reviewed_commit(_summary(None)["body"]) is None


def test_signal_reports_reviewed_commit() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gms, "_load_comments", lambda repo, pr: [_summary(HEAD)])
        result = gms.evaluate_summary_signal("gptme/gptme", 3656)
    assert result.reviewed_commit == HEAD
    assert result.eligible is True


def test_signal_stale_for_head_blocks() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gms, "_load_comments", lambda repo, pr: [_summary(OLD)])
        result = gms.evaluate_summary_signal("gptme/gptme", 3656, head_sha=HEAD)
    assert result.eligible is False
    assert result.reason == "summary_stale_for_head"
    assert result.reviewed_commit == OLD
    # The stale score is still surfaced for diagnostics.
    assert result.score == 5


def test_signal_matching_head_stays_eligible() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gms, "_load_comments", lambda repo, pr: [_summary(HEAD)])
        result = gms.evaluate_summary_signal("gptme/gptme", 3656, head_sha=HEAD)
    assert result.eligible is True
    assert result.reason == "positive_summary_comment"


def test_signal_no_provenance_fails_open() -> None:
    """Old summary formats without the footer must not be blocked by head_sha."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gms, "_load_comments", lambda repo, pr: [_summary(None)])
        result = gms.evaluate_summary_signal("gptme/gptme", 3656, head_sha=HEAD)
    assert result.eligible is True
    assert result.reviewed_commit is None


def test_signal_short_head_prefix_matches() -> None:
    """A >=7-char head prefix of the reviewed commit is a match, not stale."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gms, "_load_comments", lambda repo, pr: [_summary(HEAD)])
        result = gms.evaluate_summary_signal("gptme/gptme", 3656, head_sha=HEAD[:10])
    assert result.eligible is True


def test_signal_without_head_sha_unchanged() -> None:
    """Default CLI/API behavior (no head_sha) is exactly the old behavior."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gms, "_load_comments", lambda repo, pr: [_summary(OLD)])
        result = gms.evaluate_summary_signal("gptme/gptme", 3656)
    assert result.eligible is True
