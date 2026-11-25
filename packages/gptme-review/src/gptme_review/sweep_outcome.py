"""Portable classification of one sweep review attempt's outcome.

``gptme_review.sweep.dispatch_reviews`` schedules host callbacks; each callback
runs the reviewer CLI and has to decide what the result means. This module holds
those decisions so a second sweep host (another agent with its own identity and
key) classifies failures the same way:

* quota/limiter exhaustion versus a broken reviewer, and the early-stop breaker;
* provider-level failures that should route away from the primary transport;
* superseded heads and deferrals, which are not failures at all;
* an all-failed run's alert class;
* per-head failure backoff and quarantine over a host-owned state mapping;
* a process-group runner so a timed-out review cannot leave a model session
  spending quota after the sweep has abandoned it.

Hosts own state storage, alerting, accounting and fallback policy. Nothing here
reads files, environment variables or an agent identity.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from collections.abc import Mapping, MutableMapping, Sequence
from datetime import datetime, timedelta

from gptme_review.core import is_model_refusal
from gptme_review.shard import DEFERRED_SENTINEL, EXIT_DEFERRED

#: Quota exhaustion is transient infrastructure state, not a broken reviewer.
QUOTA_LIMIT_MARKERS = (
    "rate limit",
    "site-policy",
    "scraping github",
    "key limit exceeded",
    "daily limit",
    # OpenRouter's account-wide cap is worded differently from its per-key cap:
    # "Workspace daily budget of $10.00 exceeded" (HTTP 403). Without this the
    # openrouter_workspace_budget refusal reads as a systemic reviewer collapse
    # and the sweep unit exits 1 for a quota event that resets on a clock.
    "workspace daily budget",
    "openrouter_workspace_budget",
    "insufficient credits",
    "insufficient balance",
    "openrouter_credits",
)

#: Prose markers alone lose this race: providers word exhaustion their own way
#: and OpenRouter passes upstream messages through verbatim. The status code does
#: not vary: 402 (payment required) and 429 (too many requests) are billing or
#: quota by definition, never a broken reviewer.
_QUOTA_HTTP_STATUS_RE = re.compile(r"\bhttp[ _-]?(?:402|429)\b")

#: Failures that identify the primary transport rather than a particular PR. A
#: concurrent sweep can have several successful jobs while one request hangs;
#: requiring every selected PR to fail would make a fallback dead exactly when
#: bounded concurrency masks a provider outage with partial success.
PRIMARY_PROVIDER_FAILURE_MARKERS = (
    "openrouter did not respond",
    "openrouter timed out",
    "openrouter http",
    "openrouter request failed:",
)

#: Consecutive quota-limited failures that mean the limiter itself is tripped.
#: Two in a row can be two providers being independently unlucky; three with
#: zero successes cannot.
QUOTA_BREAKER_CONSECUTIVE = 3

#: The reviewer names the OpenRouter provider in its empty-content error:
#: ``The provider ('Io Net') returned no answer``.
_FAILED_PROVIDER_RE = re.compile(r"provider \('([^']+)'\)")

#: The reviewer refuses to publish when the PR head moves mid-review. That is the
#: reviewer working correctly; the next sweep reviews the new head.
SUPERSEDED_HEAD_MARKER = "while this review was running"

#: How long a failed head is deprioritized, and how many failures on one head
#: quarantine it until the backoff lapses.
DEFAULT_FAILURE_BACKOFF = timedelta(hours=6)
DEFAULT_QUARANTINE_STRIKES = 3


def is_quota_limited(text: str) -> bool:
    lowered = (text or "").lower()
    if _QUOTA_HTTP_STATUS_RE.search(lowered):
        return True
    return any(marker in lowered for marker in QUOTA_LIMIT_MARKERS)


def is_primary_provider_failure(failure: Mapping[str, object]) -> bool:
    """Should this failure route the next attempt away from the primary engine?

    A refusal is the model or provider declining, not a property of the diff, so
    it counts too. Oversized PRs and non-``llm`` engines never do.
    """
    if failure.get("engine") != "llm" or failure.get("too_large"):
        return False
    reason = str(failure.get("reason") or "").lower()
    return (
        bool(failure.get("rate_limited"))
        or is_model_refusal(reason)
        or any(marker in reason for marker in PRIMARY_PROVIDER_FAILURE_MARKERS)
    )


def quota_breaker_tripped(consecutive_quota_failures: int, successes: int) -> bool:
    """Stop the sweep early? Only when nothing at all has gone through.

    A single success proves the path works, so later quota failures are
    per-PR/per-provider noise and the run should keep going.
    """
    return successes == 0 and consecutive_quota_failures >= QUOTA_BREAKER_CONSECUTIVE


def is_superseded_head(stderr_text: str) -> bool:
    return SUPERSEDED_HEAD_MARKER in (stderr_text or "")


def failed_provider(stderr_text: str) -> str | None:
    m = _FAILED_PROVIDER_RE.search(stderr_text or "")
    return m.group(1) if m else None


def failure_reason(
    returncode: int | None, stdout: str | None, stderr: str | None
) -> str:
    """Why the reviewer failed, without ever losing the exit status.

    A bare ``no output`` collapses an OOM SIGKILL, a service stop and a clean
    non-zero exit into one shape; the signal name keeps them distinguishable.
    """
    # Whitespace-only stderr must not hide the cause printed on stdout.
    text = (stderr or "").strip() or (stdout or "").strip()
    tail = text.splitlines()
    last = tail[-1] if tail else None
    if returncode is not None and returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = f"signal {-returncode}"
        # Progress lines on stdout are not the cause of a kill; keep the signal.
        if last:
            return f"killed by {name} (exit {returncode}): {last}"
        return f"killed by {name} (exit {returncode}) with no output"
    if last:
        return last
    return f"exited {returncode} with no output"


def is_deferred_review(returncode: int | None, stderr: str | None) -> bool:
    """The reviewer declined to start (budget/wall clock); nothing was spent."""
    return returncode == EXIT_DEFERRED and DEFERRED_SENTINEL in (stderr or "")


def is_subscription_capacity_shortfall(text: str) -> bool:
    """The consensus routing contract refused the request before any spend.

    Matches the refusal ``gptme_review.routing`` raises when too few independent
    reviewer families are admitted.
    """
    lowered = (text or "").lower()
    return "independent reviewer" in lowered and "are available" in lowered


def classify_all_failed_run(
    *,
    attempted: int,
    failures: int,
    rate_limit_failures: int,
    too_large_failures: int,
    capacity_failures: int = 0,
) -> str | None:
    """Classify an all-failed sweep for alerting and exit policy.

    Returns ``None`` unless every attempted review failed. A singleton miss is
    noisy but not systemic; reserve ``systemic`` for multi-review collapse that
    no known transient class explains. A collapse explained only by a mix of
    known classes that includes a capacity refusal is ``transient_mix``.
    """
    if not attempted or failures != attempted:
        return None
    if attempted == 1:
        return "singleton"
    if rate_limit_failures == attempted:
        return "rate_limited"
    if too_large_failures == attempted or (
        rate_limit_failures + too_large_failures == attempted
    ):
        return "pr_local"
    if capacity_failures == attempted:
        return "capacity"
    if rate_limit_failures + too_large_failures + capacity_failures == attempted:
        return "transient_mix"
    return "systemic"


def failure_key(pr: Mapping[str, object]) -> str:
    return f"{pr['repo']}#{pr['number']}"


def _current_head_row(
    pr: Mapping[str, object], state: Mapping[str, object]
) -> Mapping[str, object] | None:
    row = state.get(failure_key(pr))
    if not isinstance(row, Mapping) or row.get("head") != pr.get("headRefOid"):
        return None
    return row


def recently_failed(
    pr: Mapping[str, object],
    state: Mapping[str, object],
    now: datetime,
    *,
    backoff: timedelta = DEFAULT_FAILURE_BACKOFF,
) -> bool:
    """True when this PR's current head failed a review within ``backoff``."""
    row = _current_head_row(pr, state)
    if row is None:
        return False
    try:
        return now - datetime.fromisoformat(str(row.get("at"))) < backoff
    except (TypeError, ValueError):
        return False


