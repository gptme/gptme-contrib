---
match:
  keywords:
    - "type: ignore"
    - "mypy import error"
    - "import-not-found"
    - "import-untyped"
    - "Cannot find implementation or library stub"
status: active
---

# Match mypy Error Codes to Actual Error Type

## Rule
Match mypy error codes to the actual error type when using `type: ignore` comments. Use `import-untyped` for packages missing type stubs, `import-not-found` for missing packages. Mismatched codes cause mypy to report both 'Unused type: ignore comment' and the original error.

## Context
Mypy requires specific error codes in `type: ignore` comments to suppress warnings. Using the wrong code (e.g., `import-not-found` when the package exists but lacks stubs) results in the comment being ignored, producing dual errors. The better solution is often to fix the root cause: install missing packages or add type stub dependencies.

## Detection
- Mypy reports 'Unused "type: ignore" comment' alongside the original error
- `type: ignore[import-not-found]` used but actual error is 'Cannot find implementation or library stub'
- `type: ignore[import-untyped]` used but package is actually missing from dependencies

## Pattern
**Common error codes**:
- `import-untyped`: Package exists but has no type stubs
- `import-not-found`: Package is completely missing
- `arg-type`: Wrong argument type
- `return-value`: Wrong return type
- `attr-defined`: Attribute not found

```python
# ❌ Wrong: Using import-not-found for installed package without stubs
import requests  # type: ignore[import-not-found]

# ✅ Correct: Using import-untyped for package without stubs
import requests  # type: ignore[import-untyped]

# ✅ Better: Add type stubs to dependencies
# In pyproject.toml: "types-requests"
```

## Outcome
- **Single error resolution**: No more dual error messages
- **Clear intent**: Error code documents why ignore is needed
- **Better fixes**: Often reveals need for type stub installation

## Related
- [Configure pytest pythonpath](./configure-pytest-pythonpath-fo.md) - Related Python import configuration
