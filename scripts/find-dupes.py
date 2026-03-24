#!/usr/bin/env python3
"""Find duplicate and near-duplicate files across a repository.

Replaces fdupes for exact duplicates, with optional jscpd integration
for near-duplicate (copy-paste) detection.

Usage:
    python3 scripts/find-dupes.py                    # Exact duplicates
    python3 scripts/find-dupes.py --near-dupes       # Also run jscpd
    python3 scripts/find-dupes.py --ext .py .sh      # Filter by extension
    python3 scripts/find-dupes.py --min-lines 10     # Skip tiny files
    python3 scripts/find-dupes.py path/to/dir        # Scan specific dirs
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path


def get_repo_root() -> Path:
    """Get git repository root, falling back to CWD."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd()


# Default scan directories (relative to repo root)
DEFAULT_SCAN_DIRS = [
    "scripts",
    "packages",
    "plugins",
]

# Patterns to exclude
EXCLUDE_PATTERNS = {
    "__pycache__",
    ".egg-info",
    "mypy_cache",
    ".ruff_cache",
    "sync-conflict",
    "__init__.py",
    ".pyc",
    "node_modules",
    ".git",
    ".venv",
}

# Default extensions to scan
DEFAULT_EXTENSIONS = {".py", ".sh", ".md"}


def should_exclude(path: Path) -> bool:
    """Check if path should be excluded."""
    path_str = str(path)
    if any(pat in path_str for pat in EXCLUDE_PATTERNS):
        return True
    # Skip files inside symlinked directories (they show up as dupes of their targets)
    for parent in path.parents:
        if parent.is_symlink():
            return True
    return False


def file_hash(path: Path) -> str:
    """Compute SHA-256 hash of file contents."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
    except (PermissionError, OSError):
        return ""
    return h.hexdigest()


def count_lines(path: Path) -> int:
    """Count non-empty lines in a file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.strip())
    except (PermissionError, OSError):
        return 0


def collect_files(
    scan_dirs: list[Path],
    extensions: set[str],
    min_lines: int = 3,
) -> list[Path]:
    """Collect files matching criteria from scan directories."""
    files = []
    for d in scan_dirs:
        if not d.exists():
            continue
        for f in d.rglob("*"):
            if not f.is_file():
                continue
            if f.is_symlink():
                continue  # Skip symlinks to avoid false dupes
            if f.suffix not in extensions:
                continue
            if should_exclude(f):
                continue
            if min_lines > 0 and count_lines(f) < min_lines:
                continue
            files.append(f)
    return files


def find_exact_duplicates(files: list[Path]) -> list[list[Path]]:
    """Find groups of files with identical content."""
    # First group by size (quick filter)
    size_groups: dict[int, list[Path]] = defaultdict(list)
    for f in files:
        try:
            size_groups[f.stat().st_size].append(f)
        except OSError:
            continue

    # Then hash files that share a size
    hash_groups: dict[str, list[Path]] = defaultdict(list)
    for paths in size_groups.values():
        if len(paths) < 2:
            continue
        for p in paths:
            h = file_hash(p)
            if h:
                hash_groups[h].append(p)

    # Return groups with 2+ files
    return [sorted(group) for group in hash_groups.values() if len(group) >= 2]


def find_near_duplicates(
    scan_dirs: list[Path],
    min_lines: int = 5,
    min_tokens: int = 50,
    threshold: int = 5,
) -> dict | None:
    """Run jscpd for near-duplicate detection. Returns parsed JSON results."""
    import shutil

    jscpd = shutil.which("jscpd")
    if not jscpd:
        print(
            "jscpd not installed. Install with: npm install -g jscpd", file=sys.stderr
        )
        return None

    dirs = [str(d) for d in scan_dirs if d.exists()]
    if not dirs:
        return None

    # Use a secure temporary directory to avoid symlink-attack via predictable /tmp paths
    with tempfile.TemporaryDirectory(prefix="jscpd-report-") as jscpd_outdir:
        cmd = [
            jscpd,
            *dirs,
            "--min-lines",
            str(min_lines),
            "--min-tokens",
            str(min_tokens),
            "--threshold",
            str(threshold),
            "--reporters",
            "json",
            "--output",
            jscpd_outdir,
            "--ignore",
            "**/__pycache__/**,**/.egg-info/**,**/node_modules/**,**/.venv/**,**/.git/**",
        ]

        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            # jscpd exits 0 even with dupes
            report_path = Path(jscpd_outdir) / "jscpd-report.json"
            if report_path.exists():
                with open(report_path) as f:
                    data: dict[str, object] = json.load(f)
                    return data
        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            json.JSONDecodeError,
        ) as e:
            print(f"jscpd error: {e}", file=sys.stderr)

    return None


