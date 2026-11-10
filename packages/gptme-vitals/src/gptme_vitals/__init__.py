"""Shared agent-dashboard skeleton for gptme agents.

See gptme/gptme-contrib#2060. Slice 1 starts with the page envelope — the
contract the portal, the standard pages and the derived ``core.json`` all read —
plus the stdlib-only primitives (SVG renderers, coercion helpers, snapshot
cache) every agent's pages need.
"""

from gptme_vitals.envelope import (
    ENVELOPE_SCHEMA,
    MAX_LINES,
    RANKABLE_STATUSES,
    DimensionRollup,
    PageEnvelope,
    Status,
    roll_up_envelopes,
    utcnow_rfc3339,
)
from gptme_vitals.routes import (
    NAV_CSS,
    DashboardRoute,
    RefreshCadence,
    RouteKind,
    RouteRegistry,
    RouteRegistryError,
    Visibility,
)

__all__ = [
    "NAV_CSS",
    "DashboardRoute",
    "RefreshCadence",
    "RouteKind",
    "RouteRegistry",
    "RouteRegistryError",
    "Visibility",
    "ENVELOPE_SCHEMA",
    "MAX_LINES",
    "RANKABLE_STATUSES",
    "DimensionRollup",
    "PageEnvelope",
    "Status",
    "roll_up_envelopes",
    "utcnow_rfc3339",
]
