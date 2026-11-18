"""Move old terminal tasks from ``tasks/`` into ``tasks/archive/``.

A workspace that never archives keeps every finished task in one flat
directory, so every scan and search pays for thousands of files that nobody
works on. The readers already resolve ``tasks/archive/**`` for dependencies
(see :func:`gptodo.utils.build_dependency_universe`); this module is the
writer that moves tasks there.

Rules (all fail closed, i.e. a task that does not clearly qualify stays put):

* Only terminal states: ``done``, ``cancelled`` and ``expired`` (or a subset).
* Tasks with ``recur:`` are skipped: they cycle back to live states.
* Age is measured from recorded terminal metadata, compared in UTC:
  ``completed`` (stamped by gptodo on the transition to done/cancelled), or
  ``expired_at`` for expired tasks. Date-only and naive values count as UTC.
  A value that does not parse skips the task.
* Tasks without that metadata (written before gptodo stamped it) use the
  latest of their ``modified`` field, file mtime and last git commit, which is
  a conservative last-change bound. Creation age is never used.
* A task stays in place when a task that stays in ``tasks/`` still needs it
  there in a way the readers cannot resolve from the archive: it is listed in
  a non-terminal task's ``spawned_tasks`` (fan-in completion looks children
  up among live tasks only), it is the ``parent``/``spawned_from`` of a
  non-terminal task, or a remaining task links to its file (``gptodo check``
  would report a broken link). ``requires``/``depends`` do not pin a task:
  they resolve from the archive.
* Never overwrites: a task whose name already exists anywhere under
  ``tasks/archive/`` is refused and reported.
* Moves use ``git mv`` when the file is tracked in a git work tree, a plain
  rename otherwise. Nothing is committed.
* Relative Markdown links inside a moved file are rebased so they still
  resolve from ``tasks/archive/``. Links in other files are not rewritten;
  a task linked from a remaining task is not moved instead.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from gptodo.utils import EXCLUDED_TASK_DIRS, TaskInfo, load_tasks

ARCHIVE_STATES: List[str] = ["done", "cancelled", "expired"]
ARCHIVE_DEFAULT_DAYS: int = 7
_TERMINAL = set(ARCHIVE_STATES)

# Same shape as the link checkers (gptodo check and the markdown-link hook):
# images are included so a moved image reference keeps resolving too.
_LINK_RE = re.compile(r"(\[[^\]]*\]\()([^)]+)(\))")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,}).*?\n.*?^\1[ \t]*$", re.MULTILINE | re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_URL_PREFIXES = ("http://", "https://", "ftp://", "mailto:", "#")


@dataclass
class ArchiveCandidate:
    task: TaskInfo
    age_days: float
    age_source: str  # "completed", "expired_at" or "last-change"
    terminal_at: datetime


@dataclass
class ArchivePlan:
    """Result of planning (and optionally applying) an archive run."""

    selected: List[ArchiveCandidate] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)
    moved: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[Dict[str, str]] = field(default_factory=list)
    total_eligible: int = 0


def _to_utc(value: Any) -> Optional[datetime]:
    """Parse a frontmatter timestamp; date-only and naive values are UTC."""
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, date):
        stamp = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                d = date.fromisoformat(text)
            except ValueError:
                return None
            stamp = datetime(d.year, d.month, d.day)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _git(cwd: Path, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _last_change(task: TaskInfo, in_git: bool) -> Optional[datetime]:
    """Latest of the ``modified`` field, file mtime and last git commit."""
    stamps: List[datetime] = []
    if "modified" in task.metadata:
        parsed = _to_utc(task.metadata["modified"])
        if parsed is not None:
            stamps.append(parsed)
    try:
        stamps.append(datetime.fromtimestamp(task.path.stat().st_mtime, timezone.utc))
    except OSError:
        return None
    if in_git:
        out = _git(task.path.parent, "log", "-1", "--format=%ct", "--", task.path.name)
        if out and out.strip().isdigit():
            stamps.append(datetime.fromtimestamp(int(out.strip()), timezone.utc))
    return max(stamps)


def terminal_evidence(
    task: TaskInfo, *, in_git: bool = False, cutoff: Optional[datetime] = None
) -> Tuple[Optional[datetime], str]:
    """Return (when the task became terminal, source of that timestamp).

    ``(None, "invalid-<field>")`` means the recorded value did not parse.
    When ``cutoff`` is given and the file mtime alone already proves the task
    changed after it, the git lookup is skipped (the result can only be later).
    """
    fields = ["expired_at", "completed"] if task.state == "expired" else ["completed"]
    for name in fields:
        if task.metadata.get(name) is not None:
            parsed = _to_utc(task.metadata[name])
            if parsed is None:
                return None, f"invalid-{name}"
            return parsed, name
    if cutoff is not None:
        try:
            mtime = datetime.fromtimestamp(task.path.stat().st_mtime, timezone.utc)
        except OSError:
            return None, "last-change"
        if mtime > cutoff:
            return mtime, "last-change"
    return _last_change(task, in_git), "last-change"


def _keys(task: TaskInfo) -> Set[str]:
    return {task.id, task.name}


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _strip_code(text: str) -> str:
    text = _FENCE_RE.sub("", text)
    return _INLINE_CODE_RE.sub("", text)


def _link_targets(path: Path, repo_root: Path) -> Set[Path]:
    """Resolved files a task's Markdown links point at (as ``gptodo check`` resolves them)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    targets: Set[Path] = set()
    for match in _LINK_RE.finditer(_strip_code(text)):
        target = match.group(2).strip()
        if target.startswith(_URL_PREFIXES):
            continue
        file_target = target.split("#")[0].split("?")[0]
        if not file_target:
            continue
        if file_target.startswith("/"):
            targets.add((repo_root / file_target.lstrip("/")).resolve())
            continue
        targets.add((path.parent / file_target).resolve())
        targets.add((repo_root / file_target).resolve())
    return targets


