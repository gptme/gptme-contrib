# gptme-contrib

Community-contributed tools and scripts for [gptme](https://github.com/ErikBjare/gptme).

## Overview

This repository contains:
- Custom tools that extend gptme's functionality
- Standalone scripts that can be used via the shell tool
- Utilities and helpers for tool development

## Installation

```bash
# Install base package
pip install gptme-contrib

# Install with specific extras
pip install "gptme-contrib[social]"    # Social media tools
pip install "gptme-contrib[dev]"       # Development tools
pip install "gptme-contrib[ai]"        # AI/ML tools

# Install all extras
pip install "gptme-contrib[all]"
```

## Usage

### Custom Tools

```python
# In your gptme config:
TOOL_MODULES = "gptme.tools,gptme_contrib.tools"
```

### Script Tools

Scripts can be used directly via the shell tool:

```bash
# Make scripts executable
chmod +x scripts/twitter-cli.py

# Use via shell tool
./scripts/twitter-cli.py "Hello, world!"
```

## Structure

- `src/gptme_contrib/tools/` - Custom tools
  - `social/` - Social media tools
  - `dev/` - Development tools
  - `ai/` - AI/ML tools
  - `system/` - System utilities
- `scripts/` - Standalone script tools

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for guidelines on contributing new tools.

## License

MIT License - feel free to use and modify as you like!
