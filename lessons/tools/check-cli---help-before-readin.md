---
match:
  keywords:
    - "examine source code to understand CLI"
    - "planning complex workaround"
    - "making assumptions about CLI options"
    - "unfamiliar command usage"
    - "discover CLI flags and options"
status: active
---

# Check CLI --help Before Reading Source Code

## Rule
Before examining source code or making assumptions about a CLI tool's capabilities, run the command with --help to discover available options, subcommands, and flags that can simplify your approach.

## Context
When encountering a new command-line tool or unfamiliar command, developers often jump directly to reading source code or making assumptions about functionality. This premature deep-dive wastes time and may miss built-in features that solve the problem more elegantly.

## Detection
- About to examine source code of a CLI tool to understand its capabilities
- Considering how to use a command you haven't run before
- Making assumptions about what options a tool might support
- Planning a complex workaround for what might be a built-in feature

## Pattern
1. Run `<command> --help` first to see top-level options and subcommands
2. For subcommands, run `<command> <subcommand> --help` to discover specific flags
3. Look specifically for utility flags like --dry-run, --status, --format, --verbose
4. Only dive into source code after understanding the documented interface

Example: Running `uv run python3 -m ace.curator generate --help` revealed --dry-run option, enabling safe testing without API calls instead of building a complex validation workaround.

## Outcome
- **Time savings**: Discover built-in features vs building workarounds
- **Better solutions**: Use intended interface vs hacking alternatives
- **Faster learning**: --help is faster than reading source code
- **Correct usage**: Avoid misusing undocumented internals

## Related
- [Shell Command Chaining](./shell-command-chaining.md) - Efficient command execution
