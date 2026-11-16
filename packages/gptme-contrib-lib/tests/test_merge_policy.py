"""gptme_contrib_lib/merge_policy.toml evaluation (ErikBjare/bob#1480: tier 0 approved 2026-11-15)."""

from __future__ import annotations

from pathlib import Path

import pytest
from gptme_contrib_lib import merge_policy
from gptme_contrib_lib.merge_policy import FileChange


class Classifier:
    """Minimal stand-in for self-merge-check's predicates."""

    def is_sensitive_path(self, path: str) -> bool:
        return path.startswith("secrets/")

    def is_lockfile(self, path: str) -> bool:
        return path.endswith(("uv.lock", "package-lock.json"))

    def is_test_file(self, path: str) -> bool:
        return path.startswith("tests/")

    def is_doc_file(self, path: str) -> bool:
        return path.endswith(".md")

    def is_spec_like_doc(self, path: str) -> bool:
        return path == "README.md"


C = Classifier()


@pytest.fixture(scope="module")
def policy() -> dict:
    return merge_policy.load_policy()


def tier(changes: list[FileChange], policy: dict) -> merge_policy.TierVerdict:
    return merge_policy.assign_tier(changes, policy, C)


def test_shipped_policy_is_tier0_plus_catch_all_with_empty_rollout(policy) -> None:
    assert [t["id"] for t in policy["tier"]] == [0, 2]
    assert policy["tier"][0]["classes"] == ["docs", "tests", "ci"]
    assert policy["size_exempt_classes"] == ["tests", "lockfile"]
    # Rollout waits on Erik's merge-rights grant; he adds repos himself.
    assert merge_policy.rollout_repos(policy) == frozenset()


def test_docs_and_tests_are_tier0(policy) -> None:
    v = tier([FileChange("docs/a.md", 10, 2), FileChange("tests/t.py", 40)], policy)
    assert v.tier == 0
    assert v.counted_lines == 12 and v.counted_files == 1


def test_tests_do_not_count_toward_size(policy) -> None:
    big_tests = [FileChange(f"tests/t{i}.py", 500) for i in range(20)]
    assert tier(big_tests, policy).tier == 0


def test_counted_size_cap(policy) -> None:
    v = tier([FileChange("docs/a.md", 301)], policy)
    assert v.tier == 2
    assert "301 counted lines > 300" in v.misses[0]
    many = [FileChange(f"docs/{i}.md", 1) for i in range(11)]
    assert "11 counted files > 10" in tier(many, policy).misses[0]


def test_lockfile_is_size_exempt_but_not_tier0_class(policy) -> None:
    v = tier([FileChange("tests/t.py", 5), FileChange("uv.lock", 900)], policy)
    assert v.tier == 2
    assert "lockfile" in v.misses[0]


@pytest.mark.parametrize(
    "path",
    ["src/app.py", "README.md", "secrets/key.md", "Makefile"],
)
def test_code_spec_doc_and_sensitive_go_to_erik(policy, path: str) -> None:
    assert tier([FileChange(path, 1)], policy).tier == 2


def test_empty_pr_is_not_tier0(policy) -> None:
    assert tier([], policy).tier == 2


CLEAN_CI_PATCH = (
    "@@ -1,3 +1,3 @@\n-      - run: make test\n+      - run: make test -x\n"
)


def test_allowed_workflow_with_clean_patch_is_ci(policy) -> None:
    v = tier([FileChange(".github/workflows/test.yml", 1, 1, CLEAN_CI_PATCH)], policy)
    assert v.tier == 0
    assert v.classes == {".github/workflows/test.yml": "ci"}


@pytest.mark.parametrize(
    "token_line",
    [
        "+permissions:",
        "+  token: ${{ secrets.PAT }}",
        "+on: pull_request_target",
        "+    runs-on: self-hosted",
    ],
)
def test_ci_patch_touching_forbidden_tokens_is_ci_other(policy, token_line) -> None:
    patch = f"@@ -1 +1 @@\n{token_line}\n"
    v = tier([FileChange(".github/workflows/test.yml", 1, 0, patch)], policy)
    assert v.classes[".github/workflows/test.yml"] == "ci-other"
    assert v.tier == 2


def test_unchanged_context_lines_do_not_trip_forbidden_tokens(policy) -> None:
    patch = "@@ -1,3 +1,3 @@\n     runs-on: ubuntu-latest\n-      - run: a\n+      - run: b\n"
    assert (
        tier([FileChange(".github/workflows/lint.yml", 1, 1, patch)], policy).tier == 0
    )


@pytest.mark.parametrize(
    "change",
    [
        FileChange(".github/workflows/test.yml", 1, 1, None),  # patch missing
        FileChange(".github/workflows/release.yml", 1, 1, CLEAN_CI_PATCH),
        FileChange(".github/workflows/sync-prod-image-pin.yml", 1, 1, CLEAN_CI_PATCH),
        FileChange(".github/dependabot.yml", 1, 1, CLEAN_CI_PATCH),
        FileChange("pkg/.GitHub/workflows/test.yml", 1, 1, CLEAN_CI_PATCH),
    ],
)
def test_ci_fails_closed(policy, change: FileChange) -> None:
    assert tier([change], policy).tier == 2


def test_policy_without_catch_all_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "merge.toml"
    bad.write_text('[[tier]]\nid = 0\nclasses = ["docs"]\n')
    with pytest.raises(ValueError, match="catch-all"):
        merge_policy.load_policy(bad)
