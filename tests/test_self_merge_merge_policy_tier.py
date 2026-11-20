"""The self-merge gate and the scanner CLI read the same gptme_contrib_lib/merge_policy.toml.

ErikBjare/bob#1480: tier 0 waives path/category policy only in rollout repos,
which stay empty until Erik grants merge rights.
"""

from __future__ import annotations

import importlib.util
import io
import json
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


smc = _load(
    "self_merge_check_merge_policy", ROOT / "scripts/github/self-merge-check.py"
)
cli = _load("merge_policy_cli", ROOT / "scripts/github/merge-policy.py")
mp = smc.merge_policy

REPO = "ActivityWatch/aw-webui"
CATEGORY = "Files outside allowed categories: docs/a.md"


@pytest.fixture(autouse=True)
def trusted_deployment():
    # Simulate deployment after the human merges the new policy to master.
    import subprocess

    def git_show(argv, **kwargs):
        rel = argv[2].split(":", 1)[1]
        return subprocess.CompletedProcess(argv, 0, (ROOT / rel).read_bytes())

    with patch.object(mp.subprocess, "run", side_effect=git_show):
        yield


def _policy_with_rollout(*repos: str) -> dict:
    policy = mp.load_policy(mp.POLICY_PATH)
    return {**policy, "rollout": {"repos": list(repos)}}


def test_outside_rollout_nothing_is_fetched_or_waived() -> None:
    with patch.object(smc, "fetch_pr_file_changes") as fetch:
        remaining, warning, verdict = smc.merge_policy_tier_waiver(
            [CATEGORY], repo=REPO, number=1, files=["docs/a.md"]
        )
    assert (remaining, warning, verdict) == ([CATEGORY], None, None)
    fetch.assert_not_called()


def test_tier0_in_rollout_waives_category_reasons() -> None:
    changes = [mp.FileChange("docs/a.md", 10, 0), mp.FileChange("tests/test_a.py", 50)]
    with (
        patch.object(mp, "load_policy", return_value=_policy_with_rollout(REPO)),
        patch.object(smc, "fetch_pr_file_changes", return_value=changes),
    ):
        remaining, warning, verdict = smc.merge_policy_tier_waiver(
            [CATEGORY], repo=REPO, number=1, files=["docs/a.md", "tests/test_a.py"]
        )
    assert remaining == []
    assert warning and "merge-policy tier 0" in warning
    assert verdict and verdict["tier"] == 0


def test_non_tier0_in_rollout_keeps_reasons_and_reports_tier() -> None:
    changes = [mp.FileChange("src/app.py", 3, 0)]
    with (
        patch.object(mp, "load_policy", return_value=_policy_with_rollout(REPO)),
        patch.object(smc, "fetch_pr_file_changes", return_value=changes),
    ):
        remaining, warning, verdict = smc.merge_policy_tier_waiver(
            [CATEGORY], repo=REPO, number=1, files=["src/app.py"]
        )
    assert remaining == [CATEGORY] and warning is None
    assert verdict and verdict["tier"] == 2


@pytest.mark.parametrize("fetched", [None, [smc.merge_policy.FileChange("docs/a.md")]])
def test_fetch_failure_or_file_mismatch_fails_closed(fetched) -> None:
    with (
        patch.object(mp, "load_policy", return_value=_policy_with_rollout(REPO)),
        patch.object(smc, "fetch_pr_file_changes", return_value=fetched),
    ):
        remaining, warning, _ = smc.merge_policy_tier_waiver(
            [CATEGORY], repo=REPO, number=1, files=["docs/a.md", "src/app.py"]
        )
    assert remaining == [CATEGORY] and warning is None


@pytest.mark.parametrize(
    "error",
    [OSError("gone"), ValueError("merge policy differs from trusted origin/master")],
)
def test_unreadable_policy_fails_closed_visibly(error) -> None:
    # A lagging gate checkout must not ignore a new rollout repo silently.
    with patch.object(mp, "load_policy", side_effect=error):
        remaining, warning, verdict = smc.merge_policy_tier_waiver(
            [CATEGORY], repo=REPO, number=1, files=["docs/a.md"]
        )
    assert (remaining, verdict) == ([CATEGORY], None)
    assert warning is not None
    assert warning.startswith("Merge-policy tier not evaluated:")
    assert str(error) in warning


def test_gate_classifier_uses_gate_predicates() -> None:
    policy = mp.load_policy(mp.POLICY_PATH)
    tier = lambda path: mp.assign_tier(  # noqa: E731
        [mp.FileChange(path, 1)], policy, smc.path_classifier()
    ).tier
    assert tier("docs/guide.md") == 0
    assert tier("tests/test_x.py") == 0
    assert tier("README.md") == 2  # spec-like doc
    assert tier("Makefile") == 2


