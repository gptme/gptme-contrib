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


def _policy_with_rollout(*repos: str) -> dict:
    policy = mp.load_policy()
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


def test_unreadable_policy_fails_closed() -> None:
    with patch.object(mp, "load_policy", side_effect=OSError("gone")):
        remaining, warning, verdict = smc.merge_policy_tier_waiver(
            [CATEGORY], repo=REPO, number=1, files=["docs/a.md"]
        )
    assert (remaining, warning, verdict) == ([CATEGORY], None, None)


def test_gate_classifier_uses_gate_predicates() -> None:
    policy = mp.load_policy()
    tier = lambda path: mp.assign_tier(  # noqa: E731
        [mp.FileChange(path, 1)], policy, smc.path_classifier()
    ).tier
    assert tier("docs/guide.md") == 0
    assert tier("tests/test_x.py") == 0
    assert tier("README.md") == 2  # spec-like doc
    assert tier("Makefile") == 2


def test_scanner_cli_stdin_json(capsys) -> None:
    payload = [
        {"repo": REPO, "number": 7, "files": [{"path": "docs/a.md", "additions": 3}]},
        {"repo": REPO, "number": 8, "files": [{"path": "src/a.ts", "additions": 3}]},
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
