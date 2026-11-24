"""Review evidence survives a rebase that leaves the PR's own diff unchanged.

A rebase or merge-from-base changes the head sha without changing what the PR
changes. Treating that as an unreviewed head costs a full re-review round per
rebase (ErikBjare/bob epic-merge-efficiency slice 3). The helper compares
``git patch-id --verbatim`` of each commit's three-dot compare diff against the
base, and must fail closed on anything it cannot read.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "github" / "self-merge-check.py"
)
spec = importlib.util.spec_from_file_location("self_merge_check_carry", MODULE_PATH)
if spec is None or spec.loader is None:
    pytest.skip(f"Could not load {MODULE_PATH}", allow_module_level=True)
smc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smc
spec.loader.exec_module(smc)

OLD = "a" * 40
NEW = "b" * 40


def _diff(added: str) -> str:
    return (
        "diff --git a/f.py b/f.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -10,2 +10,3 @@ def f():\n"
        "     x = 1\n"
        f"+{added}\n"
        "     return x\n"
    )


def _rebased(diff: str) -> str:
    # Same change, new base: blob ids and hunk line numbers move.
    return diff.replace("1111111..2222222", "3333333..4444444").replace(
        "@@ -10,2 +10,3 @@", "@@ -42,2 +42,3 @@"
    )


@pytest.fixture
def diffs(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str | None]]:
    by_sha: dict[str, str | None] = {}

    def fake(args: list[str], timeout: int = 30) -> str | None:
        path = args[-1]
        assert path.startswith("repos/o/r/compare/master...")
        ref = path.rsplit("...", 1)[1]
        # GitHub resolves an abbreviated sha, as the marker may carry one.
        return next((d for sha, d in by_sha.items() if sha.startswith(ref)), None)

    monkeypatch.setattr(smc, "run_gh_checked", fake)
    smc._pr_diff_patch_id.cache_clear()
    yield by_sha
    smc._pr_diff_patch_id.cache_clear()


def test_exact_match_needs_no_lookup(diffs: Any) -> None:
    assert smc._review_carries_to_head("", "", NEW[:12], NEW)


def test_rebase_with_identical_diff_carries(diffs: Any) -> None:
    diffs[OLD] = _diff("    y = 2")
    diffs[NEW] = _rebased(_diff("    y = 2"))
    assert smc._review_carries_to_head("o/r", "master", OLD[:12], NEW)


def test_changed_line_does_not_carry(diffs: Any) -> None:
    diffs[OLD] = _diff("    y = 2")
    diffs[NEW] = _rebased(_diff("    y = 3"))
    assert not smc._review_carries_to_head("o/r", "master", OLD, NEW)


def test_indentation_change_does_not_carry(diffs: Any) -> None:
    # Plain `git patch-id` ignores whitespace; in Python that is semantics.
    diffs[OLD] = _diff("    y = 2")
    diffs[NEW] = _rebased(_diff("        y = 2"))
    assert not smc._review_carries_to_head("o/r", "master", OLD, NEW)


def test_unreadable_diff_fails_closed(diffs: Any) -> None:
    diffs[NEW] = _diff("    y = 2")
    assert not smc._review_carries_to_head("o/r", "master", OLD, NEW)
    diffs[OLD] = ""
    smc._pr_diff_patch_id.cache_clear()
    assert not smc._review_carries_to_head("o/r", "master", OLD, NEW)


def test_binary_diff_fails_closed(diffs: Any) -> None:
    binary = "diff --git a/i.png b/i.png\nBinary files a/i.png and b/i.png differ\n"
    diffs[OLD] = binary
    diffs[NEW] = binary
    assert not smc._review_carries_to_head("o/r", "master", OLD, NEW)


def test_missing_base_ref_is_exact_only(diffs: Any) -> None:
    diffs[OLD] = diffs[NEW] = _diff("    y = 2")
    assert not smc._review_carries_to_head("o/r", "", OLD, NEW)
    assert not smc._review_carries_to_head("o/r", "../x", OLD, NEW)
    assert not smc._review_carries_to_head("o/r", "master", "not-a-sha!", NEW)


def test_stale_ai_marker_on_rebased_head_is_accepted(
    diffs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    diffs[OLD] = _diff("    y = 2")
    diffs[NEW] = _rebased(_diff("    y = 2"))
    marker = {
        "sha": OLD[:12],
        "score": 5,
        "passes": 3,
        "consensus": {"requested": 3, "answered": 3},
    }
    status = {}
    for base_ref in ("master", ""):
        status[base_ref] = smc.fetch_ai_review_status(
            "o/r",
            1,
            head_sha=NEW,
            expected_author="TimeToBuildBob",
            marker_result=marker,
            review_data=([], []),
            base_ref=base_ref,
        )
    # Without a base ref the old exact-match behaviour holds.
    assert status[""]["refusal"] == "stale"
    # With it, staleness is no longer the refusal; any other gate still applies.
    assert status["master"].get("refusal") != "stale"
