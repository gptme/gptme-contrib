---
match:
  keywords:
  - python command
  - python3
  - command not found
  - python invocation
automation:
  status: automated
  validator: scripts/precommit/validators/validate_python_invocation.py
  enforcement: warning
  automated_date: 2025-11-26
lesson_id: tools_python-invocation_265c0a80
version: 1.0.0
usage_count: 1
helpful_count: 1
harmful_count: 0
created: '2025-11-04T18:14:42.634076Z'
updated: '2025-11-04T18:24:16.449224Z'
last_used: '2025-11-04T18:24:16.449224Z'
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
- 4+ occurrences found in autonomous run logs

## Pattern
Show the minimal correct approach:
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

Benefits demonstrated:
- Prevents "command not found" errors (4+ occurrences avoided)
- Works on systems without `python` symlink
- Explicit about Python version used
- Compatible with CI/CD workflows

**Note**: This error is now automatically detected by [Shell Command Validation](../../TOOLS.md#shell-command-validation) before execution.

## Related
- [Python File Execution](./python-file-execution.md) - Context-aware execution methods
- [Shell Command Validation](../../TOOLS.md#shell-command-validation) - Automated detection
- [PEP 394](https://peps.python.org/pep-0394/) - Python version specification
