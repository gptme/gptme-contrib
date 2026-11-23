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
* Relative Markdown links inside a moved file (inline links, with or
  without a title, and reference definitions) are rebased so they still
  resolve from ``tasks/archive/``; links written from the repository root
  are kept root-based. Links in files already under ``tasks/archive/`` that
  point at a task moved now are repointed. Links in other files are not
  rewritten; a task linked from a remaining task is not moved instead. If a
  selected task ends up not moving, moved tasks it still needs are moved back.
* Rewrites go through a temporary file that replaces the task only once it
  is fully written.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from gptodo.utils import EXCLUDED_TASK_DIRS, TaskInfo, load_tasks

ARCHIVE_STATES: List[str] = ["done", "cancelled", "expired"]
ARCHIVE_DEFAULT_DAYS: int = 7
_TERMINAL = set(ARCHIVE_STATES)

# Inline links ``[text](dest "title")`` (images included, so a moved image
# reference keeps resolving too) and reference definitions ``[label]: dest``.
# Group 1 is the part that holds the destination and an optional title.
_INLINE_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_REF_DEF_RE = re.compile(r"^[ ]{0,3}\[[^\]\n]+\]:[ \t]*(\S[^\n]*)$", re.MULTILINE)
_FENCE_RE = re.compile(r"^(`{3,}|~{3,}).*?\n.*?^\1[ \t]*$", re.MULTILINE | re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_URL_PREFIXES = ("http://", "https://", "ftp://", "mailto:", "#")
# Code is blanked with this character (newlines kept) so positions still line
# up with the original text. Links are found in the blanked copy: a code span
# in a link's label does not hide the link, a link inside code is not a link.
_CODE_MASK = "\0"


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
    # Files already under tasks/archive/ whose links were repointed at a task
    # moved in this run (paths relative to tasks/).
    relinked: List[str] = field(default_factory=list)
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


def _git_commit_times(directory: Path) -> Optional[Dict[str, int]]:
    """Last commit time (unix) per file in ``directory``, from one ``git log`` pass.

    Keys are paths relative to ``directory``. Equivalent to running
    ``git log -1 --format=%ct -- <file>`` per file, without one subprocess each
    (that costs ~0.7 s per file on an 18k-commit repo).
    """
    out = _git(
        directory,
        "-c",
        "core.quotePath=false",
        "log",
        "--format=%x00%ct",
        "--name-only",
        "--relative",
        "--",
        ".",
    )
    if out is None:
        return None
    times: Dict[str, int] = {}
    current: Optional[int] = None
    for line in out.splitlines():
        if line.startswith("\0"):
            stamp = line[1:].strip()
            current = int(stamp) if stamp.isdigit() else None
        elif line and current is not None:
            times.setdefault(line, current)  # log is newest-first: keep the first seen
    return times


def _last_change(
    task: TaskInfo, in_git: bool, git_times: Optional[Dict[str, int]] = None
) -> Optional[datetime]:
    """Latest of the ``modified`` field, file mtime and last git commit.

    ``git_times`` (from ``_git_commit_times``) replaces the per-file ``git log``.
    """
    stamps: List[datetime] = []
    if "modified" in task.metadata:
        parsed = _to_utc(task.metadata["modified"])
        if parsed is not None:
            stamps.append(parsed)
    try:
        stamps.append(datetime.fromtimestamp(task.path.stat().st_mtime, timezone.utc))
    except OSError:
        return None
    if in_git and git_times is not None and task.path.name in git_times:
        stamps.append(datetime.fromtimestamp(git_times[task.path.name], timezone.utc))
    elif in_git:  # no map, or a name the map lacks (untracked): ask git for this file
        out = _git(task.path.parent, "log", "-1", "--format=%ct", "--", task.path.name)
        if out and out.strip().isdigit():
            stamps.append(datetime.fromtimestamp(int(out.strip()), timezone.utc))
    return max(stamps)


def terminal_evidence(
    task: TaskInfo,
    *,
    in_git: bool = False,
    cutoff: Optional[datetime] = None,
    git_times: Optional[Dict[str, int]] = None,
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
    return _last_change(task, in_git, git_times), "last-change"


def _keys(task: TaskInfo) -> Set[str]:
    return {task.id, task.name}


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _dest_span(inner: str) -> Optional[Tuple[int, int]]:
    """Span of the destination in a link's ``dest "title"`` part (without ``<>``)."""
    lead = len(inner) - len(inner.lstrip())
    rest = inner[lead:]
    if not rest:
        return None
    if rest.startswith("<"):
        end = rest.find(">")
        return (lead + 1, lead + end) if end > 1 else None
    return lead, lead + len(rest.split(None, 1)[0])


def _mask_code(text: str) -> str:
    """``text`` with fenced blocks and inline code spans blanked, same length."""

    def blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", _CODE_MASK, match.group(0))

    return _INLINE_CODE_RE.sub(blank, _FENCE_RE.sub(blank, text))


def _link_dests(text: str) -> Iterator[Tuple[int, int]]:
    """Spans of link destinations in ``text`` (inline links and reference definitions).

    Links inside code are skipped; code inside a link's label is not a problem.
    """
    masked = _mask_code(text)
    spans: List[Tuple[int, int]] = []
    for regex in (_INLINE_LINK_RE, _REF_DEF_RE):
        for match in regex.finditer(masked):
            span = _dest_span(match.group(1))
            if span is None:
                continue
            start, end = match.start(1) + span[0], match.start(1) + span[1]
            if _CODE_MASK not in masked[start:end]:
                spans.append((start, end))
    last_end = -1
    for start, end in sorted(spans):
        if start >= last_end:  # overlapping matches: keep the first
            yield start, end
            last_end = end


def _link_targets(path: Path, repo_root: Path) -> Set[Path]:
    """Resolved files a task's Markdown links point at (as ``gptodo check`` resolves them)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    targets: Set[Path] = set()
    for start, end in _link_dests(text):
        target = text[start:end]
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


def rebase_links(
    text: str,
    old_dir: Path,
    new_dir: Path,
    move_map: Dict[Path, Path],
    repo_root: Optional[Path] = None,
) -> str:
    """Rewrite relative links so they resolve from ``new_dir`` as they did from ``old_dir``.

    Links are resolved the way ``gptodo check`` does: from ``old_dir`` first,
    then from ``repo_root``. A directory-relative link is rewritten relative
    to ``new_dir``; a root-based one is left alone unless its target moved in
    the same run, in which case it points at the new location, still from the
    root. Links to missing files, links inside code, absolute paths and URLs
    are left alone.
    """

    def new_dest(target: str) -> Optional[str]:
        if target.startswith((*_URL_PREFIXES, "/")):
            return None
        cut = len(target)
        for sep in ("#", "?"):
            idx = target.find(sep)
            if idx != -1:
                cut = min(cut, idx)
        file_part, suffix = target[:cut], target[cut:]
        if not file_part:
            return None
        old_target = (old_dir / file_part).resolve()
        # A co-moved file is already gone from old_dir when this runs.
        if old_target.exists() or old_target in move_map:
            base = new_dir.resolve()
            if old_target not in move_map and base == old_dir.resolve():
                return None  # neither end moved: leave the link as written
        elif repo_root is not None:
            old_target = (repo_root / file_part).resolve()
            if old_target not in move_map:
                return None  # still resolves from the root after the move
            base = repo_root.resolve()
        else:
            return None
        new_target = move_map.get(old_target, old_target)
        rel = os.path.relpath(new_target, base).replace(os.sep, "/")
        if file_part.endswith("/") and not rel.endswith("/"):
            rel += "/"
        if rel == file_part:
            return None
        return rel + suffix

    out: List[str] = []
    pos = 0
    for start, end in _link_dests(text):
        replacement = new_dest(text[start:end])
        if replacement is None:
            continue
        out.append(text[pos:start])
        out.append(replacement)
        pos = end
    out.append(text[pos:])
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
    git_times = _git_commit_times(tasks_dir) if in_git else None

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
        stamp, source = terminal_evidence(
            task, in_git=in_git, cutoff=cutoff, git_times=git_times
        )
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


def _move(src: Path, dest: Path, tasks_dir: Path, use_git: bool) -> Optional[str]:
    """Move one task file; return an error message, or None on success."""
    if use_git:
        result = subprocess.run(
            ["git", "mv", "--", str(src.relative_to(tasks_dir)), str(dest.relative_to(tasks_dir))],
            cwd=tasks_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        return f"git mv failed: {result.stderr.strip()}" if result.returncode != 0 else None
    try:
        os.rename(src, dest)
    except OSError as exc:
        return str(exc)
    return None


def _replace_text(path: Path, text: str) -> None:
    """Write ``text`` to a temporary file next to ``path``, then replace ``path``.

    A failed write leaves the original file untouched.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def apply_archive(plan: ArchivePlan, tasks_dir: Path, *, repo_root: Optional[Path] = None) -> None:
    """Move the planned tasks. Appends to ``plan.moved`` and ``plan.errors``.

    All moves happen first. A selected task that did not move stays in
    ``tasks/``, so a moved task it still needs there (link, ``parent``,
    ``spawned_from``, ``spawned_tasks``) is moved back, until nothing that
    stayed needs a moved task. Links inside the moved files are rebased
    afterwards against the moves that remain.
    """
    if not plan.selected:
        return
    repo_root = repo_root if repo_root is not None else tasks_dir.parent
    archive_dir = tasks_dir / "archive"
    archive_dir.mkdir(exist_ok=True)
    tracked = _tracked_names(tasks_dir)

    moved: Dict[str, Tuple[ArchiveCandidate, Path, Dict[str, Any]]] = {}
    stayed: List[TaskInfo] = []
    for cand in plan.selected:
        src = cand.task.path
        dest = archive_dir / src.name
        # Re-check on disk right before moving: another session may have
        # reopened the task or created the destination since planning.
        fresh = load_tasks(tasks_dir, single_file=src) if src.exists() else []
        error = None
        if not fresh or fresh[0].state != cand.task.state:
            error = "changed since planning; not moved"
        elif dest.exists():
            error = f"{dest.name} exists in archive/"
        use_git = tracked is not None and src.name in tracked
        if error is None:
            error = _move(src, dest, tasks_dir, use_git)
        if error is not None:
            plan.errors.append({"id": cand.task.id, "error": error})
            stayed.extend(fresh)
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
        moved[cand.task.name] = (cand, dest, record)

    # Keep the planning guarantee for tasks that were meant to move but did not.
    stuck: Set[str] = set()
    while stayed:
        movable = {name: entry[0] for name, entry in moved.items() if name not in stuck}
        reasons = _pin_reasons(stayed, movable, repo_root, {})
        if not reasons:
            break
        stayed = []
        for name, reason in sorted(reasons.items()):
            cand, dest, record = moved[name]
            src = cand.task.path
            error = _move(dest, src, tasks_dir, record["method"] == "git mv")
            if error is not None:
                stuck.add(name)
                plan.errors.append(
                    {
                        "id": cand.task.id,
                        "error": f"moved, but needed in place ({reason}); "
                        f"moving it back failed: {error}",
                    }
                )
                continue
            del moved[name]
            plan.errors.append(
                {"id": cand.task.id, "error": f"moved back, needed in place: {reason}"}
            )
            stayed.extend(load_tasks(tasks_dir, single_file=src))

    move_map = {cand.task.path.resolve(): dest.resolve() for cand, dest, _ in moved.values()}
    for cand, dest, record in moved.values():
        plan.moved.append(record)
        try:
            text = dest.read_text(encoding="utf-8")
            new_text = rebase_links(text, cand.task.path.parent, archive_dir, move_map, repo_root)
            if new_text == text:
                continue
            _replace_text(dest, new_text)
        except OSError as exc:
            plan.errors.append({"id": record["id"], "error": f"moved, links not rebased: {exc}"})
            continue
        record["links_rebased"] = True
        if record["method"] == "git mv":
            _git(tasks_dir, "add", "--", record["to"])

    _relink_archived(plan, tasks_dir, archive_dir, move_map, repo_root)


def _relink_archived(
    plan: ArchivePlan,
    tasks_dir: Path,
    archive_dir: Path,
    move_map: Dict[Path, Path],
    repo_root: Path,
) -> None:
    """Repoint links in earlier-archived files at the tasks moved in this run.

    An archived task can link to a task that was still live when it moved (its
    link was rebased to ``../target.md``). When the target moves later, that
    link would break; this rewrites it to the target's new place.
    """
    if not move_map:
        return
    moved_now = set(move_map.values())
    names = {path.name for path in move_map}
    for path in sorted(archive_dir.glob("**/*.md")):
        rel = path.relative_to(tasks_dir).as_posix()
        if path.resolve() in moved_now or any(
            d in path.relative_to(archive_dir).parts for d in EXCLUDED_TASK_DIRS
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            if not any(name in text for name in names):
                continue
            new_text = rebase_links(text, path.parent, path.parent, move_map, repo_root)
            if new_text == text:
                continue
            _replace_text(path, new_text)
        except OSError as exc:
            plan.errors.append(
                {"id": path.stem, "error": f"links to moved tasks not updated in {rel}: {exc}"}
            )
            continue
        plan.relinked.append(rel)
        if _git(tasks_dir, "ls-files", "--error-unmatch", "--", rel) is not None:
            _git(tasks_dir, "add", "--", rel)
