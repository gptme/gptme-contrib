"""Session provenance — trace a file or GitHub ref back to the AI session that authored it.

``gptme sessions blame`` (and the ``gptme-sessions blame`` CLI subcommand) answer
the question "which AI session produced this line / PR / commit?" by correlating
git author-dates with session time-windows from the session-records store.

Phase 1 — commit-window correlation:
    git history gives commits + author dates for a path/line. Session records
    carry a ``timestamp`` (≈ session end) and ``duration_seconds``, so each
    session defines a time window ``[timestamp - duration, timestamp]``. A commit
    is attributed to the session whose window contains the commit's author date;
    otherwise to the nearest session within a 30-minute tolerance.

Phase 2 (GitHub refs):
    Pass ``owner/repo#N`` to blame a PR or issue. For PRs the commit list is
    fetched directly via the GitHub API (``gh api``). For issues, PRs that close
    the issue are discovered and their commits attributed.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# A commit can land minutes after a session's recorded end (journal commit,
# auto-push). Allow nearest-neighbour matching within this slack.
NEAREST_TOLERANCE = timedelta(minutes=30)

# Matches GitHub refs like ``owner/repo#123`` (PR or issue).
GITHUB_REF_RE = re.compile(r"^([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)#(\d+)$")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Attribution:
    """A single commit attributed (or not yet attributed) to a session."""

    sha: str
    when: datetime
    author: str
    subject: str
    session_id: str | None = None
    category: str | None = None
    productivity: float | None = None
    journal_path: str | None = None
    confidence: str = "unmatched"  # exact | near | ambiguous | unmatched
    # Explicit resolution method, never silently downgraded:
    #   trailer | commit-window | nearest | trajectory-exact | unattributable
    method: str = "unattributable"
    model: str | None = None
    harness: str | None = None
    # Raw Git-Session-Id trailer value parsed from the commit (may name a session
    # not present in the loaded windows — still authoritative).
    trailer_session_id: str | None = None
    # When confidence=="ambiguous": all session_ids whose windows contain this commit.
    candidates: list[str] = field(default_factory=list)


@dataclass
class SessionWindow:
    """A session's time window derived from session-records."""

    session_id: str
    start: datetime
    end: datetime
    category: str | None
    harness: str | None
    productivity: float | None
    journal_path: str | None
    model: str | None = None

    def distance(self, when: datetime) -> timedelta:
        """Zero if ``when`` is inside the window, else distance to the nearest edge."""
        if self.start <= when <= self.end:
            return timedelta(0)
        if when < self.start:
            return self.start - when
        return when - self.end


@dataclass
class BlameResult:
    path: str
    line: int | None
    attributions: list[Attribution] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Git / shell helpers
# ---------------------------------------------------------------------------


