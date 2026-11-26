"""Risk-tiered merge policy shared by the self-merge gate and Erik's scanner.

The policy data lives next to this module in ``merge_policy.toml``; this
module only evaluates it. Path predicates (sensitive, lockfile, test, doc,
spec-like doc) are passed in as a classifier so both readers use the exact
predicates of ``scripts/github/self-merge-check.py`` without a second copy.

Stdlib only (tomli on 3.10): the gate imports this from a plain script.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

_GITLINK_LINE = re.compile(r"[+-]Subproject commit [0-9a-f]{40}\s*$")
_TITLE_TYPE_RE = re.compile(r"^([A-Za-z]+)(?:\([^)]*\))?(!)?:\s+\S")

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
    """Load deployed policy, never authorization from a candidate PR checkout.

    The default requires the evaluator and data to match the checkout's trusted
    origin/master revision. A missing ref/file or local edit fails closed. The
    gate itself must likewise be run from the trusted deployment, not PR code.
    An explicit path is for offline calibration/tests, not merge authorization.
    """
    if path is None:
        root = Path(__file__).resolve().parents[4]
        for source in (Path(__file__).resolve(), POLICY_PATH):
            try:
                trusted = subprocess.run(
                    [
                        "git",
                        "show",
                        f"refs/remotes/origin/master:{source.relative_to(root)}",
                    ],
                    cwd=root,
                    capture_output=True,
                    check=True,
                    timeout=10,
                ).stdout
            except (subprocess.SubprocessError, OSError) as exc:
                raise ValueError("trusted merge policy unavailable") from exc
            if source.read_bytes() != trusted:
                raise ValueError("merge policy differs from trusted origin/master")
        # Parse the trusted bytes, not a second potentially changed local read.
        text = trusted.decode("utf-8")
    else:
        text = path.read_text()
    policy = tomllib.loads(text)
    tiers = policy.get("tier") or []
    if not tiers or "*" not in tiers[-1].get("classes", []):
        raise ValueError("merge policy must end with a catch-all tier")
    return policy


def rollout_repos(policy: dict[str, Any]) -> frozenset[str]:
    return frozenset((policy.get("rollout") or {}).get("repos") or [])


def is_gitlink_patch(patch: str | None) -> bool:
    """True when the GitHub files-API patch is only a submodule pointer bump."""
    if not patch:
        return False
    body = [
        line
        for line in patch.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    return bool(body) and all(_GITLINK_LINE.fullmatch(line) for line in body)


def hold_glob_match(path: str, pattern: str) -> bool:
    """Match a repo-relative path. ``*`` does not cross ``/``; ``**`` does.

    ``**/`` as a prefix matches at any directory depth, including the root.
    A trailing ``/**`` matches the directory and every descendant.
    """
    path = path.replace("\\", "/").removeprefix("./")
    pattern = pattern.replace("\\", "/")
    if not path or not pattern:
        return False
    out: list[str] = ["^"]
    i = 0
    n = len(pattern)
    while i < n:
        if pattern.startswith("**", i):
            i += 2
            if i < n and pattern[i] == "/":
                i += 1
                out.append("(?:.*/)?")
            else:
                out.append(".*")
            continue
        ch = pattern[i]
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    out.append("$")
    return re.match("".join(out), path) is not None


def conventional_title_type(title: str | None) -> str | None:
    """Return the conventional-commit type, or None if missing or breaking (``!``)."""
    if not title:
        return None
    match = _TITLE_TYPE_RE.match(title.strip())
    if match is None or match.group(2):
        return None
    return match.group(1).lower()


def tier_self_merges(policy: dict[str, Any], tier_id: int) -> bool:
    """Whether a matched tier waives path/category policy in a rollout repo.

    Explicit ``self_merge`` wins. Absent that key, only tier 0 waives (the
    original 2026-11-15 grant).
    """
    for tier in policy.get("tier") or []:
        if tier.get("id") == tier_id:
            if "self_merge" in tier:
                return bool(tier["self_merge"])
            return tier_id == 0
    return False


def file_class(
    change: FileChange, policy: dict[str, Any], classifier: PathClassifier
) -> str:
    path = change.path
    parts = path.replace("\\", "/").removeprefix("./").split("/")
    if ".github" in (part.lower() for part in parts[:-1]):
        # Diff fragments cannot prove YAML parent keys, aliases, permissions or
        # event safety. Until full base/head YAML is verified, no workflow waiver.
        return "ci-other"
    if is_gitlink_patch(change.patch):
        return "gitlink"
    if classifier.is_sensitive_path(path):
        return "sensitive"
    if classifier.is_lockfile(path):
        return "lockfile"
    if path.lower().endswith(".txt"):
        # The suffix proves nothing: requirements*.txt, CMakeLists.txt and
        # store release notes are dependency, build and release inputs. No
        # tier admits this class by name, so it falls to the catch-all.
        return "plain-text"
    if classifier.is_test_file(path):
        return "tests"
    if classifier.is_doc_file(path):
        return "spec-doc" if classifier.is_spec_like_doc(path) else "docs"
    return "code"


def assign_tier(
    changes: list[FileChange],
    policy: dict[str, Any],
    classifier: PathClassifier,
    *,
    repo: str | None = None,
    title: str | None = None,
) -> TierVerdict:
    classes = {c.path: file_class(c, policy, classifier) for c in changes}
    global_exempt = set(policy.get("size_exempt_classes") or [])
    present = set(classes.values())
    title_type = conventional_title_type(title)
    misses: dict[int, str] = {}
    for tier in policy["tier"]:
        allowed = set(tier["classes"])
        exempt = (
            set(tier.get("size_exempt_classes") or [])
            if "size_exempt_classes" in tier
            else global_exempt
        )
        counted = [c for c in changes if classes[c.path] not in exempt]
        lines = sum(c.additions + c.deletions for c in counted)
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
            elif (scoped := tier.get("repos")) is not None and (
                repo is None or repo.lower() not in {r.lower() for r in scoped}
            ):
                miss = "repo not in tier"
            elif (types := tier.get("title_types")) is not None and (
                title_type is None or title_type not in {t.lower() for t in types}
            ):
                miss = f"title type {title_type or 'missing'} not in tier"
            else:
                held = [
                    change.path
                    for change in changes
                    for glob in (tier.get("hold_globs") or [])
                    if hold_glob_match(change.path, glob)
                ]
                if held:
                    miss = f"hold glob: {held[0]}"
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
