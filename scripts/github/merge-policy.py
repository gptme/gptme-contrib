#!/usr/bin/env python3
"""Tier PRs against gptme_contrib_lib/merge_policy.toml — the reader for Erik's merge scanner.

The self-merge gate reads the same file through the same evaluator
(gptme_contrib_lib.merge_policy) and the same path predicates, so the two
readers cannot disagree about a PR's tier.

Usage:
    merge-policy.py tier OWNER/REPO#N [OWNER/REPO#N ...] [--json]
    merge-policy.py tier --stdin [--json]

``--stdin`` takes a JSON list of
``{"repo": "...", "number": N, "files": [{"path", "additions", "deletions", "patch"}]}``
so a scanner that already fetched files in its own batched query needs no
extra API call. ``patch`` matters only for .github/workflows files; without
it they classify as ci-other (fail closed).

Exit codes: 0 ok, 2 usage/fetch error.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
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
    policy = merge_policy.load_policy()
    rollout = merge_policy.rollout_repos(policy)
    classifier = gate.path_classifier()

    jobs: list[tuple[str, int, list[merge_policy.FileChange] | None]] = []
    if args.stdin:
        for item in json.load(sys.stdin):
            jobs.append(
                (item["repo"], int(item["number"]), _changes(item.get("files") or []))
            )
    for ref in args.refs:
        m = _REF_RE.match(ref)
        if not m:
            print(f"bad ref {ref!r}; expected OWNER/REPO#N", file=sys.stderr)
            return 2
        repo, number = m.group(1), int(m.group(2))
        jobs.append((repo, number, gate.fetch_pr_file_changes(repo, number)))
    if not jobs:
        ap.error("give PR refs or --stdin")

    out: list[dict[str, Any]] = []
    failed = False
    for repo, number, changes in jobs:
        row: dict[str, Any] = {"repo": repo, "number": number}
        if changes is None:
            row["error"] = "could not fetch PR files"
            failed = True
        else:
            row.update(merge_policy.assign_tier(changes, policy, classifier).as_dict())
            row["rollout"] = repo in rollout
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
