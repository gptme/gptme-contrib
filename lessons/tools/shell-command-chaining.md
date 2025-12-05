---
match:
  keywords:
  - command chaining
  - shell
  - sequential execution
  - operator
lesson_id: tools_shell-command-chaining_1e9a06b7
version: 1.0.0
usage_count: 1
helpful_count: 1
harmful_count: 0
created: '2025-11-04T18:14:42.636973Z'
updated: '2025-11-04T18:24:16.449224Z'
last_used: '2025-11-04T18:24:16.449224Z'
---

# Shell Command Chaining

## Rule
Chain related shell commands in a single block instead of multiple separate executions.

## Context
When executing multiple related shell commands where output feeds into the next, or when setting up and verifying a state.

## Detection
Observable signals indicating need for command chaining:
- Multiple consecutive shell blocks doing related operations
- Environment variables set in one block, used in next
- Commands that logically belong together split across blocks
- Repeated directory changes or context switches
- Variable scoping issues from split blocks

## Pattern
Chain related commands with appropriate operators:
```shell
# Anti-pattern: Split blocks lose context
export PROJECT_DIR=/path/to/project
# (separate block)
cd $PROJECT_DIR
# (separate block)
npm install

# Correct: Chain with && operator
export PROJECT_DIR=/path/to/project
cd $PROJECT_DIR && npm install

# OR: Single command sequence
cd /path/to/project && npm install && npm test
```

**Operator choices**:
- `&&`: Sequential execution (stops on error)
- `;`: Unconditional execution (continues on error)
- `||`: Fallback (runs if previous fails)
- `|`: Pipe output to next command

## Outcome
Following this pattern results in:
- **Efficiency**: Single execution context, no overhead
- **Reliability**: Environment preserved, variables stay in scope
- **Atomicity**: Operations complete together or fail together
- **Clarity**: Complete operation visible at once

Benefits:
- No variable scoping issues
- Reduced tool execution overhead
- Clear logical grouping

## Related
- See gptme's shell tool documentation for capabilities
