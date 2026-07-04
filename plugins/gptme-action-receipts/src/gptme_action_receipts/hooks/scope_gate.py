"""Scope-check gate for gptme-action-receipts — Phase 2.

Runs synchronously after the ledger write (Phase 1) and before tool execution.
Returns a violation description string when an out-of-scope action is detected,
or None when the action is permitted.

The gate is intentionally conservative and fail-open: any extraction or config
error falls back to warn-mode with no crash.
"""

from __future__ import annotations

import copy
import fnmatch
import logging
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Timeout for subprocess repo-extraction calls (ms → s).
_EXTRACT_TIMEOUT = 0.2


# --------------------------------------------------------------------------- #
# Sensitive-action patterns                                                    #
# --------------------------------------------------------------------------- #

_REPO_PATTERN = r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+"


def _extract_gh_repo(command: str, workspace: Path | None) -> str | None:
    """Extract owner/repo from a gh command.

    Resolution order:
    1. ``--repo OWNER/REPO`` flag (explicit)
    2. First positional ``OWNER/REPO`` argument (e.g. ``gh repo delete OWNER/REPO``)
    3. Workspace git remote origin fallback
    """
    m = re.search(r"--repo\s+(" + _REPO_PATTERN + r")", command)
    if m:
        return m.group(1)
    # Positional owner/repo: gh repo delete / gh release delete / gh repo view
    m = re.search(r"(?:delete|view|clone|archive)\s+(" + _REPO_PATTERN + r")", command)
    if m:
        return m.group(1)
    if workspace:
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=_EXTRACT_TIMEOUT,
            )
            if result.returncode == 0:
                return _parse_repo_from_url(result.stdout.strip())
        except Exception:
            pass
    return None


def _extract_git_push_repo(command: str, workspace: Path | None) -> str | None:
    """Extract owner/repo for a git push command via remote URL resolution."""
    remote = _extract_git_push_remote(command)
    if remote is None:
        remote = "origin"

    repo = _parse_repo_from_url(remote)
    if repo is not None:
        return repo

    if workspace:
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace), "remote", "get-url", remote],
                capture_output=True,
                text=True,
                timeout=_EXTRACT_TIMEOUT,
            )
            if result.returncode == 0:
                return _parse_repo_from_url(result.stdout.strip())
        except Exception:
            pass
    return None


