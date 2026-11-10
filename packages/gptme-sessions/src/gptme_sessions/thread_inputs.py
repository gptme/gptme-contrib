"""Inputs for :mod:`gptme_sessions.threads`: session records and journals -> session dicts.

``threads`` is pure and takes plain dicts. This module is the adapter between it
and what an agent actually stores: :class:`SessionRecord` rows and the journal
entries they point at.

- :func:`journal_digest` extracts the title, outcome, why-snippet and the
  sessions, commits, PRs, tasks and journals a journal entry references.
- :func:`from_records` maps records (plus their journal digests) to the session
  dicts ``threads.mine_edges`` expects, coercing the shapes real records carry
  (``task`` as a dict, PRs as URLs, deliverables as ``"subject (sha)"`` strings).
- :func:`build_forest` runs the whole pipeline and returns one row per session
  with its predecessor, the winning evidence, and its thread id.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from . import threads as th
from .record import SessionRecord

_TITLE = re.compile(r"^#\s+(.+?)\s*$", re.M)
_OUTCOME = re.compile(r"^\*\*Outcome\*\*:\s*(.+?)\s*$", re.M | re.I)
_SESSION_ID = re.compile(r"^\*\*Session ID\*\*:\s*`?([0-9a-f]{4,8})`?\s*$", re.M | re.I)
_WHY = re.compile(r"^##\s+Why[^\n]*\n+(.+?)(?:\n\s*\n|\n#|\Z)", re.M | re.I | re.S)
_SESSION_REF = re.compile(r"\bsessions?\s+`?([0-9a-f]{4,8})`?\b", re.I)
# Hex runs bounded by non-word, non-hyphen chars, so UUID segments don't count.
_SHA = re.compile(r"(?<![\w-])([0-9a-f]{7,40})(?![\w-])")
_PR_REF = re.compile(r"(?<![\w./-])([\w.-]+/[\w.-]+#\d+)\b")
_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/(?:pull|issues)/\d+")
_TASK_PATH = re.compile(r"\btasks/(?:archive/)?([a-z0-9][a-z0-9-]*[a-z0-9])\.md\b")
_JOURNAL_LINK = re.compile(r"\bjournal/\d{4}-\d{2}-\d{2}/[\w.-]+\.md\b")
_LABEL_IN_NAME = re.compile(r"session-([0-9a-f]{4,8})(?:-|$)")
_SUBJECT_SHA = re.compile(r"^(.*?)\s*\(([0-9a-f]{7,40})\)\s*$")
_BARE_SHA = re.compile(r"[0-9a-f]{7,40}")

WHY_MAX_CHARS = 500


def _uniq(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def journal_key(path: str | Path) -> str:
    """Stable id for a journal: the ``journal/...`` suffix, else the file name.

    Records store absolute or workspace-relative paths, and journals link each
    other as ``journal/YYYY-MM-DD/x.md``; this makes all three compare equal.
    """
    s = str(path).replace("\\", "/")
    pos = s.rfind("journal/")
    return s[pos:] if pos >= 0 else s.rsplit("/", 1)[-1]


def journal_digest(text: str) -> dict[str, Any]:
    """Extract threading evidence from one journal entry (no I/O).

    Returns ``title``, ``outcome_line``, ``why`` (free text for topic similarity),
    ``short`` (the ``**Session ID**`` label, if any) and the referenced
    ``sessions_mentioned``, ``shas_mentioned``, ``prs_mentioned``,
    ``tasks_mentioned`` and ``journal_links``.
    """
    digest: dict[str, Any] = {}
    if m := _TITLE.search(text):
        digest["title"] = m.group(1)
    if m := _OUTCOME.search(text):
        digest["outcome_line"] = m.group(1)
    if m := _WHY.search(text):
        digest["why"] = " ".join(m.group(1).split())[:WHY_MAX_CHARS]
    if m := _SESSION_ID.search(text):
        digest["short"] = m.group(1).lower()
    # Digits-only runs are dates, counts and issue numbers far more often than hashes.
    shas = [s for s in _SHA.findall(text) if not s.isdigit()]
    prs = [*_PR_REF.findall(text), *_PR_URL.findall(text)]
    digest.update(
        sessions_mentioned=_uniq(s.lower() for s in _SESSION_REF.findall(text)),
        shas_mentioned=_uniq(shas),
        prs_mentioned=_uniq(k for p in prs if (k := th.norm_pr(p))),
        tasks_mentioned=_uniq(_TASK_PATH.findall(text)),
        journal_links=_uniq(_JOURNAL_LINK.findall(text)),
    )
    if digest.get("short"):
        own = digest["short"]
        digest["sessions_mentioned"] = [s for s in digest["sessions_mentioned"] if s != own]
    return digest


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.timestamp()


def _as_key(value: object, *fields: str) -> str | None:
    """A task/PR reference as a string. Real records sometimes carry a dict."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        for f in fields:
            v = value.get(f)
            if isinstance(v, str) and v.strip():
                return v.strip()
        repo, num = value.get("repo"), value.get("number")
        if isinstance(repo, str) and isinstance(num, int):
            return f"{repo}#{num}"
    return None


