# gptme-contrib

Community-contributed tools and scripts for [gptme](https://github.com/ErikBjare/gptme).

## Overview

This repository contains:
- [Custom tools](https://gptme.org/docs/custom_tool.html) that extend gptme's functionality
- Standalone scripts that can be used via the shell tool
- Utilities and helpers for tool development

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

# Use via shell tool
./scripts/twitter.py --help
```

## Structure

- `tools/` - Custom tools for gptme
- `scripts/` - Standalone script tools

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for guidelines on contributing new tools.

## License

MIT License - feel free to use and modify as you like!
