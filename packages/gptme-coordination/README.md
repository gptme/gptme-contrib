# gptme-coordination

Generic inter-agent coordination via SQLite: atomic work claims and append-only messaging.

## Features

- **Work claims** — CAS-based task claiming with HMAC authentication and TTL expiry
- **Message bus** — append-only targeted and broadcast messaging between agents
- **SQLite backend** — WAL mode, concurrent-safe, no server required
- **`COORDINATION_DB` env var** — override the DB path for shared mounts

## Install

```bash
pip install gptme-coordination
# or in a uv workspace: add it as a dependency
```

## Quick Start

```python
from gptme_coordination import CoordinationDB, WorkClaimManager

with CoordinationDB("coord.db") as db:
    work = WorkClaimManager(db)
    claim = work.claim("alice", "task-123")
    if claim:
        # ... do the work ...
        work.complete("alice", "task-123", result="done")
    else:
        print("another agent already has it")
```

## CLI

```bash
gptme-coordination work-claim alice task-123
gptme-coordination work-complete alice task-123 --result "shipped"
gptme-coordination inbox alice
gptme-coordination status
```

## Extending

Agent-specific packages can inject a callback to control whether completed
tasks may be reclaimed:

```python
WorkClaimManager(db, on_completed_check=lambda task_id, db_path: my_check(task_id))
```

When `on_completed_check` is omitted (default), completed tasks are always
reclaimable — suitable for agents without workspace-level task state.
