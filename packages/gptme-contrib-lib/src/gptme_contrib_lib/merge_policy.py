"""Risk-tiered merge policy shared by the self-merge gate and Erik's scanner.

The policy data lives next to this module in ``merge_policy.toml``; this
module only evaluates it. Path predicates (sensitive, lockfile, test, doc,
spec-like doc) are passed in as a classifier so both readers use the exact
predicates of ``scripts/github/self-merge-check.py`` without a second copy.

Stdlib only (tomli on 3.10): the gate imports this from a plain script.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef,import-not-found]

POLICY_PATH = Path(__file__).with_suffix(".toml")


class PathClassifier(Protocol):
    def is_sensitive_path(self, path: str) -> bool: ...
    def is_lockfile(self, path: str) -> bool: ...
    def is_test_file(self, path: str) -> bool: ...
    def is_doc_file(self, path: str) -> bool: ...
    def is_spec_like_doc(self, path: str) -> bool: ...


@dataclass
class FileChange:
    path: str
    additions: int = 0
    deletions: int = 0
    # Unified-diff text from the GitHub files API; None when GitHub omits it
    # (binary or very large diffs). Only ci files need it.
    patch: str | None = None


@dataclass
class TierVerdict:
    tier: int
    name: str
    action: str
    classes: dict[str, str] = field(default_factory=dict)
    counted_lines: int = 0
    counted_files: int = 0
    # Why each cheaper tier did not apply, keyed by tier id.
    misses: dict[int, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "name": self.name,
            "action": self.action,
            "classes": self.classes,
            "counted_lines": self.counted_lines,
            "counted_files": self.counted_files,
            "misses": {str(k): v for k, v in self.misses.items()},
        }


def load_policy(path: Path | None = None) -> dict[str, Any]:
    policy = tomllib.loads((path or POLICY_PATH).read_text())
    tiers = policy.get("tier") or []
    if not tiers or "*" not in tiers[-1].get("classes", []):
        raise ValueError("merge policy must end with a catch-all tier")
    return policy


def rollout_repos(policy: dict[str, Any]) -> frozenset[str]:
    return frozenset((policy.get("rollout") or {}).get("repos") or [])


def _diff_lines(patch: str) -> list[str]:
    return [
        line[1:]
        for line in patch.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


def file_class(
    change: FileChange, policy: dict[str, Any], classifier: PathClassifier
) -> str:
    path = change.path
    parts = path.replace("\\", "/").removeprefix("./").split("/")
    if ".github" in (part.lower() for part in parts[:-1]):
        is_workflow = [p.lower() for p in parts[:2]] == [
            ".github",
            "workflows",
        ] and len(parts) == 3
        allowed = re.compile(policy["ci_allowed_name"], re.IGNORECASE)
        if not is_workflow or not allowed.search(parts[-1]):
            return "ci-other"
        if change.patch is None:
            return "ci-other"
        tokens = policy.get("ci_forbidden_tokens") or []
        if any(tok in line for line in _diff_lines(change.patch) for tok in tokens):
            return "ci-other"
        return "ci"
    if classifier.is_sensitive_path(path):
        return "sensitive"
    if classifier.is_lockfile(path):
        return "lockfile"
    if classifier.is_test_file(path):
        return "tests"
    if classifier.is_doc_file(path):
        return "spec-doc" if classifier.is_spec_like_doc(path) else "docs"
    return "code"


def assign_tier(
    changes: list[FileChange], policy: dict[str, Any], classifier: PathClassifier
) -> TierVerdict:
    classes = {c.path: file_class(c, policy, classifier) for c in changes}
    exempt = set(policy.get("size_exempt_classes") or [])
    counted = [c for c in changes if classes[c.path] not in exempt]
    lines = sum(c.additions + c.deletions for c in counted)
    present = set(classes.values())
    misses: dict[int, str] = {}
    for tier in policy["tier"]:
        allowed = set(tier["classes"])
        miss = None
        if "*" not in allowed:
            if not changes:
                miss = "no changed files"
            elif not present <= allowed:
                miss = f"classes outside tier: {', '.join(sorted(present - allowed))}"
            elif lines > tier.get("max_lines", 10**9):
                miss = f"{lines} counted lines > {tier['max_lines']}"
            elif len(counted) > tier.get("max_files", 10**9):
                miss = f"{len(counted)} counted files > {tier['max_files']}"
        if miss:
            misses[tier["id"]] = miss
            continue
        return TierVerdict(
            tier=tier["id"],
            name=tier.get("name", ""),
            action=tier.get("action", ""),
            classes=classes,
            counted_lines=lines,
            counted_files=len(counted),
            misses=misses,
        )
    raise ValueError("merge policy has no catch-all tier")
