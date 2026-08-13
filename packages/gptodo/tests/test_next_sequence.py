"""Tests for `gptodo next --limit N` greedy sequential (cascade) simulation.

`gptodo ready` / `gptodo next` only ever show the *currently* unblocked set.
If A unblocks B, B is invisible until A is actually completed. These tests cover
the simulated cascade: "if I did these N in order, here is the sequence,
including things that only become available partway down".
"""

import json
from pathlib import Path

from click.testing import CliRunner

from gptodo.cli import cli


def write_task(tasks_dir: Path, name: str, **metadata: object) -> None:
    """Write a minimal task file with YAML frontmatter."""
    lines = ["---"]
    for key, value in metadata.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    lines.extend(["---", f"# {name}"])
    (tasks_dir / f"{name}.md").write_text("\n".join(lines))


def _payload(output: str) -> dict:
    """Extract the JSON object from CLI output (stderr notes may be interleaved)."""
    start = output.index("{")
    return json.loads(output[start:])


def _chain_fixture(tmp_path: Path) -> Path:
    """A -> B -> C chain: only A is ready; B and C are invisible to `ready`."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "chain-a",
        state="todo",
        created="2026-03-28T00:00:00",
        priority="high",
    )
    write_task(
        tasks_dir,
        "chain-b",
        state="backlog",
        created="2026-03-28T01:00:00",
        priority="medium",
        requires=["chain-a"],
    )
    write_task(
        tasks_dir,
        "chain-c",
        state="backlog",
        created="2026-03-28T02:00:00",
        priority="low",
        requires=["chain-b"],
    )
    return tasks_dir


# ---------------------------------------------------------------------------
# Regression guard: the autonomous-session contract must not change
# ---------------------------------------------------------------------------


def test_limit_1_json_identical_to_no_flag(tmp_path: Path, monkeypatch) -> None:
    """`next --json` and `next --json --limit 1` must produce identical output.

    Autonomous sessions call `gptodo next --json`; breaking that shape breaks them.
    """
    _chain_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    bare = runner.invoke(cli, ["next", "--json"])
    limited = runner.invoke(cli, ["next", "--json", "--limit", "1"])

    assert bare.exit_code == 0, bare.output
    assert limited.exit_code == 0, limited.output
    assert bare.output == limited.output
    # And the legacy keys are still there, unchanged.
    payload = _payload(bare.output)
    assert payload["next_task"]["name"] == "chain-a"
    assert "alternatives" in payload
    assert "sequence" not in payload


def test_limit_1_text_identical_to_no_flag(tmp_path: Path, monkeypatch) -> None:
    """Human-readable output for `--limit 1` must match the no-flag output."""
    _chain_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    bare = runner.invoke(cli, ["next"])
    limited = runner.invoke(cli, ["next", "--limit", "1"])

    assert bare.exit_code == 0, bare.output
    assert limited.exit_code == 0, limited.output
    assert bare.output == limited.output


# ---------------------------------------------------------------------------
# The point of the feature: tasks invisible to `ready` appear in the cascade
# ---------------------------------------------------------------------------


def test_chain_appears_in_order_with_attribution(tmp_path: Path, monkeypatch) -> None:
    """B and C are invisible to `ready` but appear in `next --limit 3`, in order."""
    _chain_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    ready = runner.invoke(cli, ["ready", "--state", "both", "--json"])
    assert ready.exit_code == 0, ready.output
    ready_names = {t["name"] for t in _payload(ready.output)["ready_tasks"]}
    assert ready_names == {"chain-a"}, "fixture broken: B/C should be blocked"

    result = runner.invoke(cli, ["next", "--json", "--limit", "3"])
    assert result.exit_code == 0, result.output
    payload = _payload(result.output)

    seq = payload["sequence"]
    assert [step["task"]["name"] for step in seq] == ["chain-a", "chain-b", "chain-c"]

    assert seq[0]["unblocked_by"] is None
    assert seq[0]["unblocked_by_position"] is None
    assert seq[1]["unblocked_by"] == "chain-a"
    assert seq[1]["unblocked_by_position"] == 1
    assert seq[2]["unblocked_by"] == "chain-b"
    assert seq[2]["unblocked_by_position"] == 2

    assert payload["reachable"] == 3
    assert payload["requested"] == 3
    assert payload["complete"] is True
    # Legacy keys still present for consumers that only read them.
    assert payload["next_task"]["name"] == "chain-a"


def test_chain_text_output_states_why_each_item_is_there(tmp_path: Path, monkeypatch) -> None:
    """Human output must explain each item past the first (`unblocked by #N`)."""
    _chain_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli, ["next", "--limit", "3"])
    assert result.exit_code == 0, result.output
    out = result.output

    assert "chain-a" in out and "chain-b" in out and "chain-c" in out
    assert "already ready" in out
    assert "unblocked by #1" in out
    assert "unblocked by #2" in out


# ---------------------------------------------------------------------------
# --order unblock must genuinely diverge from --order priority
# ---------------------------------------------------------------------------


def _diverging_fixture(tmp_path: Path) -> Path:
    """Graph where priority order and unblock order genuinely differ.

    - `solo-high` : high priority, unblocks nothing.
    - `hub-low`   : low priority, transitively unblocks 3 tasks.

    priority order -> solo-high first.  unblock order -> hub-low first.
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "solo-high",
        state="todo",
        created="2026-03-28T00:00:00",
        priority="high",
    )
    write_task(
        tasks_dir,
        "hub-low",
        state="todo",
        created="2026-03-28T01:00:00",
        priority="low",
    )
    for i, name in enumerate(["leaf-one", "leaf-two"]):
        write_task(
            tasks_dir,
            name,
            state="backlog",
            created=f"2026-03-28T0{2 + i}:00:00",
            priority="low",
            requires=["hub-low"],
        )
    write_task(
        tasks_dir,
        "leaf-deep",
        state="backlog",
        created="2026-03-28T04:00:00",
        priority="low",
        requires=["leaf-one"],
    )
    return tasks_dir


