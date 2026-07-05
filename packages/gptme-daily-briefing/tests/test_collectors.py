"""Tests for generic collectors (no network — only filesystem/git operations)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gptme_daily_briefing.collectors import (
    collect_recent_highlights,
    collect_waiting_tasks,
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_workspace(tmp_path: Path) -> Path:
    """A fresh git repo with a couple of commits on a `master` branch.

    `collect_recent_highlights` only logs from `origin/master`, `origin/main`,
    `master`, or `main` (it explicitly avoids bare HEAD). The fixture also
    sets `commit.gpgsign=false` and uses `--no-verify` so any global commit
    hooks don't interfere.
    """
    _git("init", "-q", "-b", "master", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    _git("config", "commit.gpgsign", "false", cwd=tmp_path)
    (tmp_path / "README.md").write_text("# Test\n")
    _git("add", "README.md", cwd=tmp_path)
    _git("commit", "-q", "--no-verify", "-m", "feat: first commit", cwd=tmp_path)
    (tmp_path / "README.md").write_text("# Test\n\n## Section\n")
    _git("commit", "-aq", "--no-verify", "-m", "docs: add section", cwd=tmp_path)
    (tmp_path / "README.md").write_text("# Test\n\n## Section\n\nBody\n")
    _git("commit", "-aq", "--no-verify", "-m", "fix: trailing body", cwd=tmp_path)
    return tmp_path


@pytest.fixture
def feature_branch_workspace(git_workspace: Path) -> Path:
    """Same as git_workspace but checked out on an unrelated feature branch.

    The fallback should still surface the *master* commits, NOT whatever the
    feature branch added — that's the whole point of the explicit branch list.
    """
    _git("checkout", "-q", "-b", "feature", cwd=git_workspace)
    (git_workspace / "feature.md").write_text("feature wip\n")
    _git("add", "feature.md", cwd=git_workspace)
    _git("commit", "-q", "--no-verify", "-m", "WIP do not surface this", cwd=git_workspace)
    return git_workspace


def test_collect_recent_highlights_returns_subjects_in_order(git_workspace: Path) -> None:
    # `origin/master` doesn't exist in the fixture repo; the fallback chain
    # finds local `master` and surfaces those commits in newest-first order.
    out = collect_recent_highlights(git_workspace, limit=3)
    assert out == ["fix: trailing body", "docs: add section", "feat: first commit"]


def test_collect_recent_highlights_uses_master_not_head_on_feature_branch(
    feature_branch_workspace: Path,
) -> None:
    """Even on a feature branch with extra commits, only master is surfaced."""
    out = collect_recent_highlights(feature_branch_workspace, limit=4)
    assert "WIP do not surface this" not in out
    assert out[0] == "fix: trailing body"


def test_collect_recent_highlights_respects_limit(git_workspace: Path) -> None:
    out = collect_recent_highlights(git_workspace, limit=1)
    assert out == ["fix: trailing body"]


def test_collect_recent_highlights_empty_repo(tmp_path: Path) -> None:
    # No git repo at all — should return [] without raising
    out = collect_recent_highlights(tmp_path, limit=5)
    assert out == []


def test_collect_blockers_url_encodes_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """A label containing a space must be percent-encoded so the gh URL is valid."""
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> str:
        captured.append(cmd)
        return "[]"

    from gptme_daily_briefing import collectors as col

    monkeypatch.setattr(col, "_run", fake_run)
    col.collect_blockers("owner/repo", "help wanted")
    assert captured, "no command captured"
    url = captured[0][-1]
    assert "labels=help%20wanted" in url, f"label not encoded: {url}"
    assert "labels=help wanted" not in url


def test_collect_blockers_url_encodes_special_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    """A label containing '&' must not leak into the query as a separator."""
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> str:
        captured.append(cmd)
        return "[]"

    from gptme_daily_briefing import collectors as col

    monkeypatch.setattr(col, "_run", fake_run)
    col.collect_blockers("owner/repo", "p1&urgent")
    url = captured[0][-1]
    assert "labels=p1%26urgent" in url, f"& not encoded: {url}"


def test_collect_waiting_tasks_parses_frontmatter(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    # Quote the value because '#' would otherwise start a YAML comment
    (tasks / "blocked-on-erik.md").write_text(
        "---\n"
        "state: waiting\n"
        'waiting_for: "Erik to merge PR #123"\n'
        "created: 2026-04-29T00:00:00+00:00\n"
        "---\n"
        "# A task\n"
    )
    (tasks / "active.md").write_text(
        "---\n" "state: active\n" "created: 2026-04-29T00:00:00+00:00\n" "---\n" "# Another task\n"
    )
    (tasks / "waiting-no-blocker.md").write_text(
        "---\nstate: waiting\ncreated: 2026-04-29T00:00:00+00:00\n---\n"
    )
    (tasks / "no-frontmatter.md").write_text("Just a body.\n")

    out = collect_waiting_tasks(tmp_path)

    assert out == [{"task": "blocked-on-erik", "waiting_for": "Erik to merge PR #123"}]


def test_collect_waiting_tasks_truncates_long_blocker(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    big = "x" * 500
    (tasks / "long.md").write_text(
        "---\n"
        "state: waiting\n"
        f"waiting_for: {big}\n"
        "created: 2026-04-29T00:00:00+00:00\n"
        "---\n"
    )
    out = collect_waiting_tasks(tmp_path)
    assert len(out) == 1
    assert len(out[0]["waiting_for"]) == 200


def test_collect_waiting_tasks_no_tasks_dir(tmp_path: Path) -> None:
    # Workspace exists but no tasks/ subdir
    assert collect_waiting_tasks(tmp_path) == []


def test_collect_waiting_tasks_respects_limit(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    for i in range(12):
        (tasks / f"w{i:02d}.md").write_text(
            "---\n"
            "state: waiting\n"
            f"waiting_for: blocker {i}\n"
            "created: 2026-04-29T00:00:00+00:00\n"
            "---\n"
        )
    out = collect_waiting_tasks(tmp_path, limit=4)
    assert len(out) == 4
    # Sorted alphabetically by stem
    assert [t["task"] for t in out] == ["w00", "w01", "w02", "w03"]


def _pr_payload(number: int, login: str, title: str = "x") -> dict:
    return {
        "number": number,
        "title": title,
        "draft": False,
        "user": {"login": login},
    }


def test_collect_open_prs_paginates_when_first_page_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full first page (100 items) must trigger a second-page fetch."""
    import json as _json

    from gptme_daily_briefing import collectors as col

    captured: list[str] = []
    page1 = [_pr_payload(i, "alice" if i % 10 == 0 else "other") for i in range(100)]
    page2 = [_pr_payload(1000, "alice", "from-page-2")]
    pages = {1: page1, 2: page2, 3: []}

    def fake_run(cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> str:
        url = cmd[-1]
        captured.append(url)
        page = int(url.split("page=")[-1])
        return _json.dumps(pages.get(page, []))

    monkeypatch.setattr(col, "_run", fake_run)

    out = col.collect_open_prs(["owner/repo"], "alice", limit_per_repo=20)

    # Walked at least pages 1 and 2 (3 may or may not be hit depending on impl,
    # but the contract is "stops at first empty page")
    assert any("page=1" in u for u in captured), captured
    assert any("page=2" in u for u in captured), captured
    # Page-2-only PR must be present (the original bug: silent miss)
    assert any(p["number"] == 1000 and p["title"] == "from-page-2" for p in out), out


def test_collect_open_prs_stops_on_short_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page returning <100 items signals the last page — don't fetch more."""
    import json as _json

    from gptme_daily_briefing import collectors as col

    captured: list[str] = []
    page1 = [_pr_payload(i, "alice") for i in range(5)]

    def fake_run(cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> str:
        url = cmd[-1]
        captured.append(url)
        page = int(url.split("page=")[-1])
        return _json.dumps(page1 if page == 1 else [])

    monkeypatch.setattr(col, "_run", fake_run)
    col.collect_open_prs(["owner/repo"], "alice")

    # Only one HTTP call: short first page is the last page
    assert len(captured) == 1, captured


def test_collect_open_prs_respects_max_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cap at max_pages even if every page returns 100 results."""
    import json as _json

    from gptme_daily_briefing import collectors as col

    captured: list[str] = []
    full_page = [_pr_payload(i, "alice") for i in range(100)]

    def fake_run(cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> str:
        captured.append(cmd[-1])
        return _json.dumps(full_page)

    monkeypatch.setattr(col, "_run", fake_run)
    col.collect_open_prs(["owner/repo"], "alice", max_pages=2)

    # Exactly 2 pages walked, no more
    assert len(captured) == 2, captured
