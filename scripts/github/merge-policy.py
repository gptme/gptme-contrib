#!/usr/bin/env python3
"""Tier PRs against gptme_contrib_lib/merge_policy.toml — the reader for Erik's merge scanner.

The self-merge gate reads the same file through the same evaluator
(gptme_contrib_lib.merge_policy) and the same path predicates, so the two
readers cannot disagree about a PR's tier.

Usage:
    merge-policy.py tier OWNER/REPO#N [OWNER/REPO#N ...] [--json]
    merge-policy.py tier --stdin [--json]

``--stdin`` takes a JSON list of
``{"repo": "...", "number": N, "title": "...", "changedFiles": TOTAL, "files": [{"path", "additions", "deletions", "patch"}]}``
so a scanner that already fetched files in its own batched query needs no
extra API call. ``title`` is optional in general, but a rollout repo without
one gets its title fetched here, because a tier with ``title_types`` cannot be
evaluated without it and the gate would otherwise disagree. ``changedFiles``
must be the PR's total changed-file count, not the current page length.
Missing/mismatched counts and duplicate paths produce an error, never a tier.
Workflows remain human-review-only.

Exit codes: 0 ok, 2 usage/fetch error.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1] / "packages" / "gptme-contrib-lib" / "src"))
from gptme_contrib_lib import merge_policy  # type: ignore[import-not-found]  # noqa: E402,I001

_REF_RE = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")


def _gate_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_self_merge_check", _HERE / "self-merge-check.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)  # dataclasses resolve via sys.modules
    spec.loader.exec_module(mod)
    return mod


def _pr_title(repo: str, number: int) -> str | None:
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "title",
                "--jq",
                ".title",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _changes(files: list[dict[str, Any]]) -> list[merge_policy.FileChange]:
    return [
        merge_policy.FileChange(
            path=str(f["path"]),
            additions=int(f.get("additions") or 0),
            deletions=int(f.get("deletions") or 0),
            patch=f.get("patch") if isinstance(f.get("patch"), str) else None,
        )
        for f in files
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    tier = sub.add_parser("tier", help="tier PRs")
    tier.add_argument("refs", nargs="*", help="OWNER/REPO#N")
    tier.add_argument("--stdin", action="store_true", help="read PR files as JSON")
    tier.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    gate = _gate_module()
    try:
        policy = merge_policy.load_policy()
    except (OSError, ValueError) as exc:
        print(f"cannot load trusted merge policy: {exc}", file=sys.stderr)
        return 2
    rollout = merge_policy.rollout_repos(policy)
    classifier = gate.path_classifier()

    rollout_lower = {r.lower() for r in rollout}
    jobs: list[
        tuple[str, int, list[merge_policy.FileChange] | None, str | None, bool]
    ] = []
    if args.stdin:
        for item in json.load(sys.stdin):
            supplied = _changes(item.get("files") or [])
            total = item.get("changedFiles")
            complete = (
                type(total) is int
                and total == len(supplied)
                and len({c.path for c in supplied if c.path}) == total
            )
            repo = item["repo"]
            title = item.get("title")
            title_failed = False
            if (
                complete
                and not isinstance(title, str)
                and repo.lower() in rollout_lower
            ):
                # A tier with title_types cannot be evaluated without the title,
                # and reporting tier 2 here would disagree with the gate. A failed
                # fetch is an error, never a silent tier-2 verdict.
                title = _pr_title(repo, int(item["number"]))
                title_failed = not isinstance(title, str)
            jobs.append(
                (
                    repo,
                    int(item["number"]),
                    supplied if complete else None,
                    title if isinstance(title, str) else None,
                    title_failed,
                )
            )
    for ref in args.refs:
        m = _REF_RE.match(ref)
        if not m:
            print(f"bad ref {ref!r}; expected OWNER/REPO#N", file=sys.stderr)
            return 2
        repo, number = m.group(1), int(m.group(2))
        title = _pr_title(repo, number)
        jobs.append(
            (
                repo,
                number,
                gate.fetch_pr_file_changes(repo, number),
                title,
                repo.lower() in rollout_lower and not isinstance(title, str),
            )
        )
    if not jobs:
        ap.error("give PR refs or --stdin")

    out: list[dict[str, Any]] = []
    failed = False
    for repo, number, changes, title, title_failed in jobs:
        row: dict[str, Any] = {"repo": repo, "number": number}
        if changes is None:
            row["error"] = "could not establish complete PR file inventory"
            failed = True
        elif title_failed:
            row["error"] = "could not fetch PR title for a title-scoped tier"
            failed = True
        else:
            row.update(
                merge_policy.assign_tier(
                    changes, policy, classifier, repo=repo, title=title
                ).as_dict()
            )
            row["rollout"] = repo.lower() in rollout_lower
        out.append(row)

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        for row in out:
            ref = f"{row['repo']}#{row['number']}"
            if "error" in row:
                print(f"{ref}: ERROR {row['error']}")
                continue
            misses = "; ".join(f"t{k}: {v}" for k, v in row["misses"].items())
            roll = "" if row["rollout"] else " (repo not in rollout)"
            print(
                f"{ref}: tier {row['tier']} {row['name']}{roll}"
                + (f" [{misses}]" if misses else "")
            )
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
