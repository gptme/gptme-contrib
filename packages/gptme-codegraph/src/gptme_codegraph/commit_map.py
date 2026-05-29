#!/usr/bin/env python3
"""Generate, save, and refresh committed gptme-codegraph repo-map artifacts.

A committed ``.gptme-codegraph-map.json`` lets teammates and agents read a
repo's structural outline without re-running the tree-sitter pipeline
("analyze once, commit the graph"). Freshness is keyed off a digest of the
tracked source files, so a pre-commit-generated artifact stays fresh after the
commit lands (unlike a naive ``git_sha == HEAD`` check, which goes stale the
instant the commit that contains it is created).

Usage:
    python3 -m gptme_codegraph.commit_map <repo-dir> [--output FILE]
    python3 -m gptme_codegraph.commit_map <repo-dir> --check
    python3 -m gptme_codegraph.commit_map <repo-dir> --refresh
    python3 -m gptme_codegraph.commit_map <repo-dir> --refresh --force
    python3 -m gptme_codegraph.commit_map <repo-dir> --stale-after-days N

Or via the installed console script:
    gptme-codegraph-commit-map <repo-dir> --refresh
    gptme-codegraph-commit-map <repo-dir> --refresh --force
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import build_repo_map, format_repo_map

DEFAULT_OUTPUT_FILE = ".gptme-codegraph-map.json"
DEFAULT_STALE_DAYS = 1
ARTIFACT_VERSION = 1
SOURCE_PATTERNS = ("*.py", "*.ts", "*.tsx", "*.js", "*.rs")


def _git_sha(directory: Path) -> str | None:
    """Get the current HEAD SHA for a directory."""
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _git_toplevel(directory: Path) -> Path | None:
    """Return the git top-level directory for a path, or None if unavailable."""
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _tracked_source_files(directory: Path) -> list[Path] | None:
    """Return tracked source files for digesting, or None on git failure."""
    repo_root = _git_toplevel(directory)
    if repo_root is None:
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(directory),
                "ls-files",
                "-z",
                "--full-name",
                "--",
                *SOURCE_PATTERNS,
            ],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
    except (subprocess.TimeoutExpired, OSError):
        return None

    files: list[Path] = []
    for rel_path in result.stdout.decode("utf-8", errors="ignore").split("\0"):
        if not rel_path:
            continue
        path = repo_root / rel_path
        if path.is_file():
            files.append(path)
    return files


def _source_digest(directory: Path) -> tuple[str | None, int]:
    """Return a stable digest of tracked source files plus the file count."""
    tracked = _tracked_source_files(directory)
    if tracked is None:
        return None, 0

    digest = hashlib.sha256()
    repo_root = _git_toplevel(directory)
    if repo_root is None:
        return None, 0

    count = 0
    for path in tracked:
        try:
            rel_path = path.relative_to(repo_root).as_posix()
            digest.update(rel_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
            count += 1
        except OSError:
            return None, 0

    return digest.hexdigest(), count


def generate_map(
    directory: Path,
    *,
    max_files: int = 20,
    max_symbols_per_file: int = 12,
) -> dict[str, object]:
    """Generate a repo map with metadata, matching build_repo_map() output."""
    source_digest, source_file_count = _source_digest(directory)
    repo_map = build_repo_map(
        str(directory),
        max_files=max_files,
        max_symbols_per_file=max_symbols_per_file,
    )

    return {
        **repo_map,
        "version": ARTIFACT_VERSION,
        "generated": datetime.now(timezone.utc).isoformat(),
        "generator": "gptme-codegraph-commit-map",
        "git_sha": _git_sha(directory),
        "source_digest": source_digest,
        "source_file_count": source_file_count,
        "max_files": max_files,
        "max_symbols_per_file": max_symbols_per_file,
    }


def _load_existing_map(path: Path) -> dict[str, object] | None:
    """Load an existing map file, returning None if missing or unparseable."""
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return None


def _map_is_fresh(
    existing: dict[str, object],
    directory: Path,
    stale_after_days: int,
) -> bool:
    """Return True if the existing map is still valid (fresh + matching source)."""
    # Check version compatibility
    version = existing.get("version")
    if version != ARTIFACT_VERSION:
        return False

    # Check staleness by age
    generated_str = existing.get("generated")
    if isinstance(generated_str, str):
        try:
            generated = datetime.fromisoformat(generated_str)
            age = datetime.now(timezone.utc) - generated
            if age > timedelta(days=stale_after_days):
                return False
        except ValueError:
            return False
    else:
        return False

    # New contract: freshness tracks source content, not HEAD. A committed
    # artifact generated pre-commit should still be fresh after the commit lands.
    source_digest = existing.get("source_digest")
    if isinstance(source_digest, str) and source_digest:
        current_digest, _ = _source_digest(directory)
        if current_digest is None:
            return False
        return current_digest == source_digest

    # Backward-compat fallback for older artifacts without a source_digest.
    current_sha = _git_sha(directory)
    map_sha = existing.get("git_sha")
    if current_sha and map_sha and current_sha != map_sha:
        return False

    return True


def save_map(directory: Path, output_path: Path, map_data: dict[str, object]) -> None:
    """Write the map to disk with atomic semantics (write tmp, then rename)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(map_data, f, indent=2, sort_keys=True, default=str)
    os.replace(tmp_path, output_path)


