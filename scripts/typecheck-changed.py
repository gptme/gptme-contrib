#!/usr/bin/env python3
"""Fast typecheck for pre-commit: only run mypy on staged Python change surfaces.

Scopes `make -C packages/<pkg> typecheck` to packages with staged changes
instead of running all 19 packages on every Python commit.

Flags:
  --all    Typecheck all packages (used by CI; skips staged-file filtering).

Env:
  HEAVY_VERIFICATION_WRAPPER    If set, prepend this script path to each
                                 make invocation so callers can gate concurrency
                                 (e.g. scripts/run-heavy-verification.sh).

Trade-off: cross-package type errors are not caught incrementally.
Mitigation: keep the full `make typecheck-packages` in CI (prek --all-files or
explicit `make typecheck-packages`).
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# Packages that have a `typecheck` make target (from `make list-packages`).
# Symlinked packages (aw-watcher-agent, credential-slots) are excluded —
# they are typechecked in their source repo.
TYPECHECKED_PACKAGES = {
    "bobutils",
    "gptmail",
    "gptme-activity-summary",
    "gptme-backoff",
    "gptme-block-registry",
    "gptme-browser-semantic",
    "gptme-contrib-lib",
    "gptme-coordination",
    "gptme-daily-briefing",
    "gptme-dashboard",
    "gptme-lessons-extras",
    "gptme-lessons-mcp",
    "gptme-rag",
    "gptme-runloops",
    "gptme-sessions",
    "gptme-vision-node",
    "gptme-voice",
    "gptme-wisdom-mcp",
    "gptodo",
}

# Changes to these files trigger a full-loop typecheck.
GLOBAL_CONFIG_FILES = {"mypy.ini", "pyproject.toml", "uv.lock"}
# Package-level config files whose change affects type results even without
# any staged .py file (dependency additions, mypy settings).
PACKAGE_CONFIG_FILES = {"pyproject.toml", "mypy.ini", "setup.cfg"}


def get_staged_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=d"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return result.stdout.strip().splitlines()


def run_make_typecheck(pkg_dir: str) -> int:
    """Run `make -C packages/<pkg> typecheck`, optionally via a wrapper."""
    wrapper = os.environ.get("HEAVY_VERIFICATION_WRAPPER", "")
    cmd: list[str]
    if wrapper:
        cmd = [wrapper, "make", "-C", f"packages/{pkg_dir}", "typecheck"]
    else:
        cmd = ["make", "-C", f"packages/{pkg_dir}", "typecheck"]
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    return result.returncode


def typecheck_packages(packages: list[str]) -> int:
    """Typecheck given packages; return worst exit code."""
    worst = 0
    for pkg in sorted(packages):
        rc = run_make_typecheck(pkg)
        if rc != 0:
            worst = rc
    return worst


def main() -> int:
    run_all = "--all" in sys.argv

    if run_all:
        print(
            f"[typecheck-changed] --all flag: typechecking all {len(TYPECHECKED_PACKAGES)} packages"
        )
        return typecheck_packages(list(TYPECHECKED_PACKAGES))

    staged = get_staged_files()
    if not staged:
        print("[typecheck-changed] No staged files — skipping typecheck")
        return 0

    # Global config changes require a full typecheck.
    if any(f in GLOBAL_CONFIG_FILES for f in staged):
        changed = [f for f in staged if f in GLOBAL_CONFIG_FILES]
        print(
            f"[typecheck-changed] Global config changed ({', '.join(changed)}) — full typecheck"
        )
        return typecheck_packages(list(TYPECHECKED_PACKAGES))

    # Determine which packages have staged Python changes.
    affected: set[str] = set()
    for f in staged:
        parts = f.split("/")
        # A package-level config change (dependency addition, mypy settings)
        # can affect type results without any .py file staged.
        if not (f.endswith(".py") or parts[-1] in PACKAGE_CONFIG_FILES):
            continue
        if parts[0] == "packages" and len(parts) >= 3:
            pkg_dir = parts[1]
            pkg_path = REPO_ROOT / "packages" / pkg_dir
            if not pkg_path.is_symlink() and pkg_dir in TYPECHECKED_PACKAGES:
                affected.add(pkg_dir)

    if not affected:
        print("[typecheck-changed] No typechecked packages changed — skipping")
        return 0

    print(f"[typecheck-changed] Checking: {', '.join(sorted(affected))}")
    return typecheck_packages(list(affected))


if __name__ == "__main__":
    sys.exit(main())
