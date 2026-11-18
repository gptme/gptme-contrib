"""Portable fallback-engine orchestration for a review sweep.

After ``gptme_review.sweep.dispatch_reviews`` has run every selected PR on the
primary engine, a sweep host may retry some failures on a fallback engine (Bob
retries OpenRouter failures on subscription reviewers). Two decisions here are
policy every host should share:

* :func:`select_fallback_failures` — which primary failures are a transport
  problem worth retrying, and which are PR-local failures that must stay failed;
* :func:`run_fallback` — the bounded retry loop: stop when the fallback pool is
  empty or the sweep's admission budget refuses, never replace an existing
  same-head marker with weaker evidence, and report which failure rows were
  replaced and how many PRs recovered.

Hosts own reviewer routing, command construction, process execution, attempt
ledgers, publication and stdout capture, all through :class:`FallbackHost`.
Nothing here reads files, environment variables or an agent identity.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from gptme_review.sweep_outcome import (
    failed_provider,
    failure_reason,
    is_primary_provider_failure,
    is_quota_limited,
)

#: Reviewer stderr marker for a PR whose diff GitHub refuses to serve. A
#: property of the PR, not of the review infrastructure.
TOO_LARGE_MARKER = "too_large"


def is_too_large(text: str) -> bool:
    return TOO_LARGE_MARKER in (text or "").lower()


class FallbackSkip(Exception):
    """Raised by :meth:`FallbackHost.resolve_model` when routing refuses one PR.

    The loop skips that PR and continues; it is not a reason to stop.
    """


def select_fallback_failures(
    *,
    failure_class: str | None,
    review_failures: Sequence[Mapping[str, object]],
    too_large_failures: int,
) -> tuple[str | None, list[Mapping[str, object]]]:
    """Return ``(failure_class, failures to retry)`` for a finished primary run.

    * An all-rate-limited run, or a systemic one with no too-large PR, retries
      every failure.
    * A run that did not fail wholesale retries only failures that identify the
      primary provider; the class becomes ``provider_outage`` when there are any.
      Concurrency can mask a provider outage behind partial success. A
      co-occurring too-large PR does not block this: the provider filter
      already excludes too-large rows, so they stay failed either way.
    * Anything else (mixed PR-local failures) retries nothing: that is not a
      transport outage, so the fallback stays fail-closed.
    """
    if failure_class == "rate_limited" or (
        failure_class == "systemic" and not too_large_failures
    ):
        return failure_class, list(review_failures)
    if failure_class is None:
        provider = [f for f in review_failures if is_primary_provider_failure(f)]
        return ("provider_outage" if provider else None), provider
    return failure_class, []


def head_already_reviewed(pr: Mapping[str, object]) -> bool:
    """Whether a failed run was a same-head upgrade of an existing marker.

    Such a head has no coverage gap to recover. A single-job fallback would only
    replace a stronger marker with weaker evidence and re-arm the upgrade loop.
    An explicit human request still gets a fallback.
    """
    reviewed_sha = str(pr.get("reviewed_sha") or "")
    return bool(
        reviewed_sha
        and str(pr.get("headRefOid") or "").startswith(reviewed_sha)
        and not pr.get("requested_by")
    )


class FallbackHost(Protocol):
    """Host callbacks for :func:`run_fallback`."""

    def available_models(self) -> list[str]:
        """Fallback reviewers admitted right now, best first. Empty stops the loop."""
        ...

    def admission(self, pr: Mapping[str, object]) -> str | None:
        """Why the sweep cannot start this retry now (stops the loop), or None."""
        ...

    def resolve_model(self, pr: Mapping[str, object]) -> str:
        """The admitted fallback reviewer for this PR; raise FallbackSkip to skip it."""
        ...

    def execute(
        self, pr: Mapping[str, object], model: str
    ) -> subprocess.CompletedProcess:
        """Run the fallback review. Convert timeouts into a failed process."""
        ...

    def on_recovered(
        self, pr: Mapping[str, object], proc: subprocess.CompletedProcess
    ) -> None:
        """Publish/finish a successful fallback review."""
        ...

    def on_failed(
        self, pr: Mapping[str, object], proc: subprocess.CompletedProcess
    ) -> dict[str, object]:
        """Extra fields for the new failure row (e.g. a stdout capture path)."""
        ...


@dataclass
class FallbackResult:
    """What the fallback changed, for the host's counters and alert metadata."""

    engine: str
    models: list[str]
    attempted: int = 0
    recovered: int = 0
    skipped: int = 0
    stop_reason: str | None = None
    #: Primary failure rows the fallback attempted; they no longer stand alone.
    replaced: list[Mapping[str, object]] = field(default_factory=list)
    #: New failure rows for attempts that also failed on the fallback engine.
    added: list[dict[str, object]] = field(default_factory=list)

    def metadata(self) -> dict[str, object]:
        """The ``fallback`` block for alert metadata."""
        data: dict[str, object] = {
            "engine": self.engine,
            "model": self.models[0] if self.models else None,
            "models": list(self.models),
            "attempted": self.attempted,
            "recovered": self.recovered,
            "skipped": self.skipped,
        }
        if self.stop_reason is not None:
            data["stop_reason"] = self.stop_reason
        return data


def run_fallback(
    failures: Sequence[Mapping[str, object]],
    prs_by_label: Mapping[str, Mapping[str, object]],
    host: FallbackHost,
    *,
    engine: str,
    emit: Callable[[str], None] = print,
) -> FallbackResult:
    """Retry each failure on the fallback engine, in order.

    ``failures`` rows carry a ``pr`` label (``owner/repo#N``) keyed into
    ``prs_by_label``. Quota and admission are rechecked before every attempt,
    so a pool that empties mid-loop stops the loop rather than spending
    attempts that cannot be admitted.
    """
    result = FallbackResult(
        engine=engine, models=host.available_models(), skipped=len(failures)
    )
    for failure in failures:
        if not host.available_models():
            result.stop_reason = f"{engine} quota unavailable"
            emit(f"  fallback {engine} skipped: {result.stop_reason}")
            break
        label = str(failure["pr"])
        pr = prs_by_label[label]
        if head_already_reviewed(pr):
            emit(f"  fallback skipped {label}: head already reviewed")
            continue
        blocked = host.admission(pr)
        if blocked:
            result.stop_reason = blocked
            emit(f"  fallback {engine} stopped: {blocked}")
            break
        try:
            model = host.resolve_model(pr)
        except FallbackSkip as exc:
            emit(f"  fallback skipped {label}: {exc}")
            continue
        emit(
            f"  retrying {label} [fallback] head={pr.get('headRefOid')} "
            f"engine={engine} model={model} …"
        )
        result.attempted += 1
        result.skipped -= 1
        proc = host.execute(pr, model)
        result.replaced.append(failure)
        if proc.returncode == 0:
            result.recovered += 1
            host.on_recovered(pr, proc)
            continue
        stderr_text = (proc.stderr or proc.stdout or "").strip()
        row: dict[str, object] = {
            "pr": label,
            "engine": engine,
            "reason": failure_reason(proc.returncode, proc.stdout, proc.stderr)[:300],
            "provider": failed_provider(stderr_text),
            "rate_limited": is_quota_limited(stderr_text),
            "too_large": is_too_large(stderr_text),
        }
        row.update(host.on_failed(pr, proc))
        result.added.append(row)
    emit(
        f"  fallback engine={engine}: recovered {result.recovered}/"
        f"{len(failures)} PR(s); attempted {result.attempted}, "
        f"skipped {result.skipped}"
    )
    return result
