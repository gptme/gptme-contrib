---
match:
  keywords:
  - path spaces
  - quoted path
  - spaces quoting
  - cd
  - too many arguments
lesson_id: tools_shell-path-quoting_2d9b4b60
version: 1.0.0
usage_count: 0
helpful_count: 0
harmful_count: 0
created: '2025-11-04T18:14:42.642904Z'
updated: '2025-11-27T07:02:00.000000Z'
last_used: null
automation:
  status: automated
  validator: scripts/lessons/validators/shell_path_quoting.py
  detection_scope: workspace
  violation_baseline: 0
  pre_commit_integrated: true
  integration_date: '2025-11-27'
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

Common errors from logs (5+ occurrences):
- `cd /path with spaces` → too many arguments
- `cat /file name.txt` → No such file: /file
- `mv /source path /dest path` → Incorrect argument count

## Pattern
Show the minimal correct approach:
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
- Fewer "cd: too many arguments" errors
- Consistent behavior across all environments

## Related
- [Shell Command Chaining](./shell-command-chaining.md) - Chain related commands
