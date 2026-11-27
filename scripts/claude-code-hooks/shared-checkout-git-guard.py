#!/usr/bin/env python3
"""Claude Code PreToolUse hook: deny git commands that lose other sessions' work.

When several agent sessions share one checkout, a few common git commands can
silently stash, revert, unstage or sweep up *other* sessions' uncommitted
changes. Lessons and docs say not to run them; this hook makes the rule hold.

Two scopes:

* **Shared checkouts** (opt-in per repo with ``git config safety.sharedCheckout
  true``; ``scripts/git/setup-shared-checkout.sh`` sets it): deny whole-tree
  operations (stash, ``reset --hard``, bare/whole-index ``reset``,
  ``--autostash``, ``clean -fd``, ``checkout -- .``, ``restore .``) and broad
  staging (``add -A``/``.``/``--all``, ``commit -a``).
* **Any repo with linked worktrees**: deny writes to the stash stack, because
  all worktrees of a repo share one ``refs/stash`` and "stash, check, pop" can
  pop a sibling's entry. ``stash list|show|create`` and ``stash apply <sha>``
  stay allowed.

The matcher (:func:`detect`) is a pure function of the command and the
directory it runs in, so other runtimes (e.g. a gptme tool-confirm plugin) can
reuse it. Adapted from Bob's ``shared-worktree-git-guard.py``
(ErikBjare/bob), without Bob-specific canaries or a fixed workspace root.

Wire it in ``~/.claude/settings.json``::

    {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
      "command": "python3 /path/to/gptme-contrib/scripts/claude-code-hooks/shared-checkout-git-guard.py"}]}]}}
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

# A git invocation with any global options before the subcommand (captured as
# "opts": -C <dir>, -c k=v, --no-pager, --git-dir/--work-tree[=]<path>, ...).
# Agents add -c and --no-pager routinely; without them in the prefix every rule
# would miss `git --no-pager reset --hard`.
_GIT_OPT = (
    r"(?:-[Cc]\s+\S+"
    r"|--(?:git-dir|work-tree|namespace|exec-path|config-env)(?:=|\s+)\S+"
    r"|--[a-z][a-z-]*(?:=\S+)?"
    r"|-[pP])"
)
_GIT = r"(?:^|[;&|()\s])git(?P<opts>(?:\s+" + _GIT_OPT + r")*)\s+"
_TARGET_OPT_RE = re.compile(r"(?:-C\s+|--work-tree(?:=|\s+))(?P<path>\S+)")
_END = r"(?:\s|$|[;&|)])"

# Every stash subcommand except the read-only ones rewrites tree and/or stack.
_STASH_RE = re.compile(_GIT + r"stash(?!\s+(?:list|show)" + _END + r")(?:\s|$)")
# Stack writes (for repos with linked worktrees): also allow ``create`` (no
# stack write) and ``apply <sha|$var>`` (applies a specific object).
_STASH_STACK_RE = re.compile(
    _GIT
    + r"stash(?!\s+(?:list|show|create)"
    + _END
    + r")"
    + r'(?!\s+apply(?:\s+(?:-q|--quiet|--index))*\s+(?:[0-9a-f]{7,40}|"?\$\{?\w+\}?"?)'
    + _END
    + r")(?:\s|$)"
)
_RESET_HARD_RE = re.compile(_GIT + r"reset(?:\s+[^;&|\n]*)?\s+--hard" + _END)
# A mixed reset without a pathspec (or with ``-- .``) rewrites the whole index,
# unstaging every other session's staged files. ``--soft`` and
# ``reset -- path`` stay allowed.
_RESET_INDEX_RE = re.compile(
    _GIT + r"reset(?:\s+(?:-q|--quiet|--mixed|HEAD(?:[~^]\d*)*))*\s*(?:$|[;&|)\n])"
)
_RESET_ALL_RE = re.compile(
    _GIT + r"reset(?:\s+(?:-q|--quiet|--mixed|HEAD(?:[~^]\d*)*))*\s+--\s+\." + _END
)
_AUTOSTASH_RE = re.compile(_GIT + r"(?:pull|rebase)\b[^;&|\n]*\s--autostash" + _END)
_CLEAN_RE = re.compile(
    _GIT + r"clean(?=[^;&|\n]*\s-[A-Za-z]*f)(?=[^;&|\n]*\s-[A-Za-z]*d)"
)
_CHECKOUT_ALL_RE = re.compile(_GIT + r"checkout\s+--\s+\." + _END)
_RESTORE_ALL_RE = re.compile(_GIT + r"restore(?:\s+--\S+)*\s+\." + _END)
# Broad staging sweeps other sessions' changes into your commit.
_ADD_ALL_RE = re.compile(
    _GIT + r"add(?:\s+-[a-zA-Z]+)*\s+(?:-A|--all|\.|:/)(?:\s|$|[;&|)])"
)
_COMMIT_ALL_RE = re.compile(
    _GIT + r"commit\b[^;&|\n]*\s(?:-[a-zA-Z]*a[a-zA-Z]*|--all)" + _END
)

SHARED_CHECKOUT_RULES: dict[str, re.Pattern[str]] = {
    "git stash": _STASH_RE,
    "git reset --hard": _RESET_HARD_RE,
    "git reset (whole index)": _RESET_INDEX_RE,
    "git reset -- . (whole index)": _RESET_ALL_RE,
    "git pull/rebase --autostash": _AUTOSTASH_RE,
    "git clean -fd": _CLEAN_RE,
    "git checkout -- .": _CHECKOUT_ALL_RE,
    "git restore .": _RESTORE_ALL_RE,
    "git add -A / . (broad staging)": _ADD_ALL_RE,
    "git commit -a (broad staging)": _COMMIT_ALL_RE,
}

_LEADING_CD_RE = re.compile(r"^\s*cd\s+(?P<path>'[^']*'|\"[^\"]*\"|[^\s;&|]+)\s*&&")


def effective_cwd(command: str, cwd: Path) -> Path:
    """Where *command* runs: *cwd*, or the target of a single leading ``cd``.

    Only one unambiguous leading ``cd`` is trusted; anything else keeps *cwd*
    (fail closed: a second ``cd`` could hop back into the guarded checkout).
    """
    if len(re.findall(r"(?:^|[;&|()\s])cd\s", command)) != 1:
        return cwd
    match = _LEADING_CD_RE.match(command)
    if match is None:
        return cwd
    target = match.group("path").strip("'\"")
    if not target or target.startswith("-") or "$" in target or "`" in target:
        return cwd
    candidate = Path(os.path.expanduser(target))
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        return candidate.resolve()
    except OSError:
        return cwd


def _git_dir_marker(start: Path) -> Path | None:
    """The ``.git`` entry (dir or worktree file) of the repo containing *start*."""
    for directory in (start, *start.parents):
        dot_git = directory / ".git"
        if dot_git.exists():
            return dot_git
    return None


def shares_stash_stack(cwd: Path) -> bool:
    """True when *cwd* is in a repo whose ``refs/stash`` other worktrees share."""
    dot_git = _git_dir_marker(cwd)
    if dot_git is None:
        return False
    try:
        if dot_git.is_file():  # a linked worktree
            return "/worktrees/" in dot_git.read_text(errors="replace")
        linked = dot_git / "worktrees"
        return linked.is_dir() and any(linked.iterdir())
    except OSError:
        return False


def is_shared_checkout(cwd: Path) -> bool:
    """True when the repo at *cwd* opted in with ``safety.sharedCheckout``."""
    if _git_dir_marker(cwd) is None:
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "config", "--bool", "safety.sharedCheckout"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() == "true"


def _target_dirs(command: str, pattern: re.Pattern[str], cwd: Path) -> list[Path]:
    """Directories each matching git invocation acts on.

    Honours ``-C`` (repeated ones chain, as in git) and ``--work-tree``.
    """
    dirs = []
    for match in pattern.finditer(command):
        target = cwd
        for opt in _TARGET_OPT_RE.finditer(match.group("opts") or ""):
            path = Path(os.path.expanduser(opt.group("path").strip("'\"")))
            target = path if path.is_absolute() else target / path
        dirs.append(target)
    return dirs


def detect(
    command: str,
    cwd: Path,
    *,
    shared_checkout: Any = is_shared_checkout,
    stash_stack_shared: Any = shares_stash_stack,
) -> str | None:
    """Return why *command* (run in *cwd*) must be denied, or None.

    The two predicates are injectable for tests and other runtimes.
    """
    run_dir = effective_cwd(command, cwd)
    for name, pattern in SHARED_CHECKOUT_RULES.items():
        for target in _target_dirs(command, pattern, run_dir):
            if shared_checkout(target):
                return (
                    f"Blocked `{name}` in a shared checkout ({target}): other "
                    "sessions' uncommitted work lives in this tree, and this "
                    "command can stash, revert, unstage or sweep it up. Commit "
                    "your own explicit paths (`git add <paths>` / `git-safe-commit "
                    "<paths>`), pull with `git pull --ff-only` / `git-safe-pull`, "
                    "and do experiments that need reset/clean in a separate "
                    "worktree."
                )
    for target in _target_dirs(command, _STASH_STACK_RE, run_dir):
        if stash_stack_shared(target):
            return (
                f"Blocked `git stash` in a repo with linked worktrees ({target}): "
                "all worktrees share one stash stack, so a sibling's stash between "
                "your push and pop makes you pop THEIR entry. For a baseline use "
                "`git show origin/master:PATH` or a separate worktree; to set work "
                "aside use `sha=$(git stash create)` then `git stash apply $sha`, "
                "or a temporary commit."
            )
    return None


def evaluate_hook(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Evaluate one Claude Code PreToolUse payload."""
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or "git" not in command:
        return None
    try:
        cwd = Path(str(payload.get("cwd") or os.getcwd())).resolve()
    except OSError:
        return None
    reason = detect(command, cwd)
    if reason is None:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            result = evaluate_hook(payload)
            if result is not None:
                json.dump(result, sys.stdout)
    except Exception:
        # A broken hook must never make the Bash tool unusable.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
