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

    def fake(args: list[str], timeout: int = 30) -> bytes | None:
        path = args[-1]
        assert path.startswith("repos/o/r/compare/master...")
        ref = path.rsplit("...", 1)[1]
        # GitHub resolves an abbreviated sha, as the marker may carry one.
        diff = next((d for sha, d in by_sha.items() if sha.startswith(ref)), None)
        return None if diff is None else diff.encode(errors="surrogateescape")

    monkeypatch.setattr(smc, "run_gh_bytes", fake)
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


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("    y = 2", "    y = 2\r"),
        ("    y = 2", "    y = 2\r    z = 3"),
    ],
    ids=["crlf", "bare-cr"],
)
def test_line_ending_change_does_not_carry(diffs: Any, old: str, new: str) -> None:
    diffs[OLD] = _diff(old)
    diffs[NEW] = _rebased(_diff(new))
    assert not smc._review_carries_to_head("o/r", "master", OLD, NEW)


def test_trailing_whitespace_on_last_line_does_not_carry(diffs: Any) -> None:
    diffs[OLD] = _diff("    y = 2").replace("     return x\n", "+end\n")
    diffs[NEW] = _rebased(diffs[OLD]).replace("+end\n", "+end  \n")
    assert not smc._review_carries_to_head("o/r", "master", OLD, NEW)


@pytest.mark.parametrize("mode", ["100755", "120000"], ids=["executable", "symlink"])
def test_added_file_mode_change_does_not_carry(diffs: Any, mode: str) -> None:
    added = (
        "diff --git a/s b/s\n"
        "new file mode 100644\n"
        "index 0000000..c4d8682\n"
        "--- /dev/null\n"
        "+++ b/s\n"
        "@@ -0,0 +1 @@\n"
        "+../k\n"
        "\\ No newline at end of file\n"
    )
    diffs[OLD] = added
    diffs[NEW] = added.replace("new file mode 100644", f"new file mode {mode}")
    assert not smc._review_carries_to_head("o/r", "master", OLD, NEW)


def test_non_utf8_diff_is_compared_byte_exact(diffs: Any) -> None:
    # A Latin-1 byte is not a failure, and changing it is still a change.
    diffs[OLD] = _diff("    y = '\udce9'")
    diffs[NEW] = _rebased(diffs[OLD])
    assert smc._review_carries_to_head("o/r", "master", OLD, NEW)
    diffs[NEW] = _rebased(_diff("    y = '\udce8'"))
    smc._pr_diff_patch_id.cache_clear()
    assert not smc._review_carries_to_head("o/r", "master", OLD, NEW)


def test_run_gh_bytes_returns_stdout_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"+a\r\n+\xe9  \n"
    real_run = smc.subprocess.run

    def fake(cmd: list[str], **kwargs: Any) -> Any:
        script = f"import sys; sys.stdout.buffer.write({payload!r})"
        return real_run([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(smc.subprocess, "run", fake)
    assert smc.run_gh_bytes(["api", "x"]) == payload


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


@pytest.mark.parametrize(
    "extra",
    [
        "diff --git a/x b/y\nsimilarity index 100%\nrename from x\nrename to y\n",
        "diff --git a/x b/x\nold mode 100644\nnew mode 100755\n",
        "diff --git a/__init__.py b/__init__.py\nnew file mode 100644\nindex 0000000..e69de29\n",
        "diff --git a/__init__.py b/__init__.py\ndeleted file mode 100644\nindex e69de29..0000000\n",
    ],
    ids=["rename", "executable", "empty-add", "empty-delete"],
)
def test_header_only_change_does_not_carry(diffs: Any, extra: str) -> None:
    diffs[OLD] = _diff("    y = 2")
    diffs[NEW] = _rebased(diffs[OLD]) + extra
    assert smc._pr_diff_patch_id("o/r", "master", NEW) is None
    assert not smc._review_carries_to_head("o/r", "master", OLD, NEW)


def test_mode_change_with_content_does_not_carry(diffs: Any) -> None:
    diffs[OLD] = _diff("    y = 2")
    diffs[NEW] = _rebased(diffs[OLD]).replace(
        "index 3333333..4444444 100644\n",
        "old mode 100644\nnew mode 100755\nindex 3333333..4444444\n",
    )
    assert smc._pr_diff_patch_id("o/r", "master", NEW) is None
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


@pytest.mark.parametrize("reviewed", [OLD, NEW], ids=["carried", "exact"])
def test_accepted_review_reports_provenance(diffs: Any, reviewed: str) -> None:
    diffs[OLD] = _diff("    y = 2")
    diffs[NEW] = _rebased(diffs[OLD])
    status = smc.fetch_ai_review_status(
        "o/r",
        1,
        head_sha=NEW,
        expected_author="TimeToBuildBob",
        marker_result={
            "sha": reviewed[:12],
            "score": 5,
            "finding_provenance": {"0123456789ab": [{"model": "m", "pass": 2}]},
            "consensus": {
                "requested": 3,
                "answered": 3,
                "jobs_requested": 3,
                "jobs_answered": 3,
                "min_agreement": 2,
                "min_agreement_requested": 2,
            },
        },
        review_data=([], []),
        base_ref="master",
    )
    assert status["accepted"], status
    assert ("finding(s) advisory" in status["detail"]) == (reviewed == NEW)
    if reviewed == OLD:
        assert f"carried from {OLD[:12]} to head {NEW[:12]}" in status["detail"]
        assert "via identical patch-id" in status["detail"]
        assert "at current head" not in status["detail"]
    else:
        assert f"at current head {NEW[:12]}" in status["detail"]
