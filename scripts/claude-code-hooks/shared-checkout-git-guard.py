#!/usr/bin/env python3
"""Claude Code PreToolUse hook: deny git commands that lose other sessions' work.

When several agent sessions share one checkout, a few common git commands can
silently stash, revert, unstage or sweep up *other* sessions' uncommitted
changes. Lessons and docs say not to run them; this hook makes the rule hold.

Two scopes:

* **Shared checkouts** (opt-in per repo with ``git config safety.sharedCheckout
  true``; ``scripts/git/setup-shared-checkout.sh`` sets it): deny whole-tree
  operations (stash writes other than ``create``/``apply <sha>``, ``reset
  --hard``, a ``reset`` without paths, ``--autostash``, forced ``clean``,
  ``checkout``/``restore`` of ``.``, forced ``checkout``/``switch``) and broad
  staging (``add -A``/``-u``/``.``, ``commit -a``).
* **Any repo with linked worktrees**: deny writes to the stash stack, because
  all worktrees of a repo share one ``refs/stash`` and "stash, check, pop" can
  pop a sibling's entry. ``stash list|show|create`` and ``stash apply <sha>``
  stay allowed.

The command is tokenized like a shell would (quotes, escapes, comments,
redirections and heredoc bodies handled), split at ``;``,
``&&``, ``||``, ``|``, ``&`` and parentheses, and every ``git`` invocation is
parsed (global options, subcommand, arguments), so quoting, option clusters
and separators without spaces don't change the outcome. ``cd``/``pushd`` are
followed; a directory the guard can't determine (``cd "$DIR"``) fails closed
for destructive commands, and so does a command that doesn't tokenize.

It is a guard against common mistakes, not a sandbox: anything that hides git
behind another program (``bash -c``, ``xargs``, aliases) is out of scope.

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
from typing import Any, Callable, Mapping

# Stands for a directory the guard cannot determine from the command text
# (``cd "$DIR"``, ``git -C "$(...)"``). Destructive commands there are denied.
UNKNOWN_DIR = Path("<unresolvable>")

# Paths that mean "the whole tree" to add/checkout/restore/reset.
_WHOLE_TREE = {".", "./", ":/", ":", "*"}
_SEPARATOR_CHARS = set(";&|()")
# Leading words that run the rest of the command as a program.
_WRAPPERS = {"command", "builtin", "exec", "time", "nohup", "env", "sudo"}


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


_HEREDOC_RE = re.compile(r"""(['"]?)([^\s;&|()<>'"]+)\1""")
_OPERATORS = ("&&", "||", ";;", "|&", ";", "&", "|", "(", ")")


def _tokens(command: str) -> list[str] | None:
    """Split *command* into words and operators, roughly like a POSIX shell.

    Quotes and backslashes are removed, ``#`` comments dropped, newlines
    become ``;``, redirections (and their targets) are left out of the words,
    and heredoc bodies are skipped, so text inside a commit message or a
    heredoc is never mistaken for a command. Returns None for unbalanced
    quotes.
    """
    tokens: list[str] = []
    word: list[str] = []
    in_word = False
    drop_next_word = False
    heredocs: list[tuple[str, bool]] = []
    i, n = 0, len(command)

    def end_word() -> None:
        nonlocal in_word, drop_next_word
        if in_word:
            if drop_next_word:
                drop_next_word = False
            else:
                tokens.append("".join(word))
        word.clear()
        in_word = False

    while i < n:
        c = command[i]
        if c == "\\" and i + 1 < n:
            if command[i + 1] != "\n":  # \<newline> continues the line
                word.append(command[i + 1])
                in_word = True
            i += 2
        elif c == "'":
            close = command.find("'", i + 1)
            if close < 0:
                return None
            word.append(command[i + 1 : close])
            in_word = True
            i = close + 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and command[j] != '"':
                if command[j] == "\\" and j + 1 < n and command[j + 1] in '"\\$`\n':
                    buf.append(command[j + 1])
                    j += 2
                else:
                    buf.append(command[j])
                    j += 1
            if j >= n:
                return None
            word.append("".join(buf))
            in_word = True
            i = j + 1
        elif c == "#" and not in_word:
            newline = command.find("\n", i)
            i = n if newline < 0 else newline
        elif c == "\n":
            end_word()
            tokens.append(";")
            i += 1
            for delimiter, strip_tabs in heredocs:  # skip each body in turn
                while i < n:
                    newline = command.find("\n", i)
                    end = n if newline < 0 else newline
                    line = command[i:end]
                    i = end + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                        break
            heredocs.clear()
        elif c in " \t":
            end_word()
            i += 1
        elif command.startswith("<<", i) and not command.startswith("<<<", i):
            end_word()
            i += 2
            strip_tabs = i < n and command[i] == "-"
            i += strip_tabs
            while i < n and command[i] in " \t":
                i += 1
            m = _HEREDOC_RE.match(command, i)
            if m is None:
                return None
            heredocs.append((m.group(2), strip_tabs))
            i = m.end()
        elif c in "<>" or command.startswith("&>", i):
            # A redirection: drop a pending fd number and the target word.
            if in_word and "".join(word).isdigit():
                word.clear()
                in_word = False
            end_word()
            i += 1
            while i < n and command[i] in "<>&":
                i += 1
            drop_next_word = True
        elif c in ";&|()":
            end_word()
            op = next(o for o in _OPERATORS if command.startswith(o, i))
            tokens.append(op)
            i += len(op)
        else:
            word.append(c)
            in_word = True
            i += 1
    end_word()
    return tokens


def _is_separator(token: str) -> bool:
    return bool(token) and set(token) <= _SEPARATOR_CHARS


def _split_commands(tokens: list[str]) -> list[tuple[list[str], str]]:
    """``(argv, separator after it)`` for each simple command."""
    commands: list[tuple[list[str], str]] = []
    argv: list[str] = []
    for token in tokens:
        if _is_separator(token):
            if argv:
                commands.append((argv, token))
                argv = []
            elif commands:
                prev_argv, _prev_sep = commands[-1]
                commands[-1] = (prev_argv, token)
        else:
            argv.append(token)
    if argv:
        commands.append((argv, ""))
    return commands


def _strip_wrappers(argv: list[str]) -> list[str]:
    i = 0
    while i < len(argv):
        word = argv[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word):
            i += 1
        elif word in _WRAPPERS:
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 1
        else:
            break
    return argv[i:]


def _unresolvable(text: str) -> bool:
    return "$" in text or "`" in text


def _resolve(path: str, base: Path) -> Path:
    if _unresolvable(path) or path == "-":
        return UNKNOWN_DIR
    candidate = Path(os.path.expanduser(path))
    if not candidate.is_absolute():
        if base == UNKNOWN_DIR:
            return UNKNOWN_DIR
        candidate = base / candidate
    try:
        return candidate.resolve()
    except OSError:
        return candidate


def _git_invocation(
    argv: list[str], dirs: list[Path]
) -> tuple[list[Path], str | None, list[str], tuple[str, ...]]:
    """Target directories, subcommand, its arguments and ``-c`` settings."""
    c_paths: list[str] = []
    configs: list[str] = []
    git_dir = work_tree = None
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ("-C", "-c") or arg in (
            "--git-dir",
            "--work-tree",
            "--namespace",
            "--config-env",
            "--super-prefix",
        ):
            value = argv[i + 1] if i + 1 < len(argv) else ""
            if arg == "-C":
                c_paths.append(value)
            elif arg == "-c":
                configs.append(value)
            elif arg == "--git-dir":
                git_dir = value
            elif arg == "--work-tree":
                work_tree = value
            i += 2
            continue
        if arg.startswith("--git-dir="):
            git_dir = arg.split("=", 1)[1]
        elif arg.startswith("--work-tree="):
            work_tree = arg.split("=", 1)[1]
        elif not arg.startswith("-"):
            break
        i += 1
    sub = argv[i] if i < len(argv) else None
    args = argv[i + 1 :]
    targets: list[Path] = []
    for base in dirs:
        repo = base
        for c_path in c_paths:  # -C options chain, as in git
            repo = _resolve(c_path, repo)
        # --git-dir picks the repository (the predicates find a repo from
        # inside its .git too); --work-tree only adds the tree it touches.
        if git_dir is not None:
            targets.append(_resolve(git_dir, repo))
        else:
            targets.append(repo)
        if work_tree is not None:
            targets.append(_resolve(work_tree, repo))
    return targets, sub, args, tuple(configs)


def _split_args(
    args: list[str], short_with_value: str = "", long_with_value: tuple = ()
) -> tuple[list[str], list[str], str]:
    """``(long flags, positionals incl. after --, short option letters)``."""
    longs: list[str] = []
    positionals: list[str] = []
    shorts = ""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positionals.extend(args[i + 1 :])
            break
        if arg.startswith("--"):
            longs.append(arg.split("=", 1)[0])
            if arg in long_with_value:
                i += 1  # the value is the next token
        elif arg.startswith("-") and arg != "-":
            for j, letter in enumerate(arg[1:], start=1):
                if letter in short_with_value:
                    if j == len(arg) - 1:
                        i += 1  # the value is the next token
                    break  # the rest of the cluster is the value
                shorts += letter
        else:
            positionals.append(arg)
        i += 1
    return longs, positionals, shorts


def _whole_tree(paths: list[str]) -> bool:
    return any(path in _WHOLE_TREE for path in paths)


_AUTOSTASH_CONFIG_RE = re.compile(
    r"^(?:rebase|merge)\.autostash(?:=(?:true|yes|on|1))?$", re.IGNORECASE
)


def _classify(
    sub: str | None, args: list[str], configs: tuple[str, ...] = ()
) -> tuple[str | None, bool]:
    """``(shared-checkout rule it breaks, whether it writes the stash stack)``."""
    if sub == "stash":
        op = args[0] if args else "push"
        if op in ("list", "show", "create"):
            return None, False
        if op == "apply":
            _l, refs, _s = _split_args(args[1:])
            # A specific object is fine; no ref, stash@{n} or n is an index
            # into the stack a sibling worktree can shift.
            if refs and not refs[0].startswith("stash@") and not refs[0].isdigit():
                return None, False
        return "git stash", True
    if sub == "reset":
        longs, positionals, shorts = _split_args(args)
        if "--hard" in longs:
            return "git reset --hard", False
        if "--soft" in longs or "--patch" in longs or "p" in shorts:
            return None, False
        explicit = "--" in args
        paths = positionals if explicit else positionals[1:]
        if _whole_tree(paths):
            return "git reset -- . (whole index)", False
        if paths:
            return None, False
        # No paths: rewrites the whole index (and moves the branch with a
        # commit). One bare argument may be a commit or a path; only git
        # knows, so it is treated as a commit. Paths use `reset -- <path>`.
        return "git reset (whole index)", False
    if sub == "clean":
        longs, _p, shorts = _split_args(args, "e", ("--exclude",))
        forced = "--force" in longs or "f" in shorts
        dry = "--dry-run" in longs or "n" in shorts
        return ("git clean -f" if forced and not dry else None), False
    if sub == "checkout":
        longs, positionals, shorts = _split_args(
            args, "bB", ("--orphan", "--conflict", "--pathspec-from-file")
        )
        if "--force" in longs or "f" in shorts:
            return "git checkout/switch -f", False
        explicit = "--" in args
        paths = positionals if explicit else positionals
        if _whole_tree(paths):
            return "git checkout .", False
        return None, False
    if sub == "switch":
        longs, _p, shorts = _split_args(args, "cC", ("--orphan", "--conflict"))
        if {"--force", "--discard-changes"} & set(longs) or "f" in shorts:
            return "git checkout/switch -f", False
        return None, False
    if sub == "restore":
        _l, positionals, _s = _split_args(
            args, "s", ("--source", "--pathspec-from-file")
        )
        return ("git restore ." if _whole_tree(positionals) else None), False
    if sub == "add":
        longs, paths, shorts = _split_args(
            args, "", ("--chmod", "--pathspec-from-file")
        )
        if "--all" in longs or "A" in shorts or _whole_tree(paths):
            return "git add -A / -u / . (broad staging)", False
        if ("--update" in longs or "u" in shorts) and not paths:
            return "git add -A / -u / . (broad staging)", False
        return None, False
    if sub == "commit":
        longs, _paths, shorts = _split_args(
            args,
            "mFcCtS",
            (
                "--message",
                "--file",
                "--author",
                "--date",
                "--reuse-message",
                "--reedit-message",
                "--fixup",
                "--squash",
                "--template",
                "--cleanup",
                "--trailer",
                "--pathspec-from-file",
            ),
        )
        if "--all" in longs or "a" in shorts:
            return "git commit -a (broad staging)", False
        return None, False
    if sub in ("pull", "rebase", "merge"):
        longs, _p, _s = _split_args(args)
        autostash = "--autostash" in longs or any(
            _AUTOSTASH_CONFIG_RE.match(c) for c in configs
        )
        if autostash and "--no-autostash" not in longs:
            return "git pull/rebase/merge --autostash", False
        return None, False
    return None, False


def _shared_reason(rule: str, target: Path) -> str:
    return (
        f"Blocked `{rule}` in a shared checkout ({target}): other sessions' "
        "uncommitted work lives in this tree, and this command can stash, "
        "revert, unstage or sweep it up. Commit your own explicit paths "
        "(`git add <paths>` / `git-safe-commit <paths>`), unstage paths with "
        "`git reset -- <paths>`, pull with `git pull --ff-only` / "
        "`git-safe-pull`, and do experiments that need reset/clean in a "
        "separate worktree."
    )


def _stack_reason(target: Path) -> str:
    return (
        f"Blocked `git stash` in a repo with linked worktrees ({target}): all "
        "worktrees share one stash stack, so a sibling's stash between your "
        "push and pop makes you pop THEIR entry. For a baseline use `git show "
        "origin/master:PATH` or a separate worktree; to set work aside use "
        "`sha=$(git stash create)` then `git stash apply $sha`, or a temporary "
        "commit."
    )


def _unknown_reason(rule: str) -> str:
    return (
        f"Blocked `{rule}`: it runs in a directory given by a shell variable or "
        "substitution, so the guard cannot tell whether that is a shared "
        "checkout (fail closed). Use a literal path (`cd /path/to/repo && ...` "
        "or `git -C /path/to/repo ...`)."
    )


_DESTRUCTIVE_WORDS = re.compile(
    r"\bgit\b.*\b(stash|reset|clean|checkout|switch|restore|add|commit|pull|rebase)\b",
    re.DOTALL,
)


def detect(
    command: str,
    cwd: Path,
    *,
    shared_checkout: Callable[[Path], bool] = is_shared_checkout,
    stash_stack_shared: Callable[[Path], bool] = shares_stash_stack,
) -> str | None:
    """Return why *command* (run in *cwd*) must be denied, or None.

    The two predicates are injectable for tests and other runtimes.
    """
    tokens = _tokens(command)
    if tokens is None:
        if _DESTRUCTIVE_WORDS.search(command):
            return (
                "Blocked: the command could not be tokenized (unbalanced "
                "quotes?), so the guard cannot check its git invocations."
            )
        return None
    dirs: list[Path] = [cwd]
    # Directories to fall back to if a `cd X && ...` chain is broken by a
    # later `;`, `||`, `|` or `&` (the rest then runs where the cd failed).
    fallback: list[Path] = []
    for argv, separator in _split_commands(tokens):
        argv = _strip_wrappers(argv)
        if argv:
            word = argv[0]
            if word in ("cd", "pushd"):
                rest = [a for a in argv[1:] if not a.startswith("-") or a == "-"]
                cd_target = rest[0] if rest else "~"
                new = list(dict.fromkeys(_resolve(cd_target, d) for d in dirs))
                if separator == "&&":
                    fallback = list(dict.fromkeys(fallback + dirs))
                    dirs = new
                else:
                    dirs = list(dict.fromkeys(new + dirs))
            elif word == "popd":
                dirs = list(dict.fromkeys(dirs + [UNKNOWN_DIR]))
            elif os.path.basename(word) == "git":
                targets, sub, args, configs = _git_invocation(argv, dirs)
                rule, stack_write = _classify(sub, args, configs)
                if rule or stack_write:
                    name = rule or "git stash"
                    if UNKNOWN_DIR in targets:
                        return _unknown_reason(name)
                    if rule:
                        for target in targets:
                            if shared_checkout(target):
                                return _shared_reason(rule, target)
                    if stack_write:
                        for target in targets:
                            if stash_stack_shared(target):
                                return _stack_reason(target)
        if separator and separator != "&&" and fallback:
            dirs = list(dict.fromkeys(dirs + fallback))
            fallback = []
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
