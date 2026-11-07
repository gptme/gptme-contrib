#!/usr/bin/env python3
"""Trusted path-scoped exception to the "no direct master/main" git hooks.

The global pre-commit and pre-push hooks refuse direct commits and pushes to the
default branch of every repository that is not allow-listed. A repository owner
may publish *prose* (blog posts, a profile README) straight to the default
branch, but never code, workflows or anything runnable. This checker decides
whether a specific change qualifies. It is deliberately conservative: anything it
cannot positively classify is denied, and the hooks treat every exit status other
than 0 as a denial.

Trust boundary: the hooks resolve this script and its config from their own
install root, never from the repository being committed or pushed. With no
config, or an empty ``repositories`` list, nothing is approved.

Config (``direct-publish.json``, next to ``allowed-repos.conf``)::

    {"repositories": [
        {"repo": "github.com/owner/name", "branch": "master",
         "allow": ["_posts/*.md", "README.md"]}
    ]}

``repo`` is the canonical lowercase ``github.com/owner/name`` identity. ``allow``
globs match whole repository-relative paths; ``*`` never crosses ``/`` and there
is no ``**``.

Usage (run inside the repository, git supplies the context)::

    direct-publish-policy.py commit --config FILE --remote-url URL --branch NAME
    direct-publish-policy.py push   --config FILE --remote-url URL   # tuples on stdin

Exit status: 0 approved, 1 denied (reason on stderr), 2 error (treat as denied).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import PurePosixPath
from urllib.parse import urlsplit

ZERO_RE = re.compile(r"^0+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
# A repository name/owner as GitHub allows them; no dots-only or separators.
SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
PROTECTED_BRANCHES = ("master", "main")
MAX_RANGE_COMMITS = 100
# Plain files only: no symlink (120000), submodule (160000) or executable (100755).
SAFE_MODES = {"000000", "100644"}
SAFE_STATUSES = {"A", "M", "D"}


class Denied(Exception):
    """The change does not qualify."""


def canonical_github_identity(url: str) -> str:
    """Return ``github.com/owner/name`` (lowercase) or raise Denied."""
    if not url or any(ord(c) < 0x21 or ord(c) == 0x7F for c in url):
        raise Denied("remote URL is empty or contains control/space characters")
    host: str
    path: str
    if "://" in url:
        parts = urlsplit(url)
        if parts.scheme not in ("https", "ssh"):
            raise Denied(f"unsupported remote scheme {parts.scheme!r}")
        if parts.port is not None or parts.password is not None:
            raise Denied("remote URL has an explicit port or password")
        if parts.query or parts.fragment:
            raise Denied("remote URL has a query or fragment")
        if parts.scheme == "https" and parts.username is not None:
            raise Denied("https remote URL carries userinfo")
        if parts.scheme == "ssh" and parts.username not in (None, "git"):
            raise Denied("ssh remote URL user is not git")
        host = parts.hostname or ""
        path = parts.path
    else:
        m = re.fullmatch(r"git@([^:/]+):(.+)", url)
        if not m:
            raise Denied("unrecognised remote URL shape")
        host, path = m.group(1), "/" + m.group(2)
    if host.lower() != "github.com":
        raise Denied(f"remote host {host!r} is not github.com")
    path = path.removesuffix("/")
    path = path.removesuffix(".git")
    segments = path.lstrip("/").split("/")
    if len(segments) != 2 or not path.startswith("/"):
        raise Denied("remote path is not exactly owner/name")
    if not all(SEGMENT_RE.fullmatch(s) and s not in (".", "..") for s in segments):
        raise Denied("remote owner/name has unexpected characters")
    return "github.com/" + "/".join(segments).lower()


def load_entry(config_path: str, identity: str) -> dict:
    try:
        with open(config_path, encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"direct-publish-policy: unreadable config: {exc}") from exc
    repos = config.get("repositories") if isinstance(config, dict) else None
    if not isinstance(repos, list):
        raise SystemExit("direct-publish-policy: config has no repositories list")
    selected = None
    for entry in repos:
        if not isinstance(entry, dict):
            raise SystemExit("direct-publish-policy: malformed repository entry")
        repo, branch, allow = entry.get("repo"), entry.get("branch"), entry.get("allow")
        if (
            not isinstance(repo, str)
            or repo != repo.lower()
            or branch not in PROTECTED_BRANCHES
            or not isinstance(allow, list)
            or not allow
            or not all(isinstance(g, str) and g and "**" not in g for g in allow)
        ):
            raise SystemExit("direct-publish-policy: malformed repository entry")
        if repo == identity and selected is None:
            selected = entry
    if selected is not None:
        return selected
    raise Denied(f"{identity} is not configured for direct publication")


def glob_regex(glob: str) -> re.Pattern[str]:
    out = []
    for ch in glob:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return re.compile("".join(out))


def path_allowed(path: str, patterns: list[re.Pattern[str]]) -> bool:
    if not path or any(ord(c) < 0x20 or ord(c) == 0x7F or c == "\\" for c in path):
        return False
    posix = PurePosixPath(path)
    if posix.is_absolute() or str(posix) != path:
        return False
    # No traversal, and never dotfiles/dot-directories (.github, .git*, ...).
    if any(part in ("", ".", "..") or part.startswith(".") for part in posix.parts):
        return False
    return any(p.fullmatch(path) for p in patterns)


def git(*args: str, check: bool = True) -> bytes:
    proc = subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-c",
            "diff.external=",
            "-c",
            "core.quotepath=off",
            *args,
        ],
        capture_output=True,
        check=False,
        # Legacy grafts alter parent traversal even with replacements disabled.
        env=dict(os.environ, GIT_GRAFT_FILE=os.devnull),
    )
    if check and proc.returncode != 0:
        raise Denied(
            f"git {args[0]} failed: {proc.stderr.decode(errors='replace')[:200]}"
        )
    return proc.stdout if proc.returncode == 0 else b"\0FAILED"


def check_raw_diff(raw: bytes, patterns: list[re.Pattern[str]], where: str) -> int:
    """Validate ``--raw -z --no-renames`` output; return the number of entries."""
    tokens = raw.split(b"\0")
    count = 0
    i = 0
    while i < len(tokens):
        meta = tokens[i]
        if not meta:
            i += 1
            continue
        if not meta.startswith(b":") or i + 1 >= len(tokens):
            raise Denied(f"{where}: unparseable diff entry")
        fields = meta[1:].decode("ascii", "replace").split(" ")
        if len(fields) != 5:
            raise Denied(f"{where}: unparseable diff entry")
        old_mode, new_mode, _old, _new, status = fields
        try:
            path = tokens[i + 1].decode("utf-8")
        except UnicodeDecodeError:
            raise Denied(f"{where}: non-UTF-8 path") from None
        i += 2
        if status not in SAFE_STATUSES:
            raise Denied(f"{where}: change type {status!r} for {path!r}")
        if old_mode not in SAFE_MODES or new_mode not in SAFE_MODES:
            raise Denied(f"{where}: symlink/submodule/executable entry {path!r}")
        if not path_allowed(path, patterns):
            raise Denied(f"{where}: {path!r} is outside the direct-publish scope")
        count += 1
    return count


def run_commit(entry: dict, branch: str) -> None:
    if branch != entry["branch"]:
        raise Denied(f"branch {branch!r} is not the configured {entry['branch']!r}")
    patterns = [glob_regex(g) for g in entry["allow"]]
    raw = git(
        "diff", "--cached", "--raw", "-z", "--no-renames", "--no-ext-diff",
        "--ignore-submodules=none",
    )  # fmt: skip
    if check_raw_diff(raw, patterns, "staged changes") == 0:
        raise Denied("staged diff is empty or unknown")


def run_push(entry: dict, stdin: str) -> None:
    patterns = [glob_regex(g) for g in entry["allow"]]
    governed = 0
    for line in stdin.splitlines():
        if not line.strip():
            continue
        fields = line.split(" ")
        if len(fields) != 4:
            raise Denied("malformed push tuple")
        _local_ref, local_sha, remote_ref, remote_sha = fields
        if remote_ref not in tuple(f"refs/heads/{b}" for b in PROTECTED_BRANCHES):
            continue  # other refs are not what the hook protects
        governed += 1
        if remote_ref != f"refs/heads/{entry['branch']}":
            raise Denied(
                f"push target {remote_ref} is not refs/heads/{entry['branch']}"
            )
        if ZERO_RE.fullmatch(local_sha) or ZERO_RE.fullmatch(remote_sha):
            raise Denied("branch creation or deletion is not direct publication")
        if not SHA_RE.fullmatch(local_sha) or not SHA_RE.fullmatch(remote_sha):
            raise Denied("malformed object id in push tuple")
        for sha in (local_sha, remote_sha):
            if git("cat-file", "-e", f"{sha}^{{commit}}", check=False) == b"\0FAILED":
                raise Denied(f"object {sha} is not available locally")
        if (
            git("merge-base", "--is-ancestor", remote_sha, local_sha, check=False)
            == b"\0FAILED"
        ):
            raise Denied("push is not a fast-forward of the remote tip")
        listing = git(
            "rev-list", "--reverse", "--parents", f"{remote_sha}..{local_sha}"
        )
        commits = [ln.split() for ln in listing.decode().splitlines() if ln.strip()]
        if not commits:
            raise Denied("no outgoing commits")
        if len(commits) > MAX_RANGE_COMMITS:
            raise Denied(f"more than {MAX_RANGE_COMMITS} outgoing commits")
        for commit, *parents in commits:
            if len(parents) != 1:
                raise Denied(f"commit {commit[:12]} is a merge or root commit")
            raw = git(
                "diff-tree", "-r", "--raw", "-z", "--no-renames", "--no-commit-id",
                "--no-ext-diff", "--ignore-submodules=none", commit,
            )  # fmt: skip
            if check_raw_diff(raw, patterns, f"commit {commit[:12]}") == 0:
                raise Denied(f"commit {commit[:12]} changes nothing classifiable")
    if governed == 0:
        raise Denied("no default-branch tuple to approve")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("mode", choices=("commit", "push"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--remote-url", required=True)
    parser.add_argument("--branch", default="")
    args = parser.parse_args(argv)
    try:
        identity = canonical_github_identity(args.remote_url)
        entry = load_entry(args.config, identity)
        if args.mode == "commit":
            run_commit(entry, args.branch)
        else:
            run_push(entry, sys.stdin.read())
    except Denied as exc:
        print(f"direct-publish-policy: denied: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            sys.exit(2)
        raise
