# gptme-contrib

Plugins, packages, scripts, skills, and lessons for building persistent, autonomous AI agents with [gptme](https://github.com/gptme/gptme): task management, email and agent-to-agent messaging, multi-agent coordination, run loops, memory and retrieval, voice, and session analytics.

If [gptme](https://github.com/gptme/gptme) is the engine and [gptme-agent-template](https://github.com/gptme/gptme-agent-template) is the chassis, this repo is the parts catalog. Everything here is optional: pick the parts you need.

| Layer | Repo | What it provides |
|-------|------|------------------|
| Engine | [gptme](https://github.com/gptme/gptme) | The agent runtime: CLI, server and web UI, built-in tools (shell, tmux, patch, python, browser, vision, `gh`, `rag`, `todo`, MCP client, …), plugin and hook system |
| Chassis | [gptme-agent-template](https://github.com/gptme/gptme-agent-template) | Workspace template for persistent agents: identity files, journal, tasks, lessons, knowledge base |
| Parts | **gptme-contrib** (this repo) | Packages, plugins, scripts, skills, and lessons you compose on top |

**Check gptme core first.** Core already ships a lot, including the `gptme-util` CLI (context indexing and retrieval, conversation search, token counting, models, tools, skills, hooks, MCP, status, and more). See the [`gptme-util` docs](https://gptme.org/docs/cli.html#gptme-util).

**Contents:** [Building autonomous agents](#building-autonomous-agents) · [Catalog by category](#catalog-by-category) · [Using plugins](#using-plugins) · [Installing packages](#installing-packages) · [Scripts](#scripts) · [Skills, lessons and more](#skills-lessons-and-more) · [Ecosystem](#ecosystem) · [Contributing](#contributing)

## Building autonomous agents

A long-running agent needs a few things gptme core deliberately leaves open: somewhere to keep its work queue, a way to talk to people and other agents, a loop that wakes it up, and a way to avoid stepping on itself when several sessions run at once. These packages are what the gptme agent fleet uses day to day:

| Need | Component | What it gives you |
|------|-----------|-------------------|
| **Tasks / work queue** | [gptodo](./packages/gptodo/README.md) | Tasks as Markdown + YAML frontmatter files in git. `gptodo ready` / `gptodo next` pick unblocked work; `claim`, locks, a state machine and `lint` keep parallel sessions honest. Imports from GitHub and Linear. |
| **Delegation** | [gptme-gptodo](./plugins/gptme-gptodo/README.md) | gptme plugin exposing gptodo as Python functions so a coordinator agent can delegate tasks to sub-agents. |
| **Email and agent messaging** | [gptmail](./packages/gptmail/README.md) | Email (mbsync + msmtp) and SSH-based agent-to-agent messages, stored as Markdown files in the workspace, with reply tracking so each message is answered exactly once. |
| **Team discussion** | [gptme-forum](./packages/gptme-forum/README.md) | `agentboard`: a git-native forum with projects, threaded posts, @mentions and DMs. |
| **Coordination** | [gptme-coordination](./packages/gptme-coordination/README.md) | Serverless SQLite work claims, message bus, TTL facts and an event queue for parallel agents. |
| **Run loops** | [gptme-runloops](./packages/gptme-runloops/README.md) | Locked, backoff-aware autonomous/monitoring/email loops and a one-shot resumable `run` across gptme, Claude Code, Codex and Grok Build. |
| **Resuming in-flight work** | [gptme-gupp](./plugins/gptme-gupp/README.md), [gptme-ralph](./plugins/gptme-ralph/README.md) | Work-in-progress hooks that survive restarts and compaction; a spec + checklist loop with a fresh context per step. |
| **Lessons and skills** | [lessons/](./lessons/README.md), [skills/](./skills/README.md), [gptme-lessons-extras](./packages/gptme-lessons-extras/README.md), [gptme-lessons-mcp](./packages/gptme-lessons-mcp/README.md) | Keyword-triggered behavioural lessons and reusable skill bundles, plus tooling to validate them and serve them to any MCP client. |
| **Session analytics** | [gptme-sessions](./packages/gptme-sessions/README.md) | Discover gptme / Claude Code / Codex / Copilot / Pi trajectories, extract productivity signals and cost, keep an append-only ledger. |

**Bring your own.** None of these are required. gptme agents work fine with GitHub Issues, Linear or a single `TODO.md` for tasks, and with any email client, chat bridge or message bus for communication. gptodo and gptmail are one well-integrated option: the [gptme-agent-template](https://github.com/gptme/gptme-agent-template) `tasks/` layout follows gptodo's conventions, and both store their state as plain files in the agent's git workspace so humans can review it in a diff.

Try the two core pieces:

```bash
uv tool install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptodo"
uv tool install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/gptmail"

# In the agent's git workspace
gptodo add --priority high "Write project README"
gptodo next                     # the single best unblocked task

# Once gptmail is set up (see its README)
gptmail check-unreplied         # email still owed a reply
gptmail agent pending           # agent-to-agent messages still owed a reply
```

gptmail needs a little setup first: the `email/` folders and a mail configuration for email, or a `messages/agents.yaml` registry of SSH targets for agent messaging. Both quickstarts are in the [gptmail README](./packages/gptmail/README.md).

## Catalog by category

Status is taken from each component's README: **beta** = used in production with a mostly stable interface, **alpha / experimental** = works but may change, **internal** = shared helpers or Bob-specific code with no stability promise. "Package" means a Python package under [`packages/`](./packages/); "plugin" means a gptme plugin under [`plugins/`](./plugins/README.md).

### Agent infrastructure

| Component | Type | What it does | Status |
|-----------|------|--------------|--------|
| [gptodo](./packages/gptodo/README.md) | package | File-based task manager and work-queue CLI (Markdown + YAML tasks in git, ready/next selection, claims, locks, state machine, lint) | beta |
| [gptme-gptodo](./plugins/gptme-gptodo/README.md) | plugin | gptodo as Python functions (`delegate`, `check_agent`, `list_tasks`, …) for coordinator agents that delegate to sub-agents | experimental |
| [gptme-coordination](./packages/gptme-coordination/README.md) | package | Serverless SQLite coordination: atomic work claims, message bus, TTL fact bus, prioritised and deduplicated event queue | experimental |
| [gptme-runloops](./packages/gptme-runloops/README.md) | package | Run-loop framework: autonomous, monitoring, email and team loops plus a resumable one-shot `run` across several agent CLIs | experimental |
| [gptme-gupp](./plugins/gptme-gupp/README.md) | plugin | Work-in-progress hooks (JSON files) so an agent can resume after crashes, restarts or context compaction | experimental |
| [gptme-ralph](./plugins/gptme-ralph/README.md) | plugin | Ralph Loop: drive a spec + checkbox plan to completion one step at a time with a fresh gptme or Claude Code context per step | experimental |
| [gptme-backoff](./packages/gptme-backoff/README.md) | package | Retry decorators with exponential backoff and jitter, plus an error classifier (retry 429s patiently, fail fast on auth errors) | alpha |
| [gptme-block-registry](./packages/gptme-block-registry/README.md) | package | File-based "is this model/backend blocked?" contract for agents that fail over between LLM backends, with a credential-death circuit breaker | alpha |
| [credential-slots](./packages/credential-slots/README.md) | package | Offline rotation between several OAuth credential files behind one live symlink (expiry, drift detection, heal, busy-guard) | beta |
| [gptme-subscription](./packages/gptme-subscription/README.md) | package | Rotate load between several Claude Code subscription slots based on 5-hour and weekly quota; CLI and Python API | alpha |
| [gptme-action-receipts](./plugins/gptme-action-receipts/README.md) | plugin | Append-only, hashed audit ledger of every tool call plus a `scope.yaml` allowlist for risky git/GitHub operations | experimental |
| [gptme-hooks-examples](./plugins/gptme-hooks-examples/README.md) | plugin | Copyable template showing how to write and register gptme lifecycle hooks | example |

### Communication

| Component | Type | What it does | Status |
|-----------|------|--------------|--------|
| [gptmail](./packages/gptmail/README.md) | package | Email and SSH agent-to-agent messaging with every message stored as Markdown and replies tracked | beta |
| [gptme-forum](./packages/gptme-forum/README.md) | package | `agentboard`: git-native forum for multi-agent teams (projects, threaded posts, @mentions, DMs) | experimental |
| [gptme-whatsapp](./packages/gptme-whatsapp/README.md) | package | WhatsApp bridge (whatsapp-web.js) that runs a gptme or Claude Code agent per incoming message | experimental |
| [discord](./scripts/discord/), [telegram](./scripts/telegram/), [twitter](./scripts/twitter/), [bluesky](./scripts/bluesky/) | scripts | Chat and social integrations | varies |

### Memory, knowledge and retrieval

| Component | Type | What it does | Status |
|-----------|------|--------------|--------|
| [gptme-rag](./packages/gptme-rag/README.md) | package | Local semantic search: index files into ChromaDB and query them from the CLI, gptme's `rag` tool or an MCP server | beta |
| [gptme-retrieval](./plugins/gptme-retrieval/README.md) | plugin | Automatic RAG: a hook that queries qmd, gptme-rag, grep or a custom command and injects deduplicated matches | experimental |
| [gptme-wisdom](./packages/gptme-wisdom/README.md) | package | Local BM25 (SQLite FTS5) search over freely licensed reference books, from the CLI, Python or as session context | beta |
| [gptme-wisdom-mcp](./packages/gptme-wisdom-mcp/README.md) | package | MCP server for gptme-wisdom plus search over past agent sessions | experimental |
| [gptme-codegraph](./packages/gptme-codegraph/README.md) | package | Structural code retrieval with tree-sitter (definitions, callers, impact, repo map, symbol search) as CLI, library and MCP server | experimental |
| [gptme-cc-memory](./packages/gptme-cc-memory/README.md) | package | Typed, git-tracked Markdown memory for Claude Code, injected per prompt by a hook with no LLM calls | experimental |
| [gptme-user-memories](./plugins/gptme-user-memories/README.md) | plugin | ChatGPT-style long-term memory: extracts facts about the user at session end into a local Markdown file | experimental |
| [gptme-ace](./plugins/gptme-ace/README.md) | plugin | Agentic Context Engineering for lessons: hybrid retrieval, embedding dedup, and a pipeline that turns session logs into reviewable lesson deltas | experimental |
| [gptme-attention-tracker](./plugins/gptme-attention-tracker/README.md) | plugin | HOT/WARM/COLD attention scores with decay for workspace files, plus context-usage history | experimental |
| [gptme-lessons-extras](./packages/gptme-lessons-extras/README.md) | package | Lesson-library toolbox: format validator, LLM-assisted generation, usage analytics, duplicate detection | internal |
| [gptme-lessons-mcp](./packages/gptme-lessons-mcp/README.md) | package | MCP server exposing gptme's lesson library to any MCP client (Claude Code, Cursor, Continue.dev) | alpha |

### Context and cost management

| Component | Type | What it does | Status |
|-----------|------|--------------|--------|
| [gptme-tooloutput-trimmer](./plugins/gptme-tooloutput-trimmer/README.md) | plugin | Trims or summarizes old oversized tool outputs when the prompt cache is cold or context is under pressure | experimental, opt-in |
| [gptme-headroom-compressor](./plugins/gptme-headroom-compressor/README.md) | plugin | Losslessly compresses large JSON/tabular tool outputs with headroom-ai before they reach the model | experimental, opt-in |

### Developer tools and integrations

| Component | Type | What it does | Status |
|-----------|------|--------------|--------|
| [gptme-lsp](./plugins/gptme-lsp/README.md) | plugin | LSP diagnostics, definition/references/hover, call hierarchy and rename/format previews (Python, TS/JS, Go, Rust) | experimental |
| [gptme-warpgrep](./plugins/gptme-warpgrep/README.md) | plugin | Natural-language code search using Morph's warp-grep model over local ripgrep/read/list | experimental |
| [gptme-claude-code](./plugins/gptme-claude-code/README.md) | plugin | Delegate analyze/ask/fix/implement tasks to the Claude Code CLI, synchronously or in background tmux sessions | experimental |
| [gptme-consortium](./plugins/gptme-consortium/README.md) | plugin | Ask several LLMs the same question, then have an arbiter model synthesize an answer with a confidence score | experimental |
| [gptme-browser-semantic](./packages/gptme-browser-semantic/README.md) | package | observe/act/extract browser primitives over gptme's Playwright ARIA snapshot | experimental |

### Monitoring and analytics

| Component | Type | What it does | Status |
|-----------|------|--------------|--------|
| [gptme-sessions](./packages/gptme-sessions/README.md) | package | Session discovery, productivity signals, cost, and an append-only JSONL ledger for gptme, Claude Code, Codex, Copilot and Pi | beta |
| [gptme-usage](./packages/gptme-usage/README.md) | package | Cache-aware cost estimation and a per-agent model/quota registry across LLM backends | alpha |
| [gptme-dashboard](./packages/gptme-dashboard/README.md) | package | Static GitHub Pages dashboard for an agent workspace, with an optional live server and JSON API | experimental |
| [gptme-activity-summary](./packages/gptme-activity-summary/README.md) | package | LLM-written daily/weekly/monthly "what did I get done" reports for agents and humans | beta |
| [gptme-daily-briefing](./packages/gptme-daily-briefing/README.md) | package | Typed JSON bundle schema plus collectors for an agent's daily briefing (library only) | experimental |
| [aw-watcher-agent](./packages/aw-watcher-agent/README.md) | package | ActivityWatch watcher that logs gptme, Claude Code and Codex sessions to your aw-server timeline | alpha |
| [gptme-wrapped](./plugins/gptme-wrapped/README.md) | plugin | Spotify-Wrapped-style yearly stats from local gptme logs: tokens, costs, models, cache efficiency, heatmap | experimental |

### Voice, media and embodiment

| Component | Type | What it does | Status |
|-----------|------|--------------|--------|
| [gptme-voice](./packages/gptme-voice/README.md) | package | Real-time voice interface (OpenAI Realtime / xAI Grok) over local mic, browser or Twilio phone, with subagent delegation | experimental |
| [gptme-tts](./plugins/gptme-tts/README.md) | plugin | Reads replies aloud as they stream, via a bundled local server (Kokoro, KittenTTS, Chatterbox) or OpenRouter | experimental |
| [gptme-imagen](./plugins/gptme-imagen/README.md) | plugin | Text-to-image (Gemini, DALL-E) as a gptme tool and CLI, with style presets and cost tracking | experimental |
| [gptme-youtube](./plugins/gptme-youtube/README.md) | plugin | Fetch a YouTube video's transcript and summarize it | experimental |
| [gptme-voice-node](./packages/gptme-voice-node/README.md) | package | Headless embedded voice client (Raspberry Pi + mic array) that streams to a gptme-voice server | experimental |
| [gptme-vision-node](./packages/gptme-vision-node/README.md) | package | Camera pipeline for an embedded node: capture, person/motion detection, on-demand vision-LLM look, place recognition | experimental |
| [gptme-body-protocol](./packages/gptme-body-protocol/README.md) | package | Wire DTOs and newline-framed JSON protocol connecting gptme-voice to a robot/drone body node | experimental |

### Internal and Bob-specific

Shared helpers used by other packages and scripts in this repo, and code written for one specific agent ([Bob](https://github.com/TimeToBuildBob)). Useful as reference; no stability promise.

| Component | Type | What it does |
|-----------|------|--------------|
| [bobutils](./packages/bobutils/README.md) | package | Stdlib-only helpers: JSONL loading, datetime/duration parsing, slugs, atomic writes, text sanitizing |
| [gptme-contrib-lib](./packages/gptme-contrib-lib/README.md) | package | Shared helpers for this repo: input sources (GitHub/email/webhook/schedule to task files), rate limiter, metrics, AI-review merge policy |
| [gptme-bob-status](./packages/gptme-bob-status/README.md) | package | Bob's `gptme-util status` provider; doubles as a reference for writing your own |

## Using plugins

Plugins add tools and hooks to gptme ([plugin docs](https://gptme.org/docs/plugins.html)). Two ways to load one:

**Install it** into the same environment as gptme. Most plugins here register a `gptme.plugins` entry point and load automatically once installed:

```bash
pipx inject gptme "git+https://github.com/gptme/gptme-contrib#subdirectory=plugins/gptme-tts"
```

**Or point gptme at a checkout** in `gptme.toml` (or the user config). Point at the plugin's own directory; the allowlist name is then its Python package name:

```toml
[plugins]
paths = ["path/to/gptme-contrib/plugins/gptme-attention-tracker"]
enabled = ["gptme_attention_tracker"]   # optional allowlist; empty = all discovered plugins
```

Each plugin README lists its exact install line, extras and config. See [plugins/README.md](./plugins/README.md) for the full plugin list and naming notes.

## Installing packages

Packages are not published to PyPI (except where a README says otherwise). Install one from this repo's subdirectory:

```bash
uv tool install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/<name>"   # CLI tools
pip install "git+https://github.com/gptme/gptme-contrib#subdirectory=packages/<name>"       # libraries
```

Or work from a clone, where every package is a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) member:

```bash
git clone https://github.com/gptme/gptme-contrib && cd gptme-contrib
uv sync --all-packages
```

Some packages depend on other workspace members and only resolve from a clone; their READMEs say so. See [packages/README.md](./packages/README.md) for workspace details.

## Scripts

Standalone scripts and integrations. See [scripts/README.md](./scripts/README.md) for the full index. Scripts with `uv run` shebangs need [uv](https://docs.astral.sh/uv/).

| Area | Scripts |
|------|---------|
| Context | [context/](./scripts/context/): agent system prompt generation |
| GitHub | [github/](./scripts/github/), [github_hygiene/](./scripts/github_hygiene/), [github_resolver/](./scripts/github_resolver/) |
| Social and messaging | [discord/](./scripts/discord/), [telegram/](./scripts/telegram/), [twitter/](./scripts/twitter/), [bluesky/](./scripts/bluesky/) |
| Issue tracking | [linear/](./scripts/linear/) |
| Research | [autoresearch/](./scripts/autoresearch/), [exa.py](./scripts/exa.py), [perplexity.py](./scripts/perplexity.py) |
| Health and quota | [status/](./scripts/status/), [check-quota.py](./scripts/check-quota.py), [fleet_vitals.py](./scripts/fleet_vitals.py) |
| Safety | [agent-write-loss-scan](./scripts/README.md#agent-write-loss-scan), [workspace_validator/](./scripts/workspace_validator/), [git/](./scripts/git/) |

## Skills, lessons and more

- **[skills/](./skills/README.md)**: skill bundles (workflow + scripts + docs) for agent onboarding, plugin development, code review, artifact publishing, Home Assistant, and more.
- **[lessons/](./lessons/README.md)**: shared lessons injected into agent prompts by keyword match (autonomous, communication, infrastructure, patterns, social, tools, workflow).
- **[dotfiles/](./dotfiles/README.md)**: global git hooks and config for safer agent development workflows.
- **[docs/](./docs/)**: plugin deep-dives and cross-agent protocols, such as the [heartbeat protocol](./docs/protocols/gptme-heartbeat.md).

## Ecosystem

| Project | Description |
|---------|-------------|
| [gptme-howto](https://github.com/gptme/gptme-howto) | Copy-paste recipes for gptme |
| [gptme-plugin-registry](https://github.com/gptme/gptme-plugin-registry) | Central registry for gptme plugins: metadata, indexing, discovery |
| [gptme-lessons](https://github.com/gptme/gptme-lessons) | Shared lessons across forked agents |
| [gptme.vim](https://github.com/gptme/gptme.vim) | Vim plugin for gptme |
| [gptme-cc-plugin](https://github.com/gptme/gptme-cc-plugin) | Claude Code skills for gptme: `/gptme:run`, `/gptme:review`, `/gptme:context` |
| [gptme-skills-cc](https://github.com/gptme/gptme-skills-cc) | gptme agent skills packaged as a Claude Code plugin |
| [agent-workspace-plugin](https://github.com/gptme/agent-workspace-plugin) | Claude Code plugin: persistent agent workspace (tasks, journal, lessons, knowledge) |

Archived: [gptme-rag](https://github.com/gptme/gptme-rag) (now [packages/gptme-rag](./packages/gptme-rag/README.md)), [gptme-webui](https://github.com/gptme/gptme-webui) (now part of gptme), [gptme-tauri](https://github.com/gptme/gptme-tauri).

### Discover more

The [gptme.org Skills Gallery](https://gptme.org/docs/skills-gallery.html) curates a short list of tested skills with install commands. To make your own plugin or skill discoverable, add a GitHub topic to its repo:

| Topic | For | Browse |
|-------|-----|--------|
| `gptme-plugin` | Python plugin packages | [github.com/topics/gptme-plugin](https://github.com/topics/gptme-plugin) |
| `gptme-skill` | SKILL.md skill bundles | [github.com/topics/gptme-skill](https://github.com/topics/gptme-skill) |
| `gptme-mcp-server` | MCP servers for gptme | [github.com/topics/gptme-mcp-server](https://github.com/topics/gptme-mcp-server) |

```bash
gh repo edit owner/your-repo --add-topic gptme-plugin
```

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for how to add a package, plugin, script or lesson. Components here are community-contributed and generally less mature than gptme core; each README states its status.

## License

MIT. See [LICENSE](./LICENSE).
