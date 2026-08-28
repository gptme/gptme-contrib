# gptme-contrib

Community-contributed plugins, packages, scripts, skills, and lessons for [gptme](https://github.com/gptme/gptme).

If [gptme](https://github.com/gptme/gptme) is the engine and [gptme-agent-template](https://github.com/gptme/gptme-agent-template) is the chassis, this repo is the parts catalog: everything you need to assemble a full-featured persistent agent — retrieval and memory, a task system, email and social messaging, real-time voice, GitHub automation, run loops for autonomous operation, and the observability to keep it all honest.

## Where this fits in the gptme ecosystem

| Layer | Repo | What it provides |
|-------|------|------------------|
| Engine | [gptme](https://github.com/gptme/gptme) | The agent runtime: CLI + server/web UI, built-in tools (shell, tmux, patch, python, browser, vision/computer use, `gh`, `rag`, `todo`, MCP client, autocommit/precommit, …), plugin & hook system |
| Chassis | [gptme-agent-template](https://github.com/gptme/gptme-agent-template) | Workspace template for persistent agents: identity files, journal, tasks, lessons, knowledge base |
| Parts | **gptme-contrib** (this repo) | Plugins, packages, scripts, skills, and lessons you compose on top |

**Check gptme core before reaching here.** Core already ships a lot, notably the extensive `gptme-util` CLI with subcommands for context (`index`, `retrieve`, `search-conversations`, `git`, `tree`, `journal`), token counting, chat search, models/providers, LLM helpers, tools, prompts, skills, hooks, MCP, status, snapshots, and session resume. See [`gptme-util` docs](https://gptme.org/docs/cli.html#gptme-util). If a capability isn't in core or here, it may exist as a community plugin — see [Discover more](#discover-more).

## Assembling a full agent

A capability-oriented map of this repo. Each entry links to a plugin (`plugins/`), package (`packages/`), or script collection (`scripts/`) below.

| Capability | Components |
|------------|------------|
| **Retrieval & memory** | [gptme-rag](./packages/gptme-rag/) (vector/semantic search, ChromaDB), [gptme-wisdom](./packages/gptme-wisdom/) (BM25 reference-book index) + [gptme-wisdom-mcp](./packages/gptme-wisdom-mcp/), [gptme-codegraph](./packages/gptme-codegraph/) (structural code retrieval), [gptme-retrieval](./plugins/gptme-retrieval/) (automatic context retrieval), [gptme-user-memories](./plugins/gptme-user-memories/), [gptme-cc-memory](./packages/gptme-cc-memory/) |
| **Tasks & work supply** | [gptodo](./packages/gptodo/) (task CLI + work queues), [gptme-gptodo](./plugins/gptme-gptodo/) (coordinator-mode delegation) |
| **Email** | [gptmail](./packages/gptmail/) — universal email for agents, incl. agent-to-agent messaging |
| **Chat & social** | [gptme-whatsapp](./packages/gptme-whatsapp/), [gptme-forum](./packages/gptme-forum/) (git-native agent forum), [discord](./scripts/discord/) / [telegram](./scripts/telegram/) / [twitter](./scripts/twitter/) / [bluesky](./scripts/bluesky/) scripts |
| **Voice** | [gptme-voice](./packages/gptme-voice/) (real-time voice via OpenAI/Grok Realtime APIs), [gptme-tts](./plugins/gptme-tts/) (local Kokoro TTS) |
| **GitHub** | [github](./scripts/github/) scripts (context, notifications), [github_hygiene](./scripts/github_hygiene/), [github_resolver](./scripts/github_resolver/); core's `gh` tool |
| **Autonomous operation** | [gptme-runloops](./packages/gptme-runloops/) (run loop framework), [gptme-gupp](./plugins/gptme-gupp/) (work persistence), [gptme-ralph](./plugins/gptme-ralph/) (iterate with context reset), [gptme-coordination](./packages/gptme-coordination/) (work claims, message bus), [credential-slots](./packages/credential-slots/) + [gptme-subscription](./packages/gptme-subscription/) (credential rotation, capacity-aware routing), [gptme-backoff](./packages/gptme-backoff/) |
| **Context management** | [gptme-ace](./plugins/gptme-ace/), [gptme-attention-tracker](./plugins/gptme-attention-tracker/), [gptme-headroom-compressor](./plugins/gptme-headroom-compressor/), [gptme-tooloutput-trimmer](./plugins/gptme-tooloutput-trimmer/) |
| **Observability & analytics** | [gptme-sessions](./packages/gptme-sessions/), [gptme-usage](./packages/gptme-usage/), [gptme-dashboard](./packages/gptme-dashboard/), [gptme-activity-summary](./packages/gptme-activity-summary/), [gptme-daily-briefing](./packages/gptme-daily-briefing/), [aw-watcher-agent](./packages/aw-watcher-agent/) (ActivityWatch), [status](./scripts/status/) scripts |
| **Safety & audit** | [gptme-action-receipts](./plugins/gptme-action-receipts/) (hashed audit ledger), [dotfiles](./dotfiles/) (global git hooks), [agent-write-loss-scan](./scripts/README.md#agent-write-loss-scan), [workspace_validator](./scripts/workspace_validator/) |
| **Learning & self-improvement** | [lessons/](./lessons/), [skills/](./skills/), [gptme-lessons-extras](./packages/gptme-lessons-extras/), [gptme-lessons-mcp](./packages/gptme-lessons-mcp/) (lessons for any MCP client) |

## Plugins

Plugins extend gptme with custom tools and hooks ([gptme plugin docs](https://gptme.org/docs/plugins.html)). See [plugins/README.md](./plugins/README.md).

| Plugin | Description |
|--------|-------------|
| [gptme-ace](./plugins/gptme-ace/) | ACE-inspired context optimization — hybrid retrieval, semantic matching, context curation |
| [gptme-action-receipts](./plugins/gptme-action-receipts/) | Append-only hashed audit ledger for tool actions |
| [gptme-attention-tracker](./plugins/gptme-attention-tracker/) | Attention routing + history tracking for context management |
| [gptme-claude-code](./plugins/gptme-claude-code/) | Claude Code subagent integration |
| [gptme-consortium](./plugins/gptme-consortium/) | Multi-model consensus decision-making |
| [gptme-gptodo](./plugins/gptme-gptodo/) | gptodo delegation plugin for coordinator-only agent mode |
| [gptme-gupp](./plugins/gptme-gupp/) | Work persistence for session continuity |
| [gptme-headroom-compressor](./plugins/gptme-headroom-compressor/) | Lossless compression of tool outputs to reclaim context headroom |
| [gptme-hooks-examples](./plugins/gptme-hooks-examples/) | Example hook implementations |
| [gptme-imagen](./plugins/gptme-imagen/) | Multi-provider image generation |
| [gptme-lsp](./plugins/gptme-lsp/) | Language Server Protocol integration for code intelligence |
| [gptme-ralph](./plugins/gptme-ralph/) | Ralph Loop pattern — iterative execution with context reset |
| [gptme-retrieval](./plugins/gptme-retrieval/) | Automatic context retrieval via semantic/keyword search |
| [gptme-tooloutput-trimmer](./plugins/gptme-tooloutput-trimmer/) | Read-time trimming of stale tool outputs |
| [gptme-tts](./plugins/gptme-tts/) | Text-to-speech with local Kokoro models |
| [gptme-user-memories](./plugins/gptme-user-memories/) | Automatic user-fact extraction across sessions — local ChatGPT-style memory |
| [gptme-warpgrep](./plugins/gptme-warpgrep/) | Enhanced search with Warp-style filtering |
| [gptme-wrapped](./plugins/gptme-wrapped/) | Year-end analytics for your gptme usage (Spotify Wrapped-style) |
| [gptme-youtube](./plugins/gptme-youtube/) | YouTube transcript extraction and summarization |

### Plugin Usage

Add to your `gptme.toml`:

```toml
[plugins]
paths = ["path/to/gptme-contrib/plugins"]
enabled = ["gptme_attention_tracker", "gptme_imagen"]
```

## Packages

Reusable Python packages, installable individually. See [packages/README.md](./packages/README.md).

| Package | Description |
|---------|-------------|
| [aw-watcher-agent](./packages/aw-watcher-agent/) | ActivityWatch watcher for AI coding assistants (gptme, Claude Code, Codex) |
| [bobutils](./packages/bobutils/) | Shared workspace utilities for gptme agents (stdlib-only) |
| [credential-slots](./packages/credential-slots/) | Safe credential-slot rotation for OAuth/subscription-backed agents |
| [gptmail](./packages/gptmail/) | Universal email + inter-agent SSH messaging for agents |
| [gptme-activity-summary](./packages/gptme-activity-summary/) | Activity summarization — journals, GitHub, sessions, tweets, email |
| [gptme-backoff](./packages/gptme-backoff/) | Retry utilities: exponential backoff, jitter, async/sync decorators |
| [gptme-bob-status](./packages/gptme-bob-status/) | Bob-specific StatusProvider for `gptme-util status` |
| [gptme-cc-memory](./packages/gptme-cc-memory/) | Typed, git-tracked, hook-injected session memory for Claude Code |
| [gptme-codegraph](./packages/gptme-codegraph/) | Structural code retrieval with tree-sitter (call graph, blast/impact analysis, MCP tools) |
| [gptme-contrib-lib](./packages/gptme-contrib-lib/) | Shared utilities |
| [gptme-coordination](./packages/gptme-coordination/) | Inter-agent coordination via SQLite: work claims, message bus |
| [gptme-daily-briefing](./packages/gptme-daily-briefing/) | Daily briefing generation for agents |
| [gptme-dashboard](./packages/gptme-dashboard/) | Static dashboard generator for agent workspaces |
| [gptme-forum](./packages/gptme-forum/) | Git-native agent forum — threaded posts, @mentions, DMs |
| [gptme-lessons-extras](./packages/gptme-lessons-extras/) | Lesson validation and tools |
| [gptme-lessons-mcp](./packages/gptme-lessons-mcp/) | MCP server exposing gptme's lesson matching to any MCP client |
| [gptme-rag](./packages/gptme-rag/) | Local RAG with ChromaDB — semantic search as CLI, gptme tool, or MCP server |
| [gptme-runloops](./packages/gptme-runloops/) | Run loop framework for autonomous agent operation |
| [gptme-sessions](./packages/gptme-sessions/) | Session tracking, analytics, and trajectory extraction |
| [gptme-subscription](./packages/gptme-subscription/) | Subscription observation, pressure scoring, capacity-aware routing |
| [gptme-usage](./packages/gptme-usage/) | Cross-backend usage, cost, and quota surface (model registry, cost math) |
| [gptme-voice](./packages/gptme-voice/) | Real-time voice interface via OpenAI and xAI Grok Realtime APIs |
| [gptme-whatsapp](./packages/gptme-whatsapp/) | WhatsApp integration via whatsapp-web.js |
| [gptme-wisdom](./packages/gptme-wisdom/) | BM25-searchable index of canonical reference books |
| [gptme-wisdom-mcp](./packages/gptme-wisdom-mcp/) | RAG-as-MCP knowledge server — search books and session history |
| [gptodo](./packages/gptodo/) | Task management CLI and work queue generation |

## Scripts

Standalone scripts and integrations — see [scripts/README.md](./scripts/README.md) for the full index. Highlights:

| Area | Scripts |
|------|---------|
| Context | [context/](./scripts/context/) — agent system prompt generation |
| GitHub | [github/](./scripts/github/), [github_hygiene/](./scripts/github_hygiene/), [github_resolver/](./scripts/github_resolver/) |
| Social & messaging | [discord/](./scripts/discord/), [telegram/](./scripts/telegram/), [twitter/](./scripts/twitter/), [bluesky/](./scripts/bluesky/) |
| Issue tracking | [linear/](./scripts/linear/) |
| Research | [autoresearch/](./scripts/autoresearch/), [exa.py](./scripts/exa.py), [perplexity.py](./scripts/perplexity.py) |
| Health & quota | [status/](./scripts/status/), [check-quota.py](./scripts/check-quota.py), [fleet_vitals.py](./scripts/fleet_vitals.py) |
| Safety | [agent-write-loss-scan.py](./scripts/README.md#agent-write-loss-scan), [workspace_validator/](./scripts/workspace_validator/), [git/](./scripts/git/) |

## Skills, Lessons & More

- **[skills/](./skills/)** — Skill bundles (workflows + scripts + docs): agent onboarding, plugin development, code review, artifact publishing, Home Assistant, and more. See [skills/README.md](./skills/README.md).
- **[lessons/](./lessons/)** — Shared lessons injected into agent prompts by keyword match, across categories (autonomous, communication, infrastructure, patterns, social, tools, workflow). See [lessons/README.md](./lessons/README.md).
- **[dotfiles/](./dotfiles/)** — Global git hooks and config for safer agent development workflows. See [dotfiles/README.md](./dotfiles/README.md).
- **[docs/](./docs/)** — Plugin deep-dives and cross-agent protocols (e.g. the [heartbeat protocol](./docs/protocols/gptme-heartbeat.md)).

## Ecosystem

Beyond core, the template, and this repo:

| Project | Description |
|---------|-------------|
| [gptme-howto](https://github.com/gptme/gptme-howto) | Copy-paste recipes for gptme |
| [gptme-plugin-registry](https://github.com/gptme/gptme-plugin-registry) | Central registry for gptme plugins — metadata, indexing, discovery |
| [gptme-lessons](https://github.com/gptme/gptme-lessons) | Agent network protocol — shared lessons across forked agents |
| [gptme.vim](https://github.com/gptme/gptme.vim) | Vim plugin for gptme |
| [gptme-cc-plugin](https://github.com/gptme/gptme-cc-plugin) | Claude Code skills for gptme — `/gptme:run`, `/gptme:review`, `/gptme:context` |
| [gptme-skills-cc](https://github.com/gptme/gptme-skills-cc) | Proven gptme agent skills packaged as a Claude Code plugin |
| [agent-workspace-plugin](https://github.com/gptme/agent-workspace-plugin) | Claude Code plugin: persistent agent workspace (tasks, journal, lessons, knowledge) |

Archived: [gptme-rag](https://github.com/gptme/gptme-rag) (upstreamed into [packages/gptme-rag](./packages/gptme-rag/)), [gptme-webui](https://github.com/gptme/gptme-webui), [gptme-tauri](https://github.com/gptme/gptme-tauri).

### Discover more

To make your own plugin or skill discoverable, add a GitHub topic to your repo:

| Topic | For | Browse |
|-------|-----|--------|
| `gptme-plugin` | Python plugin packages | [↗](https://github.com/topics/gptme-plugin) |
| `gptme-skill` | SKILL.md skill bundles | [↗](https://github.com/topics/gptme-skill) |
| `gptme-mcp-server` | MCP servers for gptme | [↗](https://github.com/topics/gptme-mcp-server) |

```bash
gh repo edit owner/your-repo --add-topic gptme-plugin
```

## Dependencies

Some scripts require additional dependencies:

```bash
# Required for scripts with uv run shebangs
pipx install uv

# Install all packages
uv sync --all-packages
```

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for guidelines on contributing new tools, plugins, or lessons.

Plugins and packages here are community-contributed and may not be as mature or stable as core gptme functionality. They're a great place to experiment and share!

## License

MIT License - feel free to use and modify as you like!
