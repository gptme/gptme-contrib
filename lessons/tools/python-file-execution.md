---
match:
  keywords:
  - python execution
  - script execution
  - ./script.py
  - permission denied
  - shebang
  - uv run
  - poetry run
lesson_id: tools_python-file-execution_ff3c2971
version: 1.0.0
usage_count: 0
helpful_count: 0
harmful_count: 0
created: '2025-11-04T18:14:42.632728Z'
updated: '2025-11-04T18:14:42.632728Z'
last_used: null
---

# Python File Execution

## Rule
Choose the correct Python execution method based on script type and context: uv scripts with shebang use direct execution, poetry projects use `poetry run`, standalone scripts use appropriate tooling.

## Context
When running Python scripts from shell commands or automated workflows in any repository or project context.

## Detection
Observable signals that you need proper execution method:
- Error: `bash: FILENAME.py: Permission denied`
- Scripts failing without proper tooling context
- Missing dependencies in uv or poetry projects
- ModuleNotFoundError when dependencies should be available
- Commands failing with permission or module errors

## Pattern
Show the decision tree and minimal examples:

**Quick decision**:
```text
Poetry project (pyproject.toml)? → poetry run python3 script.py
UV script (#!/usr/bin/env -S uv run)? → ./script.py
Standalone with deps? → uv run --script script.py
Simple stdlib-only? → python3 script.py
```

**Examples**:
```shell
# Correct: direct execution for uv script
./scripts/tasks.py  # UV handles dependencies

# Correct: poetry run in poetry project
poetry run python3 test_script.py
poetry run pytest tests/

# Correct: python3 for simple script without deps
python3 analyze.py  # Stdlib-only script
```

## Outcome
Following this pattern results in:
- **Context-aware execution**: Right method for each script type
- **No permission errors**: Proper shebangs or explicit interpreter
- **Dependencies available**: UV/poetry manage dependencies correctly
- **Reliable automation**: Scripts work in different contexts

Benefits demonstrated:
- Poetry projects: virtualenv with all dependencies
- UV scripts: automatic dependency management
- Standalone scripts: explicit interpreter invocation
- No ModuleNotFoundError from missing dependencies

## Related

- [Python Invocation](./python-invocation.md) - Use python3 not python
