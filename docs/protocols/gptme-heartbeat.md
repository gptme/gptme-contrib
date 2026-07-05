# GPTME_HEARTBEAT Protocol

**Status**: Draft (v0.1)

`GPTME_HEARTBEAT` is an optional, runtime-agnostic protocol for making a
gptme-compatible agent orchestratable without forcing the agent to adopt a new
task system.

The smallest useful contract is:

- `invoke`: start one agent run with stable metadata.
- `status`: observe lifecycle state and recent events.
- `cancel`: request graceful termination, then escalate if needed.
- `heartbeat`: emit append-only status and cost events while the run is alive.

This is a control-plane adapter surface, not a replacement for an agent's own
task system, journals, issues, or coordination claims.

## Artifacts in this repo

- This spec: `docs/protocols/gptme-heartbeat.md`
- Event envelope JSON Schema: `schemas/gptme-heartbeat-event.schema.json`
- Validator CLI: `scripts/gptme-heartbeat-validate.py`
- Example event streams: `docs/protocols/examples/`

Validate an event stream with:

```bash
scripts/gptme-heartbeat-validate.py docs/protocols/examples/file-sink.jsonl
cat events.jsonl | scripts/gptme-heartbeat-validate.py -
```

## Non-Goals

- No company/org/task universe.
- No mandatory database.
- No mandatory HTTP service.
- No requirement that every runtime emits token costs.
- No attempt to standardize full conversation history or tool-call schemas.

## Compliance Levels

| Level | Requirement | Example implementation |
|-------|-------------|------------------------|
| 0 | Runs as a command | Existing `gptme` CLI invocation |
| 1 | Wrapper exposes `invoke`, `status`, `cancel` | Supervisor process tracks PID and exit code |
| 2 | Emits `GPTME_HEARTBEAT` events | JSONL file, fd, or HTTP endpoint |
| 3 | Emits cost events | Provider/model/tokens/cost attribution |

Level 1 is enough for orchestration. Level 2 gives live observability. Level 3
supports billing, quota pressure, and cost-aware scheduling.

## Invocation Metadata

The orchestrator starts a run with metadata. A CLI-only agent can receive these
as environment variables or command arguments; an HTTP adapter can receive the
same fields as JSON.

Required fields:

- `invocation_id`: stable ID for this run.
- `agent_id`: stable ID for the agent being invoked.
- `started_at`: RFC3339 timestamp assigned by the orchestrator.
- `command`: executable or adapter target.

Recommended fields:

- `session_id`: runtime session ID if already known.
- `task_id`: local task ID, GitHub issue key, or external work key.
- `run_reason`: short reason for invocation.
- `parent_invocation_id`: parent run if delegated.
- `workspace`: filesystem or remote workspace URI.
- `deadline_at`: hard timeout boundary, if any.
- `budget`: optional token/cost limits.

Example:

```json
{
  "invocation_id": "inv_20260609_2c05",
  "agent_id": "bob",
  "session_id": "2c05",
  "task_id": "gptme-heartbeat-protocol-spec",
  "started_at": "2026-06-09T01:32:32Z",
  "run_reason": "Draft GPTME_HEARTBEAT protocol spec",
  "workspace": "file:///home/bob/bob",
  "command": ["gptme", "--non-interactive"],
  "budget": {
    "max_usd": 5.0,
    "max_input_tokens": 200000,
    "max_output_tokens": 50000
  }
}
```

## Heartbeat Sink

The orchestrator advertises a sink with `GPTME_HEARTBEAT`.

Supported first-slice sink forms:

- `file:/abs/path/events.jsonl`
- `fd:3`
- `http://127.0.0.1:PORT/gptme/heartbeat`

Every event is one JSON object. File and fd sinks are JSONL. HTTP sinks receive
one JSON object per `POST`.

Agents that cannot emit events directly can be wrapped. The wrapper can emit
`invocation.started`, periodic `status`, and terminal events from process state
without modifying the underlying agent.

## Event Envelope

All events share this envelope (see
`schemas/gptme-heartbeat-event.schema.json`):

```json
{
  "protocol": "gptme-heartbeat",
  "version": "0.1",
  "event_id": "evt_01JY...",
  "invocation_id": "inv_20260609_2c05",
  "agent_id": "bob",
  "session_id": "2c05",
  "task_id": "gptme-heartbeat-protocol-spec",
  "type": "status",
  "occurred_at": "2026-06-09T01:34:00Z",
  "sequence": 4,
  "data": {}
}
```

Required envelope fields: `protocol`, `version`, `event_id`, `invocation_id`,
`agent_id`, `type`, `occurred_at`.

Envelope rules:

- `protocol` is always the literal `gptme-heartbeat`.
- `event_id` must be unique within the invocation.
- `sequence` starts at 1 and increments per invocation when possible.
- Consumers must tolerate duplicate events by `event_id`.
- Consumers must tolerate missing `session_id`, `task_id`, and `sequence`.
- Unknown fields are allowed and must be ignored by strict consumers.

## Lifecycle Events

### `invocation.started`

