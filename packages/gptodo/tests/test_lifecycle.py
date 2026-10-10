"""Shared lifecycle parity and transactional mutation regressions."""

from pathlib import Path


from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from gptodo.cli import cli
from gptodo.lifecycle import StaleTaskError, TransitionError, mutate_task, transform_post
from gptodo.unblock import auto_unblock_tasks, check_fan_in_completion
from gptodo.utils import load_tasks


from gptodo.frontmatter_compat import frontmatter
from gptodo.utils import update_task_state


def test_library_completion_cleans_metadata(tmp_path: Path) -> None:
    path = tmp_path / "task.md"
    path.write_text(
        "---\nstate: todo\ncreated: 2026-01-01\nnext_action: finish\nwaiting_for: old blocker\nwaiting_since: '2026-01-01'\nfirst_waiting_since: '2026-01-01'\nwaiting_spell_count: 2\ntracking_issue: https://github.com/org/repo/issues/1\n---\n# task\nEvidence stays.\n"
    )
    assert update_task_state(path, "done")
    post = frontmatter.load(path)
    assert "next_action" not in post.metadata
    assert "waiting_for" not in post.metadata
    assert "first_waiting_since" not in post.metadata
    assert "completed" in post.metadata
    assert post.metadata["tracking_issue"] == "https://github.com/org/repo/issues/1"
    assert "Evidence stays." in post.content


def write_task(root, name="task", **metadata):
    tasks = root / "tasks"
    tasks.mkdir(exist_ok=True)
    path = tasks / f"{name}.md"
    base = {"state": "todo", "created": "2026-01-01", "tracking_issue": "org/repo#1"}
    base.update(metadata)
    path.write_text(frontmatter.dumps(frontmatter.Post(content="# Task\nEvidence\n", **base)))
    return path


@pytest.mark.parametrize(
    "patch,force",
    [
        ({"state": "waiting", "waiting_for": "review"}, False),
        ({"state": "done"}, False),
        ({"state": "cancelled", "recur": "7d"}, False),
        ({"state": "done", "completed": "2026-02-01T00:00:00"}, False),
        ({"state": "done", "completed": None}, False),
        ({"state": "done", "recur": "weekly"}, False),
        ({"state": "done", "recur": "monthly"}, False),
        ({"state": "done", "recur": "0 9 * * 1"}, False),
        ({"state": "new"}, False),
        ({"state": "paused"}, False),
    ],
)
def test_cli_library_differential(tmp_path, monkeypatch, patch, force):
    path = write_task(tmp_path, state="active", next_action="finish", wait="2099-01-01")
    original = path.read_bytes()
    monkeypatch.chdir(tmp_path)
    args = ["edit", "task"]
    for key, value in patch.items():
        args += ["--set", key, "none" if value is None else str(value)]
    if force:
        args += ["--force"]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    cli_post = frontmatter.load(path)
    path.write_bytes(original)
    mutate_task(path, patch=patch, force=force)
    library_post = frontmatter.load(path)
    # Second-resolution timestamps may cross a boundary; compare their shape.
    for key in ("completed", "waiting_since", "first_waiting_since"):
        assert (key in cli_post.metadata) == (key in library_post.metadata)
        if key in cli_post.metadata and key not in patch:
            cli_post.metadata.pop(key)
            library_post.metadata.pop(key)
    assert cli_post.metadata == library_post.metadata
    assert cli_post.content == library_post.content


@pytest.mark.parametrize("state", ["done", "cancelled"])
def test_same_state_terminal_is_not_import_backfill(tmp_path, state):
    path = write_task(tmp_path, state=state)
    mutate_task(path, patch={"state": state})
    assert "completed" not in frontmatter.load(path).metadata


@pytest.mark.parametrize("intent", ["operator_reopen", "sync_reopen", "alert_refire"])
def test_exceptional_reopen_clears_completed(tmp_path, intent):
    path = write_task(tmp_path, state="done", completed="2026-01-01")
    target = "active" if intent == "sync_reopen" else "todo"
    result = mutate_task(path, patch={"state": target}, intent=intent)
    assert result.old_state == "done"
    assert result.effective_state == target
    assert "completed" not in result.post.metadata
    assert result.post.metadata["tracking_issue"] == "org/repo#1"


