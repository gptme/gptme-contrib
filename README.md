# gptme-contrib

Community-contributed tools and scripts for [gptme](https://github.com/ErikBjare/gptme).

## Overview

This repository contains:
- [Custom `tools/`](https://gptme.org/docs/custom_tool.html) that extend gptme's functionality
- Standalone `scripts/` that can be used via the shell tool
- Shared `lessons/` for agents

This repo is meant as a place for the community to share tools and scripts that they have created for gptme, but are not general/mature/stable enough to be included in the core gptme repository.

If you have a tool you'd like to share, please consider contributing it here!

## Dependencies

Some scripts in this repository require additional dependencies:

- **uv**: Required for scripts with `#!/usr/bin/env -S uv run` shebangs
  ```bash
  pipx install uv
  ```

## Usage

### Custom Tools

No custom tools in this repository yet. Check back later!

<!--
```python
# In your gptme config:
TOOL_MODULES = "gptme.tools,gptme_contrib.tools"
```
-->

### Script Tools

Scripts can be used directly via the shell tool:

```bash
# Make scripts executable
chmod +x scripts/twitter.py

# Use ./ to respect shebang
./scripts/twitter.py --help
```

## Available Scripts

### GitHub Integration (scripts/github/)

Scripts for GitHub context generation and repository management:

- **context-gh.sh** - Generate comprehensive GitHub context (notifications, issues, PRs, CI status)
- **repo-status.sh** - Check CI status across multiple repositories

See [scripts/github/README.md](scripts/github/README.md) for detailed documentation.

### Workspace Management

- **state-status.py** - Multi-directory state viewer (tasks, tweets, email, etc.)
- **search.py** - Enhanced workspace search with filtering

### Social Media

- **twitter/** - Twitter automation and monitoring
- **bluesky/** - Bluesky integration
- **discord/** - Discord bot

### Communication

- **email/** - Universal email system for AI agents

### Other

- **gptodo** - Task management utilities
- **wordcount.py** - Word counting utilities
- **perplexity.py** - Perplexity API integration

## Structure

- `tools/` - Custom tools for gptme
- `scripts/` - Standalone script tools
- `lessons/` - Shared lessons for prompts and workflows
  - `lessons/workflow/` - Workflow lessons (e.g., git-workflow.md)

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for guidelines on contributing new tools.

## License

MIT License - feel free to use and modify as you like!