def test_order_unblock_differs_from_order_priority(tmp_path: Path, monkeypatch) -> None:
    """`--order unblock` surfaces the critical path, not the priority path."""
    _diverging_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    by_priority = runner.invoke(cli, ["next", "--json", "--limit", "2", "--order", "priority"])
    by_unblock = runner.invoke(cli, ["next", "--json", "--limit", "2", "--order", "unblock"])
    assert by_priority.exit_code == 0, by_priority.output
    assert by_unblock.exit_code == 0, by_unblock.output

    prio_names = [s["task"]["name"] for s in _payload(by_priority.output)["sequence"]]
    unblock_names = [s["task"]["name"] for s in _payload(by_unblock.output)["sequence"]]

    assert prio_names[0] == "solo-high"
    assert unblock_names[0] == "hub-low"
    assert prio_names != unblock_names


def test_order_unblock_cascades_through_the_critical_path(tmp_path: Path, monkeypatch) -> None:
    """With --order unblock the whole chain is reachable and attributed."""
    _diverging_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli, ["next", "--json", "--limit", "5", "--order", "unblock"])
    assert result.exit_code == 0, result.output
    payload = _payload(result.output)
    seq = payload["sequence"]

    names = [s["task"]["name"] for s in seq]
    assert names[0] == "hub-low"
    assert set(names) == {"hub-low", "solo-high", "leaf-one", "leaf-two", "leaf-deep"}

    by_name = {s["task"]["name"]: s for s in seq}
    assert by_name["hub-low"]["unblocked_by"] is None
    assert by_name["solo-high"]["unblocked_by"] is None  # was already ready
    assert by_name["leaf-one"]["unblocked_by"] == "hub-low"
    assert by_name["leaf-two"]["unblocked_by"] == "hub-low"
    assert by_name["leaf-deep"]["unblocked_by"] == "leaf-one"
    assert payload["complete"] is True


# ---------------------------------------------------------------------------
# Honesty: a short list must say it is short
# ---------------------------------------------------------------------------


def test_fewer_than_n_reachable_is_stated_explicitly(tmp_path: Path, monkeypatch) -> None:
    """A short list that looks complete is the failure mode; say it is short."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "solo", state="todo", created="2026-03-28T00:00:00", priority="high")
    write_task(
        tasks_dir,
        "forever-blocked",
        state="backlog",
        created="2026-03-28T01:00:00",
        priority="high",
        requires=["nonexistent-task"],
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli, ["next", "--limit", "5"])
    assert result.exit_code == 0, result.output
    assert "1 of 5" in result.output
    assert "nothing further is unblocked" in result.output

    as_json = runner.invoke(cli, ["next", "--json", "--limit", "5"])
    assert as_json.exit_code == 0, as_json.output
    payload = _payload(as_json.output)
    assert payload["reachable"] == 1
    assert payload["requested"] == 5
    assert payload["complete"] is False
    assert "1 of 5" in payload["note"]


# ---------------------------------------------------------------------------
# Cycles must not hang the simulation
# ---------------------------------------------------------------------------


def test_dependency_cycle_terminates(tmp_path: Path, monkeypatch) -> None:
    """A cycle blocks its members forever; the simulation must still terminate."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "cycle-a",
        state="todo",
        created="2026-03-28T00:00:00",
        priority="high",
        requires=["cycle-b"],
    )
    write_task(
        tasks_dir,
        "cycle-b",
        state="todo",
        created="2026-03-28T01:00:00",
        priority="high",
        requires=["cycle-a"],
    )
    write_task(
        tasks_dir,
        "standalone",
        state="todo",
        created="2026-03-28T02:00:00",
        priority="medium",
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli, ["next", "--json", "--limit", "5"])
    assert result.exit_code == 0, result.output
    payload = _payload(result.output)
    names = [s["task"]["name"] for s in payload["sequence"]]
    assert names == ["standalone"]
    assert payload["complete"] is False


