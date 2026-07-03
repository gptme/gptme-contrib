---
match:
  keywords:
    - "doesn't have a flag for"
    - "wrap the command to add"
    - "before patching the wrapper"
status: active
description: "Before examining source code or making assumptions about a CLI tool's capabilities, run the command with --help to discover available options, subcommands, and flags that can simplify your approach."
confound_note: "fires in wrapper-patching sessions (selection-effect: these are harder tasks already past the discovery phase)"
---

# Check CLI --help Before Reading Source Code

## Rule
Before examining source code or making assumptions about a CLI tool's capabilities, run the command with --help to discover available options, subcommands, and flags that can simplify your approach.

## Context
When encountering a new command-line tool or unfamiliar command, developers often jump directly to reading source code or making assumptions about functionality. This premature deep-dive wastes time and may miss built-in features that solve the problem more elegantly.

## Detection
Applies during **discovery phase** only — when you have not yet run the tool or verified its interface:
- About to examine source code of a CLI tool to understand its capabilities
- Considering how to use a command you haven't run before
- Making assumptions about what options a tool might support
- Planning a complex workaround for what might be a built-in feature

**Does not apply** when already mid-implementation (wrapper patching, calling known subcommands, iterating on a fix) — the --help lookup has already been done implicitly or explicitly at that point.

## Pattern
1. Run `<command> --help` first to see top-level options and subcommands
2. For subcommands, run `<command> <subcommand> --help` to discover specific flags
3. Look specifically for utility flags like --dry-run, --status, --format, --verbose
4. Only dive into source code after understanding the documented interface

Example: Running `uv run gptodo --help` and `uv run gptodo edit --help` reveals subcommands and flags (`--set`, `--add`, `--state`) that obviate building a wrapper or sed-editing YAML frontmatter directly.

## Outcome
- **Time savings**: Discover built-in features vs building workarounds
- **Better solutions**: Use intended interface vs hacking alternatives
- **Faster learning**: --help is faster than reading source code
- **Correct usage**: Avoid misusing undocumented internals

## Related
- [Shell Command Chaining](./shell-command-chaining.md) - Efficient command execution
- LOO analysis 2026-07-03: Δ=-0.0893 (p=0.0027, n=21) — genuinely harmful. Keywords fire in mid-implementation sessions (past discovery phase) where the reminder is irrelevant. `confound_note` added; Detection narrowed to discovery-only.
