---
title: Multi-Agent Task Coordination Design
version: 0.1.0
status: draft
tracking: https://github.com/ErikBjare/bob/issues/263
author: Bob
created: 2026-01-27
---

# Multi-Agent Task Coordination Design

This document proposes extensions to gptodo for coordinating work across multiple concurrent agent instances, inspired by Claude Code's swarm orchestration approach.

## Overview

**Problem**: Multiple agents working on the same codebase need coordination to:
- Avoid duplicate work on the same task
- Enable parallel execution of independent tasks
- Provide real-time visibility into agent status
- Enable auto-unblocking when dependencies complete

**Solution**: File-based coordination using gptodo's existing YAML frontmatter format, extended with agent assignment and locking metadata.

## Design Principles

1. **File-based over server**: All coordination state lives in files (git-trackable, NFS-compatible)
2. **gptodo format superset**: Extend existing YAML frontmatter, don't invent new format
3. **Lock-free where possible**: Use atomic file operations, prefer optimistic coordination
4. **Observable**: OTel integration for visibility into agent activity

## Task Format Extensions

### Extended YAML Frontmatter

```yaml
---
# Existing gptodo fields (unchanged)
state: active             # backlog, todo, active, waiting, done, cancelled
created: 2026-01-27T10:00:00Z
priority: high            # high, medium, low
tags: [feature, api]
requires: [other-task]    # Blocking dependencies

# NEW: Agent coordination fields
assigned_to: bob-session-abc123    # Which agent instance owns this task
assigned_at: 2026-01-27T10:05:00Z  # When assignment started
lock_timeout_hours: 4              # Auto-release if no heartbeat

# NEW: Parallel execution hints
parallelizable: true               # Can run concurrently with other work
isolation: worktree                # none, worktree, container (how to isolate)
worktree_path: worktree/feature-x  # If using worktree isolation

# NEW: Swarm coordination
spawned_from: parent-task-id       # Parent task that spawned this subtask
spawned_tasks: [subtask-1, subtask-2]  # Child tasks created by this task
coordination_mode: sequential     # sequential, parallel, fan-out-fan-in
---
```

### Backward Compatibility

All new fields are optional. Existing tasks work unchanged:
- Missing `assigned_to` → task is unassigned
- Missing `parallelizable` → defaults to false (conservative)
- Missing `isolation` → defaults to none

## File-Based Locking

### Lock Files (existing gptodo pattern)

Location: `state/locks/{task-id}.lock`

```json
{
  "task_id": "implement-feature-x",
  "worker": "bob-session-abc123",
  "started": "2026-01-27T10:05:00Z",
  "timeout_hours": 4,
  "heartbeat": "2026-01-27T10:15:00Z",
  "pid": 12345
}
```

### Lock Lifecycle
1. **Acquire**: Agent attempts to create lock file atomically
2. **Heartbeat**: Agent updates `heartbeat` timestamp periodically (every 5 min)
3. **Release**: Agent deletes lock file on task completion
4. **Timeout**: Stale locks (no heartbeat > timeout) are auto-released

### Atomic Lock Acquisition

Using `fcntl.flock()` for cross-process safety (already in gptodo):

```python
def acquire_lock(task_id: str, worker: str, timeout_hours: float = 4) -> bool:
    """Attempt to acquire lock atomically."""
    lock_path = Path(f"state/locks/{task_id}.lock")
    with _atomic_lock_file(lock_path, write=True) as (existing, path):
        if existing:
            # Check if lock is stale
            heartbeat = datetime.fromisoformat(existing["heartbeat"])
            if datetime.utcnow() - heartbeat < timedelta(hours=existing["timeout_hours"]):
                return False  # Lock held by another worker
        # Write new lock
        lock_data = {
            "task_id": task_id,
            "worker": worker,
            "started": datetime.utcnow().isoformat() + "Z",
            "timeout_hours": timeout_hours,
            "heartbeat": datetime.utcnow().isoformat() + "Z",
        }
        path.write_text(json.dumps(lock_data, indent=2))
        return True
```

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
  "workspace": "/home/bob/bob",
  "worktree": "worktree/feature-x"
}
```

### Status Values
- `starting`: Agent initializing
- `idle`: No task assigned, looking for work
- `working`: Actively working on task
- `waiting`: Blocked on dependency/review
- `stopping`: Graceful shutdown in progress

## Swarm Orchestration

### Spawning Subtasks

When a task is too large, an agent can spawn parallel subtasks:

```yaml
---
state: active
title: Implement API endpoints
coordination_mode: fan-out-fan-in
spawned_tasks:
  - implement-auth-endpoint
  - implement-users-endpoint
  - implement-items-endpoint
---
```

Each spawned task:
```yaml
---
state: todo
spawned_from: implement-api-endpoints
isolation: worktree
---
```

### Fan-Out-Fan-In Pattern
1. **Fan-Out**: Primary agent decomposes task into subtasks
2. **Parallel Execution**: Multiple agents work on subtasks concurrently
3. **Completion Tracking**: Parent monitors spawned_tasks for completion
4. **Fan-In**: When all subtasks done → parent task auto-progresses
5. **Result Aggregation**: Parent collects outputs from all subtasks

### Sequential vs Parallel Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| `sequential` | Tasks execute one after another | Dependent changes |
| `parallel` | Tasks execute simultaneously | Independent features |
| `fan-out-fan-in` | Decompose, parallel execute, aggregate | Large features |

## Worktree Isolation

For parallel work on same repo, use git worktrees:

```shell
# Agent creates isolated worktree for task
git worktree add worktree/feature-x -b feature-x origin/master

