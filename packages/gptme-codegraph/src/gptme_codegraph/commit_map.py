#!/usr/bin/env python3
"""Generate, save, and refresh gptme-codegraph repo-map artifacts.

Generates a structural repo outline via tree-sitter, with stat-fingerprint
caching (~/.cache/gptme-codegraph/) so repeated runs on unchanged source are
near-instant (stat-only fingerprint match). The artifact is on-the-fly
generated — not committed to git — and cached for speed.

Usage:
    python3 -m gptme_codegraph.commit_map <repo-dir> [--output FILE]
    python3 -m gptme_codegraph.commit_map <repo-dir> --check
    python3 -m gptme_codegraph.commit_map <repo-dir> --refresh
    python3 -m gptme_codegraph.commit_map <repo-dir> --refresh --force
    python3 -m gptme_codegraph.commit_map <repo-dir> --refresh --no-cache
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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import build_repo_map, format_repo_map

DEFAULT_OUTPUT_FILE = ".gptme-codegraph-map.json"
DEFAULT_STALE_DAYS = 1
ARTIFACT_VERSION = 2
SOURCE_PATTERNS = ("*.py", "*.ts", "*.tsx", "*.js", "*.rs")

# Stat-fingerprint cache: avoids re-running tree-sitter when source is unchanged.
# Located outside the repo so it never shows up in git status / committed artifacts.
_CACHE_DIR = Path.home() / ".cache" / "gptme-codegraph"
# Cache entries older than this are treated as stale even if the fingerprint matches,
# so tree-sitter library upgrades and mapping improvements are picked up eventually.
_CACHE_TTL_DAYS = 7


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


def _tracked_source_files(directory: Path) -> tuple[list[Path], Path] | None:
    """Return (tracked source files, repo_root) or None on git failure."""
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
    return files, repo_root


def _source_digest(directory: Path) -> tuple[str | None, int]:
    """Return a stable digest of tracked source files plus the file count."""
    result = _tracked_source_files(directory)
    if result is None:
        return None, 0

    tracked, repo_root = result
    digest = hashlib.sha256()
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


def _stat_fingerprint(directory: Path) -> tuple[str | None, int]:
    """Return a stat-only fingerprint of tracked source files + file count.

    Uses (relative_path, mtime_ns, size) tuples — no file content reads.
    This is ~100x faster than _source_digest() for large repos because it
    only needs inode metadata, not full file reads.

    Returns (sha256_hex, file_count) or (None, 0) on failure.
    """
    result = _tracked_source_files(directory)
    if result is None:
        return None, 0

    tracked, repo_root = result
    digest = hashlib.sha256()
    count = 0
    for path in tracked:
        try:
            stat_info = path.stat()
            rel_path = path.relative_to(repo_root).as_posix()
            # Canonicalise: path + mtime_ns + size — no content read.
            fingerprint_line = (
                f"{rel_path}\0{stat_info.st_mtime_ns}\0{stat_info.st_size}"
            )
            digest.update(fingerprint_line.encode("utf-8"))
            digest.update(b"\0")
            count += 1
        except OSError:
            return None, 0

    return digest.hexdigest(), count


def _repo_cache_key(directory: Path) -> str:
    """Return a stable cache key for a repo directory (sha256 of resolved path)."""
    return hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:16]


def _read_cache(cache_key: str) -> dict[str, object] | None:
    """Read a cached map entry, returning None if missing, stale, or unparseable."""
    cache_path = _CACHE_DIR / f"{cache_key}.json"
    if not cache_path.exists():
        return None
    try:
        entry = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    # Check cache TTL
    cached_ts = entry.get("_cached_at")
    if isinstance(cached_ts, int | float):
        age = time.time() - float(cached_ts)
        if age > _CACHE_TTL_DAYS * 86400:
            return None
    else:
        # Missing _cached_at → can't verify freshness, reject
        return None

    # Version compatibility
    if entry.get("version") != ARTIFACT_VERSION:
        return None

    return entry  # type: ignore[no-any-return]


def _write_cache(cache_key: str, map_data: dict[str, object]) -> None:
    """Write a map result to the stat-fingerprint cache atomically."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _CACHE_DIR / f"{cache_key}.json"
    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")

    entry = {**map_data, "_cached_at": time.time()}
    with open(tmp_path, "w") as f:
        json.dump(entry, f, indent=2, sort_keys=True, default=str)
    os.replace(tmp_path, cache_path)


