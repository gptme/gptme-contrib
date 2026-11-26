"""A rename is judged on both paths, and build-input ``.txt`` files are not docs.

ErikBjare/bob#1571, salvaged from gptme/gptme-contrib#2537.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        pytest.skip(f"Could not load {path}", allow_module_level=True)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


smc = _load("self_merge_check_rename", ROOT / "scripts/github/self-merge-check.py")
mp = smc.merge_policy

REPO = "TimeToBuildBob/example"
WORKFLOW = ".github/workflows/x.yml"
SENSITIVE = "Touches sensitive/security/infra paths"


def _evaluate(repo: str, files: list[dict]):
    """Run evaluate_pr offline; every gate but the path ones is left unsatisfied."""
    pr = {
        "number": 1,
        "title": "refactor: move a file",
        "author": {"login": "me"},
        "state": "OPEN",
        "labels": [],
        "files": files,
    }
    marker = {"accepted": True, "detail": "stub", "unresolved": 0}
    with (
        patch.object(smc, "fetch_pr", return_value=pr),
        patch.object(smc, "get_gh_user", return_value="me"),
        patch.object(smc, "merge_permission", return_value=True),
        patch.object(smc, "repo_gates_prs_with_checks", return_value=False),
        patch.object(smc, "_fetch_greptile_review_data", return_value=([], [])),
        patch.object(smc, "_fetch_ai_review_marker_checked", return_value=None),
        patch.object(smc, "fetch_greptile_status", return_value={}),
        patch.object(smc, "fetch_ai_review_status", return_value=marker),
        patch.object(smc, "_fetch_pr_file_shapes", return_value=[]),
        patch.object(smc, "_fetch_contrib_pr_file_shapes", return_value=[]),
        patch.object(smc, "fetch_pr_file_changes", return_value=None),
        patch.object(
            smc, "operator_approval_category_waiver", lambda r, **_: (r, None)
        ),
    ):
        return smc.evaluate_pr(repo, 1, workspace_repos=[repo])


def test_fetch_pr_files_keeps_the_rename_source() -> None:
    row = {"path": "scripts/x.yml", "previous_path": WORKFLOW}
    done = subprocess.CompletedProcess([], 0, stdout=json.dumps(row))
    with patch.object(smc.subprocess, "run", return_value=done) as run:
        assert smc._fetch_pr_files(REPO, 1) == [row]
    assert "previous_path: .previous_filename" in run.call_args.args[0][-1]


@pytest.mark.parametrize(
    "repo", [REPO, "gptme/gptme", "gptme/gptme-contrib"], ids=["generic", "core", "c"]
)
def test_rename_out_of_a_sensitive_path_is_held(repo: str) -> None:
    result = _evaluate(repo, [{"path": "scripts/x.yml", "previous_path": WORKFLOW}])
    assert SENSITIVE in result.reasons
    assert result.files == ["scripts/x.yml"]
    assert result.previous_files == [WORKFLOW]


def test_rename_into_a_sensitive_path_is_still_held() -> None:
    result = _evaluate(REPO, [{"path": WORKFLOW, "previous_path": "scripts/x.yml"}])
    assert SENSITIVE in result.reasons


def test_rename_from_an_unlisted_path_has_no_category() -> None:
    result = _evaluate(REPO, [{"path": "scripts/x.py", "previous_path": "src/x.py"}])
    assert result.category is None
    assert any("src/x.py" in reason for reason in result.reasons)


def test_rename_between_allowed_paths_stays_eligible_by_category() -> None:
    result = _evaluate(
        REPO, [{"path": "scripts/b.py", "previous_path": "scripts/a.py"}]
    )
    assert result.category == "internal-tooling"


def test_moving_merge_policy_away_needs_a_human() -> None:
    source = "packages/gptme-contrib-lib/src/gptme_contrib_lib/merge_policy.toml"
    result = _evaluate(REPO, [{"path": "scripts/policy.toml", "previous_path": source}])
    assert "Merge policy authority changes require human merge" in result.reasons


def test_core_rename_out_of_a_sensitive_surface_routes_to_erik() -> None:
    shapes = [
        {
            "path": "gptme/util/x.py",
            "previous_path": "gptme/server/x.py",
            "status": "renamed",
        }
    ]
    reasons = smc.gptme_core_route_to_erik_reasons("fix: move", "", shapes)
    assert any("gptme/server/x.py" in reason for reason in reasons)


def test_contrib_moved_away_spec_doc_is_not_waived() -> None:
    category, reasons = smc.contrib_classify_category(
        ["packages/gptme-rag/README.md"],
        repo="gptme/gptme-contrib",
        previous_paths=["README.md"],
    )
    assert category is None
    assert reasons == ["Touches spec-like documentation requiring human review"]


def test_tier_waiver_classifies_the_rename_source() -> None:
    policy = {**mp.load_policy(mp.POLICY_PATH), "rollout": {"repos": [REPO]}}
    changes = [mp.FileChange("docs/x.md", 1, 0)]
    with (
        patch.object(mp, "load_policy", return_value=policy),
        patch.object(smc, "fetch_pr_file_changes", return_value=changes),
    ):
        remaining, warning, verdict = smc.merge_policy_tier_waiver(
            ["held"],
            repo=REPO,
            number=1,
            files=["docs/x.md"],
            previous_paths=[WORKFLOW],
        )
    assert (remaining, warning) == (["held"], None)
    assert verdict and verdict["tier"] != 0


@pytest.mark.parametrize(
    "path",
    [
        "requirements.txt",
        "requirements-dev.txt",
        "dev-requirements.txt",
        "constraints.txt",
        "requirements/base.txt",
        "requirements/py311/base.txt",
        "constraints/base.txt",
        "CMakeLists.txt",
        "native/CMakeLists.txt",
        "tests/requirements.txt",
    ],
)
def test_build_input_txt_is_not_docs(path: str) -> None:
    assert not smc.is_doc_file(path)
    category, reasons = smc.classify_category([path], REPO)
    assert category is None
    assert reasons


@pytest.mark.parametrize(
    "path",
    ["notes.txt", "docs/requirements.md", "docs/requirements-overview.rst", "a.md"],
)
def test_prose_stays_docs(path: str) -> None:
    assert smc.classify_category([path], REPO) == ("docs-only", [])