def _deliverables(values: Iterable[object]) -> tuple[list[str], list[str], list[str]]:
    """(commit shas, commit subjects, PR refs) from a record's ``deliverables``."""
    shas: list[str] = []
    subjects: list[str] = []
    prs: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            continue
        v = raw.strip()
        if _BARE_SHA.fullmatch(v):
            shas.append(v)
        elif m := _SUBJECT_SHA.match(v):
            shas.append(m.group(2))
            if not m.group(1).startswith("("):  # "(commit output truncated)"
                subjects.append(m.group(1))
        elif k := th.norm_pr(v):
            prs.append(k)
    return _uniq(shas), _uniq(subjects), _uniq(prs)


JournalReader = Callable[[Path], "str | None"]


def read_journal_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def from_records(
    records: Iterable[SessionRecord],
    *,
    agent: str,
    journal_root: Path | None = None,
    read_journal: JournalReader | None = read_journal_text,
) -> list[dict[str, Any]]:
    """Map session records (plus their journal digests) to ``threads`` session dicts.

    agent: the agent id these records belong to (records don't carry one).
    journal_root: base for workspace-relative ``journal_path`` values; defaults
        to the record's ``project`` directory.
    read_journal: returns a journal's text or None; pass None to skip journals.

    Records without a usable start/end time are dropped. A journal shared by
    several records in the batch (a per-day log many sessions append to) is kept
    as the ``journal`` id but not digested: its references belong to no single
    session, and attaching them to each would glue unrelated work together.
    The result is sorted by start, as ``threads`` requires.
    """
    records = list(records)
    shared: dict[Path | tuple[int, int], int] = {}
    journal_paths: list[tuple[Path, Path | tuple[int, int]] | None] = []
    for r in records:
        entry = None
        if r.journal_path and read_journal is not None:
            try:
                path = Path(r.journal_path).expanduser()
                if not path.is_absolute():
                    base = journal_root or (Path(r.project).expanduser() if r.project else None)
                    path = (base / path) if base else path
                path = path.resolve()
                identity: Path | tuple[int, int] = path
                try:
                    stat = path.stat()
                    identity = (stat.st_dev, stat.st_ino)
                except FileNotFoundError:
                    pass  # Custom readers can supply text for paths not on disk.
                entry = (path, identity)
                shared[identity] = shared.get(identity, 0) + 1
            except (OSError, RuntimeError, ValueError):
                pass  # Unusable journal evidence must not discard record fields.
        journal_paths.append(entry)

    nodes: list[dict[str, Any]] = []
    for r, entry in zip(records, journal_paths):
        start = _epoch(r.start_time)
        end = _epoch(r.end_time)
        duration = r.duration_seconds or 0
        if start is None:
            # Explicit end times anchor backwards; sync's timestamp is the
            # trajectory's first event, so timestamp-only records anchor forwards.
            start = end - duration if end is not None else _epoch(r.timestamp)
        if end is None and start is not None:
            end = start + duration
        if start is None or end is None:
            continue
        cause = r.dispatch_cause if isinstance(r.dispatch_cause, Mapping) else {}
        intent = r.cascade_intent if isinstance(r.cascade_intent, Mapping) else {}
        shas, subjects, prs = _deliverables(r.deliverables or [])
        n: dict[str, Any] = {
            "t": "s",
            "agent": agent,
            "id": r.session_id,
            "start": start,
            "end": max(end, start),
            "parent": r.parent_session_id or _as_key(cause.get("parent_session_id")),
            "task": _as_key(intent.get("task_id"), "id")
            or _as_key(cause.get("task"), "id", "task_id"),
            "pr": _as_key(cause.get("pr"), "url", "id", "ref"),
            "trigger": r.trigger,
            "dispatch_kind": r.dispatch_kind,
            "run_type": r.run_type,
            "task_label": _as_key(intent.get("task_label")),
            "name": r.session_name,
            "outcome": r.outcome,
            "delivered_shas": shas,
            "delivered_prs": prs,
            "commits": subjects,
        }
        if r.journal_path:
            jkey = journal_key(r.journal_path)
            n["journal"] = jkey
            if m := _LABEL_IN_NAME.search(Path(jkey).stem):
                n["short"] = m.group(1)
            if read_journal is not None and entry is not None and shared[entry[1]] == 1:
                text = read_journal(entry[0])
                if text:
                    digest = journal_digest(text)
                    n.update({k: v for k, v in digest.items() if v})
        nodes.append({k: v for k, v in n.items() if v not in (None, [], "")})
    nodes.sort(key=lambda n: (n["start"], n["id"]))
    return nodes


