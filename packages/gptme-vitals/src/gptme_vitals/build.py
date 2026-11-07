"""Derive ``core.json`` and ``headline.json`` from page envelopes.

Today an agent hand-builds ``core.json`` as a second contract beside its pages.
Here it is *derived*: each route declares the fixed ``core_dimension`` it feeds,
and the dimension is the rollup of its pages' statuses plus their allowlisted
``core`` projections. ``detail`` never takes part, so the derived file is safe to
pull across agents by construction rather than by a filter someone must remember.

The output matches ``fleet-vitals/core@1`` and ``fleet-vitals/headline@1`` but
this module does not import ``fleet_vitals``: the package has no dependencies,
and the only shared surface is the two schema strings and the status vocabulary
(parity-tested for the latter in ``test_envelope.py``).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from gptme_vitals.envelope import (
    PageEnvelope,
    Status,
    roll_up_envelopes,
    utcnow_rfc3339,
)
from gptme_vitals.routes import RouteRegistry

CORE_SCHEMA = "fleet-vitals/core@1"
HEADLINE_SCHEMA = "fleet-vitals/headline@1"

#: Keys the rollup computes. A page's ``core`` projection may not set them, or a
#: page could overwrite the derived status the whole design exists to compute.
RESERVED_DIMENSION_KEYS = frozenset({"status", "pages_total", "pages_unknown"})

#: Root fields of ``core@1``. A dimension named after one would overwrite it
#: when the dimensions are merged in, leaving a file that breaks the format.
RESERVED_ROOT_KEYS = frozenset({"schema", "agent", "generated_at", "status"})


class BuildError(ValueError):
    """The envelopes and registry cannot be turned into a valid core/headline."""


def derive_core(
    registry: RouteRegistry,
    envelopes: Iterable[PageEnvelope],
    *,
    agent: str,
    generated_at: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return ``(core, headline)`` derived from ``envelopes``.

    A route that declares a ``core_dimension`` but emitted no envelope is a blind
    page: it counts toward ``pages_total`` and ``pages_unknown`` and cannot
    contribute a status, so a dimension never reads healthier than its evidence.
    Raises :class:`BuildError` for an envelope no route owns, two envelopes for
    one slug, a ``core`` projection that sets a reserved key or collides with a
    sibling page's key, or a headline route whose page emitted nothing.
    """
    generated_at = generated_at or utcnow_rfc3339()
    by_slug: dict[str, PageEnvelope] = {}
    for env in envelopes:
        if env.slug in by_slug:
            raise BuildError(f"two envelopes for slug {env.slug!r}")
        if registry.get_by_slug(env.slug) is None:
            raise BuildError(f"envelope for unregistered slug {env.slug!r}")
        by_slug[env.slug] = env

    for name in registry.core_dimensions():
        if name in RESERVED_ROOT_KEYS:
            raise BuildError(
                f"core_dimension {name!r} collides with a core@1 root field"
            )

    dimensions: dict[str, dict[str, object]] = {}
    statuses: list[PageEnvelope] = []
    for name, routes in registry.core_dimensions().items():
        pages = [
            by_slug.get(r.slug)
            or PageEnvelope(
                slug=r.slug,
                status=Status.NO_DATA,
                headline="no envelope emitted",
                generated_at=generated_at,
            )
            for r in routes
        ]
        projection: dict[str, object] = {}
        owners: dict[str, str] = {}
        for page in pages:
            for key, value in (page.core or {}).items():
                if key in RESERVED_DIMENSION_KEYS:
                    raise BuildError(
                        f"{page.slug}: core key {key!r} is computed by the rollup"
                    )
                if key in owners:
                    raise BuildError(
                        f"core key {key!r} in dimension {name!r} set by both "
                        f"{owners[key]!r} and {page.slug!r}"
                    )
                owners[key] = page.slug
                projection[key] = value
        rollup = roll_up_envelopes(pages)
        # Computed fields are written last so nothing above can shadow them.
        dimensions[name] = {
            **projection,
            "status": str(rollup.status),
            "pages_total": rollup.pages_total,
            "pages_unknown": rollup.pages_unknown,
        }
        statuses.append(
            PageEnvelope(
                slug=name,
                status=rollup.status,
                headline="",
                generated_at=generated_at,
            )
        )

    # Status of the whole: worst rankable dimension; all-blind (or no dimensions)
    # is unknown, never healthy.
    overall = roll_up_envelopes(statuses).status
    core: dict[str, object] = {
        "schema": CORE_SCHEMA,
        "agent": agent,
        "generated_at": generated_at,
        "status": str(overall),
        **dimensions,
    }

    route = registry.headline_route()
    if not route.is_public:
        # headline.json copies the page's headline text, label and link into a
        # file other agents pull; a private page promised to stay out of that.
        raise BuildError(f"headline route {route.slug!r} is private")
    headline_page = by_slug.get(route.slug)
    if headline_page is None:
        raise BuildError(
            f"headline route {route.slug!r} emitted no envelope; "
            "headline.json would be undefined"
        )
    headline: dict[str, object] = {
        "schema": HEADLINE_SCHEMA,
        "agent": agent,
        "title": route.label,
        "value": headline_page.headline,
        "status": str(headline_page.status),
        "generated_at": generated_at,
        "link": route.href,
    }
    return core, headline


def _atomic_write(path: Path, content: str) -> None:
    """Write via a unique temp file in the same directory, then rename over ``path``.

    A fixed temp name would let two builds into one directory rename each
    other's half-written file.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def write_core_files(
    vitals_dir: str | Path, core: dict[str, object], headline: dict[str, object]
) -> None:
    """Write ``core.json`` then ``headline.json`` atomically into ``vitals_dir``."""
    out = Path(vitals_dir)
    out.mkdir(parents=True, exist_ok=True)
    _atomic_write(out / "core.json", json.dumps(core, indent=2) + "\n")
    _atomic_write(out / "headline.json", json.dumps(headline, indent=2) + "\n")
