#!/usr/bin/env python3
"""Ratchet source-file sizes: no new oversized files, no growth of existing ones.

A file under the threshold (default 2,000 lines) always passes. A file at or
over it passes only if it is listed in the checked-in baseline and has not grown
past its recorded size. When a listed file shrinks, the hook lowers its entry
(or drops it once the file is back under the threshold); the baseline never
goes up on its own. A commit that shrinks or deletes a listed file fails until
the lowered baseline is staged with it. Growing an oversized file on purpose, or
renaming one, means editing the baseline by hand, where review can see it.

``--growth=warn`` reports growth of a listed file without failing (the default
is ``--growth=error``). New oversized files still fail, and the baseline works
as above. On GitHub Actions each warning is also emitted as an annotation.
pre-commit hides a passing hook's output, so set ``verbose: true`` on the hook
to see warnings locally.

``--growth=budget`` is the steady-state rule. Growth is measured against the
merge base, not the baseline, so a file that grew in an earlier PR does not keep
warning. A listed file may grow by up to ``--budget`` lines (default 30) with a
warning. More than that fails unless the new files added by the change are at
least as large as the growth (the code moved into a new module) -- a token new
file does not exempt an arbitrarily large growth. More than ``--hard-stop``
lines (default 100) to a file that was already ``--hard-stop-size`` lines
(default 5,000) at the base always fails. The base is ``--base`` if given, else
``origin/$GITHUB_BASE_REF`` on a GitHub pull request, else ``HEAD`` (what a
local commit adds). When the base cannot be resolved (e.g. a shallow checkout)
a listed file that grows above its baseline entry fails instead of passing
silently: fetch the base branch or pass ``--base``.

``--growth=off`` keeps only the new-file rule: growth is not checked and the
baseline is never rewritten or required to be staged, so contributors who
shrink a listed file do not have to touch the baseline.

The baseline (default ``file-size-baseline.json`` in the repo root)::

    {
      "threshold": 2000,
      "exclude": ["src/schema_gen.py"],
      "files": {"src/big_module.py": 2450}
    }

``threshold`` is also read by ``gptme-dashboard codesize``, which uses the same
inclusive comparison, so the report and the gate flag the same files. ``exclude`` adds fnmatch globs to the built-in
exclusions (vendored and generated trees, minified bundles).

Usage::

    # create the baseline from the tracked files (once per repo)
    python3 file_size_ratchet.py --init [--threshold 2000]

    # check files (what pre-commit runs)
    python3 file_size_ratchet.py path/a.py path/b.ts

As a pre-commit hook::

    - repo: https://github.com/gptme/gptme-contrib
      rev: <SHA or tag>
      hooks:
      - id: file-size-ratchet

Pure standard library, so it also runs as a plain script.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_BASELINE = "file-size-baseline.json"
DEFAULT_THRESHOLD = 2000

# Code and tests; prose and config (.md, .yaml, .toml, .json) are not ratcheted.
CODE_SUFFIXES = {
    ".py",
    ".sh",
    ".bash",
    ".rs",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".vue",
    ".svelte",
    ".html",
    ".css",
    ".scss",
    ".sql",
    ".kt",
    ".java",
    ".go",
    ".c",
    ".h",
    ".cpp",
}

# Lockfiles and data never match CODE_SUFFIXES; these catch generated code.
DEFAULT_EXCLUDE = (
    "*.min.js",
    "*.min.css",
    "*_pb2.py",
    "*.generated.*",
    "vendor/**",
    "**/vendor/**",
    "node_modules/**",
    "**/node_modules/**",
    "dist/**",
    "build/**",
    "generated/**",
    "**/generated/**",
)


def count_lines(path: Path) -> int:
    """Line count, same rule as the codesize report; unreadable files raise."""
    return count_bytes(path.read_bytes())


def count_bytes(data: bytes) -> int:
    if not data or b"\x00" in data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def is_checked(path: str, exclude: list[str]) -> bool:
    if Path(path).suffix.lower() not in CODE_SUFFIXES:
        return False
    return not any(
        fnmatch.fnmatch(path, pattern) for pattern in (*DEFAULT_EXCLUDE, *exclude)
    )


def load_baseline(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("files", {}), dict):
        raise ValueError(f"{path}: expected an object with a 'files' mapping")
    data.setdefault("threshold", DEFAULT_THRESHOLD)
    data.setdefault("exclude", [])
    data.setdefault("files", {})
    return data


def save_baseline(path: Path, data: dict) -> None:
    data["files"] = dict(sorted(data["files"].items()))
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def staged_entries(baseline_path: Path) -> dict[str, int]:
    """The ``files`` mapping in the index: what the commit will record.

    Empty when the baseline is not in the index (or is outside the checkout).
    """
    if baseline_path.is_absolute():
        return {}
    result = subprocess.run(
        ["git", "show", f":./{baseline_path.as_posix()}"], capture_output=True
    )
    if result.returncode != 0:
        return {}
    data = json.loads(result.stdout)
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else {}


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "-z"],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", errors="replace")
    return [p for p in dict.fromkeys(out.split("\0")) if p]


def init_baseline(path: Path, threshold: int) -> int:
    if path.exists():
        print(
            f"{path} already exists; edit it instead of re-initialising.",
            file=sys.stderr,
        )
        return 1
    data: dict = {"threshold": threshold, "exclude": [], "files": {}}
    for rel in tracked_files():
        file = Path(rel)
        if is_checked(rel, []) and not file.is_symlink() and file.is_file():
            lines = count_lines(file)
            if lines >= threshold:
                data["files"][rel] = lines
    save_baseline(path, data)
    print(f"Wrote {path}: {len(data['files'])} file(s) at or over {threshold} lines.")
    return 0


def resolve_base(base: str | None) -> str | None:
    """Commit that growth is measured against, or None if it cannot be resolved."""
    if base is None:
        base = os.environ.get("FILE_SIZE_RATCHET_BASE") or (
            f"origin/{os.environ['GITHUB_BASE_REF']}"
            if os.environ.get("GITHUB_BASE_REF")
            else "HEAD"
        )
    cmd = (
        ["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"]
        if base == "HEAD"
        else ["git", "merge-base", base, "HEAD"]
    )
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def lines_at(rev: str, rel: str) -> int | None:
    result = subprocess.run(["git", "show", f"{rev}:./{rel}"], capture_output=True)
    return count_bytes(result.stdout) if result.returncode == 0 else None


def added_code_lines(rev: str, exclude: list[str]) -> int:
    """Lines added in checked files created since ``rev`` (staged or committed).

    The new-module exception is sized against this: a token new file must not
    exempt an arbitrarily large growth of a listed file.
    """
    out = subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--numstat", "-z"]
        + ["--diff-filter=A", rev],
        capture_output=True,
    ).stdout.decode("utf-8", errors="replace")
    total = 0
    for record in out.split("\0"):
        if not record:
            continue
        added, _deleted, path = record.split("\t", 2)
        if is_checked(path, exclude):
            total += int(added) if added.isdigit() else 0
    return total


def warn(path: str, message: str) -> None:
    """Print a non-blocking finding; also annotate it on GitHub Actions."""
    print(f"warning: {path}: {message}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        data = message.replace("%", "%25")
        prop = path.replace("%", "%25").replace(":", "%3A").replace(",", "%2C")
        print(f"::warning file={prop},title=File-size ratchet::{data}")


def check(
    paths: list[str],
    baseline_path: Path,
    default_threshold: int,
    growth: str = "error",
    base: str | None = None,
    budget: int = 30,
    hard_stop: int = 100,
    hard_stop_size: int = 5000,
) -> int:
    if baseline_path.exists():
        data = load_baseline(baseline_path)
    else:
        data = {"threshold": default_threshold, "exclude": [], "files": {}}
    threshold = int(data["threshold"])
    files: dict[str, int] = data["files"]
    errors: list[str] = []
    changed = False
    sizes: dict[str, int] = {}
    base_rev = resolve_base(base) if growth == "budget" else None
    if growth == "budget" and base_rev is None:
        warn(
            str(baseline_path),
            "cannot resolve the growth base; a listed file that grows will fail "
            "instead of being measured against the change.",
        )
    new_module_lines: int | None = None

    for rel in dict.fromkeys(paths):
        file = Path(rel)
        if (
            not is_checked(rel, data["exclude"])
            or file.is_symlink()
            or not file.is_file()
        ):
            continue
        lines = sizes[rel] = count_lines(file)
        allowed = files.get(rel)
        if lines < threshold:
            if allowed is not None:
                del files[rel]
                changed = True
        elif allowed is None:
            errors.append(
                f"{rel}: {lines} lines, at or over the {threshold}-line limit. "
                "Split it instead of adding to it."
            )
        elif growth == "off":
            continue
        elif base_rev is not None:
            before = lines_at(base_rev, rel)
            before = allowed if before is None else before
            grown = lines - before
            if grown > 0:
                if new_module_lines is None:
                    new_module_lines = added_code_lines(base_rev, data["exclude"])
                grew = f"grew by {grown} lines (from {before} to {lines})"
                if before >= hard_stop_size and grown > hard_stop:
                    errors.append(
                        f"{rel}: {grew}; files over {hard_stop_size} lines may "
                        f"grow by at most {hard_stop} lines per change. Split it."
                    )
                elif grown > budget and new_module_lines < grown:
                    errors.append(
                        f"{rel}: {grew}, over the {budget}-line budget for a "
                        f"file at or over the {threshold}-line limit, and the new "
                        f"files in this change add only {new_module_lines} lines. "
                        "Move the new code into a new module."
                    )
                else:
                    warn(rel, f"{grew}; it is already over the limit.")
            if lines < allowed and lines < before:
                files[rel] = lines
                changed = True
        elif lines > allowed:
            grew = (
                f"grew from {allowed} to {lines} lines; it is already at or over "
                f"the {threshold}-line limit"
            )
            if growth == "warn":
                warn(rel, f"{grew}. Prefer putting new code in a new module.")
            elif growth == "budget":
                errors.append(
                    f"{rel}: {grew} and the growth base could not be resolved "
                    "(shallow checkout?); fetch the base branch or pass --base so "
                    "the budget can measure the change, or do not grow the file."
                )
            else:
                errors.append(f"{rel}: {grew} and may only shrink.")
        elif lines < allowed:
            files[rel] = lines
            changed = True

    if growth == "off":
        for error in errors:
            print(error, file=sys.stderr)
        return 1 if errors else 0

    # Index membership distinguishes deletions (including git rm --cached)
    # from tracked files merely omitted by a sparse checkout.
    staged = staged_entries(baseline_path)
    tracked = set(tracked_files()) if files or staged else set()
    for rel in list(files):
        if rel not in tracked:
            del files[rel]
            changed = True

    if changed and baseline_path.exists():
        save_baseline(baseline_path, data)
        print(f"Lowered entries in {baseline_path}; stage it with this commit.")
    # Rewriting the file is not enough to stop a commit: in a shared checkout a
    # retry sees the already-lowered, unstaged baseline, changes nothing, and
    # would land the shrink or deletion without it. CI then fails on every later
    # PR.
    stale = [
        rel
        for rel, allowed in staged.items()
        if rel not in tracked
        or (rel in sizes and (rel not in files or files[rel] < allowed))
    ]
    if stale:
        errors.append(
            f"{baseline_path}: the staged copy is not lowered for "
            f"{', '.join(stale)}; stage it with this commit."
        )
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "paths", nargs="*", help="Files to check (passed by pre-commit)."
    )
    parser.add_argument(
        "--baseline", default=DEFAULT_BASELINE, help="Baseline JSON path."
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help="Line limit when no baseline exists yet, or for --init.",
    )
    parser.add_argument(
        "--init", action="store_true", help="Create the baseline and exit."
    )
    parser.add_argument(
        "--growth",
        choices=("error", "warn", "budget", "off"),
        default="error",
        help="How growth of a file already over the limit is handled.",
    )
    parser.add_argument(
        "--base", help="Growth base for --growth=budget (merge base with HEAD)."
    )
    parser.add_argument(
        "--budget", type=int, default=30, help="Lines a listed file may grow."
    )
    parser.add_argument(
        "--hard-stop", type=int, default=100, help="Growth that always fails..."
    )
    parser.add_argument(
        "--hard-stop-size",
        type=int,
        default=5000,
        help="...for files at least this long at the base.",
    )
    args = parser.parse_args(argv)
    baseline = Path(args.baseline)
    if args.init:
        return init_baseline(baseline, args.threshold)
    try:
        return check(
            args.paths,
            baseline,
            args.threshold,
            args.growth,
            base=args.base,
            budget=args.budget,
            hard_stop=args.hard_stop,
            hard_stop_size=args.hard_stop_size,
        )
    except (ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