def _extract_git_push_remote(command: str) -> str | None:
    """Return the git push remote argument, ignoring flags before the remote."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None

    try:
        push_index = next(
            i
            for i in range(len(parts) - 1)
            if parts[i] == "git" and parts[i + 1] == "push"
        )
    except StopIteration:
        return None

    tokens = parts[push_index + 2 :]
    skip_next = False
    options_with_values = {
        "--receive-pack",
        "--exec",
        "--push-option",
        "--repo",
        "-o",
    }
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            continue
        if token.startswith("-"):
            opt = token.split("=", 1)[0]
            if opt in options_with_values and "=" not in token:
                skip_next = True
            continue
        return token

    return None


def _parse_repo_from_url(url: str) -> str | None:
    """Convert git remote URL to 'owner/repo' string."""
    # SSH: git@github.com:owner/repo.git
    m = re.search(r"[:/]([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


# Each entry: (pattern_re, scope_key, extractor_fn)
_SCOPE_CHECKS: list[tuple[str, str, Any]] = [
    (r"gh\s+pr\s+merge\b", "merge_repos", _extract_gh_repo),
    (
        r"git\s+push\b(?=.*(?:--force(?:-[A-Za-z]+)*(?:[=\s]|$)|-[A-Za-z]*f[A-Za-z]*(?:\s|$)))",
        "force_push_repos",
        _extract_git_push_repo,
    ),
    (r"gh\s+repo\s+delete\b", "repo_delete", _extract_gh_repo),
    (r"gh\s+release\s+delete\b", "release_delete", _extract_gh_repo),
]


# --------------------------------------------------------------------------- #
# Config loading                                                               #
# --------------------------------------------------------------------------- #

_DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "violation_action": "warn",
    "scopes": {
        "merge_repos": [],
        "force_push_repos": [],
        "repo_delete": [],
        "release_delete": [],
    },
}


@dataclass(frozen=True)
class ScopeDecision:
    """Scope-check result plus the action read from the same config."""

    violation: str | None
    action: str


def _scope_manifest_path() -> Path | None:
    """Return the scope manifest path to use, or None if scope-checking is disabled."""
    import os

    env = os.environ.get("GPTME_SCOPE_MANIFEST")
    if env:
        return Path(env)
    default = Path.home() / ".config" / "gptme" / "scope.yaml"
    if default.exists():
        return default
    return None


def _load_scope_config() -> dict[str, Any]:
    """Load scope manifest, falling back to conservative defaults on any error."""
    path = _scope_manifest_path()
    if path is None:
        return copy.deepcopy(_DEFAULT_CONFIG)
    try:
        import yaml  # type: ignore[import-untyped]

        text = path.read_text(encoding="utf-8")
        loaded = yaml.safe_load(text) or {}
        cfg = _DEFAULT_CONFIG.copy()
        cfg.update(loaded)
        # Merge nested scopes dict so missing keys use defaults.
        scopes = dict(_DEFAULT_CONFIG["scopes"])
        scopes.update(loaded.get("scopes", {}))
        cfg["scopes"] = scopes
        return cfg
    except Exception as exc:
        logger.warning(
            "action-receipts scope-gate: could not load %s — falling back to defaults (%s)",
            path,
            exc,
        )
        return copy.deepcopy(_DEFAULT_CONFIG)


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #


def check_scope(
    tool_name: str,
    target: str,
    workspace: Path | None,
) -> str | None:
    """Check whether a tool action is within authorized scope.

    Returns a violation description if out-of-scope, None if permitted.
    Never raises — all errors are logged and fall back to 'no violation'.
    """
    try:
        cfg = _load_scope_config()
        return _check_scope_inner(tool_name, target, workspace, cfg)
    except Exception as exc:
        logger.warning("action-receipts scope-gate: unexpected error: %s", exc)
        return None


def check_scope_decision(
    tool_name: str,
    target: str,
    workspace: Path | None,
) -> ScopeDecision:
    """Check scope and return the violation action from the same config read."""
    try:
        cfg = _load_scope_config()
        violation = _check_scope_inner(tool_name, target, workspace, cfg)
        return ScopeDecision(violation=violation, action=violation_action(cfg))
    except Exception as exc:
        logger.warning("action-receipts scope-gate: unexpected error: %s", exc)
        return ScopeDecision(violation=None, action="warn")


def _check_scope_inner(
    tool_name: str,
    target: str,
    workspace: Path | None,
    cfg: dict[str, Any],
) -> str | None:
    """Inner scope check — may raise; callers should wrap in try/except."""
    # Only inspect shell/bash tool calls.
    if tool_name not in ("shell", "bash", "execute"):
        return None

    scopes: dict[str, list[str]] = cfg.get("scopes", {})

    for pattern, scope_key, extractor in _SCOPE_CHECKS:
        if not re.search(pattern, target, re.IGNORECASE):
            continue

        allowed: list[str] = scopes.get(scope_key, [])
        repo = extractor(target, workspace)

        if repo is None:
            logger.debug(
                "action-receipts scope-gate: could not extract repo for %s — skipping check",
                scope_key,
            )
            continue

        if not _is_authorized(repo, allowed):
            return f"action '{scope_key}' on '{repo}' not in allowlist {allowed!r}"

    return None


def _is_authorized(repo: str, allowed: list[str]) -> bool:
    return any(fnmatch.fnmatch(repo, pattern) for pattern in allowed)


def violation_action(cfg: dict[str, Any] | None = None) -> str:
    """Return the configured violation action ('warn' or 'block')."""
    if cfg is None:
        cfg = _load_scope_config()
    return str(cfg.get("violation_action", "warn"))
