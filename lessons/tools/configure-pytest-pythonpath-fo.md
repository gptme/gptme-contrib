---
match:
  keywords:
    - "pytest ModuleNotFoundError"
    - "tests fail to import"
    - "pytest pythonpath"
    - "workspace package not found"
    - "cannot import in tests"
status: active
---

# Configure pytest pythonpath for Monorepo

## Rule
Configure pytest pythonpath in pyproject.toml to include all workspace package source directories when working in a monorepo structure.

## Context
Pytest runs in its own execution context and cannot automatically discover workspace packages in a monorepo. Without explicit pythonpath configuration, pytest will fail to import local packages even when they are properly structured.

## Detection
- Pytest fails with 'ModuleNotFoundError: No module named "<package>"' when running tests
- Tests attempt to import from workspace packages (e.g., 'from context import ...')
- Project has multiple packages under a packages/ directory structure
- No pythonpath configuration exists in pyproject.toml [tool.pytest.ini_options]

## Pattern
Add pythonpath to pyproject.toml (list all workspace package src directories):
```toml
[tool.pytest.ini_options]
pythonpath = [
    "packages/mypkg1/src",
    "packages/mypkg2/src",
    # add one entry per workspace package
]
```

After adding configuration:
```shell
# Tests should now find packages
uv run pytest tests/ -v
```

## Outcome
- **Tests pass**: pytest finds all workspace packages
- **Consistent behavior**: Same imports work in tests and runtime
- **Clear configuration**: All paths documented in one place

## Related
- [Match mypy error codes](./match-mypy-error-codes-to-actu.md) - Related mypy configuration
- [uv sync after workspace changes](./uv-sync-all-packages-after-changes.md) - Keeping packages installed
