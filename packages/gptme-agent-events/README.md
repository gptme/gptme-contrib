# gptme-agent-events

A live, harness-agnostic event stream of what an agent is doing (tool calls,
test runs, commits, pushes, PRs, errors, turn ends), plus sinks that consume it.
The first sink is **sound**: hear an agent work, using gptme's own sound effects.

**Status:** alpha (`0.1.0`). Claude Code, gptme and Codex are supported and
tested against real trajectories.

## Why

Every agent harness already writes a trajectory log as it works. This package
follows those logs on the agent's host and turns new lines into one small
schema, so anything downstream (a sound player, a status widget, a dashboard)
can ask "what is the agent doing right now?" without knowing each harness's
log format, and without adding hooks to the agent's hot path.

## Install

Not published on PyPI. From a gptme-contrib checkout:

```bash
uv run --package gptme-agent-events gptme-agent-events --help
# or install the command
uv tool install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptme-agent-events"
```

It depends on [gptme-sessions](../gptme-sessions/README.md), which provides the
shell parser for detecting commits and pushes. The sound sink needs gptme
installed (for its sound files) and a system player (`afplay`, `paplay` or
`ffplay`).

## Usage

```bash
# On the agent's host: follow every live trajectory, one JSON event per line.
gptme-agent-events tail --agent bob

# Hear it from your own machine.
ssh bob 'gptme-agent-events tail --agent bob' | gptme-agent-events sound

# The whole fleet, with Alice and Gordon a bit quieter.
{ ssh bob    'gptme-agent-events tail --agent bob'    &
  ssh alice  'gptme-agent-events tail --agent alice'  &
  ssh gordon 'gptme-agent-events tail --agent gordon' & } \
  | gptme-agent-events sound --agent-volume alice=0.6 --agent-volume gordon=0.6

# Your own local sessions.
gptme-agent-events tail | gptme-agent-events sound
```

Useful `tail` options:

- `--root HARNESS=DIR` adds or replaces a trajectory root, for agents that keep
  logs outside the defaults (for example `--root gptme=~/bob/logs`).
- `--harness` limits which harnesses are followed.
- `--from-start --once` replays recently active files and exits, which is
  handy for checking what the extractors make of a session.

Default roots: `~/.claude/projects` (or `$CLAUDE_CONFIG_DIR/projects`),
`$GPTME_LOGS_HOME` or `~/.local/share/gptme/logs`, and `~/.codex/sessions` (or
`$CODEX_HOME/sessions`).

## Event schema

```json
{"kind":"tool","ts":"2026-11-12T10:20:34.334Z","agent":"bob","harness":"claude-code",
 "session":"e68fc377-…","tool":"Bash","tool_class":"test"}
```

| Field | Meaning |
|-------|---------|
| `kind` | `tool`, `turn_end`, `error`, `commit`, `push`, `pr_open`, `session_start` |
| `tool` | The harness's own tool name (`Bash`, `patch`, `exec_command`, …) |
| `tool_class` | Harness-agnostic class: `read`, `search`, `write`, `shell`, `test`, `web`, `agent`, `other` |
| `subagent` | `true` for work done by a subagent of the session |
| `detail` | `cwd` (private) and `repo` (GitHub `owner/name`) where known; `path` (private) for file writes; `sha`, `url`, `ref`, `dir` (private) and `source` (`git` or shell) for commits, pushes and PRs |

### Commits and pushes come from git

Shell output is a weak signal for whether a commit landed: wrapper scripts,
hooks and `| tail` hide the `[branch sha]` line, unattended scripts commit
without any tool call, and a commit can be undone a second later. So `tail`
also watches git's reflogs for every repository an agent touches (any event's
cwd or commit directory, plus `--repo DIR`):

- `logs/HEAD` and `worktrees/*/logs/HEAD` entries starting with `commit` become
  `commit` events;
- `update by push` entries under `logs/refs/remotes/` become `push` events.

Commits are attributed to sessions through their `Git-Session-Id` trailer, and a
push through the trailer of the commit it pushed. When the trailer names a
session the follower has seen, the event takes that session's harness;
otherwise `harness` is `git` (for example, a script commit). For watched
repositories, shell results annotate matching reflog events rather than producing
a second event. A missing trailer can inherit the shell result's session,
harness and tool: commits match by SHA when available, otherwise a unique result
of the same kind in the same repository within five seconds is required. A
trailer always wins over shell attribution. Events may wait up to five seconds
for a tool result from the next poll; unmatched shell evidence is emitted as a
fallback (including repositories with reflogs disabled). Ambiguous concurrent
results are not used to guess a session. Pass `--no-git` to rely on shell output
only.

Without git watching, or for repos that can't be resolved, commits, pushes and
PRs are reported when the tool **result** shows the command succeeded; a failed
call becomes `error` instead. PRs always come from the tool result (`gh pr
create` output). gptme's markdown tool format has no call ids, so there they
are reported with the call.

### Privacy

Events never contain prompts, replies, shell commands or tool output. Commands
are inspected in memory to classify them and then dropped. The stream is meant
to stay private (stdout, ssh). Any sink that publishes beyond the agent's own
host must drop `detail.cwd`, `detail.dir` and `detail.path`, and only forward
`repo`/`sha`/`url`/`ref` for public repos. The repo slug is resolved on the agent's host from the checkout's
`origin` remote.

## Sounds

`sound` prints one line per event (local time, agent, harness, session,
event kind, tool, repo@sha or URL, then `♪ sound` or why it was skipped:
`throttled`, `busy`, `silent`), so the terminal doubles as a live log; `-q`
turns that off.

```
11:48:48 bob      claude-code 9e91d4cc tool:shell     Bash                                     ♪ seashell_click
11:48:49 bob      claude-code 9e91d4cc commit         Bash ErikBjare/bob@d6e437d98e80 (git)     ♪ chime_clean_ring
```

`gptme-agent-events sound --list` shows the mapping. Defaults use the sounds
gptme ships with:

| Event | Sound |
|-------|-------|
| `turn_end` | `bell` (gptme's ding) |
| `tool` read / search / web | `page_turn` |
| `tool` write | `file_write` |
| `tool` shell | `seashell_click` |
| `tool` test | `drilling` |
| `tool` agent / other | `sawing` |
| `commit` / `push` / `pr_open` | `chime_clean_ring` / `chime_complex_ring` / `chime_improved` |
| `error` | `chime_beating` |
| `session_start` | `chime` |

Override with `--map tool:shell=sawing`, or silence with `--map tool:read=`.
Tool sounds are throttled during bursts (`--min-gap`, default 0.15 s);
milestones are never dropped for spacing.

## Library use

```python
from gptme_agent_events import Follower, default_sources

for event in Follower(default_sources(), agent="bob").run():
    print(event.kind, event.tool_class)
```

Extractors can also be fed records directly
(`ClaudeCodeExtractor(session).feed(record)`), which is how other sinks or
publishers can reuse the parsing without the follower.

## Roadmap

- Publisher for a public status feed (replacing the single-row
  `bob_heartbeat` beacon behind gptme.ai's "Right now Bob is" widget), with a
  privacy projection that only names things already public.
- Copilot and Pi extractors (gptme-sessions already parses both).