def _pin_reasons(
    remaining: Iterable[TaskInfo],
    moving: Dict[str, ArchiveCandidate],
    repo_root: Path,
    link_cache: Dict[Path, Set[Path]],
) -> Dict[str, str]:
    """Map candidate name -> why a task that stays in ``tasks/`` needs it there."""
    by_key: Dict[str, str] = {}
    by_path: Dict[Path, str] = {}
    for name, cand in moving.items():
        for key in _keys(cand.task):
            by_key[key] = name
        by_path[cand.task.path.resolve()] = name

    reasons: Dict[str, str] = {}
    for other in remaining:
        live = other.state not in _TERMINAL
        if live:
            for child in _as_str_list(other.spawned_tasks):
                if child in by_key:
                    reasons.setdefault(
                        by_key[child], f"spawned_tasks of {other.name} ({other.state})"
                    )
            for ref_field, ref in (("parent", other.parent), ("spawned_from", other.spawned_from)):
                if isinstance(ref, str) and ref in by_key:
                    reasons.setdefault(by_key[ref], f"{ref_field} of {other.name} ({other.state})")
        if other.path not in link_cache:
            link_cache[other.path] = _link_targets(other.path, repo_root)
        for target in link_cache[other.path]:
            if target in by_path:
                reasons.setdefault(by_path[target], f"linked from {other.name}")
    return reasons


def _settle(
    selected: Dict[str, ArchiveCandidate],
    others: List[TaskInfo],
    repo_root: Path,
    skipped: List[Dict[str, str]],
    link_cache: Dict[Path, Set[Path]],
) -> Dict[str, ArchiveCandidate]:
    """Drop pinned candidates until nothing that stays behind needs a moved task."""
    remaining = list(others)
    while True:
        reasons = _pin_reasons(remaining, selected, repo_root, link_cache)
        if not reasons:
            return selected
        for name, reason in sorted(reasons.items()):
            cand = selected.pop(name)
            remaining.append(cand.task)
            skipped.append({"id": cand.task.id, "reason": f"needed in place: {reason}"})