Emitted once when the run starts.

```json
{
  "type": "invocation.started",
  "data": {
    "pid": 12345,
    "cwd": "/home/bob/bob",
    "command": ["gptme", "--non-interactive"]
  }
}
```

### `status`

Emitted on material state change and optionally every 30-120 seconds while
running.

Valid `state` values:

- `queued`
- `starting`
- `running`
- `waiting`
- `cancelling`
- `succeeded`
- `failed`
- `cancelled`

```json
{
  "type": "status",
  "data": {
    "state": "running",
    "message": "drafting protocol spec",
    "progress": {
      "current_step": "write-spec",
      "completed_steps": 2,
      "total_steps": 4
    }
  }
}
```

`message` is operator-facing prose, not a machine contract. Consumers should
key off `state`.

### `cost`

Optional. Emitted when provider usage is known. A runtime may emit multiple
partial cost events and one final aggregate.

```json
{
  "type": "cost",
  "data": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-5-20250929",
    "input_tokens": 1234,
    "output_tokens": 567,
    "cached_input_tokens": 1000,
    "reasoning_tokens": 0,
    "cost_usd": 0.089,
    "currency": "USD",
    "billing_scope": "task",
    "is_final": false
  }
}
```

Required cost fields: `provider`, `model`.

Recommended cost fields: `input_tokens`, `output_tokens`, `cost_usd`,
`currency`, `is_final`.

Cost events are attribution records. They should not be used as the sole source
of budget enforcement unless the runtime guarantees timely emission.

### `cancellation.requested`

Emitted by the orchestrator or wrapper when cancellation starts.

```json
{
  "type": "cancellation.requested",
  "data": {
    "reason": "deadline_exceeded",
    "requested_by": "orchestrator",
    "grace_seconds": 30
  }
}
```

### `invocation.finished`

Emitted once when the invocation reaches a terminal state.

```json
{
  "type": "invocation.finished",
  "data": {
    "state": "succeeded",
    "exit_code": 0,
    "duration_ms": 612000,
    "artifact_refs": [
      "file:///home/bob/bob/knowledge/technical-designs/gptme-heartbeat-protocol.md"
    ]
  }
}
```

Terminal states are `succeeded`, `failed`, and `cancelled`.

## Status API

The status API can be implemented by reading the latest event stream state. It
does not need to be a separate server.

Minimal response:

```json
{
  "invocation_id": "inv_20260609_2c05",
  "agent_id": "bob",
  "state": "running",
  "last_event_at": "2026-06-09T01:34:00Z",
  "last_message": "drafting protocol spec"
}
```

A richer adapter may include recent events, cost totals, artifact refs, and
process metadata.

## Cancellation Semantics

Cancellation is best-effort but must be explicit.

For process-backed agents:

1. Emit `cancellation.requested`.
2. Send graceful interrupt (`SIGINT` or runtime-specific cancel command).
3. Emit `status` with `state: "cancelling"`.
4. Wait `grace_seconds`.
5. Escalate to `SIGTERM`, then `SIGKILL` if configured.
6. Emit `invocation.finished` with `state: "cancelled"` or `failed`.

For HTTP or remote adapters:

1. Send adapter-specific cancel request.
2. Continue polling `status`.
3. Treat timeout as `failed` unless the adapter confirms `cancelled`.

Consumers must not assume cancellation means rollback. If a run mutates files,
issues, or external systems before cancellation, those mutations remain real
unless the runtime emits explicit rollback artifacts.

## Timeout Semantics

`deadline_at` is a hard orchestration boundary. The runtime may also receive a
soft timeout.

Recommended behavior:

- 80% of deadline: emit `status` warning.
- deadline reached: request cancellation.
- cancellation grace expired: escalate.

This mirrors the same shape as budget soft/hard stops without forcing budget
logic into the heartbeat protocol.

## Plain CLI Wrapper Compliance

A plain command can comply without code changes:

```bash
GPTME_HEARTBEAT="file:/tmp/gptme-heartbeat.jsonl" \
gptme-heartbeat-wrapper \
  --invocation-id inv_20260609_2c05 \
  --agent-id bob \
  --task-id gptme-heartbeat-protocol-spec \
  -- gptme --non-interactive "draft the heartbeat spec"
```

The wrapper owns:

- generating `invocation.started`
- polling process liveness into `status`
- mapping exit code to `invocation.finished`
- handling cancellation signals

The underlying CLI can remain oblivious. If the CLI later emits native cost or
tool events, the wrapper can pass them through with the same envelope.

## Open Questions

- Should `cost` use integer microdollars instead of decimal `cost_usd` for
  exact billing math?
- Should a future version reserve event types for tool execution, model
  streaming, and artifact creation, or should those stay separate protocols?
- Should `GPTME_HEARTBEAT` allow multiple comma-separated sinks, or should
  fanout stay in the wrapper?

## Provenance

This protocol originated as a design draft in the Bob agent workspace as a
control-plane adapter for orchestrating gptme-compatible runs. This document is
the public, canonical version; the validator and schema in this repo are the
first implementation slice.
