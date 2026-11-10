# aw-watcher-agent

An [ActivityWatch](https://activitywatch.net) watcher for **AI coding
assistants** (gptme, Claude Code, Codex). It logs agent sessions and per-tool
activity to your **local** aw-server, so AI work shows up in the aw-webui
Timeline alongside window/AFK data instead of looking like idle time.

**Status:** alpha (`0.1.0`). Session tracking and the Codex log-tailer work and
are dogfooded against a local aw-server; the Claude Code log-tailer is tested on
fixtures and real transcripts but not yet dogfooded. A native gptme plugin hook
for per-tool events and an aw-webui "AI work" view are not built yet.

This is the controller use case behind
[ActivityWatch/activitywatch#1215](https://github.com/ActivityWatch/activitywatch/issues/1215).

## Design

- **Local-only, privacy-first.** Writes go only to your own aw-server
  (default `http://127.0.0.1:5600`). No hosted aggregation and no transcripts —
  only coarse metadata (harness, model, category, tool name, status, counts).
- **No dependencies.** A small vendored stdlib REST client instead of
  `aw-client` / `aw-core`, so it is light enough to call from a lifecycle hook.
- **One clean Timeline block per session.** `emit-start` posts a zero-duration
  placeholder; `emit-end` deletes it and posts a single event with the full
  duration plus `outcome`. If the session crashes before `emit-end`, the
  placeholder still marks that it started.
- **Never breaks the session it observes.** Errors are reported on stderr and
  the CLI exits 0, unless you pass `--strict`.

## Install

```bash
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/aw-watcher-agent"
# or, from a gptme-contrib checkout:
pip install -e packages/aw-watcher-agent
```

This installs the `aw-watcher-agent` command. You need a running aw-server
(ActivityWatch's default on port 5600).

## Usage

### Sessions

```bash
# Create the session bucket (idempotent; emit-start also does this)
aw-watcher-agent ensure-bucket

# At session start
aw-watcher-agent emit-start \
  --harness claude-code --model claude-opus-4-7 \
  --category code --session-id 8531 --trigger autonomous --workspace myagent

# At session end (reads the saved start state, records duration + outcome)
aw-watcher-agent emit-end --session-id 8531 --outcome productive
```

Events land in the bucket `aw-watcher-agent_<hostname>` (type
`app.agent.session`), which appears in aw-webui automatically.

Session flags (shared by `emit-start`, `emit-end` and `emit-activity`):
`--harness`, `--model`, `--category`, `--session-id`, `--trigger`,
`--workspace`, `--hostname` (default: the machine hostname) and `--server`
(aw-server base URL). `emit-end` additionally takes `--outcome` and
`--duration` (seconds; used only when no start state was recorded). Values
passed to `emit-end` override the ones saved at start.

Start state is kept per session id under
`$XDG_STATE_HOME/aw-watcher-agent/` (default `~/.local/state/aw-watcher-agent/`).

### Claude Code without hooks (log-tailer)

`tail-claude-code` reads the transcripts Claude Code already writes under
`~/.claude/projects/` (or `$CLAUDE_CONFIG_DIR/projects`) and posts one
session event per working block, so Claude Code work shows up on the Timeline
with no harness configuration:

```bash
aw-watcher-agent tail-claude-code            # every transcript
aw-watcher-agent tail-claude-code --gap 600  # split blocks on 10 min idle
```

Only `timestamp`, `sessionId` and `cwd` are read; prompts, replies and tool
payloads are never touched. A session is split into blocks on idle gaps
longer than `--gap` (default 15 min), so a session left open overnight is not
counted as agent time. The trailing block is held back until it has been idle
for `--gap`, and a per-transcript cursor records each successful post. Overlapping
invocations skip transcripts already being processed. Cursor writes are atomic;
retrying a failed later post does not replay the checkpointed prefix. Once blocks
have been emitted, changing `--gap` for that transcript is rejected rather than
posting overlapping representations. A digest of the checkpointed event metadata
also rejects historical inserts, rewrites or truncation that change already emitted
blocks; it does not silently skip or replay them. Only a missing cursor starts a
new import: unreadable, corrupt or older count-only cursors fail closed. Preserve
such a cursor and reconcile the destination events before resetting it, since
removing it reimports the transcript. A failed transcript is reported on stderr
without preventing other transcripts from being processed; `--strict` returns
nonzero after the scan if any failed. This is not transactional exactly-once
delivery: a crash after the server accepts a post but before its checkpoint can
still duplicate that last event on retry. Events land in the same session bucket
as the hooks below, with `harness=claude-code` and `workspace` set to the project
directory. Use either the tailer or lifecycle hooks for a session, not both.

### Wiring into Claude Code (or any harness with lifecycle hooks)

Point your harness's session-start / session-end hooks (for Claude Code:
`SessionStart` / `Stop` in `settings.json`) at `emit-start` / `emit-end`,
passing the same `--session-id` to both.

### Per-tool activity

Tool calls go to a sibling bucket, `aw-watcher-agent-activity_<hostname>`
(type `app.agent.activity`). Adjacent calls with the same tool and status within
`--pulsetime` seconds (default 5) merge into one Timeline block.

Emit a single tool event yourself, e.g. from a post-tool hook:

```bash
aw-watcher-agent emit-activity --harness claude-code --session-id 8531 \
  --tool shell --status success --duration-ms 1200
```

### Per-tool activity from Codex (log-tailer)

Codex can't host an in-process hook, so `tail-codex` reads its rollout
transcripts (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`), pairs each
`function_call` with its `function_call_output`, derives a coarse
`success` / `error` / `completed` status, and emits one activity event per call:

```bash
# Process the most recent rollout transcript
aw-watcher-agent tail-codex

# Or a specific transcript, with a custom merge window
aw-watcher-agent tail-codex --file ~/.codex/sessions/2026/05/29/rollout-...jsonl --pulsetime 5
```

A cursor per transcript (in the same state directory) records what was already
emitted, so it is safe to run `tail-codex` repeatedly from a timer or after
each Codex run.

## Command reference

| Command | Purpose |
|---------|---------|
| `ensure-bucket` | Create the session bucket if missing |
| `emit-start` | Record session start (zero-duration placeholder) |
| `emit-end` | Replace the placeholder with one event carrying duration + `--outcome` |
| `emit-activity` | Record one tool-activity heartbeat (`--tool` required) |
| `tail-codex` | Emit per-tool activity from a Codex rollout transcript |
| `tail-claude-code` | Emit session working blocks from Claude Code transcripts (no hooks) |

Global flag: `--strict` (exit non-zero on errors). Run
`aw-watcher-agent <command> --help` for all options.

## Related

- [gptme-sessions](../gptme-sessions/README.md) — session tracking and
  analytics on the agent side (this watcher puts the same sessions on your
  ActivityWatch timeline).
- [gptme-activity-summary](../gptme-activity-summary/README.md) — summarizes
  agent activity, including ActivityWatch data.
