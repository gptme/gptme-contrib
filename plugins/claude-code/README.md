# gptme-claude-code

Claude Code plugin for gptme - spawn Claude Code subagents for various coding tasks.

## Features

This plugin allows gptme agents to leverage Claude Code (the `claude` CLI) as subagents for:

- **Analyze**: Code reviews, security audits, test coverage analysis
- **Ask**: Answer questions about codebases
- **Fix**: Fix lint errors, build issues, type errors
- **Implement**: Implement features in isolated worktrees

## Installation

Requires Claude Code CLI (`claude`) to be installed:

```bash
npm install -g @anthropic-ai/claude-code
```

Then install this plugin:

```bash
pip install gptme-claude-code
```

## Usage

The plugin provides several functions available via ipython:

### analyze() - Code Analysis

```python
# Quick security scan
analyze("Review this codebase for security vulnerabilities.")

# Code review
analyze("Review changes in the last commit for code quality issues.")

# Test coverage analysis
analyze("Analyze test coverage and identify critical untested paths.")
```

### ask() - Code Questions

```python
# Understand code structure
ask("How does the authentication flow work in this codebase?")

# Find implementations
ask("Where is the database connection pool configured?")
```

### fix() - Fix Issues

```python
# Fix lint errors
fix("Fix all mypy type errors in src/")

# Fix build issues
fix("The tests are failing with ImportError, diagnose and fix.")
```

### implement() - Implement Features

```python
# Simple implementation
implement("Add a --verbose flag to the CLI")

# Complex implementation (uses worktree for isolation)
implement("Implement rate limiting for the API endpoints", use_worktree=True)
```

### Background Tasks

For long-running tasks, use `background=True`:

```python
result = analyze("Comprehensive security audit", background=True, timeout=1800)
# Returns session ID

check_session("cc_abc12345")  # Check progress
kill_session("cc_abc12345")   # Cancel if needed
```

## Configuration

The plugin requires:

1. Claude Code CLI installed (`npm install -g @anthropic-ai/claude-code`)
2. Anthropic API key configured (usually via Claude Code's own auth)

## Cost Efficiency

Claude Code uses a subscription model ($200/mo for Pro), making it cost-effective
for parallel analysis tasks compared to direct API calls.

## When to Use

**Use Claude Code subagents for:**
- Single-purpose analysis (no gptme tool ecosystem needed)
- Parallel analysis tasks
- Tasks that benefit from fresh context (no accumulated history)
- Long-running analysis where background mode is appropriate

**Use gptme tools directly for:**
- Complex multi-step workflows
- Tasks requiring file modifications with review
- Interactive debugging sessions
- Tasks needing gptme's full context

## License

MIT
