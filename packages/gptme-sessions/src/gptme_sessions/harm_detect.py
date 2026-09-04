"""Harm detection for session grading — Phase 2 of ErikBjare/bob#632.

Implements automated detectors that fill the ``grades["harm"]`` dimension
in the session grading pipeline.  The grade convention follows the rest of the
grading system: **higher = better**.

  * ``1.0`` — session is clean (no harm signal detected)
  * ``0.0`` — harm detected (e.g. a commit from this session was later reverted)

This is the inverse of what you might initially expect ("harm grade = 1 when
there is harm") but is required so that the weighted-average trajectory_grade
computation in :func:`~.record.compute_trajectory_grade` degrades properly
when harm is present.

Current detectors
-----------------
* :func:`detect_harm_revert` — checks whether any deliverable commit was later
  reverted via ``git log --grep="This reverts commit <sha>"``.

Configuration
-------------
All entry points accept explicit ``repos`` and ``store_path`` arguments so
callers outside the default Bob workspace can wire their own session store
location and search repositories.  The defaults only apply when those
arguments are ``None`` and are intentionally narrow (the Bob brain repo and
its commonly-checked worktrees); running outside a Bob-style workspace without
explicit ``repos`` is treated as "no repos to search" and surfaces a
:func:`require_repos` error rather than silently grading every session as
clean.
"""

from __future__ import annotations

import json
import os
import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# 40-char hex or 7-12 char short SHA
_SHA_RE = re.compile(r"\b([0-9a-f]{7,40})\b")

# Default repos to search for revert commits, relative to a Bob-style
# workspace root.  Ordered by likelihood.
_DEFAULT_REPO_CANDIDATES: list[str] = [
    # Brain repo (most active; git root via rev-parse)
    "",
    # Upstream gptme (most common source of reverted cross-repo work)
    "projects/gptme",
    # gptme-contrib (second-most common)
    "gptme-contrib",
]

# Bob-style workspace override of the default SessionStore directory.
#
# The :class:`~.store.SessionStore` default is
# ``~/.local/share/gptme-sessions/`` (XDG-compliant, set via
# ``GPTME_SESSIONS_DIR``); that's the right out-of-the-box location for users
# running gptme outside a Bob workspace.  In a Bob workspace,
# ``state/sessions/`` is the conventional location, and callers can opt into
# it via the ``store_path`` argument or the ``BOB_HARM_DETECT_USE_BOB_STORE``
# env var (``1``/``true`` to prefer the Bob-style path when running inside
# a Bob workspace).
_USE_BOB_STORE_ENV = "BOB_HARM_DETECT_USE_BOB_STORE"


class HarmDetectError(Exception):
    """Base error for harm detection configuration failures."""


class NoSearchReposError(HarmDetectError):
    """Raised when no repositories are available to search for reverts.

    Callers either pass ``repos=`` explicitly, run from a Bob-style
    workspace where ``_DEFAULT_REPO_CANDIDATES`` resolves, or opt in to
    "no repos, treat as clean" with ``allow_empty_repos=True``.
    """


def _workspace_root() -> Path | None:
    """Return the git workspace root of the current process, or ``None``.

    Returns ``None`` (not a hard-coded fallback) when called outside any git
    work tree so callers can detect the "not in a repo" case explicitly.
    """
    try:
        out = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
        return Path(out)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _resolve_default_repos() -> list[Path]:
    """Resolve default repo paths relative to the workspace root.

    Returns an empty list when called outside a Bob-style workspace
    (i.e. when ``_workspace_root()`` is ``None`` or the candidate paths
    don't exist on disk).  Empty results surface as :class:`NoSearchReposError`
    in the public entry points rather than silently grading every session as
    clean.
    """
    root = _workspace_root()
    if root is None:
        return []
    repos: list[Path] = []
    for rel in _DEFAULT_REPO_CANDIDATES:
        candidate = root / rel if rel else root
        if _looks_like_git_repo(candidate):
            repos.append(candidate)
    return repos


