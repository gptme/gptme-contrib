"""ActivityWatch PRs self-merge only through the merge-policy small-fix tier."""

from __future__ import annotations

import importlib.util
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
    "self_merge_check_aw_small_fix", ROOT / "scripts/github/self-merge-check.py"
)
mp = smc.merge_policy

REPO = "ActivityWatch/aw-webui"
REASON = "ActivityWatch PRs self-merge only through the merge-policy tier"


@pytest.fixture(autouse=True)
def trusted_deployment():
    import subprocess

    def git_show(argv, **kwargs):
        rel = argv[2].split(":", 1)[1]
        return subprocess.CompletedProcess(argv, 0, (ROOT / rel).read_bytes())

    with patch.object(mp.subprocess, "run", side_effect=git_show):
        yield


def test_activitywatch_bundle_is_not_a_self_merge_repo() -> None:
    assert smc.activitywatch_self_merge_repo("ActivityWatch/aw-webui")
    assert not smc.activitywatch_self_merge_repo("ActivityWatch/activitywatch")
    assert not smc.activitywatch_self_merge_repo("gptme/gptme")


def test_internal_tooling_on_aw_is_converted_then_held() -> None:
    changes = [mp.FileChange("scripts/package/build.sh", 12)]
    with patch.object(smc, "fetch_pr_file_changes", return_value=changes):
        remaining, warning, verdict = smc.merge_policy_tier_waiver(
            [REASON],
            repo=REPO,
            number=1,
            files=["scripts/package/build.sh"],
            title="fix: packaging script",
        )
    assert remaining == [REASON] and warning is None
    assert verdict and verdict["tier"] == 2


def test_aw_docs_only_is_waived_as_tier0() -> None:
    changes = [mp.FileChange("docs/a.md", 10)]
    with patch.object(smc, "fetch_pr_file_changes", return_value=changes):
        remaining, warning, verdict = smc.merge_policy_tier_waiver(
            [REASON],
            repo=REPO,
            number=1,
            files=["docs/a.md"],
            title="docs: clarify install",
        )
    assert remaining == []
    assert warning and "merge-policy tier 0" in warning
    assert verdict and verdict["tier"] == 0


def test_aw_tier1_does_not_waive_bot_ci_hard_stop() -> None:
    # Tier 1 admits class ``code``, and ``.pre-commit-config.yaml`` classifies as
    # code. A tier admits paths; it must not admit a CI/bot-policy change.
    changes = [
        mp.FileChange("src/app.py", 5),
        mp.FileChange(".pre-commit-config.yaml", 5),
    ]
    files = ["src/app.py", ".pre-commit-config.yaml"]
    hard_stop = "Bot/CI config changes require human review under current policy"
    with patch.object(smc, "fetch_pr_file_changes", return_value=changes):
        remaining, warning, verdict = smc.merge_policy_tier_waiver(
            [hard_stop],
            repo=REPO,
            number=1,
            files=files,
            title="fix: x",
        )
    assert remaining == [hard_stop]
    assert warning is None
    assert verdict and verdict["tier"] == 1


def test_aw_tier1_waives_path_reason_but_keeps_hard_stop() -> None:
    changes = [
        mp.FileChange("src/app.py", 5),
        mp.FileChange(".pre-commit-config.yaml", 5),
    ]
    files = ["src/app.py", ".pre-commit-config.yaml"]
    hard_stop = "Bot/CI config changes require human review under current policy"
    path_reason = "Files not in any allowed self-merge category: src/app.py"
    with patch.object(smc, "fetch_pr_file_changes", return_value=changes):
        remaining, warning, verdict = smc.merge_policy_tier_waiver(
            [path_reason, hard_stop],
            repo=REPO,
            number=1,
            files=files,
            title="fix: x",
        )
    assert remaining == [hard_stop]
    assert warning and "merge-policy tier 1" in warning


def test_aw_src_fix_is_waived_as_tier1() -> None:
    changes = [mp.FileChange("src/app.py", 120)]
    with patch.object(smc, "fetch_pr_file_changes", return_value=changes):
        remaining, warning, verdict = smc.merge_policy_tier_waiver(
            ["Files not in any allowed self-merge category: src/app.py"],
            repo=REPO,
            number=1,
            files=["src/app.py"],
            title="fix: stop double-save on blur",
        )
    assert remaining == []
    assert warning and "merge-policy tier 1" in warning
    assert verdict and verdict["tier"] == 1