def generate_map(
    directory: Path,
    *,
    max_files: int = 20,
    max_symbols_per_file: int = 12,
    use_cache: bool = True,
) -> dict[str, object]:
    """Generate a repo map with metadata, matching build_repo_map() output.

    Uses a stat-fingerprint cache (~/.cache/gptme-codegraph/) to skip the
    expensive tree-sitter pipeline when source files haven't changed. Set
    ``use_cache=False`` to force a full rebuild.
    """
    # Pre-compute stat fingerprint and cache key once so we can reuse them
    # on the slow path and avoid a TOCTOU window (build_repo_map can take
    # seconds, and the second _stat_fingerprint call could see different
    # mtime values).
    stat_fp: str | None = None
    cache_key: str | None = None
    if use_cache:
        stat_fp, _ = _stat_fingerprint(directory)
        if stat_fp is not None:
            cache_key = _repo_cache_key(directory)
            cached = _read_cache(cache_key)
            if (
                cached is not None
                and cached.get("_stat_fingerprint") == stat_fp
                and cached.get("max_files") == max_files
                and cached.get("max_symbols_per_file") == max_symbols_per_file
            ):
                # Cache hit: stat fingerprint and generation parameters matched.
                # Return cached tree-sitter result + metadata, with a refreshed
                # _cached_at timestamp so frequently-accessed repos stay warm
                # while the 7-day TTL still acts as a safety net for repos that
                # aren't accessed.
                try:
                    _write_cache(
                        cache_key,
                        {k: v for k, v in cached.items() if k != "_cached_at"},
                    )
                except OSError:
                    pass  # cache write-back is optional; don't discard the hit
                return {
                    **{k: v for k, v in cached.items() if not k.startswith("_")},
                    "generated": datetime.now(timezone.utc).isoformat(),
                }

    # Slow path: full content digest + tree-sitter build.
    source_digest, source_file_count = _source_digest(directory)
    repo_map = build_repo_map(
        str(directory),
        max_files=max_files,
        max_symbols_per_file=max_symbols_per_file,
    )

    result: dict[str, object] = {
        **repo_map,
        # The committed artifact lives in the repo it maps, so the absolute
        # build path from build_repo_map() is non-portable: it leaks the
        # generating machine's filesystem layout and churns the diff across
        # clones/CI. Per-file paths are already repo-relative; pin the
        # top-level directory to "." so the artifact is reproducible anywhere.
        # (The live MCP server keeps the absolute path — it is a query
        # response, not a committed file.)
        "directory": ".",
        "version": ARTIFACT_VERSION,
        "generated": datetime.now(timezone.utc).isoformat(),
        "generator": "gptme-codegraph-commit-map",
        "git_sha": _git_sha(directory),
        "source_digest": source_digest,
        "source_file_count": source_file_count,
        "max_files": max_files,
        "max_symbols_per_file": max_symbols_per_file,
    }

    if use_cache and stat_fp is not None and cache_key is not None:
        # Reuse the stat fingerprint already computed before the tree-sitter
        # build — avoids a TOCTOU window where the second call could see
        # different mtime values (parallel writes, editor auto-save).
        try:
            _write_cache(
                cache_key,
                {**result, "_stat_fingerprint": stat_fp},
            )
        except OSError:
            pass  # cache write is optional; caller still gets the computed result

    return result


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
        except (ValueError, TypeError):
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
    if current_sha is None:
        # Git unavailable: can't verify freshness, treat as stale to be safe.
        return False
    if map_sha and current_sha != map_sha:
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
    use_cache = not getattr(args, "no_cache", False)
    map_data = generate_map(
        directory,
        max_files=args.max_files,
        max_symbols_per_file=args.max_symbols_per_file,
        use_cache=use_cache,
    )
    save_map(directory, output_path, map_data)

    files_shown = map_data.get("files_shown", 0)
    symbols_shown = map_data.get("symbols_shown", 0)
    print(f"Saved repo map to {output_path}")
    print(f"  {files_shown} files, {symbols_shown} symbols shown")
    print(format_repo_map({**map_data, "directory": str(directory)}))

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
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Skip the stat-fingerprint cache; always run the full tree-sitter pipeline",
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