def test_reopen_explicit_completed_wins(tmp_path):
    path = write_task(tmp_path, state="done", completed="2026-01-01")
    result = mutate_task(path, patch={"state": "active", "completed": "2026-02-01"}, force=True)
    assert result.post.metadata["completed"] == "2026-02-01"


@pytest.mark.parametrize("value", [None, "", "invalid"])
def test_state_invalid_rejected_exact_preimage(tmp_path, monkeypatch, value):
    path = write_task(tmp_path)
    before = path.read_bytes()
    with pytest.raises(TransitionError):
        mutate_task(path, patch={"state": value, "priority": "high"})
    assert path.read_bytes() == before
    if value is None:
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", "none"])
        assert result.exit_code == 1
        assert path.read_bytes() == before


def test_strict_normal_machine_edges_and_requeue(tmp_path):
    path = write_task(tmp_path, state="backlog")
    mutate_task(path, patch={"state": "active"}, strict=True)
    with pytest.raises(TransitionError):
        mutate_task(path, patch={"state": "todo"}, strict=True)
    mutate_task(path, patch={"state": "todo"}, strict=True, intent="requeue")
    mutate_task(path, patch={"state": "done"}, strict=True)
    assert not update_task_state(path, "todo")


def test_stale_candidate_does_not_overwrite_terminal(tmp_path):
    path = write_task(tmp_path)
    mutate_task(path, patch={"state": "done"})
    before = path.read_bytes()
    with pytest.raises(StaleTaskError):
        mutate_task(path, patch={"state": "active"}, expected_state="todo")
    assert path.read_bytes() == before


def test_validation_and_dry_run_leave_exact_bytes(tmp_path):
    path = write_task(tmp_path)
    before = path.read_bytes()
    with pytest.raises(TransitionError):
        mutate_task(path, patch={"state": "done", "priority": "nonsense"})
    assert path.read_bytes() == before
    result = mutate_task(path, patch={"state": "done"}, dry_run=True)
    assert not result.written and result.effective_state == "done"
    assert path.read_bytes() == before


def test_missing_state_metadata_edit_uses_backlog_effective_state(tmp_path):
    path = write_task(tmp_path)
    post = frontmatter.load(path)
    post.metadata.pop("state")
    path.write_text(frontmatter.dumps(post))

    result = mutate_task(path, patch={"priority": "high"})

    assert result.written
    assert result.old_state == "backlog"
    assert result.effective_state == "backlog"
    updated = frontmatter.load(path)
    assert updated.metadata["priority"] == "high"
    assert "state" not in updated.metadata


def test_patch_replaces_and_clears_canonical_list_fields(tmp_path):
    path = write_task(tmp_path, requires=["old"], tags=["one"])

    replaced = mutate_task(path, patch={"requires": ["child"], "tags": []})
    assert replaced.post.metadata["requires"] == ["child"]
    assert replaced.post.metadata["tags"] == []

    cleared = mutate_task(path, patch={"requires": None})
    assert "requires" not in cleared.post.metadata


def test_body_replacement_and_subtask_edit_share_the_same_snapshot(tmp_path):
    path = write_task(tmp_path)
    post = frontmatter.load(path)
    post.content = "- [ ] A"
    path.write_text(frontmatter.dumps(post))

    result = mutate_task(
        path,
        changes=[
            ("set_subtask", "A", "done"),
            ("set_body", "", "- [ ] B\n- [ ] A"),
        ],
    )

    assert result.post.content.splitlines() == ["- [ ] B", "- [x] A"]


def test_wait_history_entry_reassertion_repark(tmp_path):
    path = write_task(tmp_path)
    first = datetime(2026, 1, 2, tzinfo=timezone.utc)
    second = datetime(2026, 1, 3, tzinfo=timezone.utc)
    result = mutate_task(path, patch={"state": "waiting", "waiting_for": "review"}, now=first)
    assert result.post.metadata["waiting_spell_count"] == 1
    mutate_task(path, patch={"state": "waiting"}, now=second)
    mutate_task(path, patch={"state": "todo", "waiting_for": None, "waiting_since": None})
    result = mutate_task(path, patch={"state": "waiting", "waiting_for": "review"}, now=second)
    assert result.post.metadata["waiting_spell_count"] == 2
    assert result.post.metadata["first_waiting_since"] == first.isoformat(timespec="seconds")
    assert result.post.metadata["waiting_since"] == second.isoformat(timespec="seconds")