def test_self_cycle_terminates(tmp_path: Path, monkeypatch) -> None:
    """A task requiring itself must not loop the simulation."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(
        tasks_dir,
        "selfref",
        state="todo",
        created="2026-03-28T00:00:00",
        priority="high",
        requires=["selfref"],
    )
    write_task(tasks_dir, "ok", state="todo", created="2026-03-28T01:00:00", priority="low")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli, ["next", "--json", "--limit", "5"])
    assert result.exit_code == 0, result.output
    payload = _payload(result.output)
    assert [s["task"]["name"] for s in payload["sequence"]] == ["ok"]


# ---------------------------------------------------------------------------
# Filters apply throughout the simulation, not just to the first pick
# ---------------------------------------------------------------------------


def test_pool_filter_applies_to_whole_cascade(tmp_path: Path, monkeypatch) -> None:
    """A frontier task unblocked mid-cascade must stay hidden under the default pool."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "root", state="todo", created="2026-03-28T00:00:00", priority="high")
    write_task(
        tasks_dir,
        "frontier-child",
        state="backlog",
        created="2026-03-28T01:00:00",
        priority="high",
        pool="frontier",
        requires=["root"],
    )
    write_task(
        tasks_dir,
        "general-child",
        state="backlog",
        created="2026-03-28T02:00:00",
        priority="low",
        requires=["root"],
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    default_pool = runner.invoke(cli, ["next", "--json", "--limit", "5"])
    assert default_pool.exit_code == 0, default_pool.output
    names = [s["task"]["name"] for s in _payload(default_pool.output)["sequence"]]
    assert names == ["root", "general-child"]

    all_pools = runner.invoke(cli, ["next", "--json", "--limit", "5", "--pool", "all"])
    assert all_pools.exit_code == 0, all_pools.output
    all_names = [s["task"]["name"] for s in _payload(all_pools.output)["sequence"]]
    assert "frontier-child" in all_names


def test_waiting_task_never_enters_the_cascade(tmp_path: Path, monkeypatch) -> None:
    """Tasks blocked on an external actor stay out, even when their deps complete."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "root2", state="todo", created="2026-03-28T00:00:00", priority="high")
    write_task(
        tasks_dir,
        "needs-erik",
        state="backlog",
        created="2026-03-28T01:00:00",
        priority="high",
        waiting_for="Erik review",
        requires=["root2"],
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli, ["next", "--json", "--limit", "5"])
    assert result.exit_code == 0, result.output
    payload = _payload(result.output)
    assert [s["task"]["name"] for s in payload["sequence"]] == ["root2"]


def test_reported_state_is_real_not_simulated(tmp_path: Path, monkeypatch) -> None:
    """Each step reports the task's actual state, not the simulated 'done'.

    The simulation marks picks as done to reveal what they unblock; leaking that
    into the output would tell the reader the work is already finished.
    """
    _chain_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli, ["next", "--json", "--limit", "3"])
    assert result.exit_code == 0, result.output
    states = {s["task"]["name"]: s["task"]["state"] for s in _payload(result.output)["sequence"]}

    assert states == {"chain-a": "todo", "chain-b": "backlog", "chain-c": "backlog"}
    assert "done" not in states.values()


def test_fan_in_attribution(tmp_path: Path, monkeypatch) -> None:
    """Fan-in task (C requires both A and B) is attributed to the last dependency.

    Attribution semantics: ``unblocked_by`` is the *final gate* — the last
    dependency whose simulated completion made the fan-in task newly ready.

    In this fixture fan-a (high) and fan-b (medium) are both ready initially.
    Priority ordering picks fan-a first, then fan-b. After fan-b completes
    (step 2), fan-c — which requires both — becomes newly ready and is
    attributed to fan-b (the last pick that opened the door).
    """
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    write_task(tasks_dir, "fan-a", state="todo", created="2026-03-28T00:00:00", priority="high")
    write_task(tasks_dir, "fan-b", state="todo", created="2026-03-28T01:00:00", priority="medium")
    write_task(
        tasks_dir,
        "fan-c",
        state="backlog",
        created="2026-03-28T02:00:00",
        priority="low",
        requires=["fan-a", "fan-b"],
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli, ["next", "--json", "--limit", "3"])
    assert result.exit_code == 0, result.output
    payload = _payload(result.output)
    seq = payload["sequence"]
    names = [s["task"]["name"] for s in seq]

    assert names == ["fan-a", "fan-b", "fan-c"], f"unexpected order: {names}"

    fan_c_step = next(s for s in seq if s["task"]["name"] == "fan-c")
    # fan-b is the final gate (picked second, at which point fan-c becomes ready)
    assert fan_c_step["unblocked_by"] == "fan-b"
    assert fan_c_step["unblocked_by_position"] == 2


def test_simulation_does_not_write_to_task_files(tmp_path: Path, monkeypatch) -> None:
    """The cascade is simulated in memory — task files must be untouched."""
    tasks_dir = _chain_fixture(tmp_path)
    before = {p.name: p.read_text() for p in tasks_dir.glob("*.md")}

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["next", "--json", "--limit", "3"])
    assert result.exit_code == 0, result.output

    after = {p.name: p.read_text() for p in tasks_dir.glob("*.md")}
    assert before == after
