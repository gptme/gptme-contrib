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
| `detail` | `path` for file writes; `repo` (GitHub `owner/name`), `sha` and `url` for commits, pushes and PRs |

Commits, pushes and PRs are reported when the tool **result** shows the command
succeeded; a failed call becomes `error` instead. (gptme's markdown tool format
has no call ids, so there they are reported with the call.)

### Privacy

Events never contain prompts, replies, shell commands or tool output. Commands
are inspected in memory to classify them and then dropped. The stream is meant
to stay private (stdout, ssh). Any sink that publishes beyond the agent's own
host must drop `detail.path` and only forward `repo`/`sha`/`url` for public
repos. The repo slug is resolved on the agent's host from the checkout's
`origin` remote.

## Sounds

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
