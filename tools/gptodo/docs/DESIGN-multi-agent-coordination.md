---
title: Multi-Agent Task Coordination Design
version: 0.2.0
status: draft
tracking: https://github.com/ErikBjare/bob/issues/263
author: Bob
created: 2026-01-27
updated: 2026-01-27
---

# Multi-Agent Task Coordination Design

This document proposes **minimal extensions** to gptodo for coordinating work across multiple concurrent agent instances. The goal is to leverage gptodo's **existing robust infrastructure** and add only what's genuinely missing.

## Key Finding: gptodo Already Has Most of This

**Investigation completed 2026-01-27**: Before proposing new features, we analyzed gptodo's existing codebase and found comprehensive multi-agent coordination infrastructure already implemented:

### Already Implemented ✅

| Feature | Implementation | Location |
|---------|---------------|----------|
| **Dependency tracking** | `requires`, `blocks`, `waiting_for`, `related` fields | `utils.py` TaskInfo |
| **Auto-unblocking** | `auto_unblock_tasks()` - clears waiting_for when deps complete | `unblock.py` |
| **File-based locking** | `fcntl.flock()` atomic locks | `locks.py` |
| **Lock CLI** | `gptodo lock`, `unlock`, `locks` commands | `cli.py` |
| **Lock timeout** | Default 4 hours with auto-cleanup | `TaskLock` dataclass |
| **Ready detection** | `gptodo ready` - finds tasks with no unmet dependencies | `cli.py` |
| **Lock expiry** | `is_expired()`, `cleanup_expired_locks()` | `locks.py` |
| **Task state machine** | backlog → todo → active → waiting → done | `utils.py` |

### What We're Actually Adding

The genuine gaps are **much smaller** than initially proposed:

| Gap | Description | Complexity |
|-----|-------------|------------|
| Agent registry | Track which agents exist and their status | Small |
| Parallel hints | Frontmatter fields for parallelizable tasks | Small |
| Swarm orchestration | spawned_from/spawned_tasks for fan-out patterns | Medium |
| Enhanced `ready` | Show parallelization info in ready output | Small |

## Design Principles

1. **Leverage existing code**: Don't reinvent gptodo's locking/unblocking
2. **gptodo format superset**: Extend existing YAML frontmatter
3. **All fields optional**: Backward compatible with existing tasks
4. **File-based coordination**: No server required (NFS-compatible)

## Extended YAML Frontmatter

### New Fields Only

```yaml
---
# Existing gptodo fields (NO CHANGES)
state: active
created: 2026-01-27T10:00:00Z
priority: high
tags: [feature, api]
requires: [other-task]     # Already exists
blocks: [dependent-task]   # Already exists
waiting_for: some-thing    # Already exists

# NEW: Agent assignment (extends existing locking)
assigned_to: bob-session-abc123    # Which agent instance owns this
assigned_at: 2026-01-27T10:05:00Z  # When assignment started
lock_timeout_hours: 4              # Override default (existing field in locks)

# NEW: Parallel execution hints
parallelizable: true               # Can run concurrently with other work
isolation: worktree                # none, worktree, container
worktree_path: worktree/feature-x  # If using worktree isolation

# NEW: Swarm coordination
spawned_from: parent-task-id       # Parent task that spawned this
spawned_tasks: [sub-1, sub-2]      # Child tasks created
coordination_mode: parallel        # sequential, parallel, fan-out-fan-in
---
```

### Backward Compatibility

All new fields are optional:
- Missing `assigned_to` → task unassigned (use existing lock mechanism)
- Missing `parallelizable` → defaults to false (conservative)
- Missing `spawned_from` → standalone task

## Agent Status Registry

### Status Files

Location: `state/agents/{agent-id}.status`

```json
{
  "agent_id": "bob-session-abc123",
  "instance_type": "autonomous",
  "started": "2026-01-27T10:00:00Z",
  "last_heartbeat": "2026-01-27T10:15:00Z",
  "current_task": "implement-feature-x",
  "tasks_completed": 3,
  "status": "working",
  "workspace": "/home/bob/bob"
}
```

### Status Values
- `starting`: Agent initializing
- `idle`: Looking for work
- `working`: Actively on task
- `waiting`: Blocked on dependency
- `stopping`: Graceful shutdown

### Integration with Existing Locks

The agent registry **complements** existing task locks:
- **Task locks** (`state/locks/`) → Which task is being worked on
- **Agent status** (`state/agents/`) → Which agents exist and their status

