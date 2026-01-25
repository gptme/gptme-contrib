# gptme-contrib Packages

Python packages for gptme agents.

## Packages

| Package | Purpose | Install |
|---------|---------|---------|
| **gptmail** | Email/message handling | `uv pip install -e packages/gptmail` |
| **gptodo** | Task management and work queues | `uv pip install -e packages/gptodo` |
| **gptme-lessons-extras** | Lesson format validation and analysis | `uv pip install -e packages/gptme-lessons-extras` |
| **gptme-contrib-lib** | Shared utilities across packages | `uv pip install -e packages/gptme-contrib-lib` |
| **gptme-runloops** | Autonomous run loop infrastructure | `uv pip install -e packages/gptme-runloops` |

## Backward Compatibility

Source-level symlinks are provided for backward compatibility with existing imports:
- `from lessons import ...` works via `gptme-lessons-extras/src/lessons` symlink
- `from lib import ...` works via `gptme-contrib-lib/src/lib` symlink
- `from run_loops import ...` works via `gptme-runloops/src/run_loops` symlink

## Structure

```text
packages/package-name/
├── pyproject.toml    # Config
├── src/package_name/ # Source (new name)
├── src/old_name/     # Symlink to package_name (backward compat)
└── tests/            # Tests
```

## Development

```shell
# Install workspace
uv sync --all-packages

# Run package tests
uv run pytest packages/gptodo/tests

# Run all tests
make test

# Type check
make typecheck
```

## Adding Dependencies

Edit `packages/NAME/pyproject.toml`, then:

```shell
uv sync
```

## References

- [uv workspaces](https://docs.astral.sh/uv/concepts/projects/workspaces/)
- Root [pyproject.toml](../pyproject.toml) - workspace configuration
