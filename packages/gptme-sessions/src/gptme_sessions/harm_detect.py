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
"""

from __future__ import annotations

import logging
import re
import subprocess
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# 40-char hex or 7-12 char short SHA
_SHA_RE = re.compile(r"\b([0-9a-f]{7,40})\b")

# Default repos to search for revert commits.  Ordered by likelihood.
_DEFAULT_REPO_CANDIDATES: list[str] = [
    # Brain repo (most active; git root via rev-parse)
    "",
    # Upstream gptme (most common source of reverted cross-repo work)
    "projects/gptme",
    # gptme-contrib (second-most common)
    "gptme-contrib",
]


def _workspace_root() -> Path:
    """Return the git workspace root (ErikBjare/bob brain repo)."""
    try:
        out = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
        return Path(out)
    except subprocess.CalledProcessError:
        return Path("/home/bob/bob")


@lru_cache(maxsize=1)
def _default_repos() -> list[Path]:
    """Resolve default repo paths relative to the workspace root."""
    root = _workspace_root()
    repos: list[Path] = []
    for rel in _DEFAULT_REPO_CANDIDATES:
        candidate = root / rel if rel else root
        if candidate.is_dir() and (candidate / ".git").exists():
            repos.append(candidate)
    return repos


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


def detect_harm_revert(
    session_id: str,
    *,
    deliverables: list[str] | None = None,
    repos: list[Path] | None = None,
) -> float:
    """Detect whether any deliverable commit from this session was later reverted.

    Args:
        session_id: Session ID to check.  Used for logging only when
            ``deliverables`` is supplied directly.
        deliverables: Explicit list of deliverable strings (commit SHAs, PR
            URLs).  When ``None``, the session record is loaded from the
            default :class:`~.store.SessionStore`.
        repos: Git repositories to search.  Defaults to the brain repo,
            ``projects/gptme``, and ``gptme-contrib``.

    Returns:
        ``0.0`` if harm detected (a deliverable commit was reverted).
        ``1.0`` if clean (no revert found, or no commit SHAs in deliverables).
    """
    if deliverables is None:
        # Import here to avoid circular imports
        from .store import SessionStore

        store = SessionStore(Path(_workspace_root()) / "state" / "sessions")
        records = {r.session_id: r for r in store.load_all()}
        record = records.get(session_id)
        if record is None:
            logger.debug("Session %s not found in store", session_id)
            return 1.0
        deliverables = record.deliverables or []

    shas = extract_commit_shas(deliverables)
    if not shas:
        logger.debug("Session %s: no commit SHAs in deliverables", session_id)
        return 1.0

    search_repos = repos if repos is not None else _default_repos()

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
) -> dict[str, float]:
    """Run :func:`detect_harm_revert` on a batch of session IDs.

    Loads the store once and reuses it across all calls.  Returns a mapping of
    ``{session_id: harm_grade}``.
    """
    from .store import SessionStore

    store = SessionStore(Path(_workspace_root()) / "state" / "sessions")
    records = {r.session_id: r for r in store.load_all()}

    results: dict[str, float] = {}
    for sid in session_ids:
        record = records.get(sid)
        deliverables = (record.deliverables or []) if record else []
        results[sid] = detect_harm_revert(sid, deliverables=deliverables, repos=repos)
    return results


def check_precision_on_ground_truth(
    harm_incidents_path: Path | None = None,
    repos: list[Path] | None = None,
) -> dict[str, object]:
    """Evaluate revert-detection precision against the annotated seed set.

    For each ``harm_type: revert`` entry in ``state/harm-incidents.jsonl``:
    1. Finds the session that produced the culprit commit (via sessions-blame).
    2. Runs :func:`detect_harm_revert` on that session.
    3. Reports precision (TP / (TP + FP)).

    Returns a dict with ``precision``, ``n_positives``, ``n_attributable``,
    ``true_positives``, and per-incident details.

    Note: Most incidents have ``culprit_session: null`` (pre-session-trailer
    era).  Only incidents with a known ``culprit_session`` can be evaluated.
    Use :mod:`scripts.analysis.sessions_blame` to backfill attribution.
    """
    import json

    root = _workspace_root()
    if harm_incidents_path is None:
        harm_incidents_path = root / "state" / "harm-incidents.jsonl"

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

    for incident in attributable:
        session_id = incident["culprit_session"]
        grade = detect_harm_revert(session_id, repos=repos)
        is_tp = grade == 0.0  # detected harm
        if is_tp:
            true_positives += 1
        details.append(
            {
                "id": incident["id"],
                "session_id": session_id,
                "grade": grade,
                "is_tp": is_tp,
            }
        )

    n = len(attributable)
    precision = true_positives / n if n > 0 else None

    return {
        "n_revert_incidents": len(reverts),
        "n_attributable": n,
        "true_positives": true_positives,
        "precision": precision,
        "details": details,
    }