def _archive_names(archive_dir: Path) -> Set[str]:
    """Every task-file stem under ``tasks/archive/`` (dependency lookup is by stem)."""
    if not archive_dir.is_dir():
        return set()
    return {
        p.stem
        for p in archive_dir.glob("**/*.md")
        if not any(d in p.parts for d in EXCLUDED_TASK_DIRS)
    }


def rebase_links(text: str, old_dir: Path, new_dir: Path, move_map: Dict[Path, Path]) -> str:
    """Rewrite relative links so they resolve from ``new_dir`` as they did from ``old_dir``.

    Only links whose target existed relative to ``old_dir`` are touched; links
    inside code, absolute paths and URLs are left alone. A target that is
    moved in the same run is pointed at its new location.
    """

    def fix(segment: str) -> str:
        def repl(match: re.Match[str]) -> str:
            target = match.group(2)
            if target.strip() != target or target.startswith((*_URL_PREFIXES, "/")):
                return match.group(0)
            cut = len(target)
            for sep in ("#", "?"):
                idx = target.find(sep)
                if idx != -1:
                    cut = min(cut, idx)
            file_part, suffix = target[:cut], target[cut:]
            if not file_part:
                return match.group(0)
            old_target = (old_dir / file_part).resolve()
            # A co-moved file is already gone from old_dir when this runs.
            if not (old_target.exists() or old_target in move_map):
                return match.group(0)
            new_target = move_map.get(old_target, old_target)
            rel = os.path.relpath(new_target, new_dir.resolve()).replace(os.sep, "/")
            if file_part.endswith("/") and not rel.endswith("/"):
                rel += "/"
            if rel == file_part:
                return match.group(0)
            return f"{match.group(1)}{rel}{suffix}{match.group(3)}"

        return _LINK_RE.sub(repl, segment)

    # Rewrite only outside fenced blocks and inline code spans.
    out: List[str] = []
    pos = 0
    for block in _FENCE_RE.finditer(text):
        out.append(_rewrite_outside_inline(text[pos : block.start()], fix))
        out.append(block.group(0))
        pos = block.end()
    out.append(_rewrite_outside_inline(text[pos:], fix))
    return "".join(out)


def _rewrite_outside_inline(segment: str, fix: Any) -> str:
    out: List[str] = []
    pos = 0
    for code in _INLINE_CODE_RE.finditer(segment):
        out.append(fix(segment[pos : code.start()]))
        out.append(code.group(0))
        pos = code.end()
    out.append(fix(segment[pos:]))
    return "".join(out)


def plan_archive(
    tasks_dir: Path,
    repo_root: Path,
    *,
    older_than_days: int = ARCHIVE_DEFAULT_DAYS,
    states: Optional[List[str]] = None,
    limit: int = 0,
    now: Optional[datetime] = None,
) -> ArchivePlan:
    """Choose which tasks to archive without touching any file."""
    states = list(states) if states else list(ARCHIVE_STATES)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=older_than_days)
    archive_dir = tasks_dir / "archive"
    in_git = _git(tasks_dir, "rev-parse", "--is-inside-work-tree") is not None

    plan = ArchivePlan()
    all_tasks = load_tasks(tasks_dir, include_subtasks=False)
    archived = _archive_names(archive_dir)

    eligible: Dict[str, ArchiveCandidate] = {}
    others: List[TaskInfo] = []
    for task in sorted(all_tasks, key=lambda t: t.name):
        if task.state not in states:
            others.append(task)
            continue
        if task.recur:
            others.append(task)
            plan.skipped.append({"id": task.id, "reason": "recurring task (recur: set)"})
            continue
        stamp, source = terminal_evidence(task, in_git=in_git, cutoff=cutoff)
        if stamp is None:
            others.append(task)
            if source.startswith("invalid-"):
                field_name = source.removeprefix("invalid-")
                plan.skipped.append({"id": task.id, "reason": f"unparseable {field_name}"})
            continue
        if stamp > cutoff:
            others.append(task)
            continue
        if task.name in archived:
            others.append(task)
            plan.skipped.append(
                {
                    "id": task.id,
                    "reason": "name already exists under tasks/archive/ (not overwritten)",
                }
            )
            continue
        age = (now - stamp).total_seconds() / 86400
        eligible[task.name] = ArchiveCandidate(task, age, source, stamp)

    link_cache: Dict[Path, Set[Path]] = {}
    eligible = _settle(eligible, others, repo_root, plan.skipped, link_cache)
    plan.total_eligible = len(eligible)

    ordered = sorted(eligible.values(), key=lambda c: (c.terminal_at, c.task.name))
    if limit and len(ordered) > limit:
        # Tasks cut by --limit stay behind, so they can pin a selected task
        # too. A pinned pick is replaced by the next oldest until the batch
        # settles; every round excludes at least one task, so this ends.
        excluded: Set[str] = set()
        while True:
            pool = [c for c in ordered if c.task.name not in excluded]
            keep = {c.task.name: c for c in pool[:limit]}
            cut_tasks = [c.task for c in ordered if c.task.name not in keep]
            dropped: List[Dict[str, str]] = []
            settled = _settle(dict(keep), others + cut_tasks, repo_root, dropped, link_cache)
            if len(settled) == len(keep):
                break
            excluded.update(set(keep) - set(settled))
            plan.skipped.extend(dropped)
        ordered = [c for c in ordered if c.task.name in settled]
    plan.selected = ordered
    return plan


