---
match:
  keywords:
    - "ModuleNotFoundError workspace package"
    - "package not installed despite pyproject.toml"
    - "uv workspace package missing"
    - "cannot import workspace package after adding"
    - "uv sync all packages after changes"
status: active
---

# Run uv sync --all-packages After Workspace Changes

## Rule
After modifying workspace structure (adding dependencies or creating new packages), run 'uv sync --all-packages' to install all workspace packages and make them importable.

## Context
In uv workspaces, packages can exist in the workspace configuration but not be installed in the virtual environment. This creates a disconnect where the workspace knows about packages, but Python cannot import them at runtime.

## Detection
- ModuleNotFoundError when importing workspace packages that exist in pyproject.toml
- 'uv pip list | grep <package>' shows package NOT installed despite being in workspace
- After adding new workspace packages or dependencies to existing packages

## Pattern
When workspace structure changes:
1. Verify package installation: `uv pip list | grep <package>`
2. If missing, run: `uv sync --all-packages`
3. Confirm all workspace packages are installed
4. Test imports work correctly

```shell
# Check if package is installed
uv pip list | grep metaproductivity

# If missing, sync all packages
uv sync --all-packages

# Verify installation
python3 -c "import metaproductivity; print('OK')"
```

## Outcome
- All workspace packages become importable immediately after sync
- Eliminates ModuleNotFoundError for workspace packages
- Ensures virtual environment matches workspace configuration

## Related
- [Configure pytest pythonpath](./configure-pytest-pythonpath-fo.md) - Related import configuration for tests
