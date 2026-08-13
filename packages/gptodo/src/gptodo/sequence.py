"""Greedy sequential simulation of the task queue ("what's next, then next").

`gptodo ready` and `gptodo next` answer a *parallel* question: which tasks are
unblocked **right now**. If task A unblocks task B, B is invisible until A is
actually completed — so the queue can look one task deep when it is really five.

This module answers the *sequential* question instead: "if I worked these N in
order, what would the sequence be, including the tasks that only become
available partway down?"

The simulation is entirely in memory — task files are never written.

Dependency resolution is **not** reimplemented here: readiness comes from
:func:`gptodo.utils.is_task_ready` and unblocking power from
:func:`gptodo.deptree.compute_unblocking_power`, exactly the primitives the
single-pick `next` already uses.
"""

from dataclasses import dataclass, replace
from typing import Any, Dict, List, Literal, Optional, Sequence

from .deptree import build_dependency_graph, compute_unblocking_power
from .utils import TaskInfo, is_task_ready, task_to_dict

Order = Literal["priority", "unblock"]

#: Simulated completion state. Matches the terminal states `is_task_ready`
#: treats as satisfying a dependency.
SIMULATED_DONE_STATE = "done"


@dataclass
class SequenceStep:
    """One task in the simulated sequence, with the reason it is there."""

    task: TaskInfo
    position: int
    #: Name of the step whose (simulated) completion made this task newly ready,
    #: or None if it was already ready at the start.
    #:
    #: **Fan-in tasks** (requiring multiple dependencies): this is the *last*
    #: dependency to complete — the "final gate." Prior deps were already done
    #: in earlier steps; this pick was the one that opened the door. This
    #: matches how the single-pick ``next`` models readiness: a task is either
    #: ready or it isn't, and the relevant event is when it first becomes ready.
    unblocked_by: Optional[str] = None
    #: 1-based position of that earlier step, or None.
    unblocked_by_position: Optional[int] = None

    @property
    def reason(self) -> str:
        """Human-readable explanation of why this task is in the sequence."""
        if self.unblocked_by is None:
            return "already ready"
        return f"unblocked by #{self.unblocked_by_position} {self.unblocked_by}"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for --json consumption."""
        return {
            "position": self.position,
            "task": task_to_dict(self.task),
            "unblocked_by": self.unblocked_by,
            "unblocked_by_position": self.unblocked_by_position,
            "reason": self.reason,
        }


def _sort_key_priority(power: Dict[str, int]):
    """Existing `next` ordering: priority, then unblocking power, then age.

    Kept byte-identical to the single-pick ordering so `--limit 1` cannot drift.
    """

    def key(task: TaskInfo):
        return (-task.priority_rank, -power.get(task.name, 0), task.created)

    return key


def _sort_key_unblock(power: Dict[str, int]):
    """Critical-path ordering: unblocking power first, then priority, then age."""

    def key(task: TaskInfo):
        return (-power.get(task.name, 0), -task.priority_rank, task.created)

    return key


def simulate_sequence(
    candidates: Sequence[TaskInfo],
    tasks_dict: Dict[str, TaskInfo],
    all_tasks: Sequence[TaskInfo],
    limit: int = 1,
    order: Order = "priority",
    issue_cache: Optional[Dict[str, Any]] = None,
) -> List[SequenceStep]:
    """Greedily simulate completing tasks one at a time.

    At each step: compute the ready set, pick the top task by ``order``,
    simulate its completion in memory, then recompute — so tasks that were
    blocked at the start surface once their blocker has been picked.

    Args:
        candidates: Pool/state-filtered tasks eligible to be picked. The same
            filters apply at every step, not just the first pick.
        tasks_dict: Full name -> task lookup used for dependency resolution
            (must include tasks outside ``candidates``, e.g. already-done deps).
        all_tasks: Full task list, used to build the dependency graph that
            unblocking-power scoring is computed from.
        limit: Maximum number of tasks to pick.
        order: "priority" (default) or "unblock".
        issue_cache: Optional URL-state cache for URL-based `requires`.

    Returns:
        Ordered list of :class:`SequenceStep`. May be shorter than ``limit``
        when nothing further is unblocked (a dependency cycle, an external
        blocker, or simply an exhausted queue). Callers must surface that.
    """
    if limit < 1:
        return []

    # Simulated view of the world. Never written back to disk.
    sim_tasks: Dict[str, TaskInfo] = dict(tasks_dict)
    remaining: List[TaskInfo] = list(candidates)

    picked: List[SequenceStep] = []
    # name -> (blocker name, blocker position); populated the moment a task
    # first becomes ready, so the attribution survives until it is picked.
    attribution: Dict[str, tuple[str, int]] = {}
    ever_ready: set[str] = set()

    # Unblocking power is computed once, against the *original* graph, for two
    # reasons. Correctness: "how much downstream work does this unlock" is a
    # property of the real graph, and the single-pick `next` computes it exactly
    # this way — so step 1 cannot drift from current behaviour. Cost:
    # compute_unblocking_power is O(V*E), and recomputing it per step made a
    # 500-deep chain at --limit 100 take 11s instead of 0.2s.
    power = compute_unblocking_power(build_dependency_graph(list(all_tasks)))
    sort_key = _sort_key_unblock(power) if order == "unblock" else _sort_key_priority(power)

    # Hard iteration bound: each successful step removes exactly one candidate,
    # and we break when nothing is ready. The bound is belt-and-braces so a
    # dependency cycle (or any future readiness bug) can never spin forever.
    for _ in range(len(remaining)):
        if len(picked) >= limit:
            break

        ready = [t for t in remaining if is_task_ready(sim_tasks[t.name], sim_tasks, issue_cache)]

        # Attribute anything newly ready to the step that just completed.
        for task in ready:
            if task.name not in ever_ready:
                ever_ready.add(task.name)
                if picked:
                    prev = picked[-1]
                    attribution[task.name] = (prev.task.name, prev.position)

        if not ready:
            break

        ready.sort(key=sort_key)

        chosen = ready[0]
        position = len(picked) + 1
        credit = attribution.pop(chosen.name, None)
        picked.append(
            SequenceStep(
                task=sim_tasks[chosen.name],
                position=position,
                unblocked_by=credit[0] if credit else None,
                unblocked_by_position=credit[1] if credit else None,
            )
        )

        remaining = [t for t in remaining if t.name != chosen.name]
        # Simulate completion: `is_task_ready` treats done/cancelled deps as met.
        sim_tasks[chosen.name] = replace(sim_tasks[chosen.name], state=SIMULATED_DONE_STATE)

    return picked


def shortfall_note(reachable: int, requested: int) -> Optional[str]:
    """Explain a short sequence, or None when the full request was satisfied.

    A short list that looks complete is the failure mode this guards against.
    """
    if reachable >= requested:
        return None
    return (
        f"only {reachable} of {requested} reachable — nothing further is unblocked by this sequence"
    )
