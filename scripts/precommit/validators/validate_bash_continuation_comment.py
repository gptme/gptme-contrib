#!/usr/bin/env python3
"""
Validator for: comment line inside a backslash-continued shell command

A line ending in `\\` joins the next line onto the command. When that next
line is a `# comment`, the comment ends the command at its newline and every
later "continuation" line runs as a separate command.

Failure history:
  - 2026-10-20 (ErikBjare/bob#1409): `scripts/github/ai-review-and-merge.sh`
    at 796aac3e92 put four comment lines between `env -u ... \\`
    continuations. `env` ran with no program and printed every `.env`
    assignment into MERGE_OUTPUT, two PM LLM sessions and a slot log.
    `shellcheck -S style` exits 0 on that revision (SC1143 only covers a
    backslash at the end of a comment line).

Fix: move the comment above the command, or end the code line before it.

Usage:
    python3 validate_bash_continuation_comment.py file1.sh file2 ...
"""

import argparse
import re
import sys
from pathlib import Path

_HEREDOC = re.compile(r"""(?<!<)<<(?!<)(-?)\s*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\2""")
# `#` starts a comment only at the start of a word.
_WORD_BREAK = set(" \t;&|()<>")


def _scan_line(line: str, in_double: bool) -> tuple[str, bool, set[int]]:
    """Return (code part, still inside a double-quoted string, quoted positions).

    Strips a trailing comment and tracks quotes so that `#` inside a string
    is not mistaken for a comment. This lightweight scanner assumes single-
    quoted strings close on the same line; it is not a full shell parser.
    """
    i = 0
    n = len(line)
    in_single = False
    quoted: set[int] = set()
    while i < n:
        c = line[i]
        if in_single or in_double:
            quoted.add(i)
        if in_single:
            if c == "'":
                in_single = False
        elif in_double:
            if c == "\\":
                i += 1
            elif c == '"':
                in_double = False
        elif c == "\\":
            i += 1
        elif c == "'":
            in_single = True
        elif c == '"':
            in_double = True
        elif c == "#" and (i == 0 or line[i - 1] in _WORD_BREAK):
            return line[:i], False, quoted
        i += 1
    return line, in_double, quoted


def _opens_heredoc(code: str, pos: int, quoted: set[int]) -> bool:
    """`<<` at pos is a heredoc, not quoted text or an arithmetic shift."""
    if pos in quoted:
        return False
    before = code[:pos]
    return before.count("((") <= before.count("))")


def _continues(code: str) -> bool:
    """True when the code part ends in an unescaped backslash."""
    stripped = code.rstrip(" \t")
    trailing = len(stripped) - len(stripped.rstrip("\\"))
    return trailing % 2 == 1


def find_violations(lines: list[str]) -> list[tuple[int, int]]:
    """Return (comment line, continued line) pairs, 1-based."""
    violations: list[tuple[int, int]] = []
    heredocs: list[tuple[str, bool]] = []
    in_double = False
    continued_from = 0

    for num, raw in enumerate(lines, start=1):
        line = raw.rstrip("\n")

        if heredocs:
            delim, strip_tabs = heredocs[0]
            body = line.lstrip("\t") if strip_tabs else line
            if body == delim:
                heredocs.pop(0)
            continue

        if continued_from and not in_double and line.lstrip().startswith("#"):
            violations.append((num, continued_from))
            continued_from = 0
            continue

        code, in_double, quoted = _scan_line(line, in_double)
        continued_from = num if _continues(code) else 0
        for m in _HEREDOC.finditer(code):
            if _opens_heredoc(code, m.start(), quoted):
                heredocs.append((m.group(3), m.group(1) == "-"))

    return violations


def check_file(path: Path) -> list[tuple[int, int]]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    return find_violations(text.splitlines())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()

    failed = False
    for path in args.files:
        for comment_line, continued_line in check_file(path):
            failed = True
            print(
                f"{path}:{comment_line}: comment inside a backslash-continued "
                f"command (continued from line {continued_line}); the comment "
                "ends the command and the following lines run separately"
            )
    if failed:
        print(
            "\nFix: move the comment above the command. "
            "See ErikBjare/bob#1409 for the `env` dump this caused."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
