"""Tests for `gptodo archive`: move old terminal tasks into tasks/archive/.

Covers selection (terminal states, age from `completed`/`expired_at`, legacy
last-change fallback, recur), what keeps a task in place (spawned_tasks,
parent, links from tasks that stay, name collisions), `--limit`, `--dry-run`,
`--json`, `git mv` vs plain rename, link rebasing inside moved files,
idempotence, and that the readers (check, ready, explain, show, list, status
and the pre-commit validators) keep working with archived tasks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from gptodo.cli import cli

PRECOMMIT_DIR = Path(__file__).resolve().parents[3] / "scripts" / "precommit"


def _ago(days: float) -> str:
    stamp = datetime.now(timezone.utc) - timedelta(days=days)
    return stamp.isoformat(timespec="seconds")


def _write(tasks_dir: Path, name: str, state: str, extra: str = "", body: str = "") -> Path:
    lines = [f"state: {state}", "created: 2026-01-01T00:00:00+00:00"]
    if extra.strip():
        lines.append(extra.strip())
    path = tasks_dir / f"{name}.md"
    path.write_text("---\n" + "\n".join(lines) + f"\n---\n# {name}\n{body}")
    return path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _commit_all(repo: Path) -> None:
    _git(repo, "add", "tasks")
    _git(repo, "commit", "--quiet", "-m", "tasks")


def _run(repo: Path, monkeypatch: pytest.MonkeyPatch, *args: str):
    monkeypatch.chdir(repo)
    return CliRunner().invoke(cli, list(args))


def _archive_json(repo: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> dict:
    result = _run(repo, monkeypatch, "archive", "--json", *args)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "tasks").mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture
def acceptance_repo(repo: Path) -> Path:
    """3 done tasks (2 finished over 7 days ago) and an active task that needs one."""
    tasks = repo / "tasks"
    _write(tasks, "old-a", "done", f"completed: '{_ago(30)}'")
    _write(tasks, "old-b", "done", f"completed: '{_ago(10)}'")
    _write(tasks, "recent", "done", f"completed: '{_ago(2)}'")
    _write(tasks, "consumer", "active", "requires: [old-a]")
    _commit_all(repo)
    return repo


# ---------- Acceptance: dry run, git mv, check, ready -------------------------


def test_dry_run_lists_exactly_the_two_old_done_tasks(
    acceptance_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _archive_json(acceptance_repo, monkeypatch, "--dry-run")

    assert data["dry_run"] is True
    assert sorted(r["id"] for r in data["archived"]) == ["old-a", "old-b"]
    assert data["older_than_days"] == 7
    assert all(r["age_source"] == "completed" for r in data["archived"])
    # Nothing moved.
    assert (acceptance_repo / "tasks" / "old-a.md").exists()
    assert not (acceptance_repo / "tasks" / "archive").exists()


def test_real_run_uses_git_mv_and_keeps_dependency_resolvable(
    acceptance_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _archive_json(acceptance_repo, monkeypatch)

    assert sorted(r["id"] for r in data["archived"]) == ["old-a", "old-b"]
    assert {r["method"] for r in data["archived"]} == {"git mv"}
    tasks = acceptance_repo / "tasks"
    assert (tasks / "archive" / "old-a.md").exists()
    assert not (tasks / "old-a.md").exists()
    assert (tasks / "recent.md").exists()

    # Staged as renames, nothing committed.
    status = _git(acceptance_repo, "status", "--porcelain")
    assert "R  tasks/old-a.md -> tasks/archive/old-a.md" in status
    assert "R  tasks/old-b.md -> tasks/archive/old-b.md" in status
    assert _git(acceptance_repo, "rev-list", "--count", "HEAD").strip() == "1"

    # check stays green, also when pre-commit passes the moved file.
    check = _run(acceptance_repo, monkeypatch, "check")
    assert check.exit_code == 0, check.output
    check_files = _run(
        acceptance_repo, monkeypatch, "check", "tasks/archive/old-a.md", "tasks/consumer.md"
    )
    assert check_files.exit_code == 0, check_files.output

    # ready still treats the archived dependency as met.
    ready = _run(acceptance_repo, monkeypatch, "ready", "--json")
    assert ready.exit_code == 0, ready.output
    ready_ids = [t["id"] for t in json.loads(ready.output)["ready_tasks"]]
    assert "consumer" in ready_ids

    # explain agrees with ready.
    explain = _run(acceptance_repo, monkeypatch, "explain", "consumer")
    assert "missing=" not in explain.output
    assert "VERDICT: READY" in explain.output


def test_archived_tasks_leave_default_listings_but_show_finds_them(
    acceptance_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _archive_json(acceptance_repo, monkeypatch)

    listed = _run(acceptance_repo, monkeypatch, "list", "--json")
    assert listed.exit_code == 0, listed.output
    listed_ids = {t["id"] for t in json.loads(listed.output)["tasks"]}
    assert listed_ids == {"recent", "consumer"}

    status = _run(acceptance_repo, monkeypatch, "status", "--json")
    assert status.exit_code == 0, status.output
    status_ids = {t["id"] for t in json.loads(status.output)["tasks"]}
    assert status_ids == {"recent", "consumer"}

    show = _run(acceptance_repo, monkeypatch, "show", "old-a")
    assert show.exit_code == 0, show.output
    assert "tasks/archive/old-a.md" in show.output


def test_second_run_is_a_no_op(acceptance_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _archive_json(acceptance_repo, monkeypatch)
    again = _archive_json(acceptance_repo, monkeypatch)
    assert again["archived"] == []
    assert again["errors"] == []


# ---------- Selection ----------------------------------------------------------


def test_only_terminal_states_are_archived(repo: Path, monkeypatch) -> None:
    tasks = repo / "tasks"
    for state in ("backlog", "todo", "active", "waiting", "someday", "ready_for_review"):
        _write(tasks, f"live-{state}", state, f"completed: '{_ago(100)}'")
    _write(tasks, "gone", "cancelled", f"completed: '{_ago(100)}'")
    _write(tasks, "reaped", "expired", f"expired_at: '{_ago(100)[:10]}'")

    data = _archive_json(repo, monkeypatch)
    assert sorted(r["id"] for r in data["archived"]) == ["gone", "reaped"]
    reaped = next(r for r in data["archived"] if r["id"] == "reaped")
    assert reaped["age_source"] == "expired_at"


def test_state_option_restricts_states(repo: Path, monkeypatch) -> None:
    tasks = repo / "tasks"
    _write(tasks, "d", "done", f"completed: '{_ago(30)}'")
    _write(tasks, "c", "cancelled", f"completed: '{_ago(30)}'")

    data = _archive_json(repo, monkeypatch, "--state", "cancelled", "--dry-run")
    assert [r["id"] for r in data["archived"]] == ["c"]
    assert data["states"] == ["cancelled"]


def test_older_than_window(repo: Path, monkeypatch) -> None:
    tasks = repo / "tasks"
    _write(tasks, "d20", "done", f"completed: '{_ago(20)}'")
    _write(tasks, "d40", "done", f"completed: '{_ago(40)}'")

    data = _archive_json(repo, monkeypatch, "--older-than", "30", "--dry-run")
    assert [r["id"] for r in data["archived"]] == ["d40"]
    # The reference archiver's spelling is accepted too.
    data = _archive_json(repo, monkeypatch, "--min-age-days", "10", "--dry-run")
    assert sorted(r["id"] for r in data["archived"]) == ["d20", "d40"]


def test_date_only_completed_counts_as_utc_midnight(repo: Path, monkeypatch) -> None:
    _write(repo / "tasks", "dated", "done", f"completed: {_ago(9)[:10]}")
    data = _archive_json(repo, monkeypatch, "--dry-run")
    assert [r["id"] for r in data["archived"]] == ["dated"]


def test_unparseable_completed_fails_closed(repo: Path, monkeypatch) -> None:
    _write(repo / "tasks", "weird", "done", "completed: 'last tuesday'")
    data = _archive_json(repo, monkeypatch, "--dry-run")
    assert data["archived"] == []
    assert data["skipped"] == [{"id": "weird", "reason": "unparseable completed"}]


def test_recurring_terminal_task_is_kept(repo: Path, monkeypatch) -> None:
    _write(repo / "tasks", "weekly", "done", f"completed: '{_ago(30)}'\nrecur: weekly")
    data = _archive_json(repo, monkeypatch, "--dry-run")
    assert data["archived"] == []
    assert data["skipped"][0]["id"] == "weekly"


def test_legacy_task_without_completed_uses_last_change(tmp_path: Path, monkeypatch) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    old = _write(tasks, "old-legacy", "done")
    _write(tasks, "fresh-legacy", "done")
    stamp = time.time() - 30 * 86400
    os.utime(old, (stamp, stamp))

    data = _archive_json(tmp_path, monkeypatch, "--dry-run")
    assert [r["id"] for r in data["archived"]] == ["old-legacy"]
    assert data["archived"][0]["age_source"] == "last-change"


def test_legacy_task_with_recent_commit_is_kept(repo: Path, monkeypatch) -> None:
    """mtime alone is not trusted: a recent git commit also counts as a change."""
    old = _write(repo / "tasks", "legacy", "done")
    _commit_all(repo)
    stamp = time.time() - 30 * 86400
    os.utime(old, (stamp, stamp))

    data = _archive_json(repo, monkeypatch, "--dry-run")
    assert data["archived"] == []


def test_limit_takes_oldest_first(repo: Path, monkeypatch) -> None:
    tasks = repo / "tasks"
    for days in (10, 50, 30):
        _write(tasks, f"d{days}", "done", f"completed: '{_ago(days)}'")

    data = _archive_json(repo, monkeypatch, "--limit", "2")
    assert [r["id"] for r in data["archived"]] == ["d50", "d30"]
    assert data["eligible"] == 3
    assert (tasks / "d10.md").exists()


# ---------- What keeps a task in place ------------------------------------------


def test_child_of_open_fan_in_parent_is_kept(repo: Path, monkeypatch) -> None:
    tasks = repo / "tasks"
    _write(tasks, "parent", "active", "spawned_tasks: [child]")
    _write(tasks, "child", "done", f"completed: '{_ago(30)}'\nspawned_from: parent")

    data = _archive_json(repo, monkeypatch)
    assert data["archived"] == []
    assert "spawned_tasks of parent" in data["skipped"][0]["reason"]


def test_umbrella_of_open_child_is_kept(repo: Path, monkeypatch) -> None:
    tasks = repo / "tasks"
    _write(tasks, "umbrella", "done", f"completed: '{_ago(30)}'")
    _write(tasks, "piece", "todo", "parent: umbrella")

    data = _archive_json(repo, monkeypatch)
    assert data["archived"] == []
    assert "parent of piece" in data["skipped"][0]["reason"]


def test_terminal_parent_does_not_pin_children(repo: Path, monkeypatch) -> None:
    tasks = repo / "tasks"
    _write(tasks, "parent", "done", f"completed: '{_ago(30)}'\nspawned_tasks: [child]")
    _write(tasks, "child", "done", f"completed: '{_ago(30)}'\nspawned_from: parent")

    data = _archive_json(repo, monkeypatch)
    assert sorted(r["id"] for r in data["archived"]) == ["child", "parent"]


def test_task_linked_from_a_remaining_task_is_kept_transitively(repo: Path, monkeypatch) -> None:
    tasks = repo / "tasks"
    _write(tasks, "hub", "todo", body="See [notes](old-notes.md).\n")
    # old-notes stays because hub links to it, so what old-notes links to stays too.
    _write(tasks, "old-notes", "done", f"completed: '{_ago(30)}'", body="[d](deeper.md)\n")
    _write(tasks, "deeper", "done", f"completed: '{_ago(30)}'")
    _write(tasks, "loner", "done", f"completed: '{_ago(30)}'")

    data = _archive_json(repo, monkeypatch)
    assert [r["id"] for r in data["archived"]] == ["loner"]
    reasons = {s["id"]: s["reason"] for s in data["skipped"]}
    assert "linked from hub" in reasons["old-notes"]
    assert "linked from old-notes" in reasons["deeper"]

    check = _run(repo, monkeypatch, "check")
    assert check.exit_code == 0, check.output


def test_limit_does_not_strand_a_link(repo: Path, monkeypatch) -> None:
    """A task cut by --limit stays behind, so what it links to must stay too."""
    tasks = repo / "tasks"
    _write(tasks, "target", "done", f"completed: '{_ago(50)}'")
    _write(tasks, "referrer", "done", f"completed: '{_ago(40)}'", body="[t](target.md)\n")
    _write(tasks, "other", "done", f"completed: '{_ago(45)}'")

    # target is oldest, but referrer would stay behind linking to it, so the
    # next oldest task fills the slot instead.
    data = _archive_json(repo, monkeypatch, "--limit", "1")
    assert [r["id"] for r in data["archived"]] == ["other"]
    assert any(s["id"] == "target" for s in data["skipped"])
    check = _run(repo, monkeypatch, "check")
    assert check.exit_code == 0, check.output


def test_name_collision_in_archive_is_refused(repo: Path, monkeypatch) -> None:
    tasks = repo / "tasks"
    (tasks / "archive" / "2025").mkdir(parents=True)
    existing = tasks / "archive" / "2025" / "dup.md"
    existing.write_text("---\nstate: done\n---\n# older dup\n")
    _write(tasks, "dup", "done", f"completed: '{_ago(30)}'")

    result = _run(repo, monkeypatch, "archive")
    assert result.exit_code == 0, result.output
    assert "dup" in result.output and "not overwritten" in result.output
    assert (tasks / "dup.md").exists()
    assert existing.read_text() == "---\nstate: done\n---\n# older dup\n"


# ---------- Moves and link rebasing ---------------------------------------------


def test_untracked_or_non_git_files_use_plain_rename(tmp_path: Path, monkeypatch) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    _write(tasks, "plain", "done", f"completed: '{_ago(30)}'")

    data = _archive_json(tmp_path, monkeypatch)
    assert data["archived"][0]["method"] == "rename"
    assert (tasks / "archive" / "plain.md").exists()


def test_links_inside_moved_file_are_rebased(repo: Path, monkeypatch) -> None:
    tasks = repo / "tasks"
    (repo / "knowledge").mkdir()
    (repo / "knowledge" / "note.md").write_text("# note\n")
    _write(tasks, "live", "todo")
    _write(tasks, "sibling", "done", f"completed: '{_ago(30)}'")
    body = (
        "[k](../knowledge/note.md#part) [l](live.md) [s](sibling.md)\n"
        "[web](https://example.com) [gone](missing.md)\n"
        "```\n[example](live.md)\n```\n"
        "inline `[x](live.md)` stays\n"
    )
    _write(tasks, "mover", "done", f"completed: '{_ago(30)}'", body=body)
    _commit_all(repo)

    data = _archive_json(repo, monkeypatch)
    assert sorted(r["id"] for r in data["archived"]) == ["mover", "sibling"]
    text = (tasks / "archive" / "mover.md").read_text()
    assert "[k](../../knowledge/note.md#part)" in text
    assert "[l](../live.md)" in text
    assert "[s](sibling.md)" in text
    assert "[web](https://example.com)" in text
    assert "[gone](missing.md)" in text
    assert "```\n[example](live.md)\n```" in text
    assert "`[x](live.md)`" in text
    mover = next(r for r in data["archived"] if r["id"] == "mover")
    assert mover["links_rebased"] is True
    # The rewrite is staged with the rename.
    assert _git(repo, "diff", "--name-only").strip() == ""

    if PRECOMMIT_DIR.is_dir():
        links = subprocess.run(
            [sys.executable, str(PRECOMMIT_DIR / "check_markdown_links.py"),
             "tasks/archive/mover.md", "tasks/archive/sibling.md"],
            cwd=repo, capture_output=True, text=True,
        )  # fmt: skip
        # missing.md was already broken before the move; nothing else is.
        assert "missing.md" in links.stderr
        assert links.stderr.count("Broken link") == 1, links.stderr


def test_precommit_frontmatter_validator_accepts_archived_task(
    acceptance_repo: Path, monkeypatch
) -> None:
    script = PRECOMMIT_DIR / "validate_task_frontmatter.py"
    if not script.exists():
        pytest.skip("pre-commit validators not present outside the monorepo")
    _archive_json(acceptance_repo, monkeypatch)
    result = subprocess.run(
        [sys.executable, str(script), "tasks/archive/old-a.md", "tasks/archive/old-b.md"],
        cwd=acceptance_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# ---------- Output ----------------------------------------------------------------


def test_json_contract(repo: Path, monkeypatch) -> None:
    _write(repo / "tasks", "d", "done", f"completed: '{_ago(30)}'")
    data = _archive_json(repo, monkeypatch, "--dry-run")
    assert set(data) == {
        "archived",
        "count",
        "eligible",
        "skipped",
        "errors",
        "relinked",
        "older_than_days",
        "states",
        "limit",
        "dry_run",
    }
    assert data["count"] == 1
    assert data["states"] == ["done", "cancelled", "expired"]
    record = data["archived"][0]
    assert record["from"] == "d.md" and record["to"] == "archive/d.md"


def test_negative_options_are_rejected(repo: Path, monkeypatch) -> None:
    result = _run(repo, monkeypatch, "archive", "--limit", "-1")
    assert result.exit_code == 1
    assert "--limit" in result.output


def test_human_output_dry_run(acceptance_repo: Path, monkeypatch) -> None:
    result = _run(acceptance_repo, monkeypatch, "archive", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "Would archive 2 task(s)" in result.output
    assert "Dry-run" in result.output


# ---------- Review follow-ups -------------------------------------------------------


def test_show_finds_archived_task_when_no_live_task_is_left(repo: Path, monkeypatch) -> None:
    _write(repo / "tasks", "only", "done", f"completed: '{_ago(30)}'")
    _archive_json(repo, monkeypatch)
    assert not list((repo / "tasks").glob("*.md"))

    show = _run(repo, monkeypatch, "show", "only")
    assert show.exit_code == 0, show.output
    assert "tasks/archive/only.md" in show.output


def test_root_based_link_between_moved_tasks_is_rewritten(repo: Path, monkeypatch) -> None:
    tasks = repo / "tasks"
    _write(tasks, "target", "done", f"completed: '{_ago(30)}'")
    _write(
        tasks,
        "referrer",
        "done",
        f"completed: '{_ago(30)}'",
        body="[t](tasks/target.md) [r](tasks/live.md)\n",
    )
    _write(tasks, "live", "todo")
    _commit_all(repo)

    data = _archive_json(repo, monkeypatch)
    assert sorted(r["id"] for r in data["archived"]) == ["referrer", "target"]
    text = (tasks / "archive" / "referrer.md").read_text()
    # Still written from the root; the unmoved target needs no change.
    assert "[t](tasks/archive/target.md)" in text
    assert "[r](tasks/live.md)" in text
    check = _run(repo, monkeypatch, "check")
    assert check.exit_code == 0, check.output


@pytest.mark.parametrize(
    "body",
    [
        'See [notes](old-notes.md "Notes").\n',
        "See [notes][n].\n\n[n]: old-notes.md\n",
        '[notes][n]\n\n[n]: <old-notes.md> "Notes"\n',
    ],
    ids=["inline-title", "reference", "reference-angle-title"],
)
def test_titled_and_reference_links_pin_their_target(repo: Path, monkeypatch, body: str) -> None:
    tasks = repo / "tasks"
    _write(tasks, "hub", "todo", body=body)
    _write(tasks, "old-notes", "done", f"completed: '{_ago(30)}'")

    data = _archive_json(repo, monkeypatch)
    assert data["archived"] == []
    assert "linked from hub" in data["skipped"][0]["reason"]


def test_titled_and_reference_links_are_rebased(repo: Path, monkeypatch) -> None:
    tasks = repo / "tasks"
    _write(tasks, "live", "todo")
    body = '[a](live.md "Live task") [b][ref]\n\n[ref]: live.md#top "Ref"\n[c]: <live.md>\n'
    _write(tasks, "mover", "done", f"completed: '{_ago(30)}'", body=body)

    _archive_json(repo, monkeypatch)
    text = (tasks / "archive" / "mover.md").read_text()
    assert '[a](../live.md "Live task")' in text
    assert '[ref]: ../live.md#top "Ref"' in text
    assert "[c]: <../live.md>" in text


def test_selected_task_that_does_not_move_keeps_its_link_target(repo: Path, monkeypatch) -> None:
    """A referrer that cannot move after planning must not strand a moved target."""
    from gptodo.archive import apply_archive, plan_archive

    tasks = repo / "tasks"
    # target is older, so it moves before the referrer is re-checked.
    _write(tasks, "target", "done", f"completed: '{_ago(50)}'")
    referrer = _write(
        tasks, "referrer", "done", f"completed: '{_ago(40)}'", body="[t](target.md)\n"
    )
    _commit_all(repo)

    plan = plan_archive(tasks, repo)
    assert [c.task.name for c in plan.selected] == ["target", "referrer"]
    # Another session reopens the referrer between planning and moving.
    referrer.write_text(referrer.read_text().replace("state: done", "state: active"))
    apply_archive(plan, tasks)

    assert plan.moved == []
    assert (tasks / "target.md").exists()
    assert not (tasks / "archive" / "target.md").exists()
    errors = {e["id"]: e["error"] for e in plan.errors}
    assert "changed since planning" in errors["referrer"]
    assert "moved back" in errors["target"] and "linked from referrer" in errors["target"]
    # The rename was undone in the index too.
    assert "target" not in _git(repo, "status", "--porcelain")
    monkeypatch.chdir(repo)
    check = CliRunner().invoke(cli, ["check"])
    assert check.exit_code == 0, check.output


def test_failed_link_rewrite_keeps_the_task_and_is_reported(repo: Path, monkeypatch) -> None:
    tasks = repo / "tasks"
    _write(tasks, "live", "todo")
    mover = _write(tasks, "mover", "done", f"completed: '{_ago(30)}'", body="[l](live.md)\n")
    original = mover.read_text()

    real_replace = os.replace

    def failing_replace(src, dst, *args, **kwargs):
        if Path(dst).name == "mover.md":
            raise OSError(28, "No space left on device")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", failing_replace)
    result = _run(repo, monkeypatch, "archive", "--json")
    assert result.exit_code == 1, result.output
    data = json.loads(result.output)
    assert [r["id"] for r in data["archived"]] == ["mover"]
    assert data["archived"][0]["links_rebased"] is False
    assert data["errors"] and "links not rebased" in data["errors"][0]["error"]
    # The moved file is intact and no temporary file is left behind.
    assert (tasks / "archive" / "mover.md").read_text() == original
    assert sorted(p.name for p in (tasks / "archive").iterdir()) == ["mover.md"]


def test_code_formatted_link_label_is_rebased(repo: Path, monkeypatch) -> None:
    tasks = repo / "tasks"
    _write(tasks, "live", "todo")
    body = "See [`live`](live.md) and [plain](live.md); `[x](live.md)` is code.\n"
    _write(tasks, "mover", "done", f"completed: '{_ago(30)}'", body=body)
    _commit_all(repo)

    data = _archive_json(repo, monkeypatch)
    assert [r["id"] for r in data["archived"]] == ["mover"]
    text = (tasks / "archive" / "mover.md").read_text()
    assert "See [`live`](../live.md) and [plain](../live.md);" in text
    assert "`[x](live.md)` is code." in text
    monkeypatch.chdir(repo)
    check = CliRunner().invoke(cli, ["check"])
    assert check.exit_code == 0, check.output


def test_link_inside_code_does_not_pin(repo: Path, monkeypatch) -> None:
    tasks = repo / "tasks"
    _write(tasks, "old", "done", f"completed: '{_ago(30)}'")
    _write(tasks, "hub", "active", body="`[o](old.md)`\n```\n[o](old.md)\n```\n")
    data = _archive_json(repo, monkeypatch)
    assert [r["id"] for r in data["archived"]] == ["old"]


def test_successive_runs_keep_links_from_archived_referrers(repo: Path, monkeypatch) -> None:
    """Run 1 archives a referrer; run 2 archives its target and repoints the link."""
    tasks = repo / "tasks"
    (repo / "knowledge").mkdir()
    (repo / "knowledge" / "x.md").write_text("# x\n")
    _write(tasks, "target", "done", f"completed: '{_ago(2)}'")
    body = "[t](target.md) [root](tasks/target.md) [other](../knowledge/x.md)\n"
    _write(tasks, "referrer", "done", f"completed: '{_ago(30)}'", body=body)
    _commit_all(repo)
    monkeypatch.chdir(repo)

    data = _archive_json(repo, monkeypatch)
    assert [r["id"] for r in data["archived"]] == ["referrer"]
    assert data["relinked"] == []
    archived = tasks / "archive" / "referrer.md"
    assert "[t](../target.md) [root](tasks/target.md)" in archived.read_text()
    assert CliRunner().invoke(cli, ["check"]).exit_code == 0
    _git(repo, "commit", "--quiet", "-m", "run 1")

    # The target ages past the window.
    _write(tasks, "target", "done", f"completed: '{_ago(30)}'")
    _git(repo, "add", "tasks")
    _git(repo, "commit", "--quiet", "-m", "age")
    data = _archive_json(repo, monkeypatch)
    assert [r["id"] for r in data["archived"]] == ["target"]
    assert data["relinked"] == ["archive/referrer.md"]
    assert data["errors"] == []
    text = archived.read_text()
    assert "[t](target.md) [root](tasks/archive/target.md)" in text
    # The link that run 1 rebased is left exactly as it was.
    assert "[other](../../knowledge/x.md)" in text
    # The repointed referrer is staged with the move.
    assert _git(repo, "diff", "--name-only").strip() == ""
    assert "M  tasks/archive/referrer.md" in _git(repo, "status", "--porcelain")
    check = CliRunner().invoke(cli, ["check"])
    assert check.exit_code == 0, check.output


def test_batched_commit_times_match_per_file_log(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One `git log` pass gives each file the same time as `git log -1 -- <file>`."""
    from gptodo.archive import _git_commit_times

    tasks = repo / "tasks"
    _write(tasks, "first", "done")
    _write(tasks, "second", "done")
    _commit_all(repo)
    (tasks / "second.md").write_text((tasks / "second.md").read_text() + "\nedit\n")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2030-01-01T00:00:00+00:00")
    _git(repo, "commit", "--quiet", "-am", "edit", "--date", "2030-01-01T00:00:00")
    times = _git_commit_times(tasks)
    assert times is not None
    for name in ("first.md", "second.md"):
        per_file = _git(tasks, "log", "-1", "--format=%ct", "--", name).strip()
        assert times[name] == int(per_file)
    assert times["first.md"] < times["second.md"]