def _tracked_names(tasks_dir: Path) -> Optional[Set[str]]:
    out = _git(tasks_dir, "ls-files", "-z", "--", ".")
    if out is None:
        return None
    return {p for p in out.split("\0") if p and "/" not in p}


def apply_archive(plan: ArchivePlan, tasks_dir: Path) -> None:
    """Move the planned tasks. Appends to ``plan.moved`` and ``plan.errors``.

    All moves happen first; links inside the moved files are rebased
    afterwards against the set of moves that actually succeeded.
    """
    if not plan.selected:
        return
    archive_dir = tasks_dir / "archive"
    archive_dir.mkdir(exist_ok=True)
    tracked = _tracked_names(tasks_dir)

    moved: List[Tuple[Path, Path, Dict[str, Any]]] = []
    for cand in plan.selected:
        src = cand.task.path
        dest = archive_dir / src.name
        # Re-check on disk right before moving: another session may have
        # reopened the task or created the destination since planning.
        fresh = load_tasks(tasks_dir, single_file=src) if src.exists() else []
        if not fresh or fresh[0].state != cand.task.state:
            plan.errors.append({"id": cand.task.id, "error": "changed since planning; not moved"})
            continue
        if dest.exists():
            plan.errors.append({"id": cand.task.id, "error": f"{dest.name} exists in archive/"})
            continue

        use_git = tracked is not None and src.name in tracked
        if use_git:
            result = subprocess.run(
                ["git", "mv", "--", src.name, f"archive/{src.name}"],
                cwd=tasks_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                plan.errors.append(
                    {"id": cand.task.id, "error": f"git mv failed: {result.stderr.strip()}"}
                )
                continue
        else:
            try:
                os.rename(src, dest)
            except OSError as exc:
                plan.errors.append({"id": cand.task.id, "error": str(exc)})
                continue
        record: Dict[str, Any] = {
            "id": cand.task.id,
            "state": cand.task.state,
            "from": src.name,
            "to": f"archive/{src.name}",
            "age_days": round(cand.age_days, 1),
            "age_source": cand.age_source,
            "method": "git mv" if use_git else "rename",
            "links_rebased": False,
        }
        moved.append((src, dest, record))
        plan.moved.append(record)

    move_map = {src.resolve(): dest.resolve() for src, dest, _ in moved}
    for src, dest, record in moved:
        try:
            text = dest.read_text(encoding="utf-8")
        except OSError as exc:
            plan.errors.append({"id": record["id"], "error": f"moved, links not rebased: {exc}"})
            continue
        new_text = rebase_links(text, src.parent, archive_dir, move_map)
        if new_text == text:
            continue
        dest.write_text(new_text, encoding="utf-8")
        record["links_rebased"] = True
        if record["method"] == "git mv":
            _git(tasks_dir, "add", "--", f"archive/{src.name}")
