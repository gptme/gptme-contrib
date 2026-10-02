# gptme-contrib Packages

Python packages for gptme agents. All packages (and plugins) are members of one [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/).

## Packages

The full list of packages, grouped by category with status and one-line descriptions, is in the [top-level README](../README.md#catalog-by-category). Each package directory has its own README with install and usage details.

From a clone, install a single package in editable mode with:

```shell
uv pip install -e packages/<name>
```

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
