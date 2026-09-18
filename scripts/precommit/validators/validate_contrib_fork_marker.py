#!/usr/bin/env python3
"""Require an AGENT-LOCAL marker on scripts that share a path with gptme-contrib.

Agent-neutral generalization of Bob's ``validate-contrib-fork-marker``
(ErikBjare/bob, added 2026-08-06). When a file in ``scripts/`` is staged and
``gptme-contrib/scripts/<same-path>`` exists, the fork must carry a marker in its
first 30 lines. This prevents the silent-no-op class: a session fixes a
``scripts/`` file and marks the task done, but the live hook sources contrib's
copy — which still has the bug. No marker existed to warn the editing session
that a same-path contrib original exists and may be what actually runs.

The marker is the governance primitive for the shared-core convergence arc
(alice#79): it makes an intentional local fork *declared*, so drift is
distinguishable from an accidental verbatim copy that should have been a symlink.

Marker token: ``AGENT-LOCAL`` by default. Override per-agent with the
``CONTRIB_FORK_MARKER`` env var (e.g. ``BOB-LOCAL`` for Bob's existing markers).
The universal ``AGENT-LOCAL`` token is *always* accepted in addition to any
override, so a file marked for one agent stays valid if the tree is reused or
forked by another.

Adoption (in a forked agent's ``.pre-commit-config.yaml``)::

    - id: validate-contrib-fork-marker
      name: Require AGENT-LOCAL marker on scripts forked from gptme-contrib
      entry: python3 gptme-contrib/scripts/precommit/validators/validate_contrib_fork_marker.py
      language: system
      files: ^scripts/
      exclude: ^gptme-contrib/
      pass_filenames: true

Usage::

    python3 validate_contrib_fork_marker.py scripts/git/guard-mass-delete.sh
    CONTRIB_FORK_MARKER=BOB-LOCAL python3 validate_contrib_fork_marker.py scripts/foo.sh
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

UNIVERSAL_MARKER = "agent-local"  # always accepted, matched case-insensitively
CHECK_LINES = 30


def markers() -> set[str]:
    """The set of accepted marker tokens (lowercased).

    Always includes the universal ``AGENT-LOCAL``; adds the per-agent override
    from ``CONTRIB_FORK_MARKER`` when set (e.g. ``BOB-LOCAL``).
    """
    accepted = {UNIVERSAL_MARKER}
    override = os.environ.get("CONTRIB_FORK_MARKER", "").strip().lower()
    if override:
        accepted.add(override)
    return accepted


def has_marker(path: Path, accepted: set[str]) -> bool:
    try:
        lines = path.read_text(errors="replace").splitlines()[:CHECK_LINES]
    except OSError:
        return True  # can't read → not our problem
    lowered = [line.lower() for line in lines]
    return any(marker in line for marker in accepted for line in lowered)


def get_repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        return 0

    repo_root = get_repo_root()
    contrib_scripts = repo_root / "gptme-contrib" / "scripts"

    if not contrib_scripts.exists():
        return 0  # contrib not present as a submodule (e.g. contrib's own CI)

    accepted = markers()
    scripts_root = (repo_root / "scripts").resolve()
    failed: list[str] = []

    for arg in args:
        path = Path(arg)
        if not path.exists():
            continue

        try:
            rel = path.resolve().relative_to(scripts_root)
        except ValueError:
            continue  # not under scripts/ — the pre-commit files filter guards this

        if not (contrib_scripts / rel).exists():
            continue  # no contrib counterpart, nothing to enforce

        if not has_marker(path, accepted):
            wanted = " or ".join(sorted(m.upper() for m in accepted))
            failed.append(
                f"  {path}\n"
                f"    → gptme-contrib/scripts/{rel} also exists but {path}\n"
                f"      lacks a {wanted} marker in its first {CHECK_LINES} lines.\n"
                f"      Add an AGENT-LOCAL FORK header documenting why the fork\n"
                f"      exists and what local extensions it carries — otherwise a\n"
                f"      contrib fix to the same path may silently not reach you."
            )

    if failed:
        print("validate-contrib-fork-marker: missing fork marker on forked scripts:")
        for msg in failed:
            print(msg)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