def cmd_generate(args: argparse.Namespace) -> int:
    """Generate a new map and save it."""
    directory = Path(args.directory).resolve()
    if not directory.is_dir():
        print(f"Error: not a directory: {directory}", file=sys.stderr)
        return 1

    output_path = directory / (args.output or DEFAULT_OUTPUT_FILE)
    map_data = generate_map(
        directory,
        max_files=args.max_files,
        max_symbols_per_file=args.max_symbols_per_file,
    )
    save_map(directory, output_path, map_data)

    files_shown = map_data.get("files_shown", 0)
    symbols_shown = map_data.get("symbols_shown", 0)
    print(f"Saved repo map to {output_path}")
    print(f"  {files_shown} files, {symbols_shown} symbols shown")
    print(format_repo_map(map_data))

    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Check freshness of existing map. Exit 0 if fresh, 1 if stale/missing."""
    directory = Path(args.directory).resolve()
    output_path = directory / (args.output or DEFAULT_OUTPUT_FILE)

    existing = _load_existing_map(output_path)
    if existing is None:
        print(f"STALE: no map found at {output_path}")
        return 1

    stale_days = getattr(args, "stale_after_days", DEFAULT_STALE_DAYS)
    if _map_is_fresh(existing, directory, stale_days):
        generated = existing.get("generated", "unknown")
        sha = existing.get("git_sha", "unknown")
        print(f"FRESH: generated {generated}, sha {sha}")
        return 0
    else:
        generated = existing.get("generated", "unknown")
        current_sha = _git_sha(directory) or "unknown"
        map_sha = existing.get("git_sha", "unknown")
        print(
            f"STALE: generated {generated}, "
            f"map sha {map_sha}, current sha {current_sha}"
        )
        return 1


def cmd_refresh(args: argparse.Namespace) -> int:
    """Check freshness and regenerate if stale."""
    check_args = argparse.Namespace(
        directory=args.directory,
        output=args.output,
        stale_after_days=args.stale_after_days,
    )
    if cmd_check(check_args) == 0 and not args.force:
        print("Map is fresh, no refresh needed (use --force to override)")
        return 0

    print("Map is stale or missing, regenerating...")
    return cmd_generate(args)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate, save, and refresh committed gptme-codegraph repo-map artifacts",
    )
    parser.add_argument("directory", help="Repository directory to map")
    parser.add_argument(
        "--output",
        "-o",
        default=DEFAULT_OUTPUT_FILE,
        help=f"Output filename (default: {DEFAULT_OUTPUT_FILE})",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=20,
        help="Maximum files to include in map",
    )
    parser.add_argument(
        "--max-symbols-per-file",
        type=int,
        default=12,
        help="Maximum symbols per file",
    )
    parser.add_argument(
        "--stale-after-days",
        type=int,
        default=DEFAULT_STALE_DAYS,
        help=f"Treat map as stale after N days (default: {DEFAULT_STALE_DAYS})",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="Check freshness (exit 0=fresh, 1=stale)"
    )
    mode.add_argument(
        "--refresh",
        action="store_true",
        help="Check freshness and regenerate if stale",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force refresh even if fresh (use with --refresh)",
    )

    args = parser.parse_args()

    if args.check:
        sys.exit(cmd_check(args))
    elif args.refresh or args.force:
        sys.exit(cmd_refresh(args))
    else:
        sys.exit(cmd_generate(args))


if __name__ == "__main__":
    main()
