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

## Status

Phase 1 (MVP): session bucket + CLI + REST client, dogfooded against a local
aw-server. Phase 2 adds per-tool activity heartbeats and a gptme-native plugin
hook; Phase 3 adds an aw-webui "AI work" view.
