# gptme-coordination

Serverless coordination for multiple agents (or parallel sessions of one agent)
sharing a machine or filesystem: atomic work claims so two agents don't grab the
same task, an append-only message bus, a TTL'd fact bus, and a durable event
queue — all in one SQLite file, no server.

**Status:** experimental (`0.1.0`, alpha), stdlib-only. Used to coordinate
parallel autonomous agent sessions; the schema and CLI may still change.

## Why

Once you run more than one agent session at a time, they start duplicating work
and clobbering each other. This package gives them cheap shared primitives:

- **Work claims** — compare-and-swap task claiming with TTL expiry, optional
  HMAC-signed claimer identity, and a guard that refuses to complete an expired
  claim.
- **Message bus** — append-only targeted or broadcast messages on named
  channels, optionally HMAC-signed.
- **Fact bus** — read-many, TTL-keyed facts ("is PR #123 merged?") so a session
  can check whether a sibling already answered a question before spending
  budget on it.
- **Event queue** — prioritised, deduplicated (by `thread_key`) event queue with
  retries and dead-lettering, to decouple trigger detection (CI failures, PR
  updates) from session dispatch.

It complements, rather than replaces, a task tracker such as
[gptodo](../gptodo/README.md) (which owns task state) and
[gptmail](../gptmail/README.md) (email and cross-machine agent messaging):
coordination is the local "who is working on what right now" layer. Bring your
own task system if you prefer — claim keys are arbitrary strings.

## Install

Not published to PyPI. Install from the repository subdirectory:

```bash
uv tool install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-coordination"
# or as a library
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-coordination"
```

In a gptme-contrib checkout it is a uv workspace member
(`uv sync --all-packages`).

## Quickstart

```python
from gptme_coordination import CoordinationDB, WorkClaimManager

with CoordinationDB("coord.db") as db:
    work = WorkClaimManager(db)
    claim = work.claim("alice", "task-123", ttl_minutes=60)
    if claim:
        # ... do the work ...
        work.complete("alice", "task-123", result="done")
    else:
        print("another agent already has it")
```

Same thing from the shell:

```bash
gptme-coordination work-claim alice task-123 --ttl 60 || echo "taken"
gptme-coordination work-complete alice task-123 --result "shipped"
```

## Where the database lives

`CoordinationDB()` with no path (and the CLI without `--db`) resolves, in order:

1. `$COORDINATION_DB`
2. `$AGENT_WORKSPACE/state/coordination/coord.db` (absolute paths only)
3. `<git root of cwd>/state/coordination/coord.db`
4. `<cwd>/state/coordination/coord.db`

`resolve_coordination_db_path()` exposes the same logic. The DB uses WAL mode
with a 5 s busy timeout, so concurrent processes are safe.

## CLI

Global option: `--db PATH`.

| Command | Purpose |
|---------|---------|
| `work-submit TASK_ID [--metadata TEXT]` | Register a task as available |
| `work-claim AGENT_ID TASK_ID [--ttl MIN]` | Claim (exit 1 and print the holder if denied); TTL default 60 min |
| `work-complete AGENT_ID TASK_ID [--result TEXT]` | Complete an active, unexpired claim |
| `work-abandon AGENT_ID TASK_ID [--reason TEXT]` | Release a claim |
| `work-list [--agent ID] [--available \| --claimed]` | List work items |
| `send AGENT_ID BODY [--to RECIPIENT] [--channel NAME]` | Send a message (no `--to` = broadcast; channel default `general`) |
| `inbox AGENT_ID` | Read messages for an agent |
| `announce AGENT_ID` | Announce presence |
| `fact-publish KEY VALUE [--session ID] [--ttl MIN]` | Publish/overwrite a fact (TTL default 60 min) |
| `fact-query KEY` | Read a non-expired fact |
| `fact-list [--prefix P]` | List non-expired facts |
| `queue-ingest TRIGGER_TYPE SOURCE THREAD_KEY [...]` | Add an event (`--repo`, `--number`, `--title`, `--url`, `--payload JSON`, `--external-id`, `--priority`, `--dedup-window MIN`) |
| `queue-list [--state S] [--repo R] [--limit N]` | List events (`pending`, `claimed`, `completed`, `dead_letter`) |
| `queue-stats` | Queue statistics |
| `queue-retry EVENT_ID` | Re-queue a dead-lettered event |
| `queue-discard EVENT_ID` | Discard any not-yet-completed event (marks it completed with result `discarded`) |
| `status` | Active claims and recent announcements |

## Python API

Exported from `gptme_coordination`: `CoordinationDB`,
`resolve_coordination_db_path`, `WorkClaimManager` / `WorkClaim`,
`MessageBus` / `Message`, `FactBus` / `Fact`, `EventQueue` / `Event` /
`QueueStats`.

```python
from gptme_coordination import CoordinationDB, EventQueue, FactBus, MessageBus

with CoordinationDB() as db:
    FactBus(db).publish("github:owner/repo#123:merged", "true", ttl_minutes=30)
    MessageBus(db).send("alice", "starting release", channel="announce")

    q = EventQueue(db)
    event = q.claim_next("worker-1")      # highest priority first, aged events boosted
    if event:
        q.complete(event.id, result="success")
```

### Authenticated identity (optional)

`WorkClaimManager.claim(..., secret=...)` and `MessageBus.send(..., secret=...)`
store an HMAC so a forged `agent_id` is detectable. Secrets are resolved by
`gptme_coordination.auth.resolve_secret(agent_id)` from
`COORDINATION_SECRET_<AGENT_ID>` (uppercased, non-alphanumerics → `_`), then
`<agent_id>.secret` in `$COORDINATION_SECRETS_DIR` or
`<git root>/secrets/coordination/`. The CLI does not sign; without a secret the
HMAC column stays empty.

### Completed-task reclaim policy

By default a completed task can be claimed again. Agents with their own task
state can veto that with a callback that receives `(task_id, db_path)` and returns
`True` to allow the reclaim, `False` to deny it:

```python
WorkClaimManager(db, on_completed_check=lambda task_id, db_path: was_reopened(task_id))
```

### Worktree guard

`gptme_coordination.worktree_guard` provides `run_guard()` (post-commit
occupancy marker) and `run_push_guard()` (pre-push branch auto-claim) for
agents that share git worktrees under `/tmp/worktrees/`. Both fail open, warn by
default, and log to `state/coordination/worktree-guard.jsonl`; set
`AGENT_WORKTREE_PUSH_GUARD_DENY=1` to block pushes over a live sibling's branch
claim. No hook scripts are shipped — call these from your own git hooks.
