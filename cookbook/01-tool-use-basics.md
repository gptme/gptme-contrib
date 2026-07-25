---
title: Tool Use Basics
description: Use gptme's built-in tools to read files, run shell commands, and write code in a single session
category: tool-use
difficulty: beginner
tags: [tools, shell, files, beginner]
deep_link: "https://gptme.ai/?prompt=Read+the+file+README.md+and+summarize+it+in+3+bullet+points%2C+then+check+if+there+are+any+TODO+comments+in+the+codebase."
---

# Tool Use Basics

## Problem

New gptme users often treat it as a chatbot. The real power is that gptme can
**act** — read and write files, run commands, and inspect output — all in one
conversational session without leaving the terminal.

## Solution

gptme ships with built-in tools that are always available:

| Tool | What it does |
|------|-------------|
| `shell` | Run any shell command and capture stdout/stderr |
| `save` / `patch` | Write or edit files |
| `ipython` | Execute Python code in a persistent interpreter |
| `read` | Read file contents into context |
| `browser` | Fetch a URL (if enabled) |

The agent orchestrates these tools automatically based on your request. You
don't need to invoke them explicitly.

## Example

Start a session and ask a compound question that requires multiple tools:

```bash
gptme "Read README.md, list the project's dependencies from pyproject.toml, and tell me if any are pinned to an old major version"
```

gptme will:
1. Read `README.md` via `read`
2. Read `pyproject.toml` via `read`
3. Reason about version ranges
4. Reply with findings

For interactive iteration, start a session and keep chatting:

```bash
gptme  # opens an interactive session
# Then type:
# > Read the failing tests and fix them
# > Now run the test suite and show me the output
# > Write a summary of what you changed to CHANGES.md
```

## Notes

- Tools run in your local environment with your permissions. Keep sessions
  scoped to the project directory.
- Use `gptme --tools` to see which tools are enabled in the current session.
- Add `--no-confirm` to skip per-tool confirmation prompts in trusted sessions.