def build_forest(
    nodes: list[dict[str, Any]],
    *,
    min_weight: float = 0.5,
    coherence: float = 1.0,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Mine evidence, keep each session's best predecessor, and assign threads.

    Returns one row per session: ``id``, ``agent``, ``start``, ``end``,
    ``predecessor`` (session id or None), ``kind``/``key``/``weight`` of the
    winning link, and ``thread`` (the id of the thread's root session); plus
    mining stats.
    """
    edges, stats = th.mine_edges(nodes)
    sims = th.topic_similarity(nodes, {(a, b) for a, b, _ in edges}) if coherence > 0 else {}
    pred = th.best_predecessors(nodes, edges, sims, minw=min_weight, coherence=coherence)
    roots = th.threads(pred)

    best: dict[int, tuple[float, str, str | None]] = {}
    for (a, b, kind), e in edges.items():
        if pred[b] != a:
            continue
        w = th.effective_weight(kind, e["w"], sims.get((a, b), -1.0), coherence)
        if b not in best or w > best[b][0]:
            best[b] = (w, kind, e.get("key"))

    rows: list[dict[str, Any]] = []
    for i, n in enumerate(nodes):
        link = best.get(i)
        rows.append(
            {
                "id": n["id"],
                "agent": n["agent"],
                "start": n["start"],
                "end": n["end"],
                "title": n.get("title") or n.get("task_label") or n.get("name"),
                "predecessor": nodes[pred[i]]["id"] if pred[i] >= 0 else None,
                "kind": link[1] if link else None,
                "key": link[2] if link else None,
                "weight": round(link[0], 4) if link else None,
                "thread": nodes[roots[i]]["id"],
            }
        )
    summary = {k: int(v) for k, v in stats.items()}
    summary["sessions"] = len(nodes)
    summary["linked"] = sum(1 for p in pred if p >= 0)
    summary["threads"] = len(set(roots))
    return rows, summary
