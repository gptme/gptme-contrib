---
match:
  keywords:
  - path spaces
  - quoted path
  - spaces quoting
  - cd
  - too many arguments
---

# Shell Path Quoting for Spaces

## Rule
Always quote paths that may contain spaces in shell commands.

## Context
When constructing shell commands with file paths, especially user-provided paths or paths with spaces.

## Detection
Observable signals that you need proper quoting:
- Error: `bash: cd: too many arguments`
- Commands breaking on paths with spaces
- File operations failing with "No such file or directory" on partial paths
- Arguments split incorrectly due to unquoted spaces

## Pattern
```shell
# Wrong: unquoted path with spaces
cd /path with spaces  # Error: too many arguments

# Correct: quoted path
cd "/path with spaces"

# Wrong: unquoted variable expansion
cd $PROJECT_PATH  # Breaks if path has spaces

# Correct: quoted variable
cd "$PROJECT_PATH"

# Wrong: unquoted command substitution
cd $(get_project_path)  # Breaks if returned path has spaces

# Correct: quoted command substitution
cd "$(get_project_path)"
```

## Outcome
Following this pattern prevents:
- Word splitting errors (too many arguments)
- Partial path failures (No such file)
- Command argument misinterpretation

Results in:
- Reliable path handling with or without spaces
- Consistent behavior across all environments

## Related
- [Shell Command Chaining](./shell-command-chaining.md) - Combining commands
- [Python Invocation](./python-invocation.md) - Python execution
