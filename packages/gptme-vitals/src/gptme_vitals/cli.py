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
import contextlib
import fcntl
import importlib
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

from gptme_vitals.build import BuildError, derive_core, headline_link, write_core_files
from gptme_vitals.envelope import PageEnvelope
from gptme_vitals.portal import render_portal
from gptme_vitals.publish import (
    LOCK_NAME,
    PublishError,
    check_outputs,
    check_roots,
    load_envelopes,
    portal_path,
    publish_page,
    write_text,
)
from gptme_vitals.routes import RouteRegistry, RouteRegistryError
from gptme_vitals.timers import TimerError, render_units

Emitter = Callable[[], PageEnvelope]


@contextlib.contextmanager
def build_lock(private_root: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``<private_root>/.gptme-vitals.lock``.

    Per-page timers share the top of the hour, so two builds can overlap. Each
    rewrites the shared ``core.json``/``headline.json``/portal; without one lock
    from the read of the stored envelopes to the last write, an older build can
    finish second and replace a newer build's view (a page that just went down
    would read healthy again). The lock lives in the private root so a pull of
    the served tree never sees it. Unix only (``fcntl.flock``), like systemd.
    """
    private_root.mkdir(parents=True, exist_ok=True)
    with open(private_root / LOCK_NAME, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


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
    title: str | None = None,
    base_url: str | None = None,
    out: Callable[[str], None] = print,
) -> int:
    """Rebuild ``pages`` (all emitters when empty); return the exit status."""
    check_roots(private_root, public_root)
    check_outputs(registry)
    try:
        # derive_core needs both; failing here means no page is built against a
        # registry that could never produce core.json/headline.json.
        headline_link(registry.headline_route(), base_url)
    except (RouteRegistryError, BuildError) as exc:
        raise UsageError(str(exc)) from exc
    envelope_slugs = {r.slug for r in registry.envelope_routes()}
    for slug in emitters:
        if slug not in envelope_slugs:
            raise UsageError(f"emitter for {slug!r}, which has no summary_source route")
    wanted = list(dict.fromkeys(pages)) or list(emitters)
    for slug in wanted:
        if slug not in emitters:
            raise UsageError(f"no emitter registered for page {slug!r}")

    failed = 0
    # Emitters run outside the lock: a slow collector must not hold up the
    # others. Only the publish -> derive -> shared-write section is serialised.
    emitted: list[PageEnvelope] = []
    for slug in wanted:
        try:
            envelope = emitters[slug]()
            if envelope.slug != slug:
                raise PublishError(
                    f"emitter {slug!r} returned envelope for {envelope.slug!r}"
                )
        except Exception as exc:  # one broken page must not stop the others
            failed += 1
            out(f"FAIL {slug}: {type(exc).__name__}: {exc}")
            continue
        emitted.append(envelope)

    try:
        with build_lock(private_root):
            return _publish_and_derive(
                registry,
                emitted,
                failed=failed,
                private_root=private_root,
                public_root=public_root,
                agent=agent,
                title=title,
                base_url=base_url,
                out=out,
            )
    except OSError as exc:  # unwritable private root: no lock, so nothing is written
        out(f"FAIL lock: {type(exc).__name__}: {exc}")
        return 1


def _publish_and_derive(
    registry: RouteRegistry,
    emitted: Sequence[PageEnvelope],
    *,
    failed: int,
    private_root: Path,
    public_root: Path,
    agent: str,
    title: str | None,
    base_url: str | None,
    out: Callable[[str], None],
) -> int:
    """Publish ``emitted``, then re-derive core/headline/portal. Hold the build lock."""
    for envelope in emitted:
        try:
            publish_page(
                registry, envelope, private_root=private_root, public_root=public_root
            )
        except Exception as exc:
            failed += 1
            out(f"FAIL {envelope.slug}: {type(exc).__name__}: {exc}")
            continue
        out(f"ok   {envelope.slug}: {envelope.status}")

    # Freshly built pages are re-read from disk with the others, so every page
    # reaches the rollup by the same path (and a failed page keeps its previous
    # file rather than being treated as absent).
    envelopes = load_envelopes(registry, private_root)
    try:
        core, headline = derive_core(
            registry, envelopes, agent=agent, base_url=base_url
        )
        write_core_files(public_root, core, headline)
    except (BuildError, OSError) as exc:
        out(f"FAIL core: {exc}")
        return 1
    out(f"ok   core: {core['status']}")
    try:
        target = portal_path(registry, public_root)
        if target is not None:
            html = render_portal(
                registry,
                envelopes,
                title=title or agent,
                generated_at=str(core["generated_at"]),
            )
            write_text(target, html)
            out("ok   portal")
    except (OSError, PublishError) as exc:
        out(f"FAIL portal: {exc}")
        return 1
    return 1 if failed else 0


def run_timers(
    registry: RouteRegistry,
    *,
    out_dir: Path,
    executable: str,
    build_args: Sequence[str],
    unit_prefix: str,
    out: Callable[[str], None] = print,
) -> int:
    """Write one service+timer pair per scheduled page into ``out_dir``."""
    pairs, unscheduled = render_units(
        registry, executable=executable, build_args=build_args, unit_prefix=unit_prefix
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    for pair in pairs:
        (out_dir / pair.service_name).write_text(pair.service)
        (out_dir / pair.timer_name).write_text(pair.timer)
        out(f"wrote {pair.timer_name}")
    for slug in unscheduled:
        out(f"skip  {slug}: cadence has no schedule (manual or n/a)")
    return 0


def _add_page_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--registry", required=True, metavar="MODULE:ATTR")
    parser.add_argument("--emitters", required=True, metavar="MODULE:ATTR")
    parser.add_argument("--private-root", required=True, type=Path)
    parser.add_argument("--public-root", required=True, type=Path)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--title", help="portal page title (default: the agent name)")
    parser.add_argument(
        "--base-url",
        help="absolute HTTP(S) URL the public root is served at; required when the "
        "headline route's href is relative (headline.json links must be absolute)",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gptme-vitals")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="rebuild pages and re-derive core.json")
    build.add_argument("pages", nargs="*", help="page slugs (default: all emitters)")
    _add_page_options(build)
    timers = sub.add_parser(
        "timers", help="write a systemd service+timer per page from its cadence"
    )
    _add_page_options(timers)
    timers.add_argument("--out", required=True, type=Path, metavar="DIR")
    timers.add_argument(
        "--executable",
        required=True,
        help="absolute path of the gptme-vitals executable the units run",
    )
    timers.add_argument("--unit-prefix", default="gptme-vitals")
    args = parser.parse_args(argv)
    try:
        registry = _load(args.registry, RouteRegistry, "registry")
        emitters = _load(args.emitters, Mapping, "emitters")
        assert isinstance(registry, RouteRegistry) and isinstance(emitters, Mapping)
        if args.command == "timers":
            check_roots(args.private_root, args.public_root)
            try:
                # A unit set whose every run would refuse to start is not worth writing.
                headline_link(registry.headline_route(), args.base_url)
            except (RouteRegistryError, BuildError) as exc:
                raise UsageError(str(exc)) from exc
            return run_timers(
                registry,
                out_dir=args.out,
                executable=args.executable,
                unit_prefix=args.unit_prefix,
                build_args=[
                    "--registry",
                    args.registry,
                    "--emitters",
                    args.emitters,
                    "--private-root",
                    str(args.private_root.resolve()),
                    "--public-root",
                    str(args.public_root.resolve()),
                    "--agent",
                    args.agent,
                    *(["--title", args.title] if args.title else []),
                    *(["--base-url", args.base_url] if args.base_url else []),
                ],
            )
        return run_build(
            registry,
            emitters,
            pages=args.pages,
            private_root=args.private_root,
            public_root=args.public_root,
            agent=args.agent,
            title=args.title,
            base_url=args.base_url,
        )
    except (UsageError, PublishError, TimerError) as exc:
        print(f"gptme-vitals: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
