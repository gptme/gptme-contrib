#!/usr/bin/env python3
"""
Enforce symlink hygiene in agent workspaces.

This lives in gptme-contrib so *any* agent (not just the template) can run it.
`--mode=default` is the check for an established agent; `--mode=forked` is the
strict template/fresh-fork check.

Two modes:

  --mode=default  (for an established agent or the template)
    Checks agent scripts/ against gptme-contrib:
    1. Hash match:  file is a verbatim copy of a contrib file → should be a symlink
    2. Name match:  file shares a name with a contrib file but has different content
                    → likely drifted, should be consolidated and symlinked

  --mode=forked   (for freshly forked agents / the template's own CI)
    Enforces the invariant: every script file in the forked agent must be either:
    (a) a symlink to gptme-contrib, or
    (b) in the allowlist (built-in defaults, or --allowlist-file)
    This catches brand-new scripts added to the template that accidentally get
    copied to forked agents without a contrib counterpart.

Usage:
    check_symlinks.py [<agent-dir>] [--mode default|forked] [--contrib-dir <path>]
                      [--allowlist-file <path>] [-v]

Exit codes:
    0 - Clean
    1 - Issues found (errors)
    2 - Bad arguments
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path

# Scripts that are legitimately regular files in a forked agent (forked mode).
# Entries are workspace-relative paths (e.g. "scripts/compare.sh"), not bare
# names, so two scripts in different directories with the same name are
# distinguished. Use --allowlist-file to override with agent-specific entries.
DEFAULT_AGENT_SPECIFIC_SCRIPTS = {
    "scripts/compare.sh",  # Benchmarking / harness comparison tool
    "scripts/install-deps.sh",  # Agent-specific dependency installer
    "scripts/migrate-journals.py",  # Journal format migration utility
    "scripts/search.sh",  # Workspace search utility
    # Autonomous run scripts (agent-customized copies of the run loop infrastructure)
    "scripts/autonomous-run.sh",
    "scripts/autonomous-run-cc.sh",
}

SKIP_DIRS = {".git", "__pycache__", ".venv", ".mypy_cache", "node_modules"}


def load_allowlist(path: Path) -> set[str]:
    """Read a forked-mode allowlist file: one workspace-relative path per line.

    Entries should be workspace-relative paths (e.g. ``scripts/compare.sh``),
    not bare names, so that two scripts with the same name in different
    directories can be distinguished.  Blank lines and ``#`` comments
    (whole-line or trailing) are ignored.
    """
    names: set[str] = set()
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.add(line)
    return names


def hash_file(path: Path) -> str | None:
    """Compute SHA-256 hash of file contents, or None if trivial (whitespace-only).

    Combines the trivial check with hashing in a single read so callers never
    load the same file twice.  Package markers like an empty ``__init__.py``
    carry no shareable logic and produce bogus hash matches, so they are
    excluded by returning None.
    """
    h = hashlib.sha256()
    has_content = False
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
            if not has_content and chunk.strip():
                has_content = True
    return h.hexdigest() if has_content else None


def is_script_file(path: Path) -> bool:
    """Check if a file is a script (.sh, .py, or executable)."""
    return (
        path.name.endswith(".sh")
        or path.name.endswith(".py")
        or os.access(path, os.X_OK)
    )


def iter_script_files(base_dir: Path, check_dirs: list[str]):
    """Yield (fpath, is_symlink) for all script files under check_dirs."""
    for check_dir_name in check_dirs:
        check_path = base_dir / check_dir_name
        if not check_path.exists():
            continue
        for root, dirs, files in os.walk(check_path):
            root_path = Path(root)
            if root_path.is_symlink():
                dirs.clear()
                yield root_path, True  # entire dir is a symlink — OK
                continue
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fname in files:
                fpath = root_path / fname
                if is_script_file(fpath):
                    yield fpath, fpath.is_symlink()


def build_contrib_index(
    contrib_dir: Path, verbose: bool = False
) -> tuple[dict[str, Path], dict[str, list[Path]]]:
    """Build hash_index and name_index of gptme-contrib scripts."""
    hash_index: dict[str, Path] = {}
    name_index: dict[str, list[Path]] = {}

    for root, dirs, files in os.walk(contrib_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        root_path = Path(root)
        for fname in files:
            fpath = root_path / fname
            if fpath.is_symlink() or not is_script_file(fpath):
                continue
            try:
                h = hash_file(fpath)
            except (OSError, PermissionError):
                continue
            if h is None:
                continue  # trivial/whitespace-only — skip
            hash_index[h] = fpath
            name_index.setdefault(fname, []).append(fpath)

    if verbose:
        print(
            f"  Indexed {len(hash_index)} contrib files, {len(name_index)} unique names"
        )
    return hash_index, name_index


def check_mode_default(
    agent_dir: Path,
    contrib_dir: Path,
    check_dirs: list[str],
    verbose: bool,
) -> int:
    """
    Original check: find agent scripts that are verbatim copies of contrib files
    (should be symlinks) or share names with contrib files (potential drift).
    """
    if not contrib_dir.exists():
        print(f"ERROR: gptme-contrib not found at {contrib_dir}", file=sys.stderr)
        print("  Run: git submodule update --init gptme-contrib", file=sys.stderr)
        return 1

    hash_index, name_index = build_contrib_index(contrib_dir, verbose)

    exact_copies: list[tuple[Path, Path]] = []
    name_collisions: list[tuple[Path, list[Path]]] = []

    for fpath, is_symlink in iter_script_files(agent_dir, check_dirs):
        if fpath.is_dir():
            if verbose:
                print(f"  OK (dir symlink): {fpath.relative_to(agent_dir)}/")
            continue
        if is_symlink:
            if verbose:
                target = os.readlink(fpath)
                print(f"  OK (symlink): {fpath.relative_to(agent_dir)} -> {target}")
            continue

        rel_str = str(fpath.relative_to(agent_dir))
        try:
            h = hash_file(fpath)
        except (OSError, PermissionError):
            continue
        if h is None:
            if verbose:
                print(f"  OK (trivial/empty): {rel_str}")
            continue

        if h in hash_index:
            exact_copies.append((fpath, hash_index[h]))
            if verbose:
                print(f"  COPY: {rel_str}")
        elif fpath.name in name_index:
            name_collisions.append((fpath, name_index[fpath.name]))
            if verbose:
                print(f"  DRIFT: {rel_str}")

    issues = False

    if exact_copies:
        issues = True
        print(
            f"✗ Found {len(exact_copies)} verbatim copy(s) that should be symlinks:\n"
        )
        for agent_path, contrib_path in sorted(exact_copies):
            rel_agent = agent_path.relative_to(agent_dir)
            rel_contrib = contrib_path.relative_to(contrib_dir)
            print(f"  {rel_agent}")
            print(f"    → same content as: gptme-contrib/{rel_contrib}")
            target = os.path.relpath(contrib_path, agent_path.parent)
            print(f"    → fix: ln -sf {target} {agent_path.name}")
            print()

    if name_collisions:
        issues = True
        print(
            f"⚠ Found {len(name_collisions)} script(s) sharing names with contrib (possible drift):\n"
        )
        for agent_path, contrib_paths in sorted(name_collisions):
            rel_agent = agent_path.relative_to(agent_dir)
            print(f"  {rel_agent}")
            for cp in contrib_paths:
                print(
                    f"    → same name as: gptme-contrib/{cp.relative_to(contrib_dir)}"
                )
            print(
                "    → Action: consolidate into contrib and symlink, or verify this is intentionally agent-specific."
            )
            print()

    if not issues:
        print("✓ No contrib overlap — all shared scripts are properly symlinked.")
        return 0

    return 1 if exact_copies else 0


def check_mode_forked(
    agent_dir: Path,
    check_dirs: list[str],
    verbose: bool,
    allowlist: set[str],
) -> int:
    """
    Strict check for freshly forked agents.

    Every script file must be EITHER:
      (a) a symlink (pointing to gptme-contrib or elsewhere), OR
      (b) in ``allowlist`` (explicitly allowed non-contrib script)

    Any other regular file is flagged as unexpected — it probably means a new
    script was added to the template that should go to contrib, or fork.sh
    needs to be updated to exclude it.
    """
    unexpected: list[Path] = []

    for fpath, is_symlink in iter_script_files(agent_dir, check_dirs):
        if fpath.is_dir():
            if verbose:
                print(f"  OK (dir symlink): {fpath.relative_to(agent_dir)}/")
            continue
        if is_symlink:
            if verbose:
                target = os.readlink(fpath)
                print(f"  OK (symlink): {fpath.relative_to(agent_dir)} -> {target}")
            continue
        rel = str(fpath.relative_to(agent_dir))
        if rel in allowlist:
            if verbose:
                print(f"  OK (agent-specific): {rel}")
            continue

        unexpected.append(fpath)
        if verbose:
            print(f"  UNEXPECTED: {fpath.relative_to(agent_dir)}")

    if unexpected:
        print(
            f"✗ Found {len(unexpected)} unexpected non-symlink script(s) in forked agent:\n"
        )
        for fpath in sorted(unexpected):
            rel = fpath.relative_to(agent_dir)
            print(f"  {rel}")
            print("    → This file should either:")
            print(
                "       1. Be added to gptme-contrib and replaced with a symlink here, OR"
            )
            print(
                "       2. Be excluded from fork.sh (if it's a template-only tool), OR"
            )
            print(
                "       3. Be added to the forked-mode allowlist (--allowlist-file, if it's"
            )
            print(
                "          intentionally agent-specific and not suitable for contrib)"
            )
            print()
        return 1

    print("✓ All scripts are symlinks or known agent-specific files.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enforce symlink hygiene in agent workspaces",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "agent_dir",
        nargs="?",
        default=".",
        help="Agent workspace directory to check (default: current directory)",
    )
    parser.add_argument(
        "--mode",
        choices=["default", "forked"],
        default="default",
        help=(
            "Check mode: 'default' checks for contrib overlap (use on an established "
            "agent or the template), 'forked' enforces the no-unexpected-scripts "
            "invariant (use on freshly forked agents / template CI)"
        ),
    )
    parser.add_argument(
        "--contrib-dir",
        default=None,
        help="Path to gptme-contrib (default: <agent-dir>/gptme-contrib; used in default mode only)",
    )
    parser.add_argument(
        "--check-dirs",
        nargs="+",
        default=["scripts", "packages"],
        help="Directories to scan (default: scripts packages)",
    )
    parser.add_argument(
        "--allowlist-file",
        default=None,
        help=(
            "Path to a forked-mode allowlist (one script name per line, # comments "
            "allowed). Replaces the built-in defaults. Used in forked mode only."
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show all files checked, not just issues",
    )
    args = parser.parse_args(argv)

    agent_dir = Path(args.agent_dir).resolve()
    if not agent_dir.is_dir():
        print(f"Error: {agent_dir} is not a directory", file=sys.stderr)
        return 2

    for d in args.check_dirs:
        p = Path(d)
        if p.is_absolute():
            print(
                f"Error: --check-dirs must be relative paths, got: {d}", file=sys.stderr
            )
            return 2
        try:
            (agent_dir / p).resolve().relative_to(agent_dir)
        except ValueError:
            print(f"Error: --check-dirs path escapes workspace: {d}", file=sys.stderr)
            return 2

    print(f"Checking agent: {agent_dir}")
    print(f"Mode:           {args.mode}")
    print(f"Scanning:       {', '.join(args.check_dirs)}")
    print()

    if args.mode == "forked":
        if args.allowlist_file:
            allowlist_path = Path(args.allowlist_file)
            if not allowlist_path.is_file():
                print(
                    f"Error: allowlist file not found: {allowlist_path}",
                    file=sys.stderr,
                )
                return 2
            allowlist = load_allowlist(allowlist_path)
        else:
            allowlist = set(DEFAULT_AGENT_SPECIFIC_SCRIPTS)
        return check_mode_forked(agent_dir, args.check_dirs, args.verbose, allowlist)

    # default mode
    contrib_dir = (
        Path(args.contrib_dir).resolve()
        if args.contrib_dir
        else agent_dir / "gptme-contrib"
    )
    print(f"Contrib dir:    {contrib_dir}")
    print()
    return check_mode_default(agent_dir, contrib_dir, args.check_dirs, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