def failure_count(pr: Mapping[str, object], state: Mapping[str, object]) -> int:
    """How many times this PR's current head has failed; 0 if the head changed."""
    row = _current_head_row(pr, state)
    if row is None:
        return 0
    try:
        count = row.get("count") or 0
        return int(count)  # type: ignore[call-overload,no-any-return]
    except (TypeError, ValueError):
        return 0


def quarantined_head(
    pr: Mapping[str, object],
    state: Mapping[str, object],
    now: datetime,
    *,
    backoff: timedelta = DEFAULT_FAILURE_BACKOFF,
    strikes: int = DEFAULT_QUARANTINE_STRIKES,
) -> bool:
    """True when this head has struck out and the backoff has not lapsed.

    A human review request overriding quarantine is host policy, so this stays a
    pure ``(pr, state, now)`` check.
    """
    return recently_failed(pr, state, now, backoff=backoff) and (
        failure_count(pr, state) >= strikes
    )


def record_failure(
    state: MutableMapping[str, object], pr: Mapping[str, object], now: datetime
) -> None:
    """Count one more failure on the PR's current head; a new head restarts at 1."""
    key = failure_key(pr)
    prev = state.get(key)
    prev_row = prev if isinstance(prev, Mapping) else {}
    head = pr.get("headRefOid")
    count = failure_count(pr, state) + 1 if prev_row.get("head") == head else 1
    state[key] = {"head": head, "at": now.isoformat(), "count": count}


def clear_failure(state: MutableMapping[str, object], pr: Mapping[str, object]) -> bool:
    """Forget the PR's failure row. Returns whether anything was removed."""
    return state.pop(failure_key(pr), None) is not None


def run_process_group(
    cmd: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run a reviewer in its own process group and kill the whole group on timeout.

    Agent-engine reviews spawn a model session. Killing only the direct child
    leaves that descendant spending quota after the sweep has abandoned it.
    ``TimeoutExpired`` is re-raised with the captured output attached.
    """
    with subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # the group finished between timeout and cleanup
            stdout, stderr = proc.communicate()
            # TimeoutExpired stores bytes even when the process uses text mode.
            exc.output, exc.stderr = stdout.encode(), stderr.encode()
            raise
        return subprocess.CompletedProcess(list(cmd), proc.returncode, stdout, stderr)
