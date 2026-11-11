"""Portable review admission: which transport may serve a review request.

Automatic routing changes transport on a known OpenRouter outage, never the
requested evidence. Explicit engines/models remain explicit. No inference or
review publication occurs here; the execution boundary rechecks admission.

The subscription side (a roster of CLI-backed reviewers with quota and pacing
state) is host-specific. This module owns the decision logic and takes the host
pieces through a :class:`RoutingBackend`. The default :class:`OpenRouterBackend`
has no subscription reviewers, so a host without a panel (a peer agent running
its own sweep) gets the ``llm`` and ``agent`` routes and a clear refusal for
``subscription``.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, TypeVar

from .core import (
    CONSENSUS_MIN_AGREEMENT,
    CONSENSUS_PASSES,
    PRODUCTION,
    ReviewError,
    ReviewTargetState,
    repo_is_sensitive,
)


class Reviewer(Protocol):
    """What routing reads from a subscription reviewer; hosts supply the type."""

    key: str
    family: str
    transport: str


R = TypeVar("R", bound=Reviewer)


class RoutingBackend(Protocol):
    """Host-specific inputs to route admission.

    ``Any`` stands for the host's reviewer type; routing only reads the
    attributes in :class:`Reviewer`.
    """

    def openrouter_unavailable(self) -> bool:
        """Known account or REVIEW-key outage; no remote billing call."""

    def repo_is_sensitive(self, repo: str) -> bool: ...

    def reviewer_for_model_spec(self, spec: str) -> Any | None: ...

    def roster(self) -> Sequence[Any]: ...

    def available_reviewers(
        self,
        reviewers: Sequence[Any],
        *,
        repo: str,
        require_copilot_opt_in: bool,
    ) -> tuple[list[Any], dict[str, str]]: ...

    def private_fallback_reviewers(
        self, reviewers: Sequence[Any]
    ) -> tuple[list[Any], str]: ...

    def overflow_reviewers(self, repo: str) -> tuple[list[Any], dict[str, str]]:
        """Extra family offered to an unpinned 2-of-3 request that has only two."""


class OpenRouterBackend:
    """Backend for hosts with no subscription panel."""

    def openrouter_unavailable(self) -> bool:
        return False

    def repo_is_sensitive(self, repo: str) -> bool:
        return repo_is_sensitive(repo)

    def reviewer_for_model_spec(self, spec: str) -> Any | None:
        return None

    def roster(self) -> Sequence[Any]:
        return ()

    def available_reviewers(
        self,
        reviewers: Sequence[Any],
        *,
        repo: str,
        require_copilot_opt_in: bool,
    ) -> tuple[list[Any], dict[str, str]]:
        return [], {}

    def private_fallback_reviewers(
        self, reviewers: Sequence[Any]
    ) -> tuple[list[Any], str]:
        return [], "no subscription reviewers configured"

    def overflow_reviewers(self, repo: str) -> tuple[list[Any], dict[str, str]]:
        return [], {}


@dataclass
class ReviewRoute:
    engine: str
    reason: str
    reviewers: list[Any] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


def distinct_families(reviewers: Sequence[R]) -> list[R]:
    """First reviewer per family, in roster order: two OpenAI arms are one opinion."""
    distinct: list[R] = []
    for reviewer in reviewers:
        if reviewer.family not in {r.family for r in distinct}:
            distinct.append(reviewer)
    return distinct


#: Quorum modes a dispatcher can observe. ``degraded-1`` admits a dispatch with
#: exactly one independent reviewer family under a stricter bar; it never
#: carries merge authority. ``unknown`` is a probe failure and fails open to a
#: normal dispatch (an unreadable route must not suppress real work).
QUORUM_MODES = ("full", "degraded-1", "held-until-T", "unknown")

#: The degraded bar: one family, one pass, no consensus claim and no self-merge.
DEGRADED_PASSES = 1
DEGRADED_MIN_AGREEMENT = 1

_SKIP_SECONDS = re.compile(r"skip (\d+)s more")


@dataclass
class ConsensusAdmission:
    """Whether a dispatcher may spend a slot on the consensus route, and how."""

    mode: str
    reason: str | None = None
    families: tuple[str, ...] = ()
    merge_authority: bool = False
    until: str | None = None
    passes: int = DEGRADED_PASSES
    min_agreement: int = DEGRADED_MIN_AGREEMENT
    cause: str | None = None

    @property
    def dispatchable(self) -> bool:
        return self.mode in {"full", "degraded-1", "unknown"}

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "families": list(self.families),
            "merge_authority": self.merge_authority,
            "until": self.until,
            "passes": self.passes,
            "min_agreement": self.min_agreement,
            "cause": self.cause,
        }


def resolve_route(
    repo: str,
    *,
    backend: RoutingBackend,
    engine: str = "auto",
    model: str | None = None,
    passes: int = 1,
    min_agreement: int = 1,
    openrouter_blocked: bool | None = None,
) -> ReviewRoute:
    """Admit the exact request or raise before any diff reaches inference."""
    if engine not in {"auto", "llm", "subscription", "agent"}:
        raise ReviewError(f"unknown review engine: {engine}")
    if passes < 1 or min_agreement < 1 or min_agreement > passes:
        raise ReviewError("review agreement must be between 1 and requested passes")
    if engine == "agent":
        return ReviewRoute("agent", "explicit agent engine")
    blocked = (
        backend.openrouter_unavailable()
        if openrouter_blocked is None
        else openrouter_blocked
    )
    pinned = backend.reviewer_for_model_spec(model) if model else None
    if engine == "auto":
        # A pinned model is an explicit route; never silently replace it.
        engine = "subscription" if pinned or (blocked and not model) else "llm"
    if engine == "llm":
        if blocked:
            raise ReviewError(
                "OpenRouter account-credits or REVIEW-key block active; explicit llm/model request refused"
            )
        return ReviewRoute("llm", "OpenRouter available")
    if model and pinned is None:
        raise ReviewError(f"unknown subscription reviewer: {model}")
    # A subscription-pinned Copilot vote is the merge-gate independent
    # family, not a sweep reviewer. Waive the opt-in flag only for that
    # pin; public-repo and quota/pacing checks still apply.
    copilot_opt_in = not (
        pinned is not None
        and pinned.transport == "copilot"
        and engine == "subscription"
    )
    available, skipped = backend.available_reviewers(
        (pinned,) if pinned else backend.roster(),
        repo=repo,
        require_copilot_opt_in=copilot_opt_in,
    )
    try:
        sensitive = backend.repo_is_sensitive(repo)
    except (OSError, ValueError):
        sensitive = True
    if sensitive:
        allowed, why = backend.private_fallback_reviewers(available)
        skipped = {
            **skipped,
            **{
                r.key: "private/unknown repository policy"
                for r in available
                if r not in allowed
            },
        }
        available = allowed
        if not available:
            raise ReviewError(
                f"subscription review refused for private/unknown repository: {why}"
            )
    independent = distinct_families(available)
    # Overflow gives an unpinned 2-of-3 request a fourth family when the
    # default roster left exactly two. The backend decides whether the extra
    # family exists (the brain keeps the Copilot opt-in on it, so with the opt-in
    # off this path cannot add the third family and resolve_route refuses; that
    # refusal is intended, see idea backlog #6079 and
    # knowledge/analysis/2026-10-20-two-family-overflow-contract.md).
    if (
        not pinned
        and not sensitive
        and passes == 3
        and min_agreement == 2
        and len(independent) == 2
    ):
        extra, extra_skipped = backend.overflow_reviewers(repo)
        skipped = {**skipped, **extra_skipped}
        independent = distinct_families([*independent, *extra])
    if len(independent) < passes:
        raise ReviewError(
            f"subscription review requested {min_agreement}-of-{passes} consensus "
            f"but only {len(independent)} independent reviewer(s) are available "
            f"({', '.join(r.key for r in independent) or 'none'}); skipped: {skipped}"
        )
    return ReviewRoute(
        "subscription",
        "OpenRouter outage" if blocked else "subscription requested",
        independent[:passes],
        skipped,
    )


def resolve_cli_route(
    argv: list[str],
    *,
    resolve: Callable[..., ReviewRoute],
) -> ReviewRoute:
    """Resolve a route from the detached wrapper's argv; raises ReviewError.

    ``resolve`` is the host's ``resolve_route`` (backend already bound), looked
    up by the caller at call time.
    """
    # Expand in place, matching the detached wrapper's last-option-wins argv.
    expanded: list[str] = []
    for arg in argv:
        if arg == "--consensus":
            expanded.extend(
                PRODUCTION.resolve(ReviewTargetState(consensus_upgrade=True)).cli_args()
            )
        else:
            expanded.append(arg)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument(
        "--engine", choices=("auto", "llm", "subscription", "agent"), default="auto"
    )
    parser.add_argument("--model")
    parser.add_argument("--passes", type=int, default=CONSENSUS_PASSES)
    parser.add_argument("--min-agreement", type=int, default=None)
    args, _ = parser.parse_known_args(expanded)
    if args.min_agreement is None:
        args.min_agreement = min(CONSENSUS_MIN_AGREEMENT, args.passes)
    return resolve(
        args.repo,
        engine=args.engine,
        model=args.model,
        passes=args.passes,
        min_agreement=args.min_agreement,
    )


def consensus_route_refusal(
    repo: str, *, resolve_cli: Callable[[list[str]], ReviewRoute]
) -> str | None:
    """Why ``ai-review-and-merge.sh --consensus`` would refuse *repo* now.

    ``None`` means the production consensus route is admitted. This is the
    **merge-authority** question: a degraded route still returns a refusal, so
    merge callers keep their 2-of-3 bar. Dispatch callers should ask
    :func:`consensus_admission` instead, which distinguishes "one family, so a
    single-pass review is still possible" from "no family at all".
    """
    try:
        resolve_cli([repo, "--consensus"])
    except ReviewError as exc:
        return str(exc)
    return None


def _hold_until(reason: str | None, now: datetime | None = None) -> str | None:
    """Earliest known reviewer return time encoded in a refusal reason."""
    if not reason:
        return None
    seconds = [int(m) for m in _SKIP_SECONDS.findall(reason)]
    if not seconds:
        return None
    base = now or datetime.now(timezone.utc)
    return (base + timedelta(seconds=min(seconds))).isoformat()


def consensus_admission(
    repo: str,
    *,
    resolve_cli: Callable[[list[str]], ReviewRoute],
    now: datetime | None = None,
) -> ConsensusAdmission:
    """Can a dispatch slot make progress on *repo* under the current reviewer set?

    Three honest outcomes for the *dispatch* question, decided from the same
    resolver the execution boundary rechecks:

    * ``full`` — the 2-of-3 consensus route admits; dispatch and merge normally.
    * ``degraded-1`` — the consensus route refuses while at least one reviewer
      family is available (a 2-of-3 sample needs three *available* families,
      so a two-family day lands here too). Dispatch one pass — the bar
      ``ai-review.py --engine subscription --passes 1 --min-agreement 1
      --outage-single-pass`` labels as outage coverage, not merge evidence —
      and never self-merge (``merge_authority=False``). ``families`` records
      the reviewer(s) that degraded pass will use.
    * ``held-until-T`` — no reviewer family at all; hold the slot, retrying for
      free at ``until`` when the refusal encodes a known return time.

    ``unknown`` means the probe itself failed and the caller should fail open
    (dispatch) rather than suppress real work.
    """
    try:
        resolve_cli([repo, "--consensus"])
    except ReviewError as exc:
        reason = str(exc)
    except Exception as exc:  # noqa: BLE001 — an unknown route must not hold work
        return ConsensusAdmission(
            "unknown",
            f"route probe failed: {type(exc).__name__}: {exc}",
            cause="probe_error",
        )
    else:
        return ConsensusAdmission(
            "full", None, merge_authority=True, cause="route_admitted"
        )

    try:
        single = resolve_cli(
            [
                repo,
                "--passes",
                str(DEGRADED_PASSES),
                "--min-agreement",
                str(DEGRADED_MIN_AGREEMENT),
            ]
        )
    except ReviewError:
        return ConsensusAdmission(
            "held-until-T",
            reason,
            until=_hold_until(reason, now),
            cause="no_reviewer_family",
        )
    except Exception as exc:  # noqa: BLE001
        return ConsensusAdmission(
            "unknown",
            f"degraded probe failed: {type(exc).__name__}: {exc}",
            cause="probe_error",
        )
    if single.engine != "subscription" or not single.reviewers:
        # The single-pass probe resolved outside subscription (e.g. llm) or
        # returned no reviewers. This can happen when the first probe failed
        # for a config reason while OpenRouter is available, in which case the
        # consensus route is still refused. Re-probe before granting merge
        # authority; return unknown on a second refusal so callers dispatch
        # but do not self-merge.
        try:
            resolve_cli([repo, "--consensus"])
        except Exception:  # noqa: BLE001
            return ConsensusAdmission(
                "unknown",
                f"consensus route still refused: {reason}",
                cause="probe_error",
            )
        return ConsensusAdmission(
            "full", None, merge_authority=True, cause="route_admitted"
        )
    return ConsensusAdmission(
        "degraded-1",
        reason,
        tuple(r.family for r in single.reviewers),
        merge_authority=False,
        cause="single_family_available",
    )


def main(
    argv: list[str] | None = None,
    *,
    resolve_cli: Callable[[list[str]], ReviewRoute],
) -> int:
    try:
        route = resolve_cli(sys.argv[1:] if argv is None else argv)
    except ReviewError as exc:
        print(f"review route refused: {exc}", file=sys.stderr)
        return 75
    print(route.engine)
    return 0
