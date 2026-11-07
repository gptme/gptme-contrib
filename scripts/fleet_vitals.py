#!/usr/bin/env python3
"""Shared helpers for the fleet-vitals contract (v1).

Every Superuser Labs agent emits a small, uniform JSON pair so the fleet
master dashboard can aggregate them without special-casing any agent:

- ``core.json``     — common-core health (status / activity / spend / services)
- ``headline.json`` — the agent-owned headline panel (one metric the agent
  defines for itself)

This module is the single source of truth for the envelope: schema strings,
the worst-of status rollup, and the emit step. Each agent fills in only the
dimensions it has — every field except ``schema``/``agent``/``generated_at``/
``status`` is optional, so an agent can ship a partial ``core.json`` on day one
and backfill later.

Stdlib only on purpose: agents copy or import this without adding deps. The
contract spec lives in alice's
``knowledge/infrastructure/fleet-vitals-contract.md``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

CORE_SCHEMA = "fleet-vitals/core@1"
HEADLINE_SCHEMA = "fleet-vitals/headline@1"

# Worst-first; index = severity. Used by roll_up_status and the aggregator.
STATUS_ORDER = ["down", "degraded", "stale", "healthy"]
_VALID_STATUS = set(STATUS_ORDER) | {"unknown", "no-data"}


def utcnow_rfc3339() -> str:
    """Current UTC time as an RFC3339 string (the contract's timestamp format)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def roll_up_status(*statuses: str | None) -> str:
    """Worst-of rollup: down > degraded > stale > healthy.

    Unknown/None values are ignored (a missing dimension must not drag the
    rollup down — that's the staleness layer's job, not the agent's). Returns
    ``"healthy"`` if nothing usable was passed.
    """
    worst = None
    for s in statuses:
        if not s or s not in STATUS_ORDER:
            continue
        if worst is None or STATUS_ORDER.index(s) < STATUS_ORDER.index(worst):
            worst = s
    return worst or "healthy"


def make_core(
    agent: str,
    status: str | None = None,
    *,
    activity: dict | None = None,
    spend: dict | None = None,
    services: dict | None = None,
    generated_at: str | None = None,
    **extra: dict,
) -> dict:
    """Build a ``fleet-vitals/core@1`` dict.

    If ``status`` is omitted it's derived by rolling up the per-dimension
    ``status`` fields of the dimensions you pass. Pass it explicitly to
    override. ``extra`` lets an agent attach future dimensions without a
    helper change.
    """
    dims = {"activity": activity, "spend": spend, "services": services, **extra}
    present = {k: v for k, v in dims.items() if v is not None}
    if status is None:
        status = roll_up_status(*(d.get("status") for d in present.values()))
    if status not in _VALID_STATUS:
        raise ValueError(
            f"invalid status {status!r}; expected one of {sorted(_VALID_STATUS)}"
        )
    return {
        "schema": CORE_SCHEMA,
        "agent": agent,
        "generated_at": generated_at or utcnow_rfc3339(),
        "status": status,
        **present,
    }


def make_headline(
    agent: str,
    title: str,
    value: str,
    *,
    status: str = "healthy",
    trend: str | None = "flat",
    detail: str | None = None,
    link: str | None = None,
    generated_at: str | None = None,
) -> dict:
    """Build a ``fleet-vitals/headline@1`` dict.

    ``value`` is a pre-formatted display string — the agent owns presentation
    so the aggregator never needs to know what the metric means.
    """
    if status not in _VALID_STATUS:
        raise ValueError(
            f"invalid status {status!r}; expected one of {sorted(_VALID_STATUS)}"
        )
    headline = {
        "schema": HEADLINE_SCHEMA,
        "agent": agent,
        "title": title,
        "value": value,
        "status": status,
        "generated_at": generated_at or utcnow_rfc3339(),
    }
    if trend is not None:
        headline["trend"] = trend
    if detail is not None:
        headline["detail"] = detail
    if link is not None:
        headline["link"] = link
    return headline


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` atomically via a temp file in the same directory."""
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content)
    tmp.rename(path)


def emit(vitals_dir: str | Path, core: dict, headline: dict | None = None) -> None:
    """Write ``core.json`` (and optional ``headline.json``) into ``vitals_dir``.

    Creates the directory if needed. This is the one place that decides file
    names, so every agent's layout stays identical for the SSH-pull aggregator.
    Writes are atomic (write-to-temp + rename) so the aggregator never reads a
    partial file. A stale ``headline.json`` is removed when headline is None so
    ``core.json`` and ``headline.json`` stay in sync.
    """
    d = Path(vitals_dir)
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write(d / "core.json", json.dumps(core, indent=2) + "\n")
    if headline is not None:
        _atomic_write(d / "headline.json", json.dumps(headline, indent=2) + "\n")
    else:
        stale = d / "headline.json"
        if stale.exists():
            stale.unlink()


# ---------------------------------------------------------------------------
# Inference-health detector (shared, agent-agnostic)
#
# Every pre-existing fleet health signal measured *liveness* — the service ran,
# a standup was written, ``loggedIn=true`` — none measured whether a model call
# actually succeeded. So an agent's vitals could read ``healthy`` ("4/4
# standups") through a multi-day billing outage with 0 productive sessions and
# thousands of ``api_error_403`` records (the Oct 26 → Nov 5 2026 Alice outage).
#
# This detector reads the agent's ``session-records.jsonl`` (no network, no
# credential — same source every agent already writes) and derives a real
# inference-health status that *names the reason* when it's down. The schema is
# shared across all Superuser Labs agents, so this lives in the shared layer:
# Alice consumes it via her ``inference_health`` shim, Bob/Gordon import these
# functions or call the ``inference`` CLI subcommand below — no per-agent fork.
#
# Status values match the envelope contract above: down > degraded > stale >
# healthy.
# ---------------------------------------------------------------------------


def _parse_record_ts(rec: dict) -> datetime | None:
    ts = rec.get("timestamp") or rec.get("end_time") or rec.get("start_time")
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def authbilling_reason(rec: dict) -> str | None:
    """Return a human label if a session record is an auth/billing failure, else None.

    Covers the 403 class that silently stalled the fleet: billing (payment past
    due) and org-level OAuth refusal (``oauth_not_allowed_for_organization``).
    """
    if rec.get("outcome") != "failed":
        return None
    fr = str(rec.get("failure_reason") or "").lower()
    err_raw = rec.get("error") or ""
    err = (err_raw if isinstance(err_raw, str) else json.dumps(err_raw)).lower()
    is_403 = (
        fr == "api_error_403"
        or "403" in fr
        or "oauth_not_allowed" in err
        or "status:403" in err
        or re.search(r"\b(?:http|status|code)\W{0,3}403\b", err) is not None
        or (isinstance(err_raw, dict) and err_raw.get("status") == 403)
    )
    if not is_403:
        return None
    if any(w in err for w in ("past due", "payment", "billing", "renew")):
        return "403 billing (payment past due)"
    if "oauth_not_allowed" in err:
        return "403 auth (oauth not allowed for org)"
    return "403 auth/billing"


def is_launch_artifact(rec: dict) -> bool:
    """True for records that carry no evidence about inference health.

    Two kinds, both "the model never actually ran for this record":

    - ``model == "<synthetic>"`` without ``outcome == "failed"``: Claude Code
      writes a synthetic assistant turn when the API call itself errors, and a
      trajectory sweep (``gptme-sessions sync``) later imports that transcript
      as a ``noop`` — typically a *duplicate* of the runner's own ``failed``
      record for the same launch, timestamped at launch start (so it sorts just
      before that failure).
    - ``outcome == "skipped"``: the run was gated off before any model call
      (quota/rate-limit), by the recording convention.

    The trailing auth/billing walk must step over these rather than stop on
    them: during the Oct–Nov 2026 billing outage every 403 ``failed`` record had
    a synthetic ``noop`` twin interleaved before it, which cut the streak to one
    record and turned an obvious ``down`` (904 consecutive 403s) into a
    non-escalating ``degraded``.
    """
    if rec.get("outcome") == "skipped":
        return True
    return rec.get("model") == "<synthetic>" and rec.get("outcome") != "failed"


# Outcomes that can carry proof a model call returned. ``failed`` is excluded
# even with tokens (a rate-limited run may have streamed output before dying —
# Erik's contract is "never push on rate-limit-only or all-failed runs"), and
# ``unknown`` is excluded until some agent has validated it against an outage
# window. Widening this set is the one-line change if that validation lands.
MODEL_CALL_OUTCOMES = frozenset({"productive", "noop"})


def model_call_succeeded(rec: dict) -> bool:
    """True when a record proves a model call came back (``output_tokens > 0``).

    This is the "did inference work?" predicate, as distinct from "was the
    session useful?" (``productive``). A noop session that ran the model and
    correctly found nothing to do is a *successful model call*; gating a
    dead-man heartbeat on ``productive`` alone ages it toward a false page on
    every quiet night.

    Validated on two agents before landing: across Alice's Oct 25 → Nov 6 2026
    billing outage no auth/billing failure and no ``<synthetic>`` launch twin
    carried ``output_tokens > 0``; on Bob's records noop 356/368 and productive
    2875/2909 carry tokens while failed only 25/841 do. The converse doesn't
    hold (thin fallback records omit tokens), so this only *adds* evidence —
    its absence never implies an outage.
    """
    outcome = rec.get("outcome")
    # isinstance first: a malformed record (e.g. ``"outcome": []``) is
    # unhashable and must not abort the whole report.
    if not isinstance(outcome, str) or outcome not in MODEL_CALL_OUTCOMES:
        return False
    if is_launch_artifact(rec) or authbilling_reason(rec):
        return False
    try:
        return int(rec.get("output_tokens") or 0) > 0
    except (TypeError, ValueError, OverflowError):  # 1e309 → inf
        return False


def heartbeat_up(status: str, metrics: dict) -> bool:
    """Push decision for a dead-man inference heartbeat (e.g. Uptime Kuma).

    Up when ``healthy`` or ``degraded`` *and no failed session since the newest
    success evidence*; ``degraded`` additionally needs in-window proof a model
    call returned. ``healthy`` gets no exemption from the recency check: a
    productive session at -6h followed by timeouts/403s is still ``healthy``
    (status keeps its productive-in-window contract) but must not push. Never
    on ``down``: the
    field can still be True there (a noop hours before the 403 streak began),
    and pushing would hide the outage. The same logic applies to non-403
    failure runs (rate limit, 5xx, timeouts) after the last success: those
    leave status ``degraded``, so the recency check is what keeps them from
    pushing. Never on ``stale``. Consumers should call this rather than
    re-deriving the policy from ``model_calls_succeeded_in_window`` alone.
    """
    # Missing = recency unknown (e.g. metrics from an older producer): fail
    # closed, since a dead-man heartbeat must not push on doubt.
    if metrics.get("failures_since_last_model_call") != 0:
        return False
    if status == "healthy":
        return True
    return status == "degraded" and bool(metrics.get("model_calls_succeeded_in_window"))


def parse_jsonl_lines(lines: object) -> list[tuple[datetime, dict]]:
    """Parse an iterable of JSONL lines into sorted (timestamp, record) pairs.

    Shared by the path loader and by callers that already hold the records as
    text (e.g. a tail fetched over SSH by a fleet trust layer), so the detector
    logic stays single-source. Unparseable or timestamp-less lines are skipped.
    """
    out: list[tuple[datetime, dict]] = []
    for line in lines:  # type: ignore[attr-defined]
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        ts = _parse_record_ts(rec)
        if ts is None:
            continue
        out.append((ts, rec))
    out.sort(key=lambda x: x[0])
    return out


def inference_status_from_records(
    records: list[tuple[datetime, dict]],
    now: datetime,
    window_hours: int,
) -> tuple[str, str, dict]:
    """Core detector: derive (status, detail, metrics) from parsed records.

    Pure — takes already-loaded records so every caller (path CLI, SSH-tail
    trust layer) shares one implementation.

    - ``healthy``  — at least one productive session in the window.
    - ``degraded`` — 0 productive in the window, no clear auth/billing failure
      streak (idle/noop starvation, not an outage).
    - ``down``     — 0 productive in the window AND a trailing streak of
      auth/billing failures; detail names the reason and how long it's run.
    - ``stale``    — no records at all in the window (loop may not be running).

    ``metrics["model_calls_succeeded_in_window"]`` reports whether any in-window
    record proves a model call returned (see ``model_call_succeeded``); it is
    what separates a quiet ``degraded`` from a broken one. Use
    ``heartbeat_up()`` for the push decision.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=window_hours)

    def _ends_failure_scan(r: dict) -> bool:
        return model_call_succeeded(r) or (
            r.get("outcome") == "productive" and not is_launch_artifact(r)
        )

    # Sort: the recency scan below walks newest-first, and direct callers may
    # pass records in any order (only the JSONL loader pre-sorts). Tie-break:
    # at equal timestamps, success evidence sorts *before* other records, so
    # the newest-first scan counts a same-instant failure before it stops —
    # an unordered tie fails closed instead of depending on input order.
    in_window = sorted(
        ((t, r) for (t, r) in records if cutoff <= t <= now),
        key=lambda x: (x[0], not _ends_failure_scan(x[1])),
    )

    productive = sum(
        1
        for _, r in in_window
        if r.get("outcome") == "productive" and not is_launch_artifact(r)
    )
    model_calls = sum(1 for _, r in in_window if model_call_succeeded(r))
    # Failures *after* the newest model-call success. Old success evidence must
    # not vouch for a run of later failures (rate limit, 5xx, timeouts), so
    # heartbeat_up() requires this to be 0. Window-wide when no success exists.
    # A productive record is success evidence too, even when it omits tokens
    # (thin fallback records), so it also ends the scan.
    failures_since = 0
    for _, r in reversed(in_window):
        if _ends_failure_scan(r):
            break
        if r.get("outcome") == "failed" and not is_launch_artifact(r):
            failures_since += 1
    metrics: dict[str, object] = {
        "window_hours": window_hours,
        "records_in_window": len(in_window),
        "productive_in_window": productive,
        "model_call_records_in_window": model_calls,
        "model_calls_succeeded_in_window": model_calls > 0,
        "failures_since_last_model_call": failures_since,
    }

    if not in_window:
        return "stale", f"no autonomous sessions recorded in {window_hours}h", metrics

    if productive > 0:
        return (
            "healthy",
            f"{productive} productive session(s) in {window_hours}h",
            metrics,
        )

    # 0 productive in window — is there an active auth/billing outage? Walk from
    # the most recent record backwards while it's an auth/billing failure; the
    # start of that trailing streak is when the outage began.
    # Exclude future-dated records: a future productive would terminate the scan
    # early (hiding a real outage), and a future 403 would open a false one.
    streak: list[tuple[datetime, dict]] = []
    reason: str | None = None
    for t, r in reversed(
        sorted([(t, r) for (t, r) in records if t <= now], key=lambda x: x[0])
    ):
        if is_launch_artifact(r):
            continue
        lbl = authbilling_reason(r)
        if lbl is None:
            break
        reason = reason or lbl
        streak.append((t, r))
    if streak:
        since = streak[-1][0]
        metrics["authbilling_streak"] = len(streak)
        metrics["outage_since"] = since.isoformat()
        detail = (
            f"0 productive in {window_hours}h; {reason} since "
            f"{since.astimezone(timezone.utc):%Y-%m-%d %H:%M UTC} ({len(streak)} consecutive failures)"
        )
        return "down", detail, metrics

    detail = f"0 productive sessions in {window_hours}h"
    if model_calls:
        detail += f"; {model_calls} session(s) with model output"
    return "degraded", detail, metrics


def inference_status_from_lines(
    lines: object,
    now: datetime | None = None,
    window_hours: int = 24,
) -> tuple[str, str, dict]:
    """Inference health from JSONL lines already in memory.

    Used by a fleet trust layer that fetches a records tail over SSH rather
    than reading a local path.
    """
    now = now or datetime.now(timezone.utc)
    return inference_status_from_records(parse_jsonl_lines(lines), now, window_hours)


def inference_status_from_path(
    path: str | Path,
    now: datetime | None = None,
    window_hours: int = 24,
) -> tuple[str, str, dict]:
    """Inference health from a ``session-records.jsonl`` path (credential-free)."""
    now = now or datetime.now(timezone.utc)
    p = Path(path)
    if not p.exists():
        return inference_status_from_records([], now, window_hours)
    with p.open() as f:
        return inference_status_from_records(parse_jsonl_lines(f), now, window_hours)


def _inference_cli(argv: list[str] | None = None) -> int:
    """CLI contract for agents that call the detector as a subprocess.

    ``python3 fleet_vitals.py inference --records PATH [--window-hours N] [--json]``

    Prints ``status: detail`` (or JSON with ``--json``) and exits 0 only when
    ``healthy`` — so a shell heartbeat can gate a Kuma ``up`` push on exit code.
    """
    import argparse

    ap = argparse.ArgumentParser(prog="fleet_vitals.py inference")
    ap.add_argument("--records", required=True, help="path to session-records.jsonl")
    ap.add_argument("--window-hours", type=int, default=24)
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)
    status, detail, metrics = inference_status_from_path(
        args.records, window_hours=args.window_hours
    )
    if args.json:
        print(json.dumps({"status": status, "detail": detail, **metrics}))
    else:
        print(f"{status}: {detail}")
    return 0 if status == "healthy" else 1


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "inference":
        raise SystemExit(_inference_cli(sys.argv[2:]))

    # Smoke test / usage example.
    core = make_core(
        "demo",
        activity={"status": "healthy", "today_sessions": 12},
        spend={"status": "degraded", "avg_weekly_usd": 0.5},
    )
    headline = make_headline(
        "demo",
        "Demo metric",
        "+$1,240",
        status="healthy",
        trend="up",
        detail="example",
        link="https://example.com",
    )
    print(json.dumps({"core": core, "headline": headline}, indent=2))
    assert core["status"] == "degraded", "rollup should pick worst dimension"
    print("\nOK: rollup picked worst-of dimension status =", core["status"])

    # Inference detector self-check — the four states (regression guard for the
    # Oct 26 → Nov 5 2026 silent billing outage that read as healthy).
    _now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)

    def _r(hours_ago: float, outcome: str, **extra: object) -> dict:
        return {
            "timestamp": (_now - timedelta(hours=hours_ago)).isoformat(),
            "outcome": outcome,
            **extra,
        }

    def _check(recs: list[dict], expect: str) -> None:
        status, _, _ = inference_status_from_records(
            parse_jsonl_lines(json.dumps(r) for r in recs), _now, 24
        )
        assert status == expect, f"expected {expect}, got {status}"

    _check([_r(2, "productive"), _r(1, "noop")], "healthy")
    _check([_r(5, "noop"), _r(1, "noop")], "degraded")
    _check([_r(40, "productive")], "stale")
    _check(
        [
            _r(h, "failed", failure_reason="api_error_403", error="payment is past due")
            for h in range(5, 0, -1)
        ],
        "down",
    )
    # A productive session after failures = recovered, not down.
    _check(
        [
            _r(5, "failed", failure_reason="api_error_403", error="status:403"),
            _r(2, "productive"),
        ],
        "healthy",
    )
    # Non-string timestamp must not raise — record is skipped, not fatal.
    _check([{"timestamp": 123, "outcome": "productive"}, _r(1, "noop")], "degraded")
    # Future-dated productive record must not mask a real auth/billing outage.
    # With future record excluded from streak scan, the 403 at 2h is correctly detected.
    _check(
        [
            {
                "timestamp": (_now + timedelta(hours=1)).isoformat(),
                "outcome": "productive",
            },
            _r(2, "failed", failure_reason="api_error_403", error="status:403"),
        ],
        "down",
    )
    # Future-dated 403 must not open a false outage when recent activity is benign.
    _check(
        [
            {
                "timestamp": (_now + timedelta(hours=1)).isoformat(),
                "outcome": "failed",
                "failure_reason": "api_error_403",
                "error": "status:403",
            },
            _r(2, "noop"),
        ],
        "degraded",
    )
    # Dict-typed error field must not raise AttributeError in authbilling_reason.
    _check(
        [
            _r(
                2,
                "failed",
                failure_reason="api_error_403",
                error={"message": "payment is past due"},
            )
        ],
        "down",
    )
    # Structured error.status == 403 with no failure_reason must be detected as down.
    # json.dumps({"status": 403}) produces '"status": 403' — the colon-spaced form
    # does not match the "status:403" string check, so we need the dict path.
    _check(
        [
            _r(
                2,
                "failed",
                error={"status": 403, "message": "payment is past due"},
            )
        ],
        "down",
    )
    # Unsorted records must still return the correct status (fix for direct callers).
    # Pass [(11:00, noop), (10:00, 403)] — without sorting, reversed scan treats older
    # 403 as the latest record and would incorrectly return "down".
    _recs_unsorted = [
        (
            _now - timedelta(hours=1),
            {"outcome": "noop", "timestamp": (_now - timedelta(hours=1)).isoformat()},
        ),
        (
            _now - timedelta(hours=2),
            {
                "outcome": "failed",
                "failure_reason": "api_error_403",
                "error": "status:403",
                "timestamp": (_now - timedelta(hours=2)).isoformat(),
            },
        ),
    ]
    status_unsorted = inference_status_from_records(_recs_unsorted, _now, 24)[0]
    assert (
        status_unsorted == "degraded"
    ), f"unsorted input: expected degraded, got {status_unsorted}"
    # Non-string failure_reason (e.g. int 403) must not crash the status call.
    _check([_r(2, "failed", failure_reason=403, error="payment is past due")], "down")
    # Naive `now` is treated as UTC (record timestamps are normalized to UTC).
    status_naive = inference_status_from_lines(
        ['{"timestamp":"2026-11-06T11:00:00","outcome":"productive"}'],
        now=datetime(2026, 11, 6, 12),
    )[0]
    assert status_naive == "healthy", f"naive now: expected healthy, got {status_naive}"
    # Synthetic launch twins interleaved with 403 failures must not break the
    # streak (Oct-Nov 2026 outage: each failure had a `<synthetic>` noop twin
    # timestamped at launch start, i.e. just before it).
    _twin = []
    for h in (5, 3, 1):
        _twin.append(_r(h + 0.01, "noop", model="<synthetic>"))
        _twin.append(
            _r(h, "failed", failure_reason="api_error_403", error="payment past due")
        )
    st, det, met = inference_status_from_records(
        parse_jsonl_lines(json.dumps(x) for x in _twin), _now, 24
    )
    assert st == "down" and met.get("authbilling_streak") == 3, (st, det, met)
    # A skipped (gated, no model call) record between failures is also neutral.
    _check(
        [
            _r(3, "failed", failure_reason="api_error_403"),
            _r(2, "skipped", failure_reason="rate_limited"),
            _r(1, "failed", failure_reason="api_error_403"),
        ],
        "down",
    )
    # But a real noop (a model actually ran) still ends the streak.
    _check(
        [_r(3, "failed", failure_reason="api_error_403"), _r(1, "noop")],
        "degraded",
    )
    # A 403 named only in a free-text error string (no failure_reason) is down.
    _check([_r(1, "failed", error="HTTP 403: payment past due")], "down")
    # A <synthetic> "productive" record carries no inference evidence either.
    _check(
        [
            _r(2, "failed", failure_reason="api_error_403"),
            _r(1, "productive", model="<synthetic>"),
        ],
        "down",
    )
    print("OK: inference detector covers healthy/degraded/stale/down + recovery")