def test_partial_structured_unblock_preserves_other_conditions(tmp_path):
    write_task(tmp_path, "child", state="done")
    path = write_task(
        tmp_path,
        "parent",
        state="waiting",
        waiting_for=[
            {"type": "task", "ref": "child"},
            {"type": "comment", "ref": "operator", "pattern": "approved"},
        ],
        waiting_since="2026-01-01",
        first_waiting_since="2026-01-01",
        waiting_spell_count=2,
    )
    tasks = load_tasks(tmp_path / "tasks")
    auto_unblock_tasks(["child"], tasks, tmp_path / "tasks")
    post = frontmatter.load(path)
    assert post.metadata["state"] == "waiting"
    assert post.metadata["waiting_for"]["ref"] == "operator"
    assert post.metadata["waiting_spell_count"] == 2
    assert "waiting_since" in post.metadata


def test_fan_in_missing_or_reopened_child_fails_closed(tmp_path):
    path = write_task(
        tmp_path, "parent", state="waiting", spawned_tasks=["child", "missing"], next_action="old"
    )
    write_task(tmp_path, "child", state="done", spawned_from="parent")
    tasks = load_tasks(tmp_path / "tasks")
    child = next(t for t in tasks if t.name == "child")
    assert check_fan_in_completion(child, tasks, tmp_path / "tasks") is None
    assert frontmatter.load(path).metadata["state"] == "waiting"
    mutate_task(path, patch={"spawned_tasks": ["child"]})
    mutate_task(child.path, patch={"state": "active"}, force=True)
    assert check_fan_in_completion(child, tasks, tmp_path / "tasks") is None
    mutate_task(child.path, patch={"state": "done"}, completion_effects=False)
    assert check_fan_in_completion(child, tasks, tmp_path / "tasks")
    assert "completed" in frontmatter.load(path).metadata
    assert "next_action" not in frontmatter.load(path).metadata


def test_expired_claim_refused(tmp_path, monkeypatch):
    path = write_task(tmp_path, state="expired")
    before = path.read_bytes()
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["claim", "task", "--agent", "bob"])
    assert result.exit_code == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize("issue_state", ["OPEN", "CLOSED"])
def test_cancelled_sync_sticky(tmp_path, monkeypatch, issue_state):
    from unittest.mock import patch

    path = write_task(tmp_path, state="cancelled", tracking="org/repo#1")
    monkeypatch.chdir(tmp_path)
    before = path.read_bytes()
    with (
        patch("gptodo.cli.fetch_github_issue_state", return_value=issue_state),
        patch("gptodo.cli.fetch_github_issue_details", return_value={}),
    ):
        result = CliRunner().invoke(cli, ["sync", "--update", "--json"])
    assert result.exit_code == 0, result.output
    assert path.read_bytes() == before


def test_atomic_replace_failure_preserves_preimage(tmp_path, monkeypatch):
    path = write_task(tmp_path)
    before = path.read_bytes()

    def fail(*args):
        raise OSError("injected replace failure")

    monkeypatch.setattr("gptodo.lifecycle.os.replace", fail)
    with pytest.raises(OSError):
        mutate_task(path, patch={"state": "done"})
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".*.tmp"))


def _increment(path):
    def prepare(post):
        return [
            ("set", "count", post.metadata.get("count", 0) + 1),
            ("set_body", "", post.content.rstrip() + "\none\n"),
        ]

    for _ in range(10):
        mutate_task(Path(path), prepare=prepare)


def test_concurrent_same_state_metadata_and_body_no_lost_updates(tmp_path):
    path = write_task(tmp_path)
    with ProcessPoolExecutor(max_workers=4) as pool:
        list(pool.map(_increment, [str(path)] * 4))
    post = frontmatter.load(path)
    assert post.metadata["count"] == 40
    assert post.content.splitlines().count("one") == 40
    assert post.metadata["tracking_issue"] == "org/repo#1"


