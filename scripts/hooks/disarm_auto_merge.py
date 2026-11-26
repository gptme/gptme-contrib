#!/usr/bin/env python3
"""Pre-push: disable auto-merge on the open PR of a pushed gptme/gptme branch.

GitHub keeps auto-merge armed across a push by someone with write access, so the
new head could merge through the merge queue unreviewed. The global ``pre-push``
hook passes its arguments and ref lines here, so a plain ``git push`` disarms
too. GitHub accepts the disable on a PR that is not armed.

Exit codes:
  0  not a repo that gets armed, no open PR for the branch, or disarmed
  1  refuse the push: the PR could not be looked up or disarmed
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator

# Repos whose PRs get auto-merge armed. Pushes to their forks are checked too.
_QUEUE_ARM_REPOS = ("gptme/gptme",)
_PROTECTED = {"master", "main"}

_GITHUB_REMOTE = re.compile(
    r"(?:https://(?:[^/@]+@)?github\.com/|(?:ssh://)?git@github\.com[:/])"
    r"(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?",
    re.IGNORECASE,
)


def pushed_branches(ref_lines: Iterable[str]) -> Iterator[str]:
    """Branches receiving commits: no deletions, tags or default branches."""
    for line in ref_lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        _, local_sha, remote_ref, _ = parts
        if set(local_sha) == {"0"} or not remote_ref.startswith("refs/heads/"):
            continue
        branch = remote_ref[len("refs/heads/") :]
        if branch not in _PROTECTED:
            yield branch


def _gh(*args: str) -> tuple[bool, str]:
    # A cached "no open PR" answer would let an armed PR's push through.
    env = {**os.environ, "GH_API_CACHE_TTL": "0"}
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=30, env=env
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()[:300]
    return True, proc.stdout


def _refuse(what: str, detail: str) -> int:
    print(f"ERROR: push refused, could not {what}: {detail}", file=sys.stderr)
    print(
        "An armed auto-merge request would merge the new head unreviewed. "
        "Retry once gh works.",
        file=sys.stderr,
    )
    return 1


def main(remote_url: str, ref_lines: Iterable[str]) -> int:
    match = _GITHUB_REMOTE.fullmatch(remote_url)
    if not match:
        return 0
    owner, name = match["owner"], match["name"].lower()
    branches = list(pushed_branches(ref_lines))
    for base in _QUEUE_ARM_REPOS:
        if name != base.split("/")[1]:
            continue
        for branch in branches:
            ok, out = _gh(
                "api",
                "-X",
                "GET",
                f"repos/{base}/pulls",
                "-f",
                f"head={owner}:{branch}",
                "-f",
                "state=open",
                "--jq",
                ".[].number",
            )
            if not ok:
                return _refuse(f"look up the {base} PR for {branch}", out)
            for number in out.split():
                disable = ("pr", "merge", number, "--repo", base, "--disable-auto")
                ok, detail = _gh(*disable)
                if not ok:
                    ok, detail = _gh(*disable)
                if not ok:
                    return _refuse(f"disable auto-merge on {base}#{number}", detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[2] if len(sys.argv) > 2 else "", sys.stdin))
