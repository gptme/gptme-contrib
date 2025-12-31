---
match:
  keywords:
  # FOUNDATIONAL KEYWORDS - Keep these general terms!
  # Python invocation is extremely common. These general terms
  # ensure the lesson triggers when users discuss Python execution.
  # Do NOT remove as "too broad" - this is a fundamental lesson.
  - "python"
  - "python3"
  # ERROR SIGNALS - Exact errors (high precision)
  - "bash: python: command not found"
  - "python command not found"
  # WORKFLOW CONTEXT - Medium specificity
  - "use python3 instead of python"
  - "python vs python3"
status: active
---

# Python Invocation

## Rule
Always use `python3` explicitly instead of `python` in shell commands and scripts.

## Context
When executing Python code from shell commands, scripts, or automated workflows on systems that may not have a `python` symlink.

## Detection
Observable signals that you need `python3`:
- Error: `bash: python: command not found`
- Python scripts failing with "command not found"
- Running on Ubuntu 20.04+ or Debian 11+ systems
- Working with Docker images or CI/CD pipelines

## Pattern
```shell
# Wrong: assumes python exists
python script.py  # Error: command not found

# Correct: explicit python3
python3 script.py

# Wrong: shebang assuming python exists
#!/usr/bin/env python

# Correct: explicit python3 shebang
#!/usr/bin/env python3

# Wrong: subprocess with python
import subprocess
subprocess.run(['python', 'script.py'])

# Correct: subprocess with python3
subprocess.run(['python3', 'script.py'])
```

## Outcome
Following this pattern results in:
- **System compatibility**: Works on Ubuntu 20.04+, Debian 11+, modern Docker images
- **Reliability**: `python3` is guaranteed on Python 3 systems
- **Consistency**: Same command works across all environments
- **PEP 394 compliance**: Follows Python's official recommendation

Benefits:
- Prevents "command not found" errors
- Works on systems without `python` symlink
- Explicit about Python version used
- Compatible with CI/CD workflows

## Related
- [Python File Execution](./python-file-execution.md) - Context-aware execution methods
- [Shell Command Chaining](./shell-command-chaining.md) - Combining commands
- [PEP 394](https://peps.python.org/pep-0394/) - Python version specification
