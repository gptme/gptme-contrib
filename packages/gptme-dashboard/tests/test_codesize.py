"""Tests for the repo-agnostic code-size report."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gptme_dashboard.codesize import (
    build_report,
    detect_profile,
    render_markdown,
)


def _git(repo: Path, *args: str) -> None:
    # -c core.hooksPath= disables the host's global git hooks: tests must not run
    # the workspace commit policy (direct-master guard, identity guard).
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=", "-C", str(repo), *args],
        capture_output=True,
        check=False,
        text=True,
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
