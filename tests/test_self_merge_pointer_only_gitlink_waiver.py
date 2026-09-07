"""Pointer-only gitlink bumps must not deadlock on AI-review abstention.

The reviewer abstains on submodule-pointer diffs (`score: null`,
`submodule_only: true`). After gptme-cloud#850 that abstention is never an
AI-review *pass*. Combined with the later "AI review is always required"
gate, pointer-only bumps that already have Greptile 5/5, an allowlisted
gitlink, and a merged upstream PR could never self-merge (gptme-cloud#899,
#900, #901 — Erik merged them by hand).

The waiver is the requirement, not the abstention: branch-only pins still
block (gptme-cloud#898's 9da4505).
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
smc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smc
spec.loader.exec_module(smc)


HEAD_SHA = "12f455f000000000000000000000000000000000"
OLD_SHA = "b3b4501d00000000000000000000000000000000"
NEW_SHA = "110a4eec00000000000000000000000000000000"
BRANCH_ONLY_SHA = "9da4505000000000000000000000000000000000"

GITLINK_PATCH = (
    f"@@ -1 +1 @@\n-Subproject commit {OLD_SHA}\n+Subproject commit {NEW_SHA}\n"
)
GITMODULES = """
[submodule "gptme"]
	path = gptme
	url = git@github.com:gptme/gptme.git
"""


def test_parse_gitmodules_maps_path_to_url() -> None:
    assert smc.parse_gitmodules(GITMODULES)["gptme"] == "git@github.com:gptme/gptme.git"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("git@github.com:gptme/gptme.git", "gptme/gptme"),
        ("https://github.com/gptme/gptme.git", "gptme/gptme"),
        ("https://github.com/gptme/gptme", "gptme/gptme"),
        ("https://example.com/gptme/gptme.git", None),
    ],
)
def test_github_repo_from_remote_url(url: str, expected: str | None) -> None:
    assert smc.github_repo_from_remote_url(url) == expected


def test_parse_pointer_only_gitlink_accepts_subproject_commit_patch() -> None:
    parsed = smc.parse_pointer_only_gitlink(
        [{"path": "gptme", "status": "modified", "patch": GITLINK_PATCH}]
    )
    assert parsed == ("gptme", NEW_SHA)


def test_parse_pointer_only_gitlink_rejects_mixed_diff() -> None:
    mixed = GITLINK_PATCH + "+print('not a gitlink')\n"
    assert (
        smc.parse_pointer_only_gitlink(
            [{"path": "gptme", "status": "modified", "patch": mixed}]
        )
        is None
    )


def test_parse_pointer_only_gitlink_rejects_multiple_files() -> None:
    entry = {"path": "gptme", "status": "modified", "patch": GITLINK_PATCH}
    assert smc.parse_pointer_only_gitlink([entry, entry]) is None


def test_parse_pointer_only_gitlink_rejects_empty_patch() -> None:
    assert (
        smc.parse_pointer_only_gitlink(
            [{"path": "gptme", "status": "modified", "patch": ""}]
        )
        is None
    )


def _pr_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "number": 901,
        "author": {"login": "TimeToBuildBob"},
        "title": "chore(gptme): bump submodule to master after gptme#3742 merge",
        "url": "https://github.com/gptme/gptme-cloud/pull/901",
        "files": [{"path": "gptme"}],
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "isDraft": False,
        "state": "OPEN",
        "reviewDecision": None,
        "headRefOid": HEAD_SHA,
        "mergeStateStatus": "CLEAN",
        "labels": [],
    }
    data.update(overrides)
    return data


def _evaluate(
    *,
    files: list[dict[str, str]] | None = None,
    greptile_score: int | None = 5,
    greptile_fresh: bool = True,
    greptile_unresolved: int = 0,
    merged_upstream: bool | None = True,
    gitmodules: str | None = GITMODULES,
    gitlink_patch: str = GITLINK_PATCH,
    allowed_paths: str = "gptme/gptme-cloud:gptme",
    pr_overrides: dict[str, object] | None = None,
) -> Any:
    pr_data = _pr_data(**(pr_overrides or {}))
    if files is not None:
        pr_data["files"] = files
    raw_files = pr_data["files"]
    file_entries = raw_files if isinstance(raw_files, list) else []
    file_paths = [
        str(entry.get("path", "")) for entry in file_entries if isinstance(entry, dict)
    ]
    shapes = [
        {"path": path, "status": "modified", "patch": gitlink_patch}
        for path in file_paths
    ]
    greptile_sha = HEAD_SHA if greptile_fresh else "deadbeef"
    with (
        patch.dict("os.environ", {"SELF_MERGE_ALLOWED_PATHS": allowed_paths}),
        patch.object(smc, "fetch_pr", return_value=pr_data),
        patch.object(smc, "get_gh_user", return_value="TimeToBuildBob"),
        patch.object(smc, "merge_permission", return_value=True),
        patch.object(smc, "_fetch_greptile_review_data", return_value=None),
        patch.object(
            smc,
            "fetch_greptile_status",
            return_value={
                "has_review": True,
                "unresolved": greptile_unresolved,
                "total": 1,
            },
        ),
        patch.object(smc, "greptile_summary_score", return_value=greptile_score),
        patch.object(
            smc, "greptile_summary_reviewed_commit", return_value=greptile_sha
        ),
        patch.object(
            smc,
            "fetch_unresolved_human_threads",
            return_value={"unresolved": 0, "authors": []},
        ),
        patch.object(
            smc,
            "fetch_ai_review_status",
            return_value={
                "accepted": False,
                "detail": "AI reviewer abstained (no score)",
            },
        ),
        patch.object(smc, "_fetch_pr_file_shapes", return_value=shapes),
        patch.object(smc, "fetch_gitmodules_text", return_value=gitmodules),
        patch.object(
            smc, "commit_has_merged_upstream_pr", return_value=merged_upstream
        ),
    ):
        return smc.evaluate_pr(
            "gptme/gptme-cloud", 901, workspace_repos=["gptme/gptme-cloud"]
        )


def test_evaluate_pr_waives_ai_review_for_901_shape() -> None:
    """Replay gptme-cloud#901: pointer-only gptme bump of a squash-merged PR."""
    result = _evaluate()
    assert result.eligible, result.reasons
    assert any("AI review waived" in w for w in result.warnings)
    assert not any("AI review required" in r for r in result.reasons)
    assert result.category == "repo-allowlisted(gptme/gptme-cloud)"


