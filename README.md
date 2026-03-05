# gptme-contrib

Community-contributed plugins, packages, scripts, and lessons for [gptme](https://github.com/gptme/gptme).

## Overview

This repository contains:
- **[`plugins/`](./plugins/)** - Extend gptme with custom functionality ([gptme docs](https://gptme.org/docs/plugins.html))
- **[`packages/`](./packages/)** - Reusable Python packages
- **[`scripts/`](./scripts/)** - Standalone scripts for automation
- **[`lessons/`](./lessons/)** - Shared lessons for prompts and workflows

## Plugins

Plugins extend gptme's capabilities with custom tools and hooks. See [plugins/README.md](./plugins/README.md) for details.

| Plugin | Description |
|--------|-------------|
| [gptme-ace](./plugins/gptme-ace/) | ACE-inspired context optimization |
| [gptme-attention-tracker](./plugins/gptme-attention-tracker/) | Attention routing + history tracking for context management |
| [gptme-claude-code](./plugins/gptme-claude-code/) | Claude Code subagent integration |
| [gptme-consortium](./plugins/gptme-consortium/) | Multi-model consensus decision-making |
| [gptme-gptodo](./plugins/gptme-gptodo/) | gptodo delegation plugin for coordinator-only agent mode |
| [gptme-gupp](./plugins/gptme-gupp/) | Work persistence for session continuity |
| [gptme-hooks-examples](./plugins/gptme-hooks-examples/) | Example hook implementations |
| [gptme-imagen](./plugins/gptme-imagen/) | Multi-provider image generation |
| [gptme-lsp](./plugins/gptme-lsp/) | Language Server Protocol integration |
| [gptme-ralph](./plugins/gptme-ralph/) | Ralph Loop pattern — iterative execution with context reset |
| [gptme-retrieval](./plugins/gptme-retrieval/) | Automatic context retrieval via semantic/keyword search |
| [gptme-warpgrep](./plugins/gptme-warpgrep/) | Enhanced search with Warp-style filtering |
| [gptme-wrapped](./plugins/gptme-wrapped/) | Year-end analytics for your gptme usage (Spotify Wrapped-style) |

### Plugin Usage

Add to your `gptme.toml`:

```toml
[plugins]
paths = ["path/to/gptme-contrib/plugins"]
enabled = ["gptme_attention_tracker", "gptme_imagen"]
```

## Packages

Reusable Python packages. See [packages/README.md](./packages/README.md).

| Package | Description |
|---------|-------------|
| [gptmail](./packages/gptmail/) | Universal email system for AI agents |
| [gptodo](./packages/gptodo/) | Task management CLI and utilities |
| [gptme-activity-summary](./packages/gptme-activity-summary/) | Activity summarization for agents — journals, GitHub, sessions, tweets, email |
| [gptme-sessions](./packages/gptme-sessions/) | Session tracking, analytics, and trajectory extraction |
| [gptme-voice](./packages/gptme-voice/) | Voice interface using OpenAI Realtime API |
| [gptme-whatsapp](./packages/gptme-whatsapp/) | WhatsApp integration for agents via whatsapp-web.js |
| [gptme_lessons_extras](./packages/gptme-lessons-extras/) | Lesson validation and tools |
| [gptme_runloops](./packages/gptme-runloops/) | Agent run loop patterns |
| [gptme_contrib_lib](./packages/gptme-contrib-lib/) | Shared utilities |

## Scripts

Standalone scripts for automation. See each directory's README for details.

| Directory | Description |
|-----------|-------------|
| [context/](./scripts/context/) | Context generation for agent system prompts |
| [discord/](./scripts/discord/) | Discord bot integration |
| [github/](./scripts/github/) | GitHub context generation, repo status |
| [linear/](./scripts/linear/) | Linear issue tracking integration |
| [telegram/](./scripts/telegram/) | Telegram bot integration |
| [twitter/](./scripts/twitter/) | Twitter automation and monitoring |
| [bluesky/](./scripts/bluesky/) | Bluesky integration |
| [status/](./scripts/status/) | Agent infrastructure status monitoring |
| [workspace_validator/](./scripts/workspace_validator/) | Agent workspace structure validation |

## Lessons

Shared lessons provide reusable prompts and workflow patterns. See [lessons/README.md](./lessons/README.md).

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
