# gptme-contrib

Community-contributed plugins, packages, scripts, and lessons for [gptme](https://github.com/ErikBjare/gptme).

## Overview

This repository contains:
- **[`plugins/`](./plugins/)** - Extend gptme with custom functionality ([gptme docs](https://gptme.org/docs/custom_tool.html))
- **[`packages/`](./packages/)** - Reusable Python packages
- **[`scripts/`](./scripts/)** - Standalone scripts for automation
- **[`lessons/`](./lessons/)** - Shared lessons for prompts and workflows

## Plugins

Plugins extend gptme's capabilities with custom tools and hooks. See [plugins/README.md](./plugins/README.md) for details.

| Plugin | Description |
|--------|-------------|
| [ace](./plugins/ace/) | ACE-inspired context optimization |
| [gptme-attention-tracker](./plugins/gptme-attention-tracker/) | Attention routing + history tracking for context management |
| [gptme-claude-code](./plugins/gptme-claude-code/) | Claude Code subagent integration |
| [gptme-consortium](./plugins/gptme-consortium/) | Multi-model consensus decision-making |
| [gptme-gupp](./plugins/gptme-gupp/) | Work persistence for session continuity |
| [gptme-hooks-examples](./plugins/gptme-hooks-examples/) | Example hook implementations |
| [gptme-imagen](./plugins/gptme-imagen/) | Multi-provider image generation |
| [gptme-lsp](./plugins/gptme-lsp/) | Language Server Protocol integration |
| [gptme-warpgrep](./plugins/gptme-warpgrep/) | Enhanced search with Warp-style filtering |
| [gptme-wrapped](./plugins/gptme-wrapped/) | Wrapped tool definitions for sandboxing |

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
| [lessons](./packages/lessons/) | Lesson validation and tools |
| [run_loops](./packages/run_loops/) | Agent run loop patterns |
| [lib](./packages/lib/) | Shared utilities |

## Scripts

Standalone scripts for automation. See each directory's README for details.

| Directory | Description |
|-----------|-------------|
| [github/](./scripts/github/) | GitHub context generation, repo status |
| [twitter/](./scripts/twitter/) | Twitter automation and monitoring |
| [discord/](./scripts/discord/) | Discord bot integration |
| [bluesky/](./scripts/bluesky/) | Bluesky integration |

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