```python
# Agent startup
def register_agent(agent_id: str, workspace: str):
    status_path = Path(f"state/agents/{agent_id}.status")
    status_path.write_text(json.dumps({
        "agent_id": agent_id,
        "started": datetime.utcnow().isoformat() + "Z",
        "status": "starting",
        "workspace": workspace,
    }))

# Uses EXISTING lock mechanism for task assignment
def claim_task(task_id: str, agent_id: str):
    # Use gptodo's existing acquire_lock!
    from gptodo.locks import acquire_lock
    return acquire_lock(task_id, agent_id)
```

## Swarm Orchestration

### Fan-Out Pattern (NEW)

When a task is too large, an agent spawns subtasks:

```yaml
# Parent task
---
state: active
title: Implement API endpoints
coordination_mode: fan-out-fan-in
spawned_tasks:
  - implement-auth-endpoint
  - implement-users-endpoint
---
```

```yaml
# Spawned subtask
---
state: todo
spawned_from: implement-api-endpoints
isolation: worktree
parallelizable: true
---
```

### Completion Flow (Leverages Existing Auto-Unblock)

When subtasks complete, gptodo's **existing** `auto_unblock_tasks()` handles propagation:

```python
def on_subtask_complete(subtask_id: str, parent_id: str):
    """Called when spawned subtask completes."""
    parent = load_task(parent_id)
    remaining = [t for t in parent.spawned_tasks
                 if load_task(t).state != "done"]

    if not remaining:
        # All subtasks done - parent can progress
        # This triggers existing auto-unblock for anything waiting on parent
        update_task_state(parent, "done")
```

## CLI Extensions

### New Commands

```shell
# Show agent status dashboard
gptodo agents
# Output:
# AGENT               STATUS    TASK                    UPTIME
# bob-session-abc123  working   implement-feature-x     2h 15m
# bob-session-def456  idle      -                       45m

# Spawn subtasks from parent
gptodo spawn <parent> --subtasks "task1,task2" --mode parallel

# Show coordination graph
gptodo graph --format mermaid
```

### Enhanced Existing Commands

```shell
# Ready command now shows parallelization info
gptodo ready --json
{
  "ready_tasks": [
    {
      "id": "feature-y",
      "priority": "high",
      "parallelizable": true,
      "isolation": "worktree"
    }
  ]
}

# Locks command (ALREADY EXISTS) continues to work
gptodo locks
# Shows existing lock state
```

## Implementation Phases

### Phase 1: Agent Registry (MVP)
- [ ] `state/agents/` directory and status files
- [ ] Agent heartbeat mechanism (using existing pattern from locks)
- [ ] `gptodo agents` command
- [ ] Cleanup stale agent registrations

### Phase 2: Frontmatter Extensions
- [ ] Add `parallelizable`, `isolation`, `worktree_path` to schema
- [ ] Update `gptodo ready` to include parallelization info
- [ ] Validate new fields in pre-commit

### Phase 3: Swarm Coordination ✅
- [x] Add `spawned_from`, `spawned_tasks`, `coordination_mode` (done in Phase 2)
- [x] `gptodo spawn` command
- [ ] Completion aggregation (fan-in) - future work
- [x] `gptodo graph` visualization

### Phase 4: OTel Observability (Optional)
- [ ] Span instrumentation for agent activity
- [ ] Metrics export (tasks active, locks held, etc.)

## What We're NOT Building

These were in the initial design but are **already provided by gptodo**:

| Feature | Why Not Needed |
|---------|----------------|
| Task locking mechanism | ✅ `locks.py` with `fcntl.flock` |
| Lock timeout/expiry | ✅ `TaskLock.is_expired()` |
| Lock CLI | ✅ `gptodo lock/unlock/locks` |
| Dependency tracking | ✅ `requires`, `blocks` fields |
| Auto-unblocking | ✅ `unblock.py` |
| Ready detection | ✅ `gptodo ready` |
| Task state machine | ✅ Established states |

## Worktree Isolation

For parallel work on same repo, use git worktrees:

```shell
# Agent creates isolated worktree
git worktree add worktree/feature-x -b feature-x origin/master

# Task metadata references it
worktree_path: worktree/feature-x
isolation: worktree

# On completion, create PR and cleanup
gh pr create --base master --head feature-x
git worktree remove worktree/feature-x
```

## Open Questions

1. **NFS locking**: Does `fcntl.flock` work reliably on NFS? May need atomic rename fallback.

2. **Heartbeat frequency**: 5 minutes? Balance freshness vs overhead.

3. **Spawn depth limit**: Limit fan-out depth to 2-3 levels?

4. **Cross-repo tasks**: How to coordinate across multiple repositories?

## References

- gptodo locks: `packages/gptodo/src/gptodo/locks.py`
- gptodo auto-unblock: `packages/gptodo/src/gptodo/unblock.py`
- gptodo TaskInfo: `packages/gptodo/src/gptodo/utils.py`
- [Issue #263](https://github.com/ErikBjare/bob/issues/263) - Feature discussion
