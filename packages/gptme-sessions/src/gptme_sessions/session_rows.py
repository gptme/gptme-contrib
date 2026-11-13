"""One row per session from ``session-records.jsonl``.

Every project-monitoring dispatch writes two ledger rows: a ``controller`` row
that lists its worker in ``child_session_ids`` and the ``worker`` row for that
child, usually with an identical ``token_count``. Counting sessions or summing
tokens over both roughly doubles the monitoring lane (2026-11-12 audit: ~2x in
October). :func:`load_session_rows` is the loader for any reader that counts
sessions or sums tokens/cost; pair it with
:class:`gptme_sessions.session_identity.SessionIndex` to resolve ids that
other ledgers store as UUIDs, 8-hex ids or 4-hex labels.

Raw rows (dicts) rather than :class:`~gptme_sessions.record.SessionRecord`
objects, so unknown fields survive. Stdlib only. Not for read-modify-write
cycles: those need every row.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from .session_identity import id_format


def dedupe_monitoring_pairs(rows: Iterable[dict], *, keep: str = "controller") -> list[dict]:
    """Return one row per session by collapsing project-monitoring pairs.

    ``keep="controller"`` (default) drops each worker whose controller is in
    *rows*; the controller row carries the dispatch identity (PR/task in
    ``dispatch_cause``) and ``outcome_class``. ``keep="worker"`` drops each
    controller instead; the worker row carries ``cost_usd``, ``served_model``
    and trajectory detail, so cost readers want it.

    The child row is matched by ``session_id`` in the controller's
    ``child_session_ids``, not by ``execution_role == "worker"``: some children
    are recorded as ``run_type: interactive`` with no role (0.2% of rows).

    Only a pair with both sides present is collapsed: a controller without
    children, or a worker whose controller rotated out of the window, stays so
    nothing is lost. Four-character child labels are not unique across history,
    so those pairs are conservatively retained rather than guessing which row
    belongs to the controller. In worker mode, a multi-child controller stays
    until all its children are present; this can retain overlapping coverage in
    a partial window, but does not discard the missing children's coverage.
    Order is preserved; non-monitoring rows pass through.
    """
    if keep not in ("controller", "worker"):
        raise ValueError(f"keep must be 'controller' or 'worker', got {keep!r}")
    rows = list(rows)
    present = {r.get("session_id") for r in rows if id_format(r.get("session_id")) != "label4"}
    if keep == "controller":
        children = {
            c
            for r in rows
            if r.get("execution_role") == "controller"
            for c in (r.get("child_session_ids") or [])
            if id_format(c) != "label4"
        }
        return [
            r
            for r in rows
            if not (r.get("execution_role") != "controller" and r.get("session_id") in children)
        ]
    return [
        r
        for r in rows
        if not (
            r.get("execution_role") == "controller"
            and r.get("child_session_ids")
            and all(c in present for c in r["child_session_ids"])
        )
    ]


def read_jsonl_rows(path: str | Path) -> list[dict]:
    """Read dict rows from a JSONL file, skipping blank and malformed lines."""
    rows: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def load_session_rows(path: str | Path, *, keep: str = "controller") -> list[dict]:
    """Load ``session-records.jsonl`` as one row per session.

    A missing file yields ``[]``. See :func:`dedupe_monitoring_pairs` for
    ``keep``.
    """
    p = Path(path)
    if not p.exists():
        return []
    return dedupe_monitoring_pairs(read_jsonl_rows(p), keep=keep)