def _run(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()


def _parse_iso(value: str) -> datetime:
    # Python 3.10 fromisoformat() doesn't support the 'Z' UTC suffix (added in 3.11)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Commit discovery — file path
# ---------------------------------------------------------------------------


def commits_for_path(path: str, limit: int = 10) -> list[Attribution]:
    """Return the most recent commits touching ``path``, up to ``limit``."""
    out = _run(
        [
            "git",
            "log",
            "--follow",
            f"--max-count={limit}",
            "--format=%H%x1f%aI%x1f%an%x1f%s%x1f%(trailers:key=Git-Session-Id,valueonly,separator=%x1e)",
            "--",
            path,
        ]
    )
    result: list[Attribution] = []
    # Use split('\n') not splitlines(): Python's splitlines() treats \x1e (ASCII
    # Record Separator) as a line boundary, which would corrupt records when \x1e
    # is used as the within-field separator for multiple trailer values.
    for line in out.split("\n"):
        if not line:
            continue
        parts = line.split("\x1f", 4)
        sha, when, author, subject = parts[:4]
        # Multiple trailers are joined by \x1e; take only the first value.
        trailer_sid = parts[4].split("\x1e")[0].strip() if len(parts) > 4 else ""
        result.append(
            Attribution(
                sha=sha,
                when=_parse_iso(when),
                author=author,
                subject=subject,
                trailer_session_id=trailer_sid or None,
            )
        )
    return result


def commit_for_line(path: str, line: int) -> list[Attribution]:
    """Return the single commit that last touched ``path`` line ``line``."""
    out = _run(["git", "blame", "-L", f"{line},{line}", "--porcelain", "--", path])
    sha = out.splitlines()[0].split(" ", 1)[0]
    meta = _run(
        [
            "git",
            "show",
            "-s",
            "--format=%aI%x1f%an%x1f%s%x1f%(trailers:key=Git-Session-Id,valueonly,separator=%x1e)",
            sha,
        ]
    )
    parts = meta.split("\x1f", 3)
    when, author, subject = parts[:3]
    # Multiple trailers are joined by \x1e; take only the first value.
    trailer_sid = parts[3].split("\x1e")[0].strip() if len(parts) > 3 else ""
    return [
        Attribution(
            sha=sha,
            when=_parse_iso(when),
            author=author,
            subject=subject,
            trailer_session_id=trailer_sid or None,
        )
    ]


# ---------------------------------------------------------------------------
# Commit discovery — GitHub refs
# ---------------------------------------------------------------------------


def commits_for_github_ref(ref: str) -> list[Attribution]:
    """Return commits associated with a GitHub PR or issue ref (``owner/repo#N``).

    For PRs: fetches the PR's commit list directly via the GitHub API.
    For issues: discovers PRs that close the issue and returns their commits.
    Returns an empty list (with a stderr warning) when ``gh`` is unavailable or
    the ref resolves to neither a PR nor any closing PRs.
    """
    m = GITHUB_REF_RE.match(ref)
    if not m:
        raise ValueError(f"Not a valid GitHub ref: {ref!r}")
    owner_repo, number = m.group(1), m.group(2)

    _JQ_COMMITS = (
        ".[] | [.sha, .commit.author.date, .commit.author.name,"
        ' (.commit.message | split("\\n")[0])] | @tsv'
    )

    def _parse_tsv(out: str) -> list[Attribution]:
        result = []
        for line in out.splitlines():
            if not line:
                continue
            parts = line.split("\t", 3)
            if len(parts) != 4:
                continue
            sha, when, author, subject = parts
            result.append(
                Attribution(sha=sha, when=_parse_iso(when), author=author, subject=subject)
            )
        return result

    # Try as a PR first — the pulls endpoint returns 404 for plain issues.
    try:
        pr_commits = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{owner_repo}/pulls/{number}/commits",
                "--jq",
                _JQ_COMMITS,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        pr_results = _parse_tsv(pr_commits)
        if pr_results:
            return pr_results
    except subprocess.CalledProcessError:
        pass  # 404 or gh unavailable — fall through to issue path

    # It's an issue (or an empty PR): find PRs that close it.
    try:
        pr_numbers = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                owner_repo,
                "--state",
                "all",
                "--search",
                f"closes:#{number}",
                "--json",
                "number",
                "--jq",
                ".[].number",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        print(
            f"warning: could not list closing PRs for {ref} (gh unavailable?)",
            file=sys.stderr,
        )
        return []

    attributions: list[Attribution] = []
    for pr_num in pr_numbers.splitlines():
        pr_num = pr_num.strip()
        if not pr_num:
            continue
        try:
            out = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{owner_repo}/pulls/{pr_num}/commits",
                    "--jq",
                    _JQ_COMMITS,
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            attributions.extend(_parse_tsv(out))
        except subprocess.CalledProcessError:
            continue

    if not attributions:
        print(
            f"warning: no commits found for {ref} (not a PR and no closing PRs discovered)",
            file=sys.stderr,
        )
    return attributions


# ---------------------------------------------------------------------------
# Session-records loading
# ---------------------------------------------------------------------------


def consolidated_records_sources(primary: Path) -> list[Path]:
    """``primary`` plus its consolidated siblings in the same directory.

    The active ``session-records.jsonl`` is rotated: older sessions live in
    ``session-records-archive-*.jsonl`` and ``session-records.jsonl.bak-*``.
    We read the archive/backup files too (primary first so it wins on
    session_id collisions). Custom/test paths with no siblings are unaffected.
    """
    sources = [primary]
    parent = primary.parent
    if parent.exists():
        for sib in sorted(parent.glob("session-records*.jsonl*")):
            if sib != primary and sib.is_file():
                sources.append(sib)
    return sources


def load_windows(records_path: Path) -> list[SessionWindow]:
    """Load session time-windows from a session-records JSONL file (+ siblings)."""
    windows: list[SessionWindow] = []
    seen: set[str] = set()
    for src in consolidated_records_sources(records_path):
        if not src.exists():
            continue
        with src.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = rec.get("timestamp")
                dur = rec.get("duration_seconds")
                sid = rec.get("session_id")
                if not ts or not sid or sid in seen:
                    continue
                try:
                    end = _parse_iso(ts)
                except ValueError:
                    continue
                start = end - timedelta(seconds=dur) if dur else end
                grades = rec.get("grades") or {}
                seen.add(sid)
                windows.append(
                    SessionWindow(
                        session_id=sid,
                        start=start,
                        end=end,
                        category=rec.get("category"),
                        harness=rec.get("harness"),
                        productivity=grades.get("productivity"),
                        journal_path=rec.get("journal_path"),
                        model=rec.get("model"),
                    )
                )
    return windows


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def _fill_from_window(att: Attribution, w: SessionWindow) -> None:
    """Copy session metadata from ``w`` into ``att`` in-place."""
    att.session_id = w.session_id
    att.category = w.category
    att.productivity = w.productivity
    att.journal_path = w.journal_path
    att.model = w.model
    att.harness = w.harness


def attribute(att: Attribution, windows: list[SessionWindow]) -> Attribution:
    """Attribute ``att`` to the best-matching session window in-place.

    Priority:
    1. ``Git-Session-Id`` trailer (``att.trailer_session_id``): strongest evidence;
       beats any window match.  When the trailer names a session not present in
       ``windows`` the metadata fields are left None but ``session_id`` is still set.
    2. Exact window match (distance == 0):
       - Exactly one window → ``exact`` / ``commit-window`` (unchanged behaviour).
       - Two or more windows → ``ambiguous`` / ``commit-window``; ``session_id``
         is set to the closest-midpoint window; all candidates recorded in
         ``att.candidates``.
    3. Nearest window within ``NEAREST_TOLERANCE`` → ``near`` / ``nearest``.
    4. No match → left as ``unmatched`` / ``unattributable``.
    """
    # --- Step 1: trailer-first (strongest evidence) ---------------------------
    if att.trailer_session_id:
        matching = next((w for w in windows if w.session_id == att.trailer_session_id), None)
        att.method = "trailer"
        att.confidence = "exact"
        att.session_id = att.trailer_session_id
        if matching:
            _fill_from_window(att, matching)
        return att

    # --- Step 2: collect all windows that contain the commit ------------------
    exact_windows = [w for w in windows if w.distance(att.when) == timedelta(0)]

    if len(exact_windows) == 1:
        att.confidence = "exact"
        att.method = "commit-window"
        _fill_from_window(att, exact_windows[0])
        return att

    if len(exact_windows) > 1:
        # Ambiguous: pick the window whose midpoint is closest (deterministic).
        def _midpoint_dist(w: SessionWindow) -> timedelta:
            mid = w.start + (w.end - w.start) / 2
            delta = att.when - mid
            return delta if delta.total_seconds() >= 0 else -delta

        best = min(exact_windows, key=_midpoint_dist)
        att.confidence = "ambiguous"
        att.method = "commit-window"
        att.candidates = [w.session_id for w in exact_windows]
        _fill_from_window(att, best)
        return att

    # --- Step 3: nearest window within tolerance ------------------------------
    best_w: SessionWindow | None = None
    best_dist: timedelta | None = None
    for w in windows:
        d = w.distance(att.when)
        if best_dist is None or d < best_dist:
            best_w, best_dist = w, d

    if best_w is None or best_dist is None:
        return att

    if best_dist <= NEAREST_TOLERANCE:
        att.confidence = "near"
        att.method = "nearest"
        _fill_from_window(att, best_w)

    return att


def attribute_all(
    attributions: list[Attribution], windows: list[SessionWindow]
) -> list[Attribution]:
    """Attribute every commit in ``attributions`` using ``windows``."""
    for a in attributions:
        attribute(a, windows)
    return attributions


# ---------------------------------------------------------------------------
# Trajectory scanning
# ---------------------------------------------------------------------------

#: Write-ish tool names in CC trajectories that author or mutate a file.
_WRITE_TOOLS = frozenset(
    {"Write", "Edit", "MultiEdit", "str_replace_editor", "NotebookEdit", "create"}
)

#: Max seconds for the grep prefilter per source directory.
_GREP_TIMEOUT = 60


@dataclass
class TrajectoryHit:
    """A single Write/Edit tool-call touching the target path."""

    session_uuid: str
    when: datetime | None
    tool: str
    file_path: str  # path as recorded in the trajectory
    cwd: str | None
    source: str  # path of the trajectory JSONL file


def _parse_traj_ts(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _traj_tool_uses(record: dict) -> list[tuple[str, dict]]:
    """Return (tool_name, input_dict) for tool_use blocks in a CC record."""
    msg = record.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    out: list[tuple[str, dict]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        inp = block.get("input")
        if isinstance(name, str) and isinstance(inp, dict):
            out.append((name, inp))
    return out


def _traj_file_paths(inp: dict) -> list[str]:
    """Extract candidate file paths from a write-ish tool input."""
    paths: list[str] = []
    for key in ("file_path", "path", "notebook_path"):
        val = inp.get(key)
        if isinstance(val, str) and val:
            paths.append(val)
    return paths


def _traj_path_matches(traj_path: str, target_rel: str, target_abs: str | None) -> bool:
    """Return True if ``traj_path`` refers to the target file.

    Matches on exact absolute path or on a ``/``-boundary suffix of the
    repo-relative target, so worktree checkouts at different roots still match.
    """
    if not traj_path:
        return False
    norm = traj_path.rstrip("/")
    if target_abs and norm == target_abs.rstrip("/"):
        return True
    rel = target_rel.strip("/")
    return norm == rel or norm.endswith("/" + rel)


def _traj_candidate_files(basename: str, sources: list[Path]) -> list[Path]:
    """Return trajectory JSONL files whose content mentions ``basename``."""
    files: list[Path] = []
    seen: set[str] = set()
    for src in sources:
        if not src.exists():
            continue
        try:
            proc = subprocess.run(
                ["grep", "-rlF", "--include=*.jsonl", basename, str(src)],
                capture_output=True,
                text=True,
                timeout=_GREP_TIMEOUT,
            )
        except (subprocess.SubprocessError, OSError):
            continue
        for p in proc.stdout.splitlines():
            if p and p not in seen:
                seen.add(p)
                files.append(Path(p))
    return files


def scan_trajectories(
    target_rel: str,
    target_abs: str | None = None,
    sources: list[Path] | None = None,
    limit: int | None = None,
) -> list[TrajectoryHit]:
    """Find Write/Edit tool-calls that authored ``target_rel``, newest first.

    ``sources`` must be supplied explicitly; ``None`` or an empty list returns
    ``[]`` immediately — non-Bob deployments have no trajectory store, and the
    caller chooses whether to provide one.
    """
    if not sources:
        return []
    basename = Path(target_rel).name
    hits: list[TrajectoryHit] = []
    for traj in _traj_candidate_files(basename, sources):
        try:
            with traj.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for tool, inp in _traj_tool_uses(rec):
                        if tool not in _WRITE_TOOLS:
                            continue
                        matched = [
                            fp
                            for fp in _traj_file_paths(inp)
                            if _traj_path_matches(fp, target_rel, target_abs)
                        ]
                        if matched:
                            hits.append(
                                TrajectoryHit(
                                    session_uuid=rec.get("sessionId") or traj.stem,
                                    when=_parse_traj_ts(rec.get("timestamp")),
                                    tool=tool,
                                    file_path=matched[0],
                                    cwd=rec.get("cwd"),
                                    source=str(traj),
                                )
                            )
        except OSError:
            continue
    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    hits.sort(key=lambda h: h.when or _epoch, reverse=True)
    return hits[:limit] if limit else hits


def model_for_trajectory(source: str) -> str | None:
    """Resolve the model used by the trajectory file at ``source``."""
    try:
        from gptme_sessions.discovery import extract_cc_model
    except ImportError:
        return None
    try:
        model = extract_cc_model(Path(source))
    except (OSError, ValueError):
        return None
    return str(model) if model else None


def enrich_with_trajectory(
    att: Attribution,
    target_rel: str,
    target_abs: str | None,
    windows: list[SessionWindow],
    trajectories_dirs: list[Path],
) -> None:
    """Back-fill ``att`` from a trajectory scan when commit-window failed.

    Modifies ``att`` in-place.  On a hit sets ``method='trajectory-exact'`` and
    ``confidence='exact'``.  On a miss leaves ``method='unattributable'``.
    """
    hits = scan_trajectories(target_rel, target_abs, sources=trajectories_dirs, limit=1)
    if not hits:
        att.method = "unattributable"
        return
    hit = hits[0]
    att.method = "trajectory-exact"
    att.confidence = "exact"
    att.session_id = hit.session_uuid
    att.harness = "claude-code"
    att.model = model_for_trajectory(hit.source)
    if hit.when is not None:
        for w in windows:
            if w.distance(hit.when) == timedelta(0):
                att.category = att.category or w.category
                att.productivity = (
                    att.productivity if att.productivity is not None else w.productivity
                )
                att.journal_path = att.journal_path or w.journal_path
                att.model = att.model or w.model
                break


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------


def render_text(result: BlameResult) -> str:
    lines: list[str] = []
    target = result.path
    if result.line is not None:
        target += f":{result.line}"
    lines.append(f"Session provenance for {target}")
    lines.append("")
    if not result.attributions:
        lines.append("  (no commits found)")
        return "\n".join(lines)
    for a in result.attributions:
        date_str = a.when.strftime("%Y-%m-%d %H:%M")
        sess = a.session_id or "—"
        cat = a.category or "—"
        prod = f"{a.productivity:.2f}" if a.productivity is not None else "—"
        model = a.model or "—"
        mark = {"exact": "●", "near": "○", "ambiguous": "◐", "unmatched": "·"}.get(
            a.confidence, "·"
        )
        lines.append(f"  {mark} {date_str}  {a.sha[:9]}  session={sess}")
        lines.append(f"      category={cat}  model={model}  productivity={prod}  method={a.method}")
        lines.append(f"      {a.subject}")
        if a.journal_path:
            lines.append(f"      journal: {a.journal_path}")
        if a.candidates:
            lines.append(f"      candidates: {', '.join(a.candidates)}")
    lines.append("")
    lines.append(
        "  ● exact (commit-window/trajectory/trailer)"
        "  ◐ ambiguous (multiple windows)"
        "  ○ nearest (≤30m)"
        "  · unattributable"
    )
    return "\n".join(lines)


def render_json(result: BlameResult) -> str:
    return json.dumps(
        {
            "path": result.path,
            "line": result.line,
            "attributions": [
                {
                    "sha": a.sha,
                    "when": a.when.isoformat(),
                    "author": a.author,
                    "subject": a.subject,
                    "session_id": a.session_id,
                    "category": a.category,
                    "productivity": a.productivity,
                    "journal_path": a.journal_path,
                    "confidence": a.confidence,
                    "method": a.method,
                    "model": a.model,
                    "harness": a.harness,
                    "candidates": a.candidates,
                }
                for a in result.attributions
            ],
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# High-level entry point
# ---------------------------------------------------------------------------

#: Default location of the session-records JSONL file.
DEFAULT_RECORDS = Path("state/sessions/session-records.jsonl")


def blame(
    path_or_ref: str,
    *,
    line: int | None = None,
    limit: int = 10,
    records: Path | None = None,
    trajectories_dirs: list[Path] | None = None,
) -> BlameResult:
    """Attribute a file path or GitHub ref to its authoring session(s).

    Args:
        path_or_ref: Repo-relative file path, absolute path, or ``owner/repo#N``.
        line: If given, blame only this line (file path mode only).
        limit: Max commits for whole-file mode.
        records: Path to session-records JSONL; defaults to ``DEFAULT_RECORDS``
            resolved against the current git root when inside a repo.
        trajectories_dirs: Directories containing CC trajectory JSONL files.
            When provided, trajectory-exact attribution is attempted for commits
            that remain ``unattributable`` or ``ambiguous`` after commit-window
            matching.  ``None`` (default) skips trajectory scan — backward
            compatible with deployments that have no trajectory store.

    Returns:
        A :class:`BlameResult` with attributions populated.
    """
    # Resolve the records path.
    if records is None:
        try:
            repo_root = Path(_run(["git", "rev-parse", "--show-toplevel"]))
            records = repo_root / DEFAULT_RECORDS
        except subprocess.CalledProcessError:
            records = DEFAULT_RECORDS

    windows = load_windows(records)

    # GitHub ref path (owner/repo#N) — no local git history needed.
    if GITHUB_REF_RE.match(path_or_ref):
        if line is not None:
            raise ValueError("--line is not supported for GitHub refs")
        attributions = commits_for_github_ref(path_or_ref)
        attribute_all(attributions, windows)
        return BlameResult(path=path_or_ref, line=None, attributions=attributions)

    # File path — requires a local git repo.
    try:
        repo_root = Path(_run(["git", "rev-parse", "--show-toplevel"]))
    except subprocess.CalledProcessError:
        raise RuntimeError("not inside a git repository")

    abs_path = Path(path_or_ref).resolve()
    try:
        rel_path = str(abs_path.relative_to(repo_root))
    except ValueError:
        rel_path = path_or_ref

    if line is not None:
        attributions = commit_for_line(rel_path, line)
    else:
        attributions = commits_for_path(rel_path, limit)

    attribute_all(attributions, windows)

    if trajectories_dirs is not None:
        abs_path_str = str(abs_path)
        for a in attributions:
            if a.method == "unattributable" or (
                a.method == "commit-window" and a.confidence == "ambiguous"
            ):
                enrich_with_trajectory(a, rel_path, abs_path_str, windows, trajectories_dirs)

    return BlameResult(path=rel_path, line=line, attributions=attributions)