def test_hook_legacy_reassertion_and_recurrence_skip(tmp_path, monkeypatch):
    from unittest.mock import patch

    path = write_task(tmp_path, state="active")
    monkeypatch.setenv("HOOK_TASK_DONE", "hook")
    with patch("subprocess.run") as hook:
        mutate_task(path, patch={"state": "done"})
        completed = frontmatter.load(path).metadata["completed"]
        mutate_task(path, patch={"state": "done"})
        assert hook.call_count == 2
        assert frontmatter.load(path).metadata["completed"] == completed
        mutate_task(path, patch={"state": "active", "recur": "7d"}, force=True)
        mutate_task(path, patch={"state": "done"})
        assert hook.call_count == 2


def test_effects_recheck_after_unlock_and_hook_reopen(tmp_path, monkeypatch):
    import gptodo.lifecycle as lifecycle
    from unittest.mock import patch

    path = write_task(tmp_path)
    mutate_task(path, patch={"state": "done"}, completion_effects=False)
    mutate_task(path, patch={"state": "todo"}, force=True)
    monkeypatch.setenv("HOOK_TASK_DONE", "hook")
    with (
        patch("subprocess.run") as hook,
        patch("gptodo.unblock.auto_unblock_with_fan_in") as unblock,
    ):
        lifecycle._completion_effects(path)
        hook.assert_not_called()
        unblock.assert_not_called()
        mutate_task(path, patch={"state": "done"}, completion_effects=False)
        hook.side_effect = lambda *a, **kw: mutate_task(path, patch={"state": "todo"}, force=True)
        lifecycle._completion_effects(path)
        assert hook.call_count == 1
        unblock.assert_not_called()


@pytest.mark.parametrize(
    "wait,recur,expected",
    [
        (None, "7d", "2026-01-09"),
        ("2025-01-01", "monthly", "2026-02-01"),
        ("2099-01-01", "7d", "2099-01-08"),
        (None, "6h", "2026-01-02T18:00:00"),
        ("2025-01-01T01:00:00+00:00", "24h", "2026-01-03T12:00:00+00:00"),
        ("2026-01-05T01:00:00", "24h", "2026-01-06T01:00:00"),
    ],
)
def test_frozen_recurrence_clock(wait, recur, expected, monkeypatch):
    import time

    if hasattr(time, "tzset"):
        monkeypatch.setenv("TZ", "UTC")
        time.tzset()
    metadata = {"state": "active", "created": "2026-01-01", "recur": recur}
    if wait:
        metadata["wait"] = wait
    now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    result = transform_post(frontmatter.Post("", **metadata), [("set", "state", "done")], now=now)
    assert result.metadata["wait"] == expected
    assert result.metadata["waiting_since"] == now.isoformat(timespec="seconds")


def test_naive_recurrence_wait_uses_local_wall_clock(monkeypatch):
    import os
    import time

    from gptodo.utils import advance_wait

    if not hasattr(time, "tzset"):
        pytest.skip("requires time.tzset")
    original_tz = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "Europe/Stockholm")
        time.tzset()

        result = advance_wait(
            datetime(2026, 1, 1, 1),
            "6h",
            now=datetime(2026, 1, 2, 12, tzinfo=timezone.utc),
        )

        assert result == datetime(2026, 1, 2, 19)
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        time.tzset()


def test_fan_in_fresh_same_state_child_list_and_parent_hooks(tmp_path, monkeypatch):
    from unittest.mock import patch

    parent = write_task(tmp_path, "parent", state="waiting", spawned_tasks=["first"])
    write_task(tmp_path, "first", state="done", spawned_from="parent")
    stale = load_tasks(tmp_path / "tasks")
    child = next(t for t in stale if t.name == "first")
    # Same-state parent metadata changed after enumeration, newly created child
    # absent from candidate lookup. It must block until its fresh state is done.
    mutate_task(parent, patch={"spawned_tasks": ["first", "second"]})
    second = write_task(tmp_path, "second", state="todo", spawned_from="parent")
    assert check_fan_in_completion(child, stale, tmp_path / "tasks") is None
    mutate_task(second, patch={"state": "done"}, completion_effects=False)
    monkeypatch.setenv("HOOK_TASK_DONE", "hook")
    with patch("subprocess.run") as hook:
        assert check_fan_in_completion(child, stale, tmp_path / "tasks")
        assert any(call.args[0][1] == "parent" for call in hook.call_args_list)


