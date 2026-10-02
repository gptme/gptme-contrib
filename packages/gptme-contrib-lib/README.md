# gptme-contrib-lib

Shared library code for gptme agents.

## Overview

This package provides common utilities and shared code used across gptme-contrib packages and agent workspaces.

## Installation

```bash
uv pip install -e packages/gptme-contrib-lib
```

## Backward Compatibility

Source-level symlink is provided for backward compatibility:
- `from lib import ...` works via `src/lib` symlink

## Usage

See the package source for available utilities.

## References

- Root [pyproject.toml](../pyproject.toml) - workspace configuration
- [gptme-contrib packages](./README.md)
