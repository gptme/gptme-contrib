#!/usr/bin/env python3
"""Resolve addressed Greptile review threads on a PR (the resolveReviewThread gap).

Our self-merge gate (`gptme-contrib/scripts/github/self-merge-check.py`) blocks on ANY
unresolved Greptile review thread. But nothing ever resolves them — we reply, never
resolve — so the deterministic self-merge path can't fire on nitpick-laden PRs. greptile's
own `greploop` skill resolves threads via the `resolveReviewThread` GraphQL mutation after
addressing each comment; this is that primitive for our flow.

SAFETY: only ever resolves threads authored by Greptile (bot). Never touches human threads.
Intended to be called by an agent session AFTER it has addressed the findings (or with
--outdated-only for the conservative "code changed since the comment" heuristic).

Usage:
  resolve-greptile-threads.py OWNER/REPO#NUM [--dry-run] [--outdated-only] [--json]
  resolve-greptile-threads.py OWNER/REPO NUM  [...]

Exit: 0 ok (prints count resolved), 2 on arg/gh error.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import NoReturn

GREPTILE_LOGINS = ("greptile-apps", "greptile-apps[bot]", "greptile-apps-staging[bot]")


def _run_gh(args: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _die(msg: str) -> NoReturn:
    """Print to stderr and exit 2 (the documented arg/gh-error code)."""
    print(msg, file=sys.stderr)
    raise SystemExit(2)


def _graphql_data(args: list[str]) -> dict:
    """Run a gh graphql query and return its ``data`` object, or die cleanly.

    GitHub's GraphQL API can return ``{"data": null, "errors": [...]}`` with HTTP 200,
    so rc==0 is not sufficient — inspect the body before subscripting ``data``.
    """
    rc, out, err = _run_gh(args)
    if rc != 0:
        _die(f"gh graphql failed: {err or out}")
    try:
        body = json.loads(out)
    except json.JSONDecodeError:
        _die(f"gh graphql returned non-JSON: {out[:200]}")
    if body.get("errors"):
        _die(f"GraphQL errors: {body['errors']}")
    data = body.get("data")
    if not isinstance(data, dict):
        _die("GraphQL returned null/empty data")
    return data


def parse_pr(spec: str, second: str | None) -> tuple[str, str, int]:
    """Accept 'owner/repo#123', 'owner/repo 123', or a PR URL."""
    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", spec)
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    if "#" in spec:
        repo, num = spec.split("#", 1)
        owner, name = repo.split("/", 1)
        try:
            return owner, name, int(num)
        except ValueError:
            _die(f"PR number must be an integer, got {num!r}.")
    if second and "/" in spec:
        owner, name = spec.split("/", 1)
        try:
            return owner, name, int(second)
        except ValueError:
            _die(f"PR number must be an integer, got {second!r}.")
    _die(f"Unrecognized PR specifier: {spec!r}. Use OWNER/REPO#NUM or OWNER/REPO NUM.")


def fetch_greptile_threads(owner: str, name: str, num: int) -> list[dict]:
    """Return Greptile-authored review threads with id/isResolved/isOutdated."""
    query = """
    query($owner:String!,$name:String!,$num:Int!,$cursor:String){
      repository(owner:$owner,name:$name){
        pullRequest(number:$num){
          reviewThreads(first:100,after:$cursor){
            pageInfo{hasNextPage endCursor}
            nodes{ id isResolved isOutdated
              comments(first:1){nodes{author{login} path}} }
          }
        }
      }
    }"""
    threads: list[dict] = []
    cursor = None
    while True:
        args = [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"num={num}",
        ]
        if cursor:
            args += ["-F", f"cursor={cursor}"]
        gdata = _graphql_data(args)
        pr = (gdata.get("repository") or {}).get("pullRequest")
        if not pr:
            _die(f"{owner}/{name}#{num}: repository or PR not found")
        data = pr["reviewThreads"]
        for n in data["nodes"]:
            cnodes = n.get("comments", {}).get("nodes", [])
            login = (cnodes[0].get("author") or {}).get("login", "") if cnodes else ""
            if login in GREPTILE_LOGINS:
                threads.append(
                    {
                        "id": n["id"],
                        "isResolved": n["isResolved"],
                        "isOutdated": n["isOutdated"],
                        "path": (cnodes[0].get("path") if cnodes else "") or "",
                    }
                )
        if not data["pageInfo"]["hasNextPage"]:
            break
        cursor = data["pageInfo"]["endCursor"]
    return threads


def resolve_thread(thread_id: str) -> bool:
    """Resolve one thread. Non-fatal: returns False on any error (already-resolved,
    permission, HTTP-200 GraphQL error) so the caller's loop continues."""
    mutation = "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}"
    rc, out, err = _run_gh(
        ["api", "graphql", "-f", f"query={mutation}", "-F", f"id={thread_id}"]
    )
    if rc != 0:
        print(f"  ! failed to resolve {thread_id}: {err or out}", file=sys.stderr)
        return False
    try:
        body = json.loads(out)
    except json.JSONDecodeError:
        print(f"  ! resolve {thread_id}: non-JSON response", file=sys.stderr)
        return False
    if body.get("errors") or not isinstance(body.get("data"), dict):
        print(
            f"  ! resolve {thread_id}: {body.get('errors') or 'null data'}",
            file=sys.stderr,
        )
        return False
    try:
        return bool(body["data"]["resolveReviewThread"]["thread"]["isResolved"])
    except (KeyError, TypeError) as exc:
        print(
            f"  ! resolve {thread_id}: unexpected response shape: {exc}",
            file=sys.stderr,
        )
        return False


def filter_targets(threads: list[dict], outdated_only: bool) -> list[dict]:
    """Return unresolved threads eligible for resolution."""
    return [
        t
        for t in threads
        if not t["isResolved"] and (t["isOutdated"] or not outdated_only)
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pr")
    ap.add_argument("num", nargs="?")
    ap.add_argument(
        "--dry-run", action="store_true", help="list what would be resolved"
    )
    ap.add_argument(
        "--outdated-only",
        action="store_true",
        help="only resolve threads whose code changed since the comment (isOutdated) — conservative",
    )
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    owner, name, num = parse_pr(a.pr, a.num)
    threads = fetch_greptile_threads(owner, name, num)
    targets = filter_targets(threads, a.outdated_only)

    # When emitting JSON, per-thread progress goes to stderr so stdout stays parseable.
    log = sys.stderr if a.json else sys.stdout
    resolved = []
    for t in targets:
        if a.dry_run:
            print(
                f"  [dry-run] would resolve {t['id']} ({t['path']}, outdated={t['isOutdated']})",
                file=log,
            )
            continue
        if resolve_thread(t["id"]):
            resolved.append(t["id"])
            print(f"  resolved {t['id']} ({t['path']})", file=log)

    summary = {
        "repo": f"{owner}/{name}",
        "pr": num,
        "greptile_threads": len(threads),
        "unresolved": len([t for t in threads if not t["isResolved"]]),
        "targeted": len(targets),
        "resolved": len(resolved),
        "dry_run": a.dry_run,
    }
    if a.json:
        print(json.dumps(summary))
    else:
        print(
            f"{summary['repo']}#{num}: {summary['unresolved']} unresolved greptile thread(s), "
            f"{'would resolve' if a.dry_run else 'resolved'} {summary['targeted'] if a.dry_run else summary['resolved']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