def test_recursive_hook_cycle_bounded(tmp_path, monkeypatch):
    from unittest.mock import patch

    path = write_task(tmp_path, state="active")
    monkeypatch.setenv("HOOK_TASK_DONE", "hook")
    with patch("subprocess.run") as hook:
        hook.side_effect = lambda *a, **kw: mutate_task(path, patch={"state": "done"})
        mutate_task(path, patch={"state": "done"})
        assert hook.call_count == 1


def test_fan_in_recurring_parent_remains_waiting(tmp_path, monkeypatch):
    from unittest.mock import patch

    parent = write_task(tmp_path, "parent", state="waiting", spawned_tasks=["child"], recur="7d")
    write_task(tmp_path, "child", state="done", spawned_from="parent")
    tasks = load_tasks(tmp_path / "tasks")
    child = next(t for t in tasks if t.name == "child")
    monkeypatch.setenv("HOOK_TASK_DONE", "hook")
    with patch("subprocess.run") as hook:
        assert check_fan_in_completion(child, tasks, tmp_path / "tasks") is None
        hook.assert_not_called()
    assert frontmatter.load(parent).metadata["state"] == "waiting"


def test_batch_edit_commits_parent_before_child_completion_effects(tmp_path, monkeypatch):
    child = write_task(tmp_path, "child", state="active", spawned_from="parent")
    parent = write_task(
        tmp_path,
        "parent",
        state="waiting",
        spawned_tasks=["child"],
        waiting_for="child",
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["edit", "child", "parent", "--set", "state", "done"])

    assert result.exit_code == 0, result.output
    assert frontmatter.load(child).metadata["state"] == "done"
    assert frontmatter.load(parent).metadata["state"] == "done"


def test_malformed_recurrence_closure_legacy_compatible(tmp_path):
    path = write_task(
        tmp_path, state="active", recur="nonsense", next_action="old", wait="2099-01-01"
    )
    result = mutate_task(path, patch={"state": "done"})
    assert result.effective_state == "done"
    assert "completed" in result.post.metadata
    assert "next_action" not in result.post.metadata
    assert "wait" not in result.post.metadata


def test_fresh_claim_same_state_reassignment(tmp_path, monkeypatch):
    import gptodo.lifecycle as lifecycle
    from unittest.mock import patch

    path = write_task(tmp_path, state="active", assigned_to="bob", assigned_at="2026-01-01")
    monkeypatch.chdir(tmp_path)
    original = lifecycle.mutate_task

    def mutate(*args, **kwargs):
        original(path, patch={"assigned_to": "other", "assigned_at": "2026-01-02"})
        return original(*args, **kwargs)

    with patch("gptodo.lifecycle.mutate_task", side_effect=mutate):
        result = CliRunner().invoke(cli, ["claim", "task", "--agent", "bob"])
    assert result.exit_code == 0, result.output
    assert frontmatter.load(path).metadata["assigned_to"] == "bob"
    assert frontmatter.load(path).metadata["assigned_at"] != "2026-01-01"


def test_expire_fresh_same_state_priority_blocks(tmp_path, monkeypatch):
    import gptodo.lifecycle as lifecycle
    from unittest.mock import patch

    path = write_task(tmp_path, state="todo", created="2000-01-01", priority="low")
    monkeypatch.chdir(tmp_path)
    original = lifecycle.mutate_task

    def mutate(*args, **kwargs):
        original(path, patch={"priority": "high"})
        return original(*args, **kwargs)

    with patch("gptodo.lifecycle.mutate_task", side_effect=mutate):
        result = CliRunner().invoke(cli, ["expire", "--days", "90", "--json"])
    assert result.exit_code == 0, result.output
    assert frontmatter.load(path).metadata["state"] == "todo"
    assert '"count": 0' in result.output


