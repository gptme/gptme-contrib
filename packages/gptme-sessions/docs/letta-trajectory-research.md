# Letta trajectory-v1 — research note

**Issue**: gptme/gptme-contrib#1924
**Date**: 2026-10-18
**Status**: Prototype shipped (`gptme_sessions.letta`, 25 passing tests)

---

## 1. What fields does Letta's ingest expect?

Letta trajectory-v1 is a **flat JSON array** of typed records. The schema
(`trajectory-v1.schema.json`, [letta-ai/trajectory]) enforces
`additionalProperties: false` on every record type.

### Record types and required fields

| role | Required fields | Optional fields |
|---|---|---|
| `meta` | `role`, `source` | `cwd`, `git_branch`, `model` |
| `user` | `role`, `content`, `timestamp` | — |
| `system` | `role`, `content`, `timestamp` | — |
| `reasoning` | `role`, `content`, `timestamp` | — |
| `assistant` | `role`, `content` (null when tool_calls present), `timestamp` | `tool_calls` |
| `tool` | `role`, `tool_call_id`, `content`, `timestamp` | `ok` |

### Tool call encoding

```json
{
  "role": "assistant",
  "content": null,
  "tool_calls": [{"id": "call_1", "name": "bash", "args": "{\"command\": \"ls\"}"}],
  "timestamp": "2026-10-18T10:00:00.000Z"
}
```

`args` is a **JSON-encoded string** (not a dict). The schema constraint on
`assistant`: when `tool_calls` is present, `content` must be `null`; otherwise
`content` must be a non-empty string.

### Tool result encoding

```json
{"role": "tool", "tool_call_id": "call_1", "content": "file.txt", "ok": true, "timestamp": "..."}
```

`ok` is present only when the source exposes a reliable structured status
(maps to `not is_error` in NormalizedMessage). When the source does not expose
status, `ok` is omitted — **never inferred from result text**.

### Metrics

None. The format deliberately omits per-step token counts and costs. It
targets agent consumption (memory formation, search, dreaming), not
benchmarking or RL pipeline training. ATIF v1.7 carries metrics; Letta does
not. This is by design.

### Timestamps

Required on all non-meta records (ISO 8601 with timezone). gptme
`conversation.jsonl` messages often omit timestamps. The adapter substitutes
`1970-01-01T00:00:00.000Z` with a diagnostic, or drops the record when
`drop_missing_timestamps=True`.

---

## 2. Is there a shared envelope? Do we need a second adapter?

**Short answer**: No new adapter is strictly required — Letta already ingests
ATIF. A direct adapter is still valuable for efficiency and for bypassing ATIF
grouping overhead.

### The existing path

Letta's `normalizeTranscript()` (TypeScript, Python wrapper `agent-trajectory`)
includes an `atif` source adapter that accepts **ATIF-v1.0 through ATIF-v1.7**.
The existing `session_transcript_to_atif()` from PR #1916 produces ATIF-v1.7,
which Letta can ingest directly:

```
gptme-sessions → ATIF v1.7 (existing) → Letta atif adapter → trajectory-v1
```

No new code was strictly required for Letta compatibility.

### Why a direct adapter still makes sense

1. **Flat vs grouped**: ATIF groups consecutive tool calls from the same
   assistant turn into one step, with accumulation logic. Letta's format is
   flat — one record per NormalizedMessage. The direct adapter is simpler and
   produces a one-to-one mapping.
2. **No metrics overhead**: ATIF accumulates per-step token counts; Letta
   drops them. The direct adapter skips that work.
3. **Compactness**: The Letta format is ~5x smaller than ATIF for the same
   session (Letta reports a 5x token reduction on Claude Code sessions when
   feeding to an LLM).
4. **OpenTelemetry**: Neither ATIF nor Letta uses OTel spans. They share no
   common envelope. OTel is not a viable intermediate format here.

### Conclusion

There is no shared intermediate envelope that serves both targets. The
practical strategy is:

- **ATIF** → benchmarking, RL pipelines, Pier, DeepSWE, Terminal-Bench
- **Letta trajectory-v1** → agent memory, dreaming, search
- **ATIF → Letta** via Letta's built-in adapter: works today, zero new code
- **Direct gptme → Letta**: this PR — simpler code path, useful for direct
  integration or when ATIF grouping is undesirable

---

## 3. Effort to add `normalize_gptme_to_letta()` alongside ATIF

**Actual effort**: ~1.5h including schema research, implementation, and tests.

### What was built

`packages/gptme-sessions/src/gptme_sessions/letta.py` — `session_transcript_to_letta()`:
- Input: `SessionTranscript` (same as ATIF adapter)
- Output: `(list[dict], list[str])` — records + diagnostics
- Handles all harnesses (gptme, claude-code, codex, copilot, grok, pi) via
  the normalized `NormalizedMessage` layer
- Returns diagnostics list (same pattern as `normalizeTranscript()` in the
  Letta TypeScript package)
- 25 unit tests, all passing

### Remaining gaps

**Same as ATIF** (gptme/gptme-contrib#1922): gptme `conversation.jsonl` does
not yet parse tool calls out of message content. Until #1922 lands:
- gptme sessions → text-only assistant records, no `tool_calls`
- Claude Code, Codex, Copilot, Grok, Pi → full tool call fidelity

**Timestamp gap**: gptme sessions frequently have no per-message timestamps.
The adapter handles this gracefully (placeholder + diagnostic), but the Letta
schema technically requires timestamps on all non-meta records. Sessions from
harnesses that do record timestamps (CC, Codex, Grok) are fully schema-valid.

### Not in scope (deliberate)

- `listTrajectories()` equivalent — not needed for export
- Validation against `trajectory-v1.schema.json` at runtime — call Letta's
  own `normalizeTranscript()` for validation
- CLI integration — can follow in a separate PR if gptme-sessions adds a
  `--format letta` flag to the export command

---

## References

- [letta-ai/trajectory](https://github.com/letta-ai/trajectory) — schema,
  adapters, `trajectory-v1.schema.json`
- [Letta blog: Trajectory](https://www.letta.com/blog/trajectory/) — format rationale
- [gptme/gptme-contrib#1916](https://github.com/gptme/gptme-contrib/pull/1916) — ATIF v1.7 export
- [gptme/gptme-contrib#1922](https://github.com/gptme/gptme-contrib/issues/1922) — tool call parsing gap
- [datacurve/pier](https://github.com/datacurve-ai/pier) — ATIF v1.7 consumer