# Task metadata references worktree
worktree_path: worktree/feature-x
isolation: worktree

# On completion, agent creates PR and cleans up
gh pr create --base master --head feature-x
git worktree remove worktree/feature-x
```

**Benefits**:
- Multiple agents work on same repo without conflicts
- Each task has isolated branch/directory
- Natural PR workflow for review
- Clean separation of concerns

## Auto-Unblocking Flow

When a task completes, gptodo's existing auto-unblock triggers:

```python
def on_task_complete(task_id: str):
    """Called when task state → done."""
    # 1. Find dependent tasks
    dependents = find_dependent_tasks(task_id, all_tasks)

    # 2. Check if now unblocked
    for task in dependents:
        if is_task_ready(task, all_tasks):
            # Clear waiting_for if it pointed to completed task
            if task.metadata.get("waiting_for") == task_id:
                clear_waiting_for(task)
            # Optionally: Update state from waiting → todo
            if task.state == "waiting":
                update_task_state(task.path, "todo")
            print(f"✅ Unblocked: {task.name}")
```

## OTel Observability Integration

### Spans for Agent Activity

```python
with tracer.start_as_current_span("agent.task.execute") as span:
    span.set_attribute("task.id", task_id)
    span.set_attribute("agent.id", agent_id)
    span.set_attribute("task.state", task.state)
    span.set_attribute("task.priority", task.priority)

    # Work execution...

    span.set_attribute("task.outcome", "completed")
```

### Key Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `gptodo.tasks.active` | Gauge | Currently active tasks |
| `gptodo.agents.active` | Gauge | Currently running agents |
| `gptodo.locks.held` | Gauge | Currently held locks |
| `gptodo.tasks.completed` | Counter | Total tasks completed |
| `gptodo.coordination.conflicts` | Counter | Lock acquisition failures |

### Trace Attributes

Standard attributes for all gptodo spans:
- `task.id`: Task identifier
- `task.state`: Current state
- `agent.id`: Agent instance identifier
- `agent.type`: Agent type (bob, alice, etc.)
- `coordination.mode`: sequential/parallel/fan-out
- `isolation.type`: none/worktree/container

## CLI Extensions

### New Commands

```shell
# Show agent status dashboard
gptodo agents

# Assign task to specific agent
gptodo assign <task> --to <agent-id>

# Spawn subtasks from parent
gptodo spawn <parent-task> --subtasks "task1,task2,task3" --mode parallel

# Check what's blocking a task
gptodo blocking <task>

# Force release stale lock
gptodo unlock <task> --force

# Show coordination graph
gptodo graph --format mermaid
```

### Enhanced `ready` Command

```shell
# Show ready tasks with agent assignment info
gptodo ready --json
{
  "ready_tasks": [
    {
      "id": "implement-feature-y",
      "priority": "high",
      "assigned_to": null,
      "parallelizable": true,
      "isolation": "worktree"
    }
  ],
  "blocked_tasks": [
    {
      "id": "integrate-features",
      "blocked_by": ["implement-feature-x", "implement-feature-y"]
    }
  ]
}
```

## Implementation Phases

### Phase 1: Core Coordination (MVP)
- [ ] Extended YAML frontmatter schema
- [ ] Agent registration and heartbeat
- [ ] Lock timeout and auto-release
- [ ] Basic `gptodo agents` command

### Phase 2: Parallel Execution
- [ ] Worktree isolation support
- [ ] `parallelizable` field handling
- [ ] `gptodo assign` command
- [ ] Conflict detection and reporting

### Phase 3: Swarm Orchestration
- [ ] `spawned_from` / `spawned_tasks` tracking
- [ ] `gptodo spawn` command
- [ ] Fan-out-fan-in coordination
- [ ] Completion aggregation

### Phase 4: Observability
- [ ] OTel span instrumentation
- [ ] Metrics export
- [ ] Coordination dashboard
- [ ] Alert on stale locks/stuck agents

## Comparison with Claude Code Swarm

| Feature | Claude Code Swarm | gptodo Multi-Agent |
|---------|------------------|-------------------|
| Coordination | Server-based | File-based |
| Lock Storage | In-memory | `state/locks/` files |
| Agent Registry | Central server | `state/agents/` files |
| Task Format | Custom JSON | YAML frontmatter (gptodo) |
| Observability | Proprietary | OpenTelemetry |
| Git Integration | Via tools | Native (worktrees) |

**Key Difference**: gptodo uses file-based coordination that works across NFS, enables git tracking of state, and requires no central server.

## Open Questions

1. **NFS locking**: Does fcntl.flock work reliably on NFS? May need fallback to atomic rename pattern.

2. **Heartbeat frequency**: 5 minutes reasonable? Balance between freshness and overhead.

3. **Spawn depth**: Should we limit how deep task spawning can go? (2-3 levels?)

4. **Cross-repo coordination**: How to handle tasks spanning multiple repositories?

5. **Agent discovery**: How do agents find each other? Shared state directory?

## References

- [Claude Code Swarm Orchestration](https://github.com/cline/cline) - Inspiration for patterns
- gptodo existing locking: `packages/gptodo/src/gptodo/locks.py`
- gptodo auto-unblock: `packages/gptodo/src/gptodo/unblock.py`
- [Issue #263](https://github.com/ErikBjare/bob/issues/263) - Feature discussion
