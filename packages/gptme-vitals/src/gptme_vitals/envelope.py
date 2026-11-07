"""The typed page envelope every ``gptme-vitals`` dashboard page emits.

Why this exists
---------------
``DashboardRoute.summary_source`` is an untyped promise, so every page invents
its own summary shape and the portal grows one parser per page. Measured on
Bob's workspace: four pages, four schemas, and a portal 14x past the 100-160
line spec in ``skills/agent-dashboard-setup/SKILL.md``. A typed envelope gives
the portal one renderer and makes portal-pollution structurally impossible.

Two properties are deliberate and load-bearing:

**The status is an enum, validated at construction.** ``fleet_vitals``
validates the *top-level* ``core.json`` status but never the *per-dimension*
one — those pass straight into :func:`roll_up_status`, which silently skips
anything outside ``STATUS_ORDER``. A derived ``core.json`` turns pages into
dimensions, landing exactly on the unvalidated layer: a page reporting
``critical`` would be dropped on the floor and the rollup would still report a
plausible status. Constructing a :class:`PageEnvelope` with an unrecognised
status therefore raises.

**``detail`` and ``core`` are separate fields, not one filtered payload.**
``detail`` is the un-projected page payload and is written only to the private
root; ``core`` is the explicit allowlisted projection and is the only thing
that reaches the served tree or a core dimension. Separate fields mean there is
no code path from private to public that a forgotten filter can open. This is
the shape ``metaproductivity/error_lifecycle_publication.py`` already uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

__all__ = [
    "ENVELOPE_SCHEMA",
    "MAX_LINES",
    "RANKABLE_STATUSES",
    "DimensionRollup",
    "PageEnvelope",
    "Status",
    "roll_up_envelopes",
    "utcnow_rfc3339",
]

ENVELOPE_SCHEMA = "gptme-vitals/page@1"

#: Maximum ``lines`` entries. The portal renders these verbatim; the cap is
#: what keeps a page from turning the portal card back into a page.
MAX_LINES = 4


class Status(StrEnum):
    """The contract's status vocabulary.

    The first four are ``fleet_vitals.STATUS_ORDER`` verbatim (worst first) and
    are the only rankable values. ``UNKNOWN``/``NO_DATA`` are legal to emit but
    carry no severity — see :func:`roll_up_envelopes` for how they are counted
    rather than ranked.

    Deliberately closed: every added value is one some other agent's rollup
    will skip. A page that means "this only applies sometimes" puts that in
    ``headline``/``lines``; a page that means "this is bad" picks a severity.
    """

    DOWN = "down"
    DEGRADED = "degraded"
    STALE = "stale"
    HEALTHY = "healthy"
    UNKNOWN = "unknown"
    NO_DATA = "no-data"


#: Worst-first severity ranking. Mirrors ``fleet_vitals.STATUS_ORDER``; the
#: parity test in ``tests/test_envelope.py`` fails if the two ever drift.
RANKABLE_STATUSES: tuple[Status, ...] = (
    Status.DOWN,
    Status.DEGRADED,
    Status.STALE,
    Status.HEALTHY,
)


def utcnow_rfc3339() -> str:
    """Current UTC time in the contract's timestamp format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class PageEnvelope:
    """One dashboard page's summary, in the one shape the portal understands.

    :param slug: matches ``DashboardRoute.slug``.
    :param status: a :class:`Status`; an unrecognised value raises here rather
        than being silently skipped by a downstream rollup.
    :param headline: pre-formatted display string. The page owns presentation
        so no consumer needs to know what the metric means.
    :param lines: at most :data:`MAX_LINES` short strings — the portal card's
        entire body for this page.
    :param detail: un-projected payload. **Private root only.**
    :param core: explicit allowlisted projection (enums and numbers). The only
        field that may reach a served tree or a ``core.json`` dimension.
    """

    slug: str
    status: Status
    headline: str
    generated_at: str = field(default_factory=utcnow_rfc3339)
    lines: tuple[str, ...] = ()
    detail: dict | None = None
    core: dict | None = None

    def __post_init__(self) -> None:
        if not self.slug or not self.slug.strip():
            raise ValueError("slug must be a non-empty string")
        # Runtime belt for callers that are not type-checked (a status read
        # out of JSON, say): Status(...) raises on an unrecognised value rather
        # than letting it reach a rollup that would silently skip it.
        object.__setattr__(self, "status", Status(self.status))
        if not isinstance(self.headline, str):
            raise TypeError(
                f"headline must be a str, got {type(self.headline).__name__}"
            )
        lines = tuple(self.lines)
        if len(lines) > MAX_LINES:
            raise ValueError(
                f"{self.slug}: {len(lines)} lines exceeds MAX_LINES={MAX_LINES}; "
                "a portal card is a summary, not a page"
            )
        if any(not isinstance(line, str) for line in lines):
            raise TypeError(f"{self.slug}: every entry in lines must be a str")
        object.__setattr__(self, "lines", lines)

    @property
    def is_rankable(self) -> bool:
        """Whether this status carries severity (vs. being an absence of signal)."""
        return self.status in RANKABLE_STATUSES

    def to_public(self) -> dict[str, object]:
        """The served-tree representation. ``detail`` is structurally absent."""
        payload: dict[str, object] = {
            "schema": ENVELOPE_SCHEMA,
            "slug": self.slug,
            "generated_at": self.generated_at,
            "status": str(self.status),
            "headline": self.headline,
            "lines": list(self.lines),
        }
        if self.core is not None:
            payload["core"] = self.core
        return payload

    def to_private(self) -> dict[str, object]:
        """The private-root representation, including ``detail``."""
        payload = self.to_public()
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True, slots=True)
class DimensionRollup:
    """The result of rolling several page envelopes into one core dimension."""

    status: Status
    pages_total: int
    pages_unknown: int

    def as_dimension(self, **extra: object) -> dict[str, object]:
        """The dimension dict for ``core.json``.

        ``pages_unknown``/``pages_total`` travel with the status on purpose: a
        dimension of one healthy page and one blind page still rolls up
        ``healthy``, and without the counts the blind page simply disappears.
        Missing evidence must not read as good evidence.
        """
        dimension: dict[str, object] = {
            "status": str(self.status),
            "pages_total": self.pages_total,
            "pages_unknown": self.pages_unknown,
        }
        dimension.update(extra)
        return dimension


def roll_up_envelopes(envelopes: list[PageEnvelope]) -> DimensionRollup:
    """Worst-of rollup over page envelopes, with blind pages counted.

    Differs from ``fleet_vitals.roll_up_status`` in exactly one way, and only
    for this derived path: when **no** contributing page yields a rankable
    status the dimension is :attr:`Status.UNKNOWN`, not ``healthy``. The
    upstream "healthy if nothing usable was passed" default is documented and
    load-bearing for existing agent emits, so it is not changed; but a derived
    dimension whose pages are all blind must not claim health.
    """
    rankable = [e.status for e in envelopes if e.is_rankable]
    if not rankable:
        worst = Status.UNKNOWN
    else:
        worst = min(rankable, key=RANKABLE_STATUSES.index)
    return DimensionRollup(
        status=worst,
        pages_total=len(envelopes),
        pages_unknown=len(envelopes) - len(rankable),
    )
