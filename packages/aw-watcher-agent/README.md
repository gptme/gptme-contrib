# aw-watcher-agent

An [ActivityWatch](https://activitywatch.net) watcher for **AI coding
assistants** (gptme, Claude Code, Codex). It logs assistant session activity to
your **local** aw-server so AI work shows up in aw-webui's Timeline alongside
window/AFK data — instead of being logged as idle while an autonomous agent does
the most valuable work.

This is the controller use case behind
[ActivityWatch/activitywatch#1215](https://github.com/ActivityWatch/activitywatch/issues/1215).

## Design

- **Local-only, privacy-first.** Writes go only to your own aw-server. No hosted
  aggregation, no transcripts — only coarse metadata (harness, model, category,
  counts).
- **Zero heavy deps.** Vendored ~150-line stdlib REST client; no `aw-client` /
  `aw-core` dependency, so it stays pip-installable and light enough to call from
  a lifecycle hook.
- **One clean Timeline block per session.** `emit-start` posts a zero-duration
  placeholder; `emit-end` deletes it and posts a single event with the full
  duration plus `outcome`.

Full design note (bucket schema, event taxonomy, phased plan):
`knowledge/technical-designs/aw-watcher-agent-design.md` in the Bob workspace.

## Install

```bash
pip install -e packages/aw-watcher-agent
```

## Usage

```bash
# Create the session bucket (idempotent)
aw-watcher-agent ensure-bucket

# At session start
aw-watcher-agent emit-start \
  --harness claude-code --model claude-opus-4-7 \
  --category code --session-id 8531 --trigger autonomous --workspace bob

# At session end (reads the start state, records duration + outcome)
aw-watcher-agent emit-end --session-id 8531 --outcome productive
```

The bucket `aw-watcher-agent_<hostname>` (type `app.agent.session`) appears in
aw-webui automatically.

### Wiring Claude Code hooks

Point `SessionStart` / `Stop` hooks (in `~/.claude/settings.json`) at the
`emit-start` / `emit-end` subcommands. Failures are non-fatal by default
(`--strict` to opt in) so a watcher problem never breaks the agent session it
observes.

### Per-tool activity from Codex (log-tailer)

Codex can't host an in-process hook, so per-tool observability comes from
tailing its rollout transcripts (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`).
`tail-codex` pairs each `function_call` with its `function_call_output`, derives
a coarse `success`/`error`/`completed` status, and emits one
`app.agent.activity` event per tool call:

```bash
# Process the most recent rollout transcript
aw-watcher-agent tail-codex

# Or a specific transcript, with a custom heartbeat merge window
aw-watcher-agent tail-codex --file ~/.codex/sessions/2026/05/29/rollout-...jsonl --pulsetime 5
```

Events land in `aw-watcher-agent-activity_<hostname>` (type `app.agent.activity`),
a sibling of the session bucket. Adjacent same-tool/same-status calls within
`--pulsetime` seconds merge into one Timeline block. Run it on a timer (or after
each Codex run) to keep the bucket current.

## Status

Phase 1 (MVP): session bucket + CLI + REST client, dogfooded against a local
aw-server. Phase 2 (in progress): per-tool `app.agent.activity` heartbeats —
the Codex log-tailer (`tail-codex`) is shipped; the gptme-native plugin hook is
the remaining deliverable. Phase 3 adds an aw-webui "AI work" view.
