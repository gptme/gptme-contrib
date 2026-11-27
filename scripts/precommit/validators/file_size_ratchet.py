#!/usr/bin/env python3
"""Ratchet source-file sizes: no new oversized files, no growth of existing ones.

A file under the threshold (default 2,000 lines) always passes. A file at or
over it passes only if it is listed in the checked-in baseline and has not grown
past its recorded size. When a listed file shrinks, the hook lowers its entry
(or drops it once the file is back under the threshold); the baseline never
goes up on its own. Growing an oversized file on purpose, or renaming one,
means editing the baseline by hand, where review can see it.

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
    data = path.read_bytes()
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


def check(paths: list[str], baseline_path: Path, default_threshold: int) -> int:
    if baseline_path.exists():
        data = load_baseline(baseline_path)
    else:
        data = {"threshold": default_threshold, "exclude": [], "files": {}}
    threshold = int(data["threshold"])
    files: dict[str, int] = data["files"]
    errors: list[str] = []
    changed = False

    for rel in dict.fromkeys(paths):
        file = Path(rel)
        if (
            not is_checked(rel, data["exclude"])
            or file.is_symlink()
            or not file.is_file()
        ):
            continue
        lines = count_lines(file)
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
        elif lines > allowed:
            errors.append(
                f"{rel}: grew from {allowed} to {lines} lines; it is already at or over "
                f"the {threshold}-line limit and may only shrink."
            )
        elif lines < allowed:
            files[rel] = lines
            changed = True

    # Index membership distinguishes deletions (including git rm --cached)
    # from tracked files merely omitted by a sparse checkout.
    if files:
        tracked = set(tracked_files())
        for rel in list(files):
            if rel not in tracked:
                del files[rel]
                changed = True

    if changed and baseline_path.exists():
        save_baseline(baseline_path, data)
        print(f"Lowered entries in {baseline_path}; stage it with this commit.")
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
    args = parser.parse_args(argv)
    baseline = Path(args.baseline)
    if args.init:
        return init_baseline(baseline, args.threshold)
    try:
        return check(args.paths, baseline, args.threshold)
    except (ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