def test_scanner_cli_stdin_json(capsys) -> None:
    payload = [
        {
            "repo": REPO,
            "number": 7,
            "changedFiles": 1,
            "files": [{"path": "docs/a.md", "additions": 3}],
        },
        {
            "repo": REPO,
            "number": 8,
            "changedFiles": 1,
            "files": [{"path": "src/a.ts", "additions": 3}],
        },
    ]
    with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
        assert cli.main(["tier", "--stdin", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["number"], r["tier"], r["rollout"]) for r in rows] == [
        (7, 0, False),
        (8, 2, False),
    ]


def test_scanner_cli_rejects_bad_ref(capsys) -> None:
    assert cli.main(["tier", "not-a-ref"]) == 2


@pytest.mark.parametrize("name", ["merge_policy.py", "merge_policy.toml"])
def test_policy_authority_cannot_use_internal_tooling_route(name) -> None:
    path = f"packages/gptme-contrib-lib/src/gptme_contrib_lib/{name}"
    category, reasons = smc.contrib_classify_category(
        [path], repo="gptme/gptme-contrib"
    )
    assert category is None
    assert "Touches sensitive/security/infra paths" in reasons
    remaining, warning = smc.operator_approval_category_waiver(
        reasons, repo="gptme/gptme-contrib", number=1, head_sha="abc"
    )
    assert remaining == reasons and warning is None


@pytest.mark.parametrize("total", [None, 101, True])
def test_scanner_rejects_unproven_or_partial_file_inventory(capsys, total) -> None:
    item = {
        "repo": REPO,
        "number": 7,
        "files": [{"path": f"tests/test_{i}.py"} for i in range(100)],
    }
    if total is not None:
        item["changedFiles"] = total
    with patch.object(sys, "stdin", io.StringIO(json.dumps([item]))):
        assert cli.main(["tier", "--stdin", "--json"]) == 2
    row = json.loads(capsys.readouterr().out)[0]
    assert "error" in row and "tier" not in row


@pytest.mark.parametrize("total", [101, 100])
def test_rest_file_fetch_requires_total_count(total) -> None:
    import subprocess

    files = [{"path": f"tests/test_{i}.py"} for i in range(100)]

    def run(argv, **kwargs):
        output = (
            str(total) if "changedFiles" in argv else "\n".join(map(json.dumps, files))
        )
        return subprocess.CompletedProcess(argv, 0, output)

    with patch.object(smc.subprocess, "run", side_effect=run):
        result = smc.fetch_pr_file_changes(REPO, 7)
    assert (result is not None) == (total == 100)


def test_scanner_rejects_duplicate_paths(capsys) -> None:
    item = {
        "repo": REPO,
        "number": 7,
        "changedFiles": 2,
        "files": [{"path": "tests/test_a.py"}] * 2,
    }
    with patch.object(sys, "stdin", io.StringIO(json.dumps([item]))):
        assert cli.main(["tier", "--stdin", "--json"]) == 2
    assert "tier" not in json.loads(capsys.readouterr().out)[0]


@pytest.mark.parametrize("name", ["merge_policy.py", "merge_policy.toml"])
def test_policy_human_merge_stop_survives_all_category_waivers(
    monkeypatch, name
) -> None:
    path = f"packages/gptme-contrib-lib/src/gptme_contrib_lib/{name}"
    monkeypatch.setattr(
        smc,
        "fetch_pr",
        lambda *a: {
            "author": {"login": "TimeToBuildBob"},
            "files": [{"path": path}],
            "headRefOid": "abc",
            "state": "OPEN",
            "labels": [],
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        },
    )
    monkeypatch.setattr(smc, "get_gh_user", lambda: "TimeToBuildBob")
    monkeypatch.setattr(smc, "merge_permission", lambda *a: True)
    monkeypatch.setattr(
        smc,
        "fetch_greptile_status",
        lambda *a, **k: {
            "has_review": False,
            "unresolved": 0,
        },
    )
    monkeypatch.setattr(smc, "_fetch_greptile_review_data", lambda *a: ([], []))
    monkeypatch.setattr(
        smc,
        "fetch_unresolved_human_threads",
        lambda *a, **k: {
            "unresolved": 0,
            "authors": [],
        },
    )
    monkeypatch.setenv("SELF_MERGE_ACCEPT_AI_REVIEW", "0")
    monkeypatch.setattr(
        smc, "_fetch_ai_review_marker_checked", lambda *a, **k: (None, True)
    )
    monkeypatch.setattr(
        smc,
        "fetch_ai_review_status",
        lambda *a, **k: {
            "accepted": False,
            "detail": "no review",
        },
    )
    monkeypatch.setattr(
        smc, "merge_policy_tier_waiver", lambda *a, **k: ([], "tier waiver", {})
    )
    monkeypatch.setattr(
        smc, "operator_approval_category_waiver", lambda *a, **k: ([], "approved")
    )
    result = smc.evaluate_pr(
        "gptme/gptme-contrib", 1, workspace_repos=["gptme/gptme-contrib"]
    )
    assert not result.eligible
    assert "Merge policy authority changes require human merge" in result.reasons