def _looks_like_git_repo(path: Path) -> bool:
    """Return True if ``path`` is a usable git work tree.

    Three layouts are accepted:
      1. ``path/.git`` is a directory (regular repo)
      2. ``path/.git`` is a file pointing to an *existing* worktree metadata
         directory (active worktree)
      3. ``path/.git`` is a file pointing to a *missing* target (stale
         worktree pointer) — rejected, since ``git log`` would still work
         but the search semantics drift from the user's intent

    Bare ``gitdir:`` prefixes without a resolvable target are not enough;
    a stale pointer would silently let searches "succeed" with no revs.
    """
    if not path.is_dir():
        return False
    git_entry = path / ".git"
    if git_entry.is_dir():
        return True
    if git_entry.is_file():
        try:
            head = git_entry.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return False
        if not head.startswith("gitdir:"):
            return False
        target = head[len("gitdir:") :].strip()
        if not target:
            return False
        # Resolve relative paths against the repo's parent directory.
        target_path = Path(target)
        if not target_path.is_absolute():
            target_path = (path / target).resolve()
        return target_path.is_dir()
    return False


# Backwards-compatible alias (used by the batch API and the test suite).
_default_repos = _resolve_default_repos


def _default_store_path() -> Path:
    """Return the default SessionStore path.

    Resolution order:
      1. ``GPTME_SESSIONS_DIR`` env var (SessionStore's primary hook).
      2. ``~/.local/share/gptme-sessions/`` (SessionStore's XDG default).
      3. If ``BOB_HARM_DETECT_USE_BOB_STORE`` is set and we are in a Bob-style
         workspace (git root exists and has a ``state/sessions/`` directory),
         return ``<workspace>/state/sessions/`` instead.  This makes the
         detector work out of the box in Bob without forcing Bob-style paths
         on other users.
    """
    from .store import _default_sessions_dir

    sessions_default = _default_sessions_dir()
    if os.environ.get(_USE_BOB_STORE_ENV, "").lower() in ("1", "true", "yes"):
        root = _workspace_root()
        if root is not None:
            bob_store = root / "state" / "sessions"
            if bob_store.is_dir():
                return bob_store
    return sessions_default


def extract_commit_shas(deliverables: list[str]) -> list[str]:
    """Extract commit SHAs (7-40 hex chars) from a deliverables list.

    Skips entries that look like URLs (http/https) since those are PR links,
    not commit SHAs.
    """
    shas: list[str] = []
    for item in deliverables:
        if item.startswith(("http://", "https://")):
            continue
        for m in _SHA_RE.finditer(item):
            sha = m.group(1)
            # Prefer longer SHAs; skip trivially short hex strings
            if len(sha) >= 7:
                shas.append(sha)
    return list(dict.fromkeys(shas))  # dedupe, preserve order


