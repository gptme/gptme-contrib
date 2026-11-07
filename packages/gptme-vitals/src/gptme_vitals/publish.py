"""Write page envelopes into a private root and a public (served) root.

Route-level filtering cannot protect the data files in a served tree: a page's
``summary.json`` is a file in the HTTP root whether or not the nav links to it.
So the split is made at write time, by construction:

* the **private root** receives every page's :meth:`PageEnvelope.to_private`
  (including ``detail``), and is the only place ``detail`` is ever written;
* the **public root** receives :meth:`PageEnvelope.to_public` for ``PUBLIC``
  routes only, and nothing at all for ``PRIVATE`` ones.

The two roots must not overlap, or the private root would be reachable through
the public one. A partial rebuild re-reads the other pages from the **private** root, so a
private page that feeds a dimension counts the same whether it was rebuilt this
run or not. Only ``status``/``headline``/``lines``/``core`` are read back;
``detail`` is dropped on load, and ``core.json`` is derived from ``core`` alone.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from gptme_vitals.envelope import ENVELOPE_SCHEMA, PageEnvelope
from gptme_vitals.routes import DashboardRoute, RouteRegistry


class PublishError(ValueError):
    """An envelope cannot be written without breaking the private/public split."""


def check_roots(private_root: Path, public_root: Path) -> None:
    """Raise unless the two roots are disjoint (neither contains the other)."""
    private, public = private_root.resolve(), public_root.resolve()
    if (
        private == public
        or private.is_relative_to(public)
        or public.is_relative_to(private)
    ):
        raise PublishError(
            f"private root {private} and public root {public} overlap; "
            "detail would be reachable through the served tree"
        )


def _relative_inside(slug: str, source: str, root: Path, what: str) -> Path:
    rel = PurePosixPath(source)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise PublishError(
            f"route {slug!r}: {what} {source!r} must be a relative path inside the root"
        )
    target = root.joinpath(*rel.parts)
    # A symlinked directory inside the root would let a write land elsewhere (a
    # private page inside the served tree, say). resolve() follows existing
    # links and tolerates not-yet-created components.
    if not target.parent.resolve().is_relative_to(root.resolve()):
        raise PublishError(
            f"route {slug!r}: {what} {source!r} resolves outside {root} "
            "through a symlink"
        )
    return target


def summary_path(route: DashboardRoute, root: Path) -> Path:
    """Where ``route``'s envelope lives under ``root``.

    Raises :class:`PublishError` for a route with no ``summary_source`` or one
    that would escape ``root``.
    """
    if route.summary_source is None:
        raise PublishError(f"route {route.slug!r} has no summary_source")
    return _relative_inside(route.slug, route.summary_source, root, "summary_source")


def portal_path(registry: RouteRegistry, root: Path) -> Path | None:
    """Where the portal page is written, or ``None`` if the registry has none."""
    portal = registry.portal
    if portal is None or portal.output_path is None:
        return None
    return _relative_inside(portal.slug, portal.output_path, root, "output_path")


#: Files the build derives into the public root; no page may write to them.
RESERVED_PUBLIC_FILES = ("core.json", "headline.json")


def check_outputs(registry: RouteRegistry) -> None:
    """Raise if two outputs would write the same file under one root.

    Covers summary sources, the portal page, the derived ``core.json`` and
    ``headline.json``, and other routes' ``output_path`` values. Compared
    case-insensitively so a case-insensitive filesystem cannot merge two paths
    that look distinct here. A shared path would otherwise make the later write
    silently replace the earlier page, which the loader then counts as blind.
    """
    owners: dict[tuple[str, ...], list[tuple[str, str]]] = {}

    def claim(kind: str, owner: str, path: str | None) -> None:
        if path is None:
            return
        key = tuple(part.casefold() for part in PurePosixPath(path).parts)
        owners.setdefault(key, []).append((kind, owner))

    for name in RESERVED_PUBLIC_FILES:
        claim("derived file", name, name)
    portal = registry.portal
    if portal is not None:
        claim("portal output_path", portal.slug, portal.output_path)
    for route in registry.routes:
        claim("summary_source", route.slug, route.summary_source)
        claim("output_path", route.slug, route.output_path)
    for key, claims in owners.items():
        # Plain output_path/output_path repeats are the registry's own business.
        if len(claims) > 1 and any(kind != "output_path" for kind, _ in claims):
            who = ", ".join(f"{kind} of {owner!r}" for kind, owner in claims)
            raise PublishError(
                f"{'/'.join(key)} is written by more than one output: {who}"
            )


def _atomic_write(path: Path, content: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def write_text(path: Path, content: str, *, mode: int = 0o644) -> None:
    """Atomically write ``content`` to ``path`` (parents created)."""
    _atomic_write(path, content, mode=mode)


def publish_page(
    registry: RouteRegistry,
    envelope: PageEnvelope,
    *,
    private_root: str | Path,
    public_root: str | Path,
) -> list[Path]:
    """Write ``envelope`` to the roots its route's visibility allows.

    Returns the paths written (private first). The private copy is mode 0600;
    the public copy is world-readable like the rest of the served tree.
    """
    private, public = Path(private_root), Path(public_root)
    check_roots(private, public)
    route = registry.get_by_slug(envelope.slug)
    if route is None:
        raise PublishError(f"envelope for unregistered slug {envelope.slug!r}")
    written: list[Path] = []
    if not route.is_public:
        # A route that used to be public may have a served copy; leaving it would
        # keep its headline, lines and core reachable at the old URL.
        summary_path(route, public).unlink(missing_ok=True)
    target = summary_path(route, private)
    _atomic_write(
        target, json.dumps(envelope.to_private(), indent=2) + "\n", mode=0o600
    )
    written.append(target)
    if route.is_public:
        target = summary_path(route, public)
        _atomic_write(
            target, json.dumps(envelope.to_public(), indent=2) + "\n", mode=0o644
        )
        written.append(target)
    return written


def load_envelope(route: DashboardRoute, root: str | Path) -> PageEnvelope | None:
    """Read ``route``'s stored envelope back from ``root``, or ``None`` if unusable.

    A missing, unparsable or wrongly-shaped file is a blind page, not an error:
    ``derive_core`` counts it in ``pages_unknown`` instead of reading it as
    healthy. A file carrying an unrecognised status is also blind rather than
    raising, so one corrupt page cannot stop every other page being rebuilt.
    ``detail`` is never loaded.
    """
    try:
        path = summary_path(route, Path(root))
        data = json.loads(path.read_text())
    except (OSError, ValueError, PublishError):
        return None
    if not isinstance(data, dict) or data.get("schema") != ENVELOPE_SCHEMA:
        return None
    if data.get("slug") != route.slug:
        return None
    core = data.get("core")
    if core is not None and not isinstance(core, dict):
        return None
    try:
        return PageEnvelope(
            slug=route.slug,
            status=data["status"],
            headline=data["headline"],
            generated_at=data["generated_at"],
            lines=tuple(data.get("lines", ())),
            core=core,
        )
    except (KeyError, TypeError, ValueError):
        return None


def load_envelopes(
    registry: RouteRegistry,
    root: str | Path,
    *,
    exclude: Iterable[str] = (),
) -> list[PageEnvelope]:
    """Every usable stored envelope under ``root``, minus the slugs in ``exclude``."""
    skip = set(exclude)
    loaded = (
        load_envelope(route, root)
        for route in registry.envelope_routes()
        if route.slug not in skip
    )
    return [env for env in loaded if env is not None]
