"""Map harness tool names to harness-agnostic classes, and spot landing commands.

Commit and push detection reuses gptme-sessions' shell parser (tree-sitter-bash
when installed, regex otherwise), so this stream and the session ledger agree
on what counts as a commit.
"""

from __future__ import annotations

import re

from gptme_sessions.shell_parse import has_git_commit_command, has_git_push_command

CLAUDE_CODE_TOOLS = {
    "Read": "read",
    "NotebookRead": "read",
    "LS": "read",
    "Glob": "search",
    "Grep": "search",
    "ToolSearch": "search",
    "Write": "write",
    "Edit": "write",
    "MultiEdit": "write",
    "NotebookEdit": "write",
    "Bash": "shell",
    "BashOutput": "shell",
    "KillShell": "shell",
    "Monitor": "shell",
    "WebFetch": "web",
    "WebSearch": "web",
    "Agent": "agent",
    "Task": "agent",
}

GPTME_TOOLS = {
    "read": "read",
    "save": "write",
    "write": "write",
    "append": "write",
    "patch": "write",
    "edit": "write",
    "morph": "write",
    "shell": "shell",
    "gh": "shell",
    "tmux": "shell",
    "ipython": "shell",
    "python": "shell",
    "browser": "web",
    "screenshot": "web",
    "subagent": "agent",
    "rag": "search",
    "chats": "search",
}

CODEX_TOOLS = {
    "exec_command": "shell",
    "exec": "shell",
    "shell": "shell",
    "shell_command": "shell",
    "wait": "shell",
    "local_shell": "shell",
    "write_stdin": "shell",
    "apply_patch": "write",
    "web_search": "web",
    "view_image": "read",
}

_TABLES = {"claude-code": CLAUDE_CODE_TOOLS, "gptme": GPTME_TOOLS, "codex": CODEX_TOOLS}

# Shell commands that run a test suite get their own class ("test"), so a sink
# can tell "running tests" apart from other shell work.
_TEST_RE = re.compile(
    r"(?:^|[\s;&|(])(?:"
    r"pytest|py\.test|vitest|jest|tox|nox"
    r"|make\s+(?:test|check)\w*"
    r"|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test"
    r"|cargo\s+(?:test|nextest)|go\s+test"
    r")\b"
)
_PR_CREATE_RE = re.compile(r"\bgh\s+pr\s+create\b")


def classify_tool(harness: str, name: str, command: str | None = None) -> str:
    cls = _TABLES.get(harness, {}).get(name, "other")
    if cls == "shell" and command and _TEST_RE.search(command):
        return "test"
    return cls


def landing_kinds(command: str | None) -> list[str]:
    """Event kinds a shell command would produce if it succeeds, in order."""
    if not command:
        return []
    kinds = []
    if has_git_commit_command(command):
        kinds.append("commit")
    if has_git_push_command(command):
        kinds.append("push")
    if _PR_CREATE_RE.search(command) and "--dry-run" not in command:
        kinds.append("pr_open")
    return kinds