def _is_sha_reverted(sha: str, repo: Path, timeout: int = 10) -> bool:
    """Return True if ``sha`` was reverted in ``repo``.

    Searches ``git log`` for the pattern ``This reverts commit <sha>`` in
    commit bodies (standard ``git revert`` message format).
    """
    # Use --all so we catch reverts on any branch
    pattern = f"This reverts commit {sha}"
    try:
        result = subprocess.run(
            ["git", "log", "--all", "--oneline", "--grep", pattern, "--max-count", "5"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.debug(
                "SHA %s reverted in %s: %s", sha[:7], repo.name, result.stdout.strip()[:120]
            )
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("git log failed for %s in %s: %s", sha[:7], repo, exc)
    return False


def _resolve_repos(repos: list[Path] | None) -> list[Path]:
    """Apply the default if ``repos`` is ``None``; return the resolved list."""
    return _resolve_default_repos() if repos is None else list(repos)


def _require_repos(resolved: list[Path], *, allow_empty: bool) -> list[Path]:
    """Return ``resolved`` or raise :class:`NoSearchReposError`.

    ``allow_empty=True`` is an opt-in for callers that genuinely want
    "no repos to search ⇒ treat as clean" semantics (e.g. dry-run tooling
    that wants to validate the rest of the pipeline without hitting git).
    """
    if resolved:
        return resolved
    if allow_empty:
        return resolved
    raise NoSearchReposError(
        "No repositories available to search for revert commits. "
        "Pass `repos=[...]` explicitly, run from a Bob-style workspace "
        "where _DEFAULT_REPO_CANDIDATES resolves, or set `allow_empty_repos=True`."
    )


def detect_harm_revert(
    session_id: str,
    *,
    deliverables: list[str] | None = None,
    repos: list[Path] | None = None,
    store_path: Path | None = None,
    allow_empty_repos: bool = False,
) -> float:
    """Detect whether any deliverable commit from this session was later reverted.

    Args:
        session_id: Session ID to check.  Used for logging only when
            ``deliverables`` is supplied directly.
        deliverables: Explicit list of deliverable strings (commit SHAs, PR
            URLs).  When ``None``, the session record is loaded from the
            :class:`~.store.SessionStore` at ``store_path`` (or the default
            Bob ``state/sessions/`` path if ``store_path`` is also ``None``).
        repos: Git repositories to search.  Defaults to the brain repo,
            ``projects/gptme``, and ``gptme-contrib`` when run from a
            Bob-style workspace; empty when run from anywhere else (caller
            must pass an explicit list or set ``allow_empty_repos=True``).
        store_path: Path to the SessionStore directory.  Defaults to the
            Bob-style ``state/sessions/`` path; ``None`` is treated as
            "use default".  When the resolved path doesn't exist, the
            function falls back to a no-store lookup (deliverables must be
            supplied explicitly or the session is graded clean by default).
        allow_empty_repos: Opt-in to "no repos ⇒ treat as clean" semantics.
            Defaults to ``False`` (raises :class:`NoSearchReposError`) so
            misconfigurations fail loudly.

    Returns:
        ``0.0`` if harm detected (a deliverable commit was reverted).
        ``1.0`` if clean (no revert found, no commit SHAs in deliverables,
        or repos unavailable and ``allow_empty_repos=True``).
    """
    if deliverables is None:
        # Import here to avoid circular imports
        from .store import SessionStore

        resolved_store = store_path if store_path is not None else _default_store_path()
        if resolved_store is None or not resolved_store.exists():
            logger.debug(
                "Session %s: no SessionStore path available (resolved=%s)",
                session_id,
                resolved_store,
            )
            deliverables = []
        else:
            store = SessionStore(resolved_store)
            records = {r.session_id: r for r in store.load_all(include_archives=True)}
            record = records.get(session_id)
            if record is None:
                logger.debug("Session %s not found in store", session_id)
                deliverables = []
            else:
                deliverables = record.deliverables or []

    shas = extract_commit_shas(deliverables)
    if not shas:
        logger.debug("Session %s: no commit SHAs in deliverables", session_id)
        return 1.0

    search_repos = _require_repos(_resolve_repos(repos), allow_empty=allow_empty_repos)

    for sha in shas:
        for repo in search_repos:
            if _is_sha_reverted(sha, repo):
                logger.info(
                    "HARM DETECTED: session=%s sha=%s reverted in %s",
                    session_id,
                    sha[:7],
                    repo.name,
                )
                return 0.0

    return 1.0


def batch_detect_harm_revert(
    session_ids: list[str],
    *,
    repos: list[Path] | None = None,
    store_path: Path | None = None,
    allow_empty_repos: bool = False,
) -> dict[str, float]:
    """Run :func:`detect_harm_revert` on a batch of session IDs.

    Loads the store once and reuses it across all calls.  Returns a mapping of
    ``{session_id: harm_grade}``.

    The ``store_path`` and ``allow_empty_repos`` arguments are forwarded to
    each :func:`detect_harm_revert` call.
    """
    from .store import SessionStore

    resolved_store = store_path if store_path is not None else _default_store_path()
    store = (
        SessionStore(resolved_store)
        if resolved_store is not None and resolved_store.exists()
        else None
    )
    records = (
        {r.session_id: r for r in store.load_all(include_archives=True)}
        if store is not None
        else {}
    )
    resolved_repos = _resolve_repos(repos)

    results: dict[str, float] = {}
    for sid in session_ids:
        record = records.get(sid)
        deliverables = (record.deliverables or []) if record else []
        results[sid] = detect_harm_revert(
            sid,
            deliverables=deliverables,
            repos=resolved_repos,
            store_path=store_path,
            allow_empty_repos=allow_empty_repos,
        )
    return results


def check_precision_on_ground_truth(
    harm_incidents_path: Path | None = None,
    repos: list[Path] | None = None,
    *,
    allow_empty_repos: bool = True,
) -> dict[str, object]:
    """Evaluate revert-detection precision/recall against the annotated seed set.

    For each ``harm_type: revert`` entry in ``state/harm-incidents.jsonl``:
    1. Finds the session that produced the culprit commit (via session record).
    2. Runs :func:`detect_harm_revert` on that session.
    3. Reports precision (TP / (TP + FP)), recall (TP / (TP + FN)),
       and per-incident details.

    Definitions:
        * TP: detector flagged harm (grade == 0.0) on a real revert incident
        * FP: detector flagged harm on a non-incident or a non-attributable
          incident
        * FN: detector graded clean on a real revert incident with a known
          ``culprit_session``

    Most incidents have ``culprit_session: null`` (pre-session-trailer era).
    Those are reported as ``attribution=unattributable`` and are excluded
    from precision/recall denominators (the detector cannot be evaluated on
    cases where we don't know which session to ask about).

    Returns a dict with ``precision``, ``recall``, ``n_attributable``,
    ``n_unattributable``, ``true_positives``, ``false_positives``,
    ``false_negatives``, and per-incident ``details``.

    Note: ``allow_empty_repos`` defaults to ``True`` here so the evaluation
    helper never raises on misconfigured environments — if no repos are
    found, every attributable incident becomes a FN (the detector returns
    1.0 = clean) and the report makes that visible.
    """
    if harm_incidents_path is None:
        # harm incidents are a Bob-specific concept; in non-Bob workspaces
        # callers must pass the path explicitly.  Inside a Bob workspace,
        # honor BOB_HARM_DETECT_USE_BOB_STORE for the same opt-in that
        # store_path uses.
        root = _workspace_root()
        if root is None:
            raise FileNotFoundError(
                "harm_incidents_path is required when no Bob workspace is "
                "available; pass the path explicitly."
            )
        harm_incidents_path = root / "state" / "harm-incidents.jsonl"
        if not harm_incidents_path.exists():
            raise FileNotFoundError(
                f"Default harm incidents file not found at {harm_incidents_path}; "
                "pass harm_incidents_path explicitly or run from a Bob-style "
                "workspace that has state/harm-incidents.jsonl."
            )

    reverts: list[dict] = []
    with open(harm_incidents_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("harm_type") == "revert":
                    reverts.append(d)
            except json.JSONDecodeError:
                continue

    attributable = [r for r in reverts if r.get("culprit_session")]
    details: list[dict] = []
    true_positives = 0
    false_positives = 0
    false_negatives = 0

    for incident in attributable:
        session_id = incident["culprit_session"]
        try:
            grade = detect_harm_revert(session_id, repos=repos, allow_empty_repos=allow_empty_repos)
        except NoSearchReposError as exc:
            logger.warning("Skipping incident %s: %s", incident.get("id"), exc)
            grade = 1.0  # clean (no repos → detector says clean)
        is_tp = grade == 0.0
        if is_tp:
            true_positives += 1
        else:
            false_negatives += 1
        details.append(
            {
                "id": incident["id"],
                "session_id": session_id,
                "grade": grade,
                "is_tp": is_tp,
                "attribution": "attributable",
            }
        )

    n = len(attributable)
    precision = true_positives / n if n > 0 else None
    recall = true_positives / n if n > 0 else None

    return {
        "n_revert_incidents": len(reverts),
        "n_attributable": n,
        "n_unattributable": len(reverts) - n,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "precision": precision,
        "recall": recall,
        "details": details,
    }
