"""``gptme-vitals build [page...]``: rebuild pages, then re-derive ``core.json``.

An agent supplies its own pages as data, not as a fork of this package:

* ``--registry module:ATTR`` — a :class:`~gptme_vitals.routes.RouteRegistry`;
* ``--emitters module:ATTR`` — a mapping ``slug -> () -> PageEnvelope``.

Naming pages rebuilds only those, so a cheap page can refresh every few minutes
without recomputing an expensive one. ``core.json``/``headline.json`` are always
re-derived over *all* pages (the rest read back from the private root), so a
partial build never leaves the fleet view describing only the pages just touched.

Exit status: 0 on success, 1 when any page failed to build or the derived files
could not be written (other pages are still published), 2 for a usage error.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from gptme_vitals.build import BuildError, derive_core, write_core_files
from gptme_vitals.envelope import PageEnvelope
from gptme_vitals.publish import (
    PublishError,
    check_roots,
    load_envelopes,
    publish_page,
)
from gptme_vitals.routes import RouteRegistry

Emitter = Callable[[], PageEnvelope]


class UsageError(ValueError):
    """The command line names something that cannot be run."""


def _load(spec: str, kind: type | tuple[type, ...], what: str) -> object:
    module_name, sep, attr = spec.partition(":")
    if not (module_name and sep and attr):
        raise UsageError(f"{what} must look like 'module:ATTR', got {spec!r}")
    try:
        obj = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise UsageError(f"cannot load {what} {spec!r}: {exc}") from exc
    if not isinstance(obj, kind):
        raise UsageError(f"{what} {spec!r} is a {type(obj).__name__}")
    return obj


def run_build(
    registry: RouteRegistry,
    emitters: Mapping[str, Emitter],
    *,
    pages: Sequence[str],
    private_root: Path,
    public_root: Path,
    agent: str,
    out: Callable[[str], None] = print,
) -> int:
    """Rebuild ``pages`` (all emitters when empty); return the exit status."""
    check_roots(private_root, public_root)
    envelope_slugs = {r.slug for r in registry.envelope_routes()}
    for slug in emitters:
        if slug not in envelope_slugs:
            raise UsageError(f"emitter for {slug!r}, which has no summary_source route")
    wanted = list(dict.fromkeys(pages)) or list(emitters)
    for slug in wanted:
        if slug not in emitters:
            raise UsageError(f"no emitter registered for page {slug!r}")

    failed = 0
    built: list[PageEnvelope] = []
    for slug in wanted:
        try:
            envelope = emitters[slug]()
            if envelope.slug != slug:
                raise PublishError(
                    f"emitter {slug!r} returned envelope for {envelope.slug!r}"
                )
            publish_page(
                registry, envelope, private_root=private_root, public_root=public_root
            )
        except Exception as exc:  # one broken page must not stop the others
            failed += 1
            out(f"FAIL {slug}: {type(exc).__name__}: {exc}")
            continue
        built.append(envelope)
        out(f"ok   {slug}: {envelope.status}")

    # Freshly built pages are re-read from disk with the others, so every page
    # reaches the rollup by the same path (and a failed page keeps its previous
    # file rather than being treated as absent).
    envelopes = load_envelopes(registry, private_root)
    try:
        core, headline = derive_core(registry, envelopes, agent=agent)
        write_core_files(public_root, core, headline)
    except BuildError as exc:
        out(f"FAIL core: {exc}")
        return 1
    out(f"ok   core: {core['status']}")
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gptme-vitals")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="rebuild pages and re-derive core.json")
    build.add_argument("pages", nargs="*", help="page slugs (default: all emitters)")
    build.add_argument("--registry", required=True, metavar="MODULE:ATTR")
    build.add_argument("--emitters", required=True, metavar="MODULE:ATTR")
    build.add_argument("--private-root", required=True, type=Path)
    build.add_argument("--public-root", required=True, type=Path)
    build.add_argument("--agent", required=True)
    args = parser.parse_args(argv)
    try:
        registry = _load(args.registry, RouteRegistry, "registry")
        emitters = _load(args.emitters, Mapping, "emitters")
        assert isinstance(registry, RouteRegistry) and isinstance(emitters, Mapping)
        return run_build(
            registry,
            emitters,
            pages=args.pages,
            private_root=args.private_root,
            public_root=args.public_root,
            agent=args.agent,
        )
    except (UsageError, PublishError) as exc:
        print(f"gptme-vitals: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
