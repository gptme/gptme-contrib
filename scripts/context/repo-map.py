#!/usr/bin/env python3
"""Generate a compact repo-map for the current workspace using gptme-codegraph.

Helps sessions quickly understand the structure of a target repo without loading
the full codebase. Intended for use in autonomous sessions that need structural
context on a repository they're about to work on.

Usage:
    uv run python3 scripts/context/repo-map.py /path/to/repo --max-files 10
    uv run python3 scripts/context/repo-map.py /path/to/repo --max-files 15 --max-symbols 100
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # gptme-contrib root
_CODEGRAPH_COMMIT_MAP = _REPO_ROOT / "scripts" / "codegraph-commit-map.py"
_COMMITTED_MAP_FILE = ".gptme-codegraph-map.json"
_CODEGRAPH_SRC = _REPO_ROOT / "packages" / "gptme-codegraph" / "src"

if _CODEGRAPH_SRC.is_dir():
    sys.path.insert(0, str(_CODEGRAPH_SRC))

try:
    from gptme_codegraph.core import format_repo_map  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only in degraded local envs
    format_repo_map = None


def _load_committed_repo_map(path: str) -> str | None:
    """Return a fresh committed repo-map artifact when available."""
    if format_repo_map is None:
        return None

    directory = Path(path).resolve()
    artifact_path = directory / _COMMITTED_MAP_FILE
    if not artifact_path.exists():
        return None

    result = subprocess.run(
        [
            sys.executable,
            str(_CODEGRAPH_COMMIT_MAP),
            str(directory),
            "--check",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    try:
        with artifact_path.open() as f:
            repo_map = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    return str(format_repo_map(repo_map))


def get_repo_map(
    path: str, max_files: int = 10, max_symbols: int = 80
) -> tuple[str, str]:
    """Return repo-map text plus the source used to produce it."""
    committed_map = _load_committed_repo_map(path)
    if committed_map:
        return committed_map, f"committed artifact ({_COMMITTED_MAP_FILE})"

    cmd = [
        "uv",
        "run",
        "gptme-codegraph",
        path,
        "map",
        "--max-files",
        str(max_files),
        "--max-symbols",
        str(max_symbols),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = (result.stdout or "") + (result.stderr or "")
    return output.strip(), "live gptme-codegraph"


def get_repo_landmarks(path: str) -> dict[str, object]:
    """Quick structural landmarks for a repo: key config files, entry points."""
    p = Path(path).resolve()
    landmarks: dict[str, object] = {}

    for name in (
        "pyproject.toml",
        "Cargo.toml",
        "package.json",
        "Makefile",
        "README.md",
        "CLAUDE.md",
        "AGENTS.md",
        "gptme.toml",
    ):
        candidate = p / name
        if candidate.exists():
            landmarks[name] = str(candidate)

    # entry points
    for src_subdir in ("src", "gptme"):
        src = p / src_subdir
        if src.is_dir():
            main_candidates = (
                list(src.glob("__main__.py"))
                + list(src.glob("main.py"))
                + list(src.glob("cli.py"))
            )
            if main_candidates:
                landmarks["entry_points"] = [str(m) for m in main_candidates]
            break

    package_dirs = [d for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")]
    landmarks["subdirectories"] = sorted(d.name for d in package_dirs)

    return landmarks


def format_repo_summary(path: str, max_files: int = 10, max_symbols: int = 80) -> str:
    """Full summary: landmarks + repo map."""
    landmarks = get_repo_landmarks(path)
    repo_map, repo_map_source = get_repo_map(
        path, max_files=max_files, max_symbols=max_symbols
    )

    parts = [f"## Repo Map: {path}"]
    parts.append("")

    if landmarks:
        if landmarks.get("subdirectories"):
            parts.append(
                f"**Subdirectories**: {', '.join(landmarks['subdirectories'])}"  # type: ignore[arg-type]
            )
        parts.append("")
        cfg_keys = [
            k
            for k in (
                "pyproject.toml",
                "Cargo.toml",
                "package.json",
                "Makefile",
                "README.md",
                "CLAUDE.md",
                "AGENTS.md",
                "gptme.toml",
            )
            if k in landmarks
        ]
        if cfg_keys:
            parts.append(f"**Config files**: {', '.join(cfg_keys)}")
        if landmarks.get("entry_points"):
            parts.append(
                f"**Entry points**: {', '.join(landmarks['entry_points'])}"  # type: ignore[arg-type]
            )
        parts.append("")

    parts.append(f"**Repo-map source**: {repo_map_source}")
    parts.append("")
    parts.append("```txt")
    if repo_map:
        # Trim per-file detail unless it's short
        lines = repo_map.split("\n")
        for line in lines[: max_files * 3 + 5]:
            parts.append(line)
    else:
        parts.append("(no repo-map data)")
    parts.append("```")

    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(
        description="Generate repo-map context for a target repo"
    )
    parser.add_argument("path", help="Path to the repository or package directory")
    parser.add_argument(
        "--max-files", type=int, default=10, help="Max files in repo-map output"
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=80,
        help="Max symbols in repo-map output",
    )
    parser.add_argument(
        "--save", type=str, help="Save output to a file instead of stdout"
    )
    args = parser.parse_args()

    summary = format_repo_summary(
        args.path, max_files=args.max_files, max_symbols=args.max_symbols
    )

    if args.save:
        Path(args.save).write_text(summary + "\n")
        print(f"Saved to {args.save}")
    else:
        print(summary)


if __name__ == "__main__":
    main()
