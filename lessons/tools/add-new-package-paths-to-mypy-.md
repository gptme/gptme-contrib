---
match:
  keywords:
    - "mypy Can't find package error"
    - "new workspace package mypy failure"
    - "mypy_path missing new package"
    - "type checking fails after adding package"
    - "add package to mypy.ini"
status: active
---

# Add New Package Paths to mypy Configuration

## Rule
When creating a new workspace package, immediately add 'packages/PACKAGE_NAME/src' to the mypy_path setting in mypy.ini (or pyproject.toml) to ensure mypy can locate the package for type checking.

## Context
Mypy requires explicit source paths in its configuration to discover packages, even when those packages are properly installed in the workspace. Without the correct mypy_path entries, type checking will fail with 'Can't find package' errors despite the package being present and functional.

## Detection
- 'make typecheck' fails with "error: Can't find package 'PACKAGE_NAME'" despite package existing in workspace
- Mypy errors occur after creating a new package in packages/ directory
- Inspecting mypy configuration shows mypy_path is missing the new package's src directory

## Pattern
1. Create new package in packages/PACKAGE_NAME/
2. Open pyproject.toml or mypy.ini configuration file
3. Locate the mypy_path setting
4. Add 'packages/PACKAGE_NAME/src' to the mypy_path list
5. Run 'make typecheck' to verify mypy can now find the package

```toml
# In pyproject.toml
[tool.mypy]
mypy_path = "packages/metaproductivity/src,packages/context/src,packages/NEW_PACKAGE/src"
```

## Outcome
- **Type checking works**: mypy finds all workspace packages
- **CI passes**: No false positives from missing packages
- **Complete coverage**: All packages included in type checks

## Related
- [Configure pytest pythonpath](./configure-pytest-pythonpath-fo.md) - Similar pytest configuration