def test_unblock_fresh_same_state_conditions_not_overwritten(tmp_path):
    write_task(tmp_path, "child", state="done")
    path = write_task(tmp_path, "parent", state="waiting", waiting_for="child")
    stale = load_tasks(tmp_path / "tasks")
    mutate_task(path, patch={"waiting_for": "operator approval", "priority": "high"})
    auto_unblock_tasks(["child"], stale, tmp_path / "tasks")
    assert frontmatter.load(path).metadata["waiting_for"] == "operator approval"
    assert frontmatter.load(path).metadata["priority"] == "high"


def _atomic_writer(path):
    for index in range(100):
        mutate_task(Path(path), patch={"counter": index, "payload": str(index) * 1000})


def test_unlocked_readers_never_observe_partial_yaml(tmp_path):
    path = write_task(tmp_path, counter=-1, payload="initial")
    with ProcessPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(_atomic_writer, str(path))
        observations = 0
        while not writer.done():
            post = frontmatter.load(path)
            assert post.metadata["state"] == "todo"
            counter = post.metadata["counter"]
            if counter >= 0:
                assert post.metadata["payload"] == str(counter) * 1000
            observations += 1
        writer.result()
    assert observations > 0


def test_partial_unblock_preserves_unresolved_extensions(tmp_path):
    write_task(tmp_path, "child", state="done")
    condition = {
        "type": "comment",
        "ref": "org/repo#2",
        "pattern": "approved",
        "evidence": "retain",
    }
    path = write_task(
        tmp_path,
        "parent",
        state="waiting",
        waiting_for=[{"type": "task", "ref": "child"}, condition],
    )
    auto_unblock_tasks(["child"], load_tasks(tmp_path / "tasks"), tmp_path / "tasks")
    assert frontmatter.load(path).metadata["waiting_for"] == condition


def test_unblock_revoked_completion_preserves_blocker(tmp_path):
    child = write_task(tmp_path, "child", state="done")
    parent = write_task(tmp_path, "parent", state="waiting", waiting_for="child")
    stale = load_tasks(tmp_path / "tasks")
    mutate_task(child, patch={"state": "todo"}, force=True)
    assert not auto_unblock_tasks(["child"], stale, tmp_path / "tasks")
    assert frontmatter.load(parent).metadata["waiting_for"] == "child"


@pytest.mark.parametrize("missing_from_snapshot", [False, True])
def test_unblock_missing_completion_preserves_blocker(tmp_path, missing_from_snapshot):
    child = write_task(tmp_path, "child", state="done")
    parent = write_task(
        tmp_path, "parent", state="waiting", waiting_for="child", waiting_since="2026-01-01"
    )
    stale = load_tasks(tmp_path / "tasks")
    if missing_from_snapshot:
        stale = [task for task in stale if task.name != "child"]
    child.unlink()
    original = parent.read_bytes()

    assert not auto_unblock_tasks(["child"], stale, tmp_path / "tasks")
    assert parent.read_bytes() == original


@pytest.mark.parametrize(
    "initial,remote,target", [("waiting", "CLOSED", "done"), ("done", "OPEN", "active")]
)
def test_sync_uses_completion_and_reopen_lifecycle(tmp_path, monkeypatch, initial, remote, target):
    from unittest.mock import patch

    path = write_task(
        tmp_path,
        state=initial,
        tracking="org/repo#1",
        completed="2026-01-01",
        next_action="old",
        waiting_for="review",
    )
    monkeypatch.chdir(tmp_path)
    with (
        patch("gptodo.cli.fetch_github_issue_state", return_value=remote),
        patch("gptodo.cli.fetch_github_issue_details", return_value={}),
    ):
        result = CliRunner().invoke(cli, ["sync", "--update", "--json"])
    assert result.exit_code == 0, result.output
    post = frontmatter.load(path)
    assert post.metadata["state"] == target
    if target == "done":
        assert "next_action" not in post.metadata
        assert post.metadata["completed"] != "2026-01-01"
    else:
        assert "completed" not in post.metadata
    assert post.metadata["tracking"] == "org/repo#1"


