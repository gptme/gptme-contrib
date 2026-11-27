"""Tests for the repo-agnostic code-size report."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner
from gptme_dashboard.cli import main
from gptme_dashboard.codesize import (
    _is_test_path,
    build_report,
    detect_profile,
    render_markdown,
)


def _git(repo: Path, *args: str, date: str | None = None) -> None:
    # Ignore the host's global and system config (commit signing, templates, hooks):
    # tests must not depend on it or run the workspace commit policy.
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    if date:
        env |= {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=", "-C", str(repo), *args],
        capture_output=True,
        check=False,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")


def _write(repo: Path, rel: str, lines: int) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"x{i} = {i}\n" for i in range(lines)), encoding="utf-8")
    return path


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "bob@superuserlabs.org")
    _git(repo, "config", "user.name", "Test")


def test_largest_files_come_first_and_tests_are_split(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/big.py", 3000)
    _write(repo, "src/small.py", 10)
    _write(repo, "tests/test_big.py", 400)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    report = build_report(repo, history_weeks=0)

    assert report.profile == "monolith"
    assert report.largest_files[0].path == "src/big.py"
    assert [f.path for f in report.largest_files] == ["src/big.py", "src/small.py"]
    assert report.total_source_lines == 3010
    assert report.total_test_lines == 400
    # the 3,000-line file is flagged at both thresholds, the 10-line one at neither
    assert [f.path for f in report.over_threshold["1500"]] == ["src/big.py"]
    assert [f.path for f in report.over_threshold["3000"]] == ["src/big.py"]


def test_threshold_is_inclusive(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/exact.py", 1500)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    report = build_report(repo, history_weeks=0)

    assert [f.path for f in report.over_threshold["1500"]] == ["src/exact.py"]


def test_monorepo_groups_by_package(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "packages/alpha").mkdir(parents=True)
    (repo / "packages/alpha/pyproject.toml").write_text("[project]\nname='alpha'\n")
    _write(repo, "packages/alpha/src/alpha/core.py", 100)
    _write(repo, "packages/alpha/tests/test_core.py", 50)
    _write(repo, "packages/beta/src/beta/cli.py", 20)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    report = build_report(repo, history_weeks=0)

    assert detect_profile(repo) == "monorepo"
    by_name = {g.name: g for g in report.groups}
    assert set(by_name) == {"alpha", "beta"}
    # pyproject.toml counts as a source file, so the package has 102 source lines
    assert by_name["alpha"].source_lines == 102
    assert by_name["alpha"].test_lines == 50
    assert by_name["alpha"].tests_ratio == round(50 / 152, 3)
    assert by_name["alpha"].path == "packages/alpha"
    assert by_name["alpha"].last_commit  # resolved from the package directory


def test_group_map_override_and_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/core/a.py", 10)
    _write(repo, "src/llm/b.py", 20)
    (repo / "codesize-groups.json").write_text(
        json.dumps({"src/core": "core", "src/llm": "llm"}), encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    report = build_report(repo, history_weeks=0)

    assert {g.name for g in report.groups} == {"core", "llm"}


def test_hotspots_weight_size_by_churn(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/churny.py", 1000)
    _write(repo, "src/stable.py", 1000)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    for i in range(3):
        _write(repo, "src/churny.py", 1000 + (i + 1) * 10)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", f"change {i}")

    report = build_report(repo, history_weeks=0)

    assert report.hotspots[0].path == "src/churny.py"
    assert report.hotspots[0].changes >= 3
    stable = next(f for f in report.largest_files if f.path == "src/stable.py")
    assert stable.changes == 1


def test_history_samples_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", 100)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    _write(repo, "src/a.py", 200)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "grow")

    report = build_report(repo, history_weeks=4)

    assert report.history, "expected at least one weekly sample"
    assert report.history[-1].source_lines == 200
    assert report.history[-1].largest_file == "src/a.py"


def test_non_git_repo_warns_instead_of_failing(tmp_path: Path) -> None:
    repo = tmp_path / "plain"
    repo.mkdir()
    _write(repo, "src/a.py", 5)

    report = build_report(repo)

    assert report.history == []
    assert any("not a git repository" in w for w in report.warnings)


def test_render_leads_with_largest_source_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/big.py", 1600)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    text = render_markdown(build_report(repo, history_weeks=0))

    assert text.index("## Largest source files") < text.index("## Over threshold")
    assert "`src/big.py`" in text
    assert "## By group" in text


def test_json_export_includes_totals(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", 7)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    data = json.loads(build_report(repo, history_weeks=0).to_json())

    assert data["totals"]["source_lines"] == 7
    assert data["groups"][0]["tests_ratio"] == 0.0


def _commit_all(repo: Path, message: str = "init", date: str | None = None) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message, date=date)


def _add_submodule(parent: Path, source: Path, path: str) -> None:
    _git(parent, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), path)
    # the submodule checkout is a repo of its own and needs an identity to commit in
    _git(parent / path, "config", "user.email", "bob@superuserlabs.org")
    _git(parent / path, "config", "user.name", "Test")


def _metarepo(tmp_path: Path) -> Path:
    """A superproject with two submodules, each holding one source file."""
    meta = tmp_path / "meta"
    _init_repo(meta)
    for name, size in (("alpha", 40), ("beta", 20)):
        source = tmp_path / f"{name}-src"
        _init_repo(source)
        _write(source, "src/mod.py", size)
        _commit_all(source)
        _add_submodule(meta, source, f"modules/{name}")
    _commit_all(meta)
    return meta


@pytest.mark.parametrize(
    "path",
    [
        "tests/helpers.py",
        "pkg/test/util.go",
        "web/src/__tests__/a.ts",
        "app/src/androidTest/Main.kt",
        "test_cli.py",
        "pkg/cli_test.go",
        "pkg/conftest.py",
        "web/button.test.tsx",
        "web/button.spec.js",
    ],
)
def test_test_paths_are_recognised(path: str) -> None:
    assert _is_test_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "src/latest_release.py",
        "src/contest_rules.py",
        "src/attest_signature.py",
        "latest/main.py",
        "contest/main.py",
        "docs/respec.md",
        "src/inspec.py",
        "api/openapi.spec.yaml",
    ],
)
def test_lookalike_source_paths_are_not_tests(path: str) -> None:
    assert not _is_test_path(path)


def test_history_weeks_zero_disables_the_trend(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", 10)
    _commit_all(repo)

    report = build_report(repo, history_weeks=0)

    assert report.history == []
    assert "## Trend" not in render_markdown(report)


def test_history_returns_at_most_the_requested_samples(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    now = datetime.now(timezone.utc)
    for size, days_ago in ((10, 13), (20, 7), (30, 0)):
        _write(repo, "src/a.py", size)
        _commit_all(repo, f"size {size}", date=(now - timedelta(days=days_ago)).isoformat())

    report = build_report(repo, history_weeks=2)

    assert [p.source_lines for p in report.history] == [20, 30]


def test_subdirectory_report_has_churn_and_history(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "packages/alpha/src/core.py", 10)
    _write(repo, "outside.py", 99)
    _commit_all(repo)
    _write(repo, "packages/alpha/src/core.py", 20)
    _commit_all(repo, "grow")

    report = build_report(repo / "packages/alpha", history_weeks=4)

    assert [(f.path, f.changes) for f in report.hotspots] == [("src/core.py", 2)]
    assert report.history[-1].source_lines == 20
    assert report.history[-1].largest_file == "src/core.py"


def test_only_tracked_files_are_counted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", 10)
    _commit_all(repo)
    _write(repo, "scratch/untracked.py", 5000)
    (repo / "link.py").symlink_to("src/a.py")
    _git(repo, "add", "link.py")
    (repo / "src/blob.py").write_bytes(b"\x00\n" * 100)
    _git(repo, "add", "src/blob.py")

    report = build_report(repo, history_weeks=0)

    assert [f.path for f in report.largest_files] == ["src/a.py"]


def test_non_ascii_paths_keep_their_churn(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/m\u00e4tning.py", 10)
    _commit_all(repo)

    report = build_report(repo, history_weeks=4)

    assert [(f.path, f.changes) for f in report.hotspots] == [("src/m\u00e4tning.py", 1)]
    assert report.history[-1].largest_file == "src/m\u00e4tning.py"


def test_brain_layout_wins_over_packages_and_submodules(tmp_path: Path) -> None:
    brain = _metarepo(tmp_path)
    for surface in ("tasks", "lessons", "journal"):
        _write(brain, f"{surface}/a.md", 3)
    _write(brain, "packages/tool/pyproject.toml", 2)
    _commit_all(brain, "brain layout")

    report = build_report(brain, history_weeks=4)

    assert report.profile == "brain"
    # vendored submodules are not the brain's own code, now or in the trend
    assert "modules/alpha/src/mod.py" not in [f.path for f in report.largest_files]
    assert report.history[-1].source_lines == report.total_source_lines


def test_metarepo_reports_submodule_churn_groups_and_history(tmp_path: Path) -> None:
    meta = _metarepo(tmp_path)
    alpha = meta / "modules/alpha"
    _write(alpha, "src/mod.py", 50)
    _commit_all(alpha, "grow")
    off_pin = build_report(meta, history_weeks=0).warnings
    assert [w for w in off_pin if "modules/alpha is not at its pinned commit" in w]
    _commit_all(meta, "bump alpha")

    report = build_report(meta, history_weeks=4)

    assert report.profile == "metarepo"
    by_name = {g.name: g for g in report.groups}
    assert {"modules/alpha", "modules/beta"} <= set(by_name)
    assert by_name["modules/alpha"].source_lines == 50
    assert by_name["modules/alpha"].last_commit
    hot = {f.path: f.changes for f in report.hotspots}
    assert hot["modules/alpha/src/mod.py"] == 2
    assert hot["modules/beta/src/mod.py"] == 1
    assert report.history[-1].source_lines == report.total_source_lines
    assert report.history[-1].largest_file == "modules/alpha/src/mod.py"
    assert report.warnings == []


def test_metarepo_warns_about_missing_submodule(tmp_path: Path) -> None:
    meta = _metarepo(tmp_path)
    _git(meta, "submodule", "deinit", "-q", "-f", "modules/beta")

    report = build_report(meta, history_weeks=4)

    assert any("modules/beta is not checked out" in w for w in report.warnings)
    assert any("trend omits submodule modules/beta" in w for w in report.warnings)
    assert "modules/beta" not in {g.name for g in report.groups}


def test_monorepo_file_directly_under_packages_is_not_a_package(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "packages/alpha/pyproject.toml", 2)
    _write(repo, "packages/README.md", 3)
    _commit_all(repo)

    report = build_report(repo, history_weeks=0)

    assert {g.name for g in report.groups} == {"alpha", "packages"}


def test_longest_group_prefix_wins_and_merged_groups_take_newest_commit(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "old/a.py", 1)
    _write(repo, "src/core/b.py", 2)
    _commit_all(repo, date="2020-01-01T00:00:00+00:00")
    _write(repo, "src/new.py", 3)
    _commit_all(repo, "new", date="2021-01-01T00:00:00+00:00")
    _write(repo, "top.py", 4)
    _commit_all(repo, "top", date="2022-01-01T00:00:00+00:00")

    report = build_report(
        repo,
        group_map={"src": "merged", "old": "merged", "src/core": "core"},
        thresholds=(2, 2),
        history_weeks=0,
    )

    by_name = {g.name: g for g in report.groups}
    assert by_name["core"].source_lines == 2
    assert by_name["merged"].source_lines == 4
    assert by_name["merged"].last_commit == "2021-01-01"
    assert by_name["(root)"].last_commit == "2022-01-01"
    assert report.thresholds == [2]


def test_submodule_paths_with_spaces(tmp_path: Path) -> None:
    meta = _metarepo(tmp_path)
    _add_submodule(meta, tmp_path / "alpha-src", "modules/my lib")
    _commit_all(meta, "spaced")
    spaced = meta / "modules/my lib"
    _write(spaced, "src/mod.py", 60)
    _commit_all(spaced, "grow")

    report = build_report(meta, history_weeks=0)

    assert "modules/my lib" in {g.name for g in report.groups}
    assert report.warnings == [
        "submodule modules/my lib is not at its pinned commit: current numbers and trend differ"
    ]
    assert {f.path: f.changes for f in report.hotspots}["modules/my lib/src/mod.py"] == 2


@pytest.mark.parametrize("content", ["[]", '{"src": null}', "{not json"])
def test_malformed_group_file_warns_or_rejects(tmp_path: Path, content: str) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", 5)
    (repo / "codesize-groups.json").write_text(content, encoding="utf-8")
    _commit_all(repo)

    # found in the repo: report anyway, and say it was ignored
    report = build_report(repo, history_weeks=0)
    assert any("ignoring codesize-groups.json" in w for w in report.warnings)
    assert "src" in {g.name for g in report.groups}

    # passed explicitly: a usage error, not a traceback
    result = CliRunner().invoke(
        main,
        ["codesize", "--repo", str(repo), "--groups", str(repo / "codesize-groups.json")],
    )
    assert result.exit_code == 2
    assert "Invalid value for '--groups'" in result.output


def test_package_and_top_level_directory_of_same_name_stay_apart(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "packages/docs/pyproject.toml", 2)
    _write(repo, "docs/guide.md", 7)
    _commit_all(repo)

    report = build_report(repo, history_weeks=0)

    assert {g.name: g.source_lines for g in report.groups} == {"packages/docs": 2, "docs": 7}


def test_pnpm_lockfile_is_not_source(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.ts", 5)
    _write(repo, "web/pnpm-lock.yaml", 9000)
    _commit_all(repo)

    report = build_report(repo, history_weeks=4)

    assert [f.path for f in report.largest_files] == ["src/a.ts"]
    assert report.history[-1].source_lines == 5


def test_user_grep_config_does_not_empty_or_reprefix_the_trend(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "sub/src/a.py", 10)
    _commit_all(repo)
    _git(repo, "config", "color.grep", "always")
    _git(repo, "config", "grep.fullName", "true")

    report = build_report(repo / "sub", history_weeks=4)

    assert report.history[-1].largest_file == "src/a.py"


def test_metarepo_counts_a_checked_out_but_inactive_submodule(tmp_path: Path) -> None:
    meta = _metarepo(tmp_path)
    _git(meta, "config", "--remove-section", "submodule.modules/beta")

    report = build_report(meta, history_weeks=4)

    assert {g.name for g in report.groups} >= {"modules/alpha", "modules/beta"}
    assert report.history[-1].source_lines == report.total_source_lines


@pytest.mark.parametrize("brain", [False, True])
def test_user_submodule_recurse_does_not_inflate_the_trend(tmp_path: Path, brain: bool) -> None:
    meta = _metarepo(tmp_path)
    if brain:
        for surface in ("tasks", "lessons", "journal"):
            _write(meta, f"{surface}/a.md", 3)
        _commit_all(meta, "brain layout")
    # a common global setting; git grep honours it
    _git(meta, "config", "submodule.recurse", "true")

    report = build_report(meta, history_weeks=4)

    assert report.history[-1].source_lines == report.total_source_lines


def _can_ssh_sign() -> bool:
    """SSH commit signing needs ssh-keygen and git >= 2.34."""
    out = subprocess.run(["git", "version"], capture_output=True, text=True, check=True).stdout
    version = re.search(r"(\d+)\.(\d+)", out)
    return bool(
        shutil.which("ssh-keygen") and version and (int(version[1]), int(version[2])) >= (2, 34)
    )


@pytest.mark.skipif(not _can_ssh_sign(), reason="host cannot make SSH-signed commits")
def test_user_show_signature_does_not_corrupt_commit_dates(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    key = tmp_path / "key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    _write(repo, "src/a.py", 10)
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "gpg.format=ssh",
        "-c",
        f"user.signingkey={key}",
        "commit",
        "-q",
        "-S",
        "-m",
        "init",
    )
    _git(repo, "config", "log.showSignature", "true")

    report = build_report(repo, history_weeks=0)

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", report.groups[0].last_commit)


def test_submodule_mounted_under_a_skipped_name_is_still_counted(tmp_path: Path) -> None:
    meta = _metarepo(tmp_path)
    _add_submodule(meta, tmp_path / "alpha-src", "build/toolchain")
    mounted = meta / "build/toolchain"
    # skipped directories inside the submodule stay skipped
    _write(mounted, "dist/bundle.js", 500)
    _commit_all(mounted, "vendored output")
    _commit_all(meta, "mount toolchain")

    report = build_report(meta, history_weeks=4)

    by_name = {g.name: g.source_lines for g in report.groups}
    assert by_name["build/toolchain"] == 40
    assert report.history[-1].source_lines == report.total_source_lines


def test_unmerged_files_are_counted_once(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", 10)
    _commit_all(repo)
    _git(repo, "checkout", "-q", "-b", "other")
    _write(repo, "src/a.py", 20)
    _commit_all(repo, "theirs")
    _git(repo, "checkout", "-q", "-")
    _write(repo, "src/a.py", 30)
    _commit_all(repo, "ours")
    with pytest.raises(AssertionError):
        _git(repo, "merge", "-q", "other")

    report = build_report(repo, history_weeks=0)

    assert [f.path for f in report.largest_files] == ["src/a.py"]
    assert [g.source_files for g in report.groups] == [1]


def test_json_file_entries_carry_the_hotspot_score(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", 2000)
    _commit_all(repo)

    data = json.loads(build_report(repo, history_weeks=0).to_json())

    # one commit in the window: 2000 * sqrt(2)
    for entries in (data["hotspots"], data["largest_files"], data["over_threshold"]["1500"]):
        assert [e["hotspot_score"] for e in entries] == [2828.4]


@pytest.mark.parametrize("manifest", ["package.json", "Cargo.toml", "go.mod"])
def test_non_python_workspaces_group_by_package(tmp_path: Path, manifest: str) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    for name in ("alpha", "beta"):
        _write(repo, f"packages/{name}/{manifest}", 1)
        _write(repo, f"packages/{name}/src/index.ts", 5)
    _commit_all(repo)

    report = build_report(repo, history_weeks=0)

    assert report.profile == "monorepo"
    assert {g.name for g in report.groups} == {"alpha", "beta"}


def test_code_only_filters_current_and_history(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/logic.py", 10)
    _write(tmp_path, "tests/test_logic.py", 20)
    _write(tmp_path, "tests/fixtures/config.yml", 100)
    _write(tmp_path, "docs/big.md", 3000)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "initial")

    report = build_report(tmp_path, code_only=True)
    assert [f.path for f in report.largest_files] == ["src/logic.py"]
    assert report.total_source_lines == report.history[-1].source_lines == 10
    assert report.total_test_lines == report.history[-1].test_lines == 20
    assert report.history[-1].group_source_lines == {"src": 10}
    assert report.history[-1].threshold_counts == {"1500": 0, "3000": 0}
    assert build_report(tmp_path).largest_files[0].path == "docs/big.md"


@pytest.mark.parametrize("brain", [False, True])
def test_dashboard_codesize_uses_matrix_and_distinct_agent_checkouts(
    tmp_path: Path, brain: bool
) -> None:
    repo = tmp_path / "workspace"
    _init_repo(repo)
    if brain:
        for surface in ("tasks", "lessons", "journal"):
            _write(repo, f"{surface}/entry.md", 3)
    for name, stage in [("core", "5 - Production/Stable"), ("old", "3 - Alpha")]:
        _write(repo, f"packages/{name}/src/logic.py", 1500 if name == "core" else 10)
        manifest = repo / f"packages/{name}/pyproject.toml"
        manifest.write_text(
            f'[project]\nname = "{name}"\nclassifiers = ["Development Status :: {stage}"]\n'
        )
    _git(repo, "add", "-A")
    old_date = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    _git(repo, "commit", "-qm", "scaffold", date=old_date)

    prior_week = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    _write(repo, "packages/core/src/logic.py", 1504)
    _git(repo, "add", "packages/core/src/logic.py")
    _git(repo, "commit", "-qm", "grow package", date=prior_week)
    _write(repo, "packages/core/src/logic.py", 1505)
    _git(repo, "add", "packages/core/src/logic.py")

    (repo / "pyproject.toml").write_text(
        '[project]\nname = "workspace"\ndependencies = ["Core>=0.1"]\n'
    )
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-qm", "workspace dependency")
    agents = [tmp_path / "agent1", tmp_path / "agent2"]
    for agent in agents:
        agent.mkdir()
        (agent / "pyproject.toml").write_text(
            '[project]\nname = "agent"\n[project.optional-dependencies]\nall = ["core[client]"]\n'
        )
    matrix = tmp_path / "matrix.json"
    matrix.write_text('["core"]')
    output = tmp_path / "site"

    result = CliRunner().invoke(
        main,
        [
            "generate",
            "--workspace",
            str(repo),
            "--output",
            str(output),
            "--codesize",
            "--ci-packages",
            str(matrix),
            "--dependent-repo",
            str(agents[0]),
            "--dependent-repo",
            str(agents[0]),
            "--dependent-repo",
            str(agents[1]),
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads((output / "data.json").read_text())["codesize"]
    assert report["agent_repos_scanned"] == 2
    assert "root" not in report
    assert report["profile"] == ("brain" if brain else "monorepo")
    if brain:
        standalone = build_report(repo, code_only=True, history_weeks=0)
        assert {group.name for group in standalone.groups} == {"packages"}
    rows = {row["name"]: row for row in report["packages"]}
    assert rows["core"]["flags"] == ["Upstream", "Split"]
    assert rows["core"]["path"] == "packages/core"
    assert rows["core"]["source_lines"] == 1505
    assert rows["old"]["source_lines"] == 10
    assert rows["core"]["internal_dependents"] == 1
    assert rows["core"]["source_delta"] == 1
    assert report["history"][-1]["threshold_counts"] == {"1500": 1, "3000": 0}
    assert rows["core"]["external_dependents"] == 2
    assert rows["core"]["ci_selected"] is True
    assert rows["old"]["flags"] == ["Remove"]
    assert rows["old"]["ci_selected"] is False
    page = (output / "index.html").read_text()
    assert '<section id="codesize">' in page
    assert "Largest source files" in page
    assert "Upstream, Split" in page

    public = CliRunner().invoke(
        main, ["generate", "--workspace", str(repo), "--codesize", "--json"]
    )
    assert public.exit_code == 0, public.output
    rows = {row["name"]: row for row in json.loads(public.output)["codesize"]["packages"]}
    assert rows["old"]["external_dependents"] is None
    assert rows["old"]["ci_selected"] is None
    assert rows["old"]["flags"] == []


def test_history_carries_the_last_commit_across_quiet_weeks(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path, "src/steady.py", 42)
    _git(tmp_path, "add", "-A")
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    _git(tmp_path, "commit", "-qm", "initial", date=old)

    report = build_report(tmp_path, history_weeks=4)
    assert len(report.history) == 4
    assert len({point.week for point in report.history}) == 4
    assert {point.source_lines for point in report.history} == {42}
    assert len({point.commit for point in report.history}) == 1


def test_git_paths_keep_whitespace_in_history_and_churn(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = " src/long\nname\r.py"
    _write(tmp_path, path, 17)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "initial")

    report = build_report(tmp_path)
    assert report.largest_files[0].path == path
    assert report.largest_files[0].changes == 1
    assert report.history[-1].largest_file == path
    assert report.history[-1].source_lines == report.total_source_lines == 17


def test_history_keeps_zero_samples_after_last_source_is_deleted(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    now = datetime.now(timezone.utc)
    _write(tmp_path, "src/a.py", 100)
    _commit_all(tmp_path, date=(now - timedelta(days=21)).isoformat())
    _git(tmp_path, "rm", "src/a.py")
    _git(
        tmp_path,
        "commit",
        "-qm",
        "delete last source",
        date=(now - timedelta(days=8)).isoformat(),
    )

    report = build_report(tmp_path, history_weeks=4)

    assert [point.source_lines for point in report.history] == [100, 100, 0, 0]
    assert report.total_source_lines == report.total_test_lines == 0
    for point in report.history[-2:]:
        assert point.test_lines == point.largest_file_lines == 0
        assert point.largest_file == ""
        assert point.threshold_counts == {"1500": 0, "3000": 0}
        assert point.group_source_lines == {}


def test_submodule_test_classification_uses_its_own_paths(tmp_path: Path) -> None:
    meta = _metarepo(tmp_path)
    _add_submodule(meta, tmp_path / "alpha-src", "tests/toolchain")
    mounted = meta / "tests/toolchain"
    _write(mounted, "tests/test_compiler.py", 7)
    # A nested submodule's own paths must also ignore all ancestor mount names.
    _add_submodule(mounted, tmp_path / "beta-src", "test/nested")
    _commit_all(mounted, "add tests and nested tool")
    _commit_all(meta, "mount toolchain")

    report = build_report(meta, thresholds=(20, 40), history_weeks=4)

    groups = {group.name: group for group in report.groups}
    assert groups["tests/toolchain"].source_lines == 40
    assert groups["tests/toolchain"].test_lines == 7
    assert groups["tests/toolchain/test/nested"].source_lines == 20
    assert groups["tests/toolchain/test/nested"].test_lines == 0
    for entries in (report.largest_files, report.hotspots, report.over_threshold["40"]):
        assert "tests/toolchain/src/mod.py" in {entry.path for entry in entries}
    assert report.history[-1].source_lines == report.total_source_lines == 120
    assert report.history[-1].test_lines == report.total_test_lines == 7
    assert report.history[-1].threshold_counts == {"20": 4, "40": 2}


def test_ratchet_baseline_threshold_matches_the_gate(tmp_path: Path, monkeypatch) -> None:
    """The report flags exactly the files the file-size-ratchet hook would fail."""
    import importlib.util

    hook_path = Path(__file__).parents[3] / "scripts/precommit/validators/file_size_ratchet.py"
    spec = importlib.util.spec_from_file_location("file_size_ratchet", hook_path)
    assert spec and spec.loader
    ratchet = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ratchet)

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/exact.py", 20)
    _write(repo, "src/under.py", 19)
    (repo / "file-size-baseline.json").write_text('{"threshold": 20, "files": {}}')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    report = build_report(repo, history_weeks=0)
    assert 20 in report.thresholds
    flagged = [f.path for f in report.over_threshold["20"]]
    assert flagged == ["src/exact.py"]

    monkeypatch.chdir(repo)
    assert ratchet.main(["src/exact.py"]) == 1
    assert ratchet.main(["src/under.py"]) == 0


def test_unreadable_ratchet_baseline_is_a_warning(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "src/a.py", 5)
    (repo / "file-size-baseline.json").write_text("not json")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    report = build_report(repo, history_weeks=0)
    assert report.thresholds == [1500, 3000]
    assert any("file-size-baseline.json" in w for w in report.warnings)
