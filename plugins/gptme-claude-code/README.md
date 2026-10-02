# gptme-claude-code — delegate coding tasks from gptme to Claude Code

Lets a gptme agent spawn the Claude Code CLI (`claude -p`) as a one-shot
subagent to analyze, answer questions about, fix, or implement things in a
codebase — synchronously or in a background tmux session.

**Status:** experimental. Small, thin wrapper; behaviour is mostly "build a
prompt, run `claude -p`, return the output".

## Why / when to use it

Use a Claude Code subagent when the task is self-contained and benefits from a
fresh context: a security review, "where is X configured?", fixing a failing
build, or implementing a small feature while the main gptme session keeps its
own context (and prompt cache) intact. Several background runs can go in
parallel.

Keep the work in gptme itself for multi-step workflows that need gptme's tools,
lessons, or interactive review.

Alternatives: gptme's built-in `subagent` tool spawns gptme subagents, and can
drive any ACP-compatible agent (`use_acp=True, acp_command=...`).

## Requirements

- The `claude` CLI on `PATH`, already authenticated
  (`npm install -g @anthropic-ai/claude-code`).
- `tmux` for `background=True`.

## Install

Not published on PyPI. Install into the same environment as gptme:

```bash
pip install "gptme-claude-code @ git+https://github.com/gptme/gptme-contrib.git#subdirectory=plugins/gptme-claude-code"
```

It registers a `gptme.plugins` entry point named `gptme_claude_code`, so it
loads automatically (add `gptme_claude_code` to `[plugins] enabled` if you use
an allowlist). Or load it from a checkout without installing:

```toml
# gptme.toml
[plugins]
paths = ["path/to/gptme-contrib/plugins/gptme-claude-code"]
```

This adds a `claude_code` tool whose functions are callable from gptme's
Python (ipython) tool.

## Quickstart

```python
# Quick question (sync, default timeout 300s)
ask("Where is the database connection pool configured?")

# Fix something without committing (default); auto_commit=True to commit
fix("The tests fail with ImportError in test_api.py — diagnose and fix.")

# Longer work: run in tmux and poll
sid = analyze("Review this codebase for security vulnerabilities.",
              background=True, timeout=1800)
check_session("claude_code_a1b2c3d4")   # session ID from the returned message
kill_session("claude_code_a1b2c3d4")
```

## Functions

| Function | Default timeout | Notes |
|----------|-----------------|-------|
| `analyze(prompt, workspace=None, timeout=600, background=False)` | 600s | Sync calls with `timeout >= 300` raise `ValueError` (to avoid blocking the session and losing the prompt cache) — pass `background=True` or a shorter `timeout` |
| `ask(question, workspace=None, timeout=300, background=False)` | 300s | |
| `fix(issue, workspace=None, timeout=600, background=False, auto_commit=False)` | 600s | Tells Claude Code not to commit unless `auto_commit=True` |
| `implement(feature, workspace=None, timeout=900, background=False, use_worktree=False, branch_name=None)` | 900s | `use_worktree=True` instructs Claude Code to work in `../worktree-<branch>` on a new branch |
| `check_session(session_id)` | | Shows tmux pane output for a background run |
| `kill_session(session_id)` | | Kills a background run |

`workspace` defaults to the current directory. Sync calls return a result with
the output, exit code and duration; background calls return a message
containing the tmux session ID (`claude_code_<8 hex>`).

## Notes

- Authentication, model choice and billing are whatever your `claude` CLI is
  configured with; this plugin passes no model or permission flags.
- Background runs are wrapped in `timeout <seconds>` inside tmux.

## License

MIT