def format_path(path: Path, workspace: Path) -> str:
    """Format path relative to workspace or home."""
    try:
        return str(path.relative_to(workspace))
    except ValueError:
        try:
            return str(path.relative_to(Path.home()))
        except ValueError:
            return str(path)


def main():
    parser = argparse.ArgumentParser(
        description="Find duplicate files across a repository"
    )
    parser.add_argument(
        "--ext",
        nargs="+",
        default=None,
        help="File extensions to scan (default: .py .sh .md)",
    )
    parser.add_argument(
        "--min-lines", type=int, default=3, help="Minimum non-empty lines (default: 3)"
    )
    parser.add_argument(
        "--near-dupes",
        action="store_true",
        help="Also run jscpd for near-duplicate detection",
    )
    parser.add_argument(
        "--cross-repo-dir",
        action="append",
        dest="cross_repo_dirs",
        metavar="DIR",
        help="Additional cross-repo directory to include (can be specified multiple times)",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "dirs", nargs="*", help="Directories to scan (overrides defaults)"
    )
    args = parser.parse_args()

    workspace = get_repo_root()
    extensions = set(args.ext) if args.ext else DEFAULT_EXTENSIONS

    # Build scan dirs: explicit dirs override defaults; cross-repo dirs are always additive
    if args.dirs:
        scan_dirs = [Path(d).resolve() for d in args.dirs]
    else:
        scan_dirs = [workspace / d for d in DEFAULT_SCAN_DIRS]
    if args.cross_repo_dirs:
        scan_dirs.extend(Path(d).resolve() for d in args.cross_repo_dirs)

    # Collect files
    files = collect_files(scan_dirs, extensions, args.min_lines)
    print(
        f"Scanning {len(files)} files across {len(scan_dirs)} directories...",
        file=sys.stderr,
    )

    # Find exact duplicates
    exact_groups = find_exact_duplicates(files)

    if args.json:
        result: dict[str, object] = {
            "exact_duplicates": [
                {
                    "files": [format_path(f, workspace) for f in group],
                    "lines": count_lines(group[0]),
                    "size": group[0].stat().st_size,
                }
                for group in exact_groups
            ],
        }
        if args.near_dupes:
            near = find_near_duplicates(scan_dirs, args.min_lines)
            if near:
                result["near_duplicates"] = near
        print(json.dumps(result, indent=2))
    else:
        # Print exact duplicates
        if exact_groups:
            print(f"\n## Exact Duplicates ({len(exact_groups)} groups)\n")
            sorted_groups = sorted(
                [(count_lines(g[0]), g) for g in exact_groups], key=lambda t: -t[0]
            )
            for i, (lines, group) in enumerate(sorted_groups, 1):
                size = group[0].stat().st_size
                print(f"### Group {i} ({lines} lines, {size} bytes)")
                for f in group:
                    print(f"  - {format_path(f, workspace)}")
                print()
        else:
            print("\nNo exact duplicates found.")

        # Near duplicates
        if args.near_dupes:
            print("\n## Near Duplicates (via jscpd)\n")
            near = find_near_duplicates(scan_dirs, args.min_lines)
            if near and "duplicates" in near:
                dupes = near["duplicates"]
                if dupes:
                    for d in sorted(dupes, key=lambda x: -x.get("lines", 0)):
                        first = d.get("firstFile", {})
                        second = d.get("secondFile", {})
                        lines = d.get("lines", 0)
                        tokens = d.get("tokens", 0)
                        print(f"  {lines} lines, {tokens} tokens:")
                        print(
                            f"    {first.get('name', '?')}:{first.get('startLoc', {}).get('line', '?')}-{first.get('endLoc', {}).get('line', '?')}"
                        )
                        print(
                            f"    {second.get('name', '?')}:{second.get('startLoc', {}).get('line', '?')}-{second.get('endLoc', {}).get('line', '?')}"
                        )
                        print()
                else:
                    print("No near duplicates found above threshold.")
            elif near:
                print("jscpd completed but no duplicates section in output.")
            else:
                print("jscpd failed or not installed.")

    # Summary
    total_waste = sum(count_lines(g[0]) * (len(g) - 1) for g in exact_groups)
    print(
        f"\nSummary: {len(exact_groups)} exact duplicate groups, ~{total_waste} duplicate lines",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
