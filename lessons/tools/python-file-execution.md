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
status: active
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
**Quick decision**:
```text
Script has shebang (#!/usr/bin/env python3) and +x → ./script.py
Poetry project → poetry run python script.py
uv project → uv run python script.py
Standalone → python3 script.py
```

**For uv scripts with inline metadata**:
```bash
# If script has: # /// script dependencies = [...] ///
./script.py  # Direct execution works

# Or explicitly
uv run script.py
```

**For poetry projects**:
```bash
poetry run python script.py
poetry run pytest
```

**Setting executable permissions**:
```bash
chmod +x script.py
./script.py
```

## Outcome
Following this pattern leads to:
- Scripts execute with correct dependencies
- No permission errors
- Proper tooling context available
- Consistent execution across environments

## Related
- [Python Invocation](./python-invocation.md) - Use `python3` not `python`
- [Shell Command Chaining](./shell-command-chaining.md) - Combining commands