def test_sync_closed_issue_does_not_advance_existing_recurrence_gate(tmp_path, monkeypatch):
    from unittest.mock import patch

    path = write_task(
        tmp_path,
        state="waiting",
        tracking="org/repo#1",
        recur="7d",
        wait="2099-01-01",
        wait_kind="machine",
        waiting_for="next recurrence gate (wait: 2099-01-01)",
        waiting_since="2026-01-01",
    )
    monkeypatch.chdir(tmp_path)
    before = path.read_bytes()
    with (
        patch("gptodo.cli.fetch_github_issue_state", return_value="CLOSED"),
        patch("gptodo.cli.fetch_github_issue_details", return_value={}),
    ):
        result = CliRunner().invoke(cli, ["sync", "--update", "--json"])

    assert result.exit_code == 0, result.output
    assert path.read_bytes() == before


def test_terminal_alias_reopen_clears_completed(tmp_path):
    path = write_task(tmp_path, state="done", completed="2026-01-01")
    result = mutate_task(path, patch={"state": "new"}, force=True)
    assert result.effective_state == "backlog"
    assert "completed" not in result.post.metadata


def test_cli_stale_open_snapshot_cannot_reopen_fresh_terminal(tmp_path, monkeypatch):
    import gptodo.lifecycle as lifecycle
    from unittest.mock import patch

    path = write_task(tmp_path)
    monkeypatch.chdir(tmp_path)
    original = lifecycle.mutate_task

    def mutate(*args, **kwargs):
        original(path, patch={"state": "done"})
        return original(*args, **kwargs)

    with patch("gptodo.lifecycle.mutate_task", side_effect=mutate):
        result = CliRunner().invoke(cli, ["edit", "task", "--set", "state", "active"])
    assert result.exit_code == 1
    assert frontmatter.load(path).metadata["state"] == "done"


@pytest.mark.parametrize("error", [OSError("replace failed"), FileNotFoundError("gone")])
def test_batch_edit_drains_committed_effects_on_write_error(tmp_path, monkeypatch, error):
    import gptodo.lifecycle as lifecycle

    first = write_task(tmp_path, "first")
    second = write_task(tmp_path, "second")
    monkeypatch.chdir(tmp_path)
    original = lifecycle.mutate_task
    effects = []

    def mutate(path, *args, **kwargs):
        if path == second:
            raise error
        return original(path, *args, **kwargs)

    monkeypatch.setattr(lifecycle, "mutate_task", mutate)
    monkeypatch.setattr(lifecycle, "run_completion_effects", effects.append)
    result = CliRunner().invoke(cli, ["edit", "first", "second", "--set", "state", "done"])
    assert result.exit_code != 0
    assert frontmatter.load(first).metadata["state"] == "done"
    assert frontmatter.load(second).metadata["state"] == "todo"
    assert effects == [first]


@pytest.mark.parametrize("unreadable", ["metadata", "yaml"])
def test_expire_skips_candidate_that_becomes_unreadable(tmp_path, monkeypatch, unreadable):
    import gptodo.lifecycle as lifecycle

    first = write_task(tmp_path, "first", created="2000-01-01", priority="low")
    second = write_task(tmp_path, "second", created="2000-01-02", priority="low")
    monkeypatch.chdir(tmp_path)
    original = lifecycle.mutate_task
    damaged = "---\nstate: todo\ncreated: 2000-01-01\n---\n# First\n"
    if unreadable == "yaml":
        damaged = "---\nstate: [\n---\n# First\n"
    else:
        import importlib

        cli_module = importlib.import_module("gptodo.cli")
        original_load = cli_module.load_tasks

        def load(*args, **kwargs):
            if kwargs.get("single_file") == first:
                return []
            return original_load(*args, **kwargs)

        monkeypatch.setattr(cli_module, "load_tasks", load)

    def mutate(path, *args, **kwargs):
        if path == first:
            first.write_text(damaged)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(lifecycle, "mutate_task", mutate)
    result = CliRunner().invoke(cli, ["expire", "--days", "90", "--json"])
    assert result.exit_code == 0, result.output
    assert first.read_text() == damaged
    assert frontmatter.load(second).metadata["state"] == "expired"
    assert '"count": 1' in result.output


def test_library_bool_failure_contract(tmp_path):
    assert not update_task_state(tmp_path / "missing.md", "done")
    path = write_task(tmp_path)
    before = path.read_bytes()
    assert not update_task_state(path, None)
    assert not update_task_state(path, "invalid")
    assert path.read_bytes() == before
