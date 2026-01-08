---
match:
  keywords:
  - heredoc
  - EOF
  - "<<EOF"
  - "<<-"
  - multiline shell
  - multiline input
  - shell multiline
  - cat <<
lesson_id: tools_shell-heredoc-avoidance_lofty
version: 1.0.0
usage_count: 0
helpful_count: 0
harmful_count: 0
created: '2026-01-08T23:10:00Z'
updated: '2026-01-08T23:10:00Z'
last_used: null
---

# Avoid Heredoc in Shell Commands

## Context
When using the shell tool to execute commands in gptme.

## Problem
Heredoc/EOF syntax (`<<EOF ... EOF`) is not supported by gptme's shell tool and will fail with syntax errors or unexpected behavior.

## Solution
Use alternative approaches for multiline content:
1. **save tool** (preferred for files)
2. **echo with newlines** (for simple content)
3. **printf** (for formatted content)
4. **Multiple echo commands** (for appending)

## Anti-pattern
```shell
# DON'T DO THIS - will fail
cat << EOF > config.yaml
name: myproject
version: 1.0.0
EOF
```

## Correct Patterns

### Pattern 1: Use save tool (recommended for files)
```save config.yaml
name: myproject
version: 1.0.0
```

### Pattern 2: Echo with embedded newlines
```shell
echo "name: myproject
version: 1.0.0" > config.yaml
```

### Pattern 3: Printf for more control
```shell
printf "name: myproject\nversion: 1.0.0\n" > config.yaml
```

## Why This Matters
The shell tool executes commands line by line and cannot handle heredoc syntax which spans multiple lines in a special block format.

## Related
- [Shell Command Chaining](./shell-command-chaining.md) - For multi-command sequences
- [Shell Path Quoting](./shell-path-quoting.md) - For path handling

## Origin
- Date: 2024-12-05
- Source: Common failure pattern in gptme autonomous sessions
- Impact: Medium (frequently attempted, clear alternatives exist)
