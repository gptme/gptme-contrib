"""Shared agent-dashboard skeleton for gptme agents.

See gptme/gptme-contrib#2060. Slice 1 starts with the page envelope — the
contract the portal, the standard pages and the derived ``core.json`` all read —
plus the stdlib-only primitives (SVG renderers, coercion helpers, snapshot
cache) every agent's pages need.
"""

from gptme_vitals.build import BuildError, derive_core, write_core_files
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
from gptme_vitals.publish import (
    PublishError,
    load_envelope,
    load_envelopes,
    publish_page,
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
from gptme_vitals.theme import (
    BASE_CSS,
    THEME_INIT_JS,
    TOKENS_CSS,
    render_head_assets,
    render_theme_toggle,
)

__all__ = [
    "BASE_CSS",
    "THEME_INIT_JS",
    "TOKENS_CSS",
    "render_head_assets",
    "render_theme_toggle",
    "BuildError",
    "derive_core",
    "write_core_files",
    "PublishError",
    "load_envelope",
    "load_envelopes",
    "publish_page",
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
