"""Tests for the repo-agnostic code-size report."""

from __future__ import annotations

import json
import os
import re
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