def test_evaluate_pr_blocks_branch_only_pin() -> None:
    """Replay gptme-cloud#898's 9da4505: no merged upstream PR → still ineligible."""
    branch_patch = (
        f"@@ -1 +1 @@\n-Subproject commit {OLD_SHA}\n"
        f"+Subproject commit {BRANCH_ONLY_SHA}\n"
    )
    result = _evaluate(merged_upstream=False, gitlink_patch=branch_patch)
    assert not result.eligible
    assert any("AI review required" in r for r in result.reasons)
    assert not any("AI review waived" in w for w in result.warnings)


def test_evaluate_pr_does_not_waive_when_greptile_is_dark() -> None:
    result = _evaluate(greptile_fresh=False)
    assert not result.eligible
    assert any("AI review required" in r for r in result.reasons)


def test_evaluate_pr_does_not_waive_unallowlisted_gitlink() -> None:
    result = _evaluate(allowed_paths="gptme/gptme-cloud:other")
    assert not result.eligible
    assert any("AI review required" in r for r in result.reasons)


def test_evaluate_pr_does_not_waive_mixed_diff() -> None:
    result = _evaluate(
        files=[{"path": "gptme"}, {"path": "README.md"}],
        allowed_paths="gptme/gptme-cloud:gptme,gptme/gptme-cloud:README.md",
    )
    assert not result.eligible
    assert any("AI review required" in r for r in result.reasons)


def test_evaluate_pr_does_not_waive_unparseable_greptile_score() -> None:
    """Waiver needs an explicit numeric 5/5, not Greptile-present-but-unscored."""
    result = _evaluate(greptile_score=None)
    assert not result.eligible
    assert any("AI review required" in r for r in result.reasons)


def test_commit_has_merged_upstream_pr_true_on_merged_payload() -> None:
    payload = json.dumps({"number": 3742, "merged_at": "2026-09-07T20:43:31Z"})

    def _gh(args: list[str], timeout: int = 30) -> str | None:
        assert "gptme/gptme/commits/110a4eec/pulls" in " ".join(args)
        return payload

    with patch.object(smc, "run_gh_checked", _gh):
        assert smc.commit_has_merged_upstream_pr("gptme/gptme", "110a4eec") is True


def test_commit_has_merged_upstream_pr_false_when_no_prs() -> None:
    with patch.object(smc, "run_gh_checked", lambda *a, **k: ""):
        assert smc.commit_has_merged_upstream_pr("gptme/gptme", "9da4505") is False


def test_commit_has_merged_upstream_pr_unknown_on_api_failure() -> None:
    with patch.object(smc, "run_gh_checked", lambda *a, **k: None):
        assert smc.commit_has_merged_upstream_pr("gptme/gptme", "110a4eec") is None
