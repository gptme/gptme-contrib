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
    return merge_policy.load_policy(merge_policy.POLICY_PATH)


def tier(changes: list[FileChange], policy: dict) -> merge_policy.TierVerdict:
    return merge_policy.assign_tier(changes, policy, C)


def test_shipped_policy_is_tier0_aw_small_fix_and_catch_all(policy) -> None:
    assert [t["id"] for t in policy["tier"]] == [0, 1, 2]
    assert policy["tier"][0]["classes"] == ["docs", "tests"]
    assert policy["tier"][0]["self_merge"] is True
    assert policy["tier"][1]["name"] == "ActivityWatch small fix"
    assert policy["size_exempt_classes"] == ["tests", "lockfile"]
    assert "ActivityWatch/aw-webui" in merge_policy.rollout_repos(policy)
    assert "ActivityWatch/activitywatch" not in merge_policy.rollout_repos(policy)


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


def test_allowed_workflow_with_clean_patch_still_requires_human(policy) -> None:
    v = tier([FileChange(".github/workflows/test.yml", 1, 1, CLEAN_CI_PATCH)], policy)
    assert v.tier == 2
    assert v.classes == {".github/workflows/test.yml": "ci-other"}


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


def test_workflow_context_does_not_establish_full_yaml_safety(policy) -> None:
    patch = "@@ -1,3 +1,3 @@\n     runs-on: ubuntu-latest\n-      - run: a\n+      - run: b\n"
    assert (
        tier([FileChange(".github/workflows/lint.yml", 1, 1, patch)], policy).tier == 2
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


@pytest.mark.parametrize(
    "patch",
    [
        "@@ -1,3 +1,3 @@\n permissions:\n-  contents: read\n+  contents: write\n",
        '@@ -1 +1 @@\n+"permissions": write-all\n',
        CLEAN_CI_PATCH,
    ],
)
def test_workflows_need_human_review_without_full_yaml_proof(policy, patch) -> None:
    assert (
        tier([FileChange(".github/workflows/test.yml", 1, 1, patch)], policy).tier == 2
    )


@pytest.mark.parametrize("altered", ["merge_policy.py", "merge_policy.toml"])
def test_default_loader_rejects_candidate_policy_changes(monkeypatch, altered) -> None:
    import subprocess

    def git_show(argv, **kwargs):
        name = argv[2].rsplit("/", 1)[-1]
        source = merge_policy.POLICY_PATH.with_name(name)
        data = source.read_bytes() + (b"\n# changed" if name == altered else b"")
        return subprocess.CompletedProcess(argv, 0, data)

    monkeypatch.setattr(subprocess, "run", git_show)
    with pytest.raises(ValueError, match="differs from trusted"):
        merge_policy.load_policy()


def test_default_loader_rejects_missing_trusted_revision(monkeypatch) -> None:
    import subprocess

    def missing(*args, **kwargs):
        raise subprocess.CalledProcessError(128, args[0])

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(ValueError, match="trusted merge policy unavailable"):
        merge_policy.load_policy()


GITLINK_PATCH = (
    "@@ -1 +1 @@\n"
    "-Subproject commit 0123456789abcdef0123456789abcdef01234567\n"
    "+Subproject commit 89abcdef0123456789abcdef0123456789abcdef\n"
)


def test_gitlink_patch_is_gitlink_class(policy) -> None:
    v = tier([FileChange("aw-server", 1, 1, GITLINK_PATCH)], policy)
    assert v.classes == {"aw-server": "gitlink"}
    assert v.tier == 2


def test_hold_glob_star_does_not_cross_slash() -> None:
    assert merge_policy.hold_glob_match("scripts/package.sh", "scripts/*")
    assert not merge_policy.hold_glob_match("scripts/pkg/foo.sh", "scripts/*")
    assert merge_policy.hold_glob_match("scripts/pkg/foo.sh", "scripts/**")
    assert merge_policy.hold_glob_match("src/datastore/x.py", "**/*datastore*/**")
    assert merge_policy.hold_glob_match("Cargo.toml", "**/Cargo.toml")
    assert merge_policy.hold_glob_match("foo/Cargo.toml", "**/Cargo.toml")
    assert merge_policy.hold_glob_match("schema.json", "**/schema*")
    assert merge_policy.hold_glob_match("aw_core/schemas/event.json", "**/schema*/**")
    assert not merge_policy.hold_glob_match("src/app.py", "scripts/**")


def test_conventional_title_type_rejects_feat_and_breaking() -> None:
    assert merge_policy.conventional_title_type("fix: a") == "fix"
    assert merge_policy.conventional_title_type("fix(webui): a") == "fix"
    assert merge_policy.conventional_title_type("docs: a") == "docs"
    assert merge_policy.conventional_title_type("feat: a") == "feat"
    assert merge_policy.conventional_title_type("fix!: a") is None
    assert merge_policy.conventional_title_type("fix(api)!: a") is None
    assert merge_policy.conventional_title_type("") is None


def test_aw_small_fix_120_line_src_is_tier1(policy) -> None:
    v = merge_policy.assign_tier(
        [FileChange("src/app.py", 120)],
        policy,
        C,
        repo="ActivityWatch/aw-webui",
        title="fix: stop double-save on blur",
    )
    assert v.tier == 1
    assert merge_policy.tier_self_merges(policy, v.tier)


@pytest.mark.parametrize(
    "title",
    ["feat: new inspector", "fix!: breaking save path"],
)
def test_aw_small_fix_rejects_feat_and_breaking(policy, title: str) -> None:
    v = merge_policy.assign_tier(
        [FileChange("src/app.py", 10)],
        policy,
        C,
        repo="ActivityWatch/aw-webui",
        title=title,
    )
    assert v.tier == 2
    assert "title type" in v.misses[1]


def test_aw_small_fix_counts_tests_toward_size(policy) -> None:
    v = merge_policy.assign_tier(
        [FileChange("src/app.py", 1), FileChange("tests/t.py", 300)],
        policy,
        C,
        repo="ActivityWatch/aw-webui",
        title="fix: a",
    )
    assert v.tier == 2
    assert "301 counted lines > 300" in v.misses[1]


def test_aw_small_fix_holds_scripts_lockfile_migration_gitlink(policy) -> None:
    repo = "ActivityWatch/aw-webui"
    title = "fix: a"
    scripts = merge_policy.assign_tier(
        [FileChange("scripts/package/build.sh", 10)],
        policy,
        C,
        repo=repo,
        title=title,
    )
    assert scripts.tier == 2
    assert "hold glob:" in scripts.misses[1]
    lock = merge_policy.assign_tier(
        [FileChange("uv.lock", 20)],
        policy,
        C,
        repo=repo,
        title=title,
    )
    assert lock.tier == 2
    assert "lockfile" in lock.misses[1]
    migration = merge_policy.assign_tier(
        [FileChange("aw-server/migrations/001.sql", 8)],
        policy,
        C,
        repo=repo,
        title=title,
    )
    assert migration.tier == 2
    schema = merge_policy.assign_tier(
        [FileChange("aw_core/schemas/event.json", 8)],
        policy,
        C,
        repo=repo,
        title=title,
    )
    assert schema.tier == 2
    assert "hold glob:" in schema.misses[1]
    gitlink = merge_policy.assign_tier(
        [FileChange("aw-server", 1, 1, GITLINK_PATCH)],
        policy,
        C,
        repo=repo,
        title=title,
    )
    assert gitlink.tier == 2
    assert gitlink.classes["aw-server"] == "gitlink"


def test_aw_small_fix_ignores_repo_outside_tier_list(policy) -> None:
    v = merge_policy.assign_tier(
        [FileChange("src/app.py", 10)],
        policy,
        C,
        repo="ActivityWatch/activitywatch",
        title="fix: a",
    )
    assert v.tier == 2
    assert v.misses[1] == "repo not in tier"
