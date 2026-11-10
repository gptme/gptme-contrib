"""Private/public split: where each envelope goes, and what is read back."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from gptme_vitals.envelope import PageEnvelope, Status
from gptme_vitals.publish import (
    PublishError,
    check_outputs,
    check_roots,
    load_envelope,
    load_envelopes,
    publish_page,
    summary_path,
)
from gptme_vitals.routes import (
    DashboardRoute,
    RefreshCadence,
    RouteKind,
    RouteRegistry,
    Visibility,
)

NOW = "2026-11-07T20:00:00Z"


def _route(slug: str, **kw: object) -> DashboardRoute:
    base: dict[str, object] = dict(
        slug=slug,
        label=slug.title(),
        href=f"/{slug}/",
        kind=RouteKind.STATIC,
        section="dashboards",
        cadence=RefreshCadence.HOURLY,
        description=f"{slug} page",
        output_path=f"{slug}/index.html",
        summary_source=f"{slug}/summary.json",
    )
    base.update(kw)
    return DashboardRoute(**base)  # type: ignore[arg-type]


def _env(slug: str, status: Status = Status.HEALTHY, **kw: object) -> PageEnvelope:
    return PageEnvelope(
        slug=slug,
        status=status,
        headline=f"{slug} ok",
        generated_at=NOW,
        **kw,  # type: ignore[arg-type]
    )


@pytest.fixture
def registry() -> RouteRegistry:
    return RouteRegistry(
        (
            _route("pub", core_dimension="activity"),
            _route("priv", visibility=Visibility.PRIVATE, core_dimension="spend"),
        )
    )


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "private", tmp_path / "public"


def test_public_page_goes_to_both_roots_and_detail_stays_private(
    registry: RouteRegistry, roots: tuple[Path, Path]
) -> None:
    private, public = roots
    env = _env("pub", detail={"secret": "token-123"}, core={"calls": 3})
    written = publish_page(registry, env, private_root=private, public_root=public)
    assert written == [private / "pub/summary.json", public / "pub/summary.json"]
    assert json.loads(written[0].read_text())["detail"] == {"secret": "token-123"}
    served = written[1].read_text()
    assert "detail" not in json.loads(served)
    assert "token-123" not in served


def test_private_route_never_touches_the_public_root(
    registry: RouteRegistry, roots: tuple[Path, Path]
) -> None:
    private, public = roots
    written = publish_page(
        registry,
        _env("priv", detail={"x": 1}),
        private_root=private,
        public_root=public,
    )
    assert written == [private / "priv/summary.json"]
    assert not public.exists()


def test_private_copy_is_not_world_readable(
    registry: RouteRegistry, roots: tuple[Path, Path]
) -> None:
    private, public = roots
    publish_page(registry, _env("pub"), private_root=private, public_root=public)
    assert stat.S_IMODE((private / "pub/summary.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((public / "pub/summary.json").stat().st_mode) == 0o644


@pytest.mark.parametrize("inner", ["private", "public"])
def test_nested_roots_are_refused(tmp_path: Path, inner: str) -> None:
    outer = tmp_path / "root"
    nested = outer / "inner"
    pair = (nested, outer) if inner == "private" else (outer, nested)
    with pytest.raises(PublishError, match="overlap"):
        check_roots(*pair)


def test_identical_roots_are_refused(tmp_path: Path) -> None:
    with pytest.raises(PublishError, match="overlap"):
        check_roots(tmp_path, tmp_path)


def test_symlinked_public_root_into_private_is_refused(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir()
    link = tmp_path / "served"
    link.symlink_to(private)
    with pytest.raises(PublishError, match="overlap"):
        check_roots(private, link)


def test_publish_refuses_overlap_before_writing(
    registry: RouteRegistry, tmp_path: Path
) -> None:
    with pytest.raises(PublishError, match="overlap"):
        publish_page(
            registry, _env("pub"), private_root=tmp_path, public_root=tmp_path / "web"
        )
    assert not (tmp_path / "pub").exists()


def test_unregistered_slug_is_refused(
    registry: RouteRegistry, roots: tuple[Path, Path]
) -> None:
    with pytest.raises(PublishError, match="unregistered"):
        publish_page(
            registry, _env("ghost"), private_root=roots[0], public_root=roots[1]
        )


@pytest.mark.parametrize("source", ["../escape.json", "/etc/passwd", "a/../../b.json"])
def test_summary_source_cannot_escape_the_root(source: str, tmp_path: Path) -> None:
    route = _route("x", summary_source=source)
    with pytest.raises(PublishError, match="relative path"):
        summary_path(route, tmp_path)


def test_route_without_summary_source_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PublishError, match="no summary_source"):
        summary_path(_route("x", summary_source=None), tmp_path)


def test_load_round_trips_everything_but_detail(
    registry: RouteRegistry, roots: tuple[Path, Path]
) -> None:
    private, public = roots
    env = _env(
        "pub",
        Status.DEGRADED,
        lines=("a", "b"),
        detail={"secret": 1},
        core={"calls": 3},
    )
    publish_page(registry, env, private_root=private, public_root=public)
    route = registry.get_by_slug("pub")
    assert route is not None
    for root in (private, public):
        loaded = load_envelope(route, root)
        assert loaded is not None
        assert (loaded.status, loaded.lines, loaded.core) == (
            Status.DEGRADED,
            ("a", "b"),
            {"calls": 3},
        )
        assert loaded.detail is None


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"schema": "other/1"}',
        '{"schema": "gptme-vitals/page@1", "slug": "other", "status": "healthy",'
        ' "headline": "x", "generated_at": "t"}',
        '{"schema": "gptme-vitals/page@1", "slug": "pub", "status": "critical",'
        ' "headline": "x", "generated_at": "t"}',
        '{"schema": "gptme-vitals/page@1", "slug": "pub", "status": "healthy"}',
        '{"schema": "gptme-vitals/page@1", "slug": "pub", "status": "healthy",'
        ' "headline": "x", "generated_at": "t", "core": [1]}',
    ],
)
def test_unusable_file_is_a_blind_page_not_an_error(
    content: str, registry: RouteRegistry, tmp_path: Path
) -> None:
    route = registry.get_by_slug("pub")
    assert route is not None
    path = summary_path(route, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(content)
    assert load_envelope(route, tmp_path) is None


def test_missing_file_is_a_blind_page(registry: RouteRegistry, tmp_path: Path) -> None:
    route = registry.get_by_slug("pub")
    assert route is not None
    assert load_envelope(route, tmp_path) is None


def test_load_envelopes_honours_exclude(
    registry: RouteRegistry, roots: tuple[Path, Path]
) -> None:
    private, public = roots
    for slug in ("pub", "priv"):
        publish_page(registry, _env(slug), private_root=private, public_root=public)
    assert {e.slug for e in load_envelopes(registry, private)} == {"pub", "priv"}
    assert {e.slug for e in load_envelopes(registry, private, exclude=["pub"])} == {
        "priv"
    }


def test_symlinked_directory_cannot_route_a_private_write_into_the_public_root(
    registry: RouteRegistry, roots: tuple[Path, Path]
) -> None:
    private, public = roots
    (public / "priv").mkdir(parents=True)
    private.mkdir()
    (private / "priv").symlink_to(public / "priv")
    with pytest.raises(PublishError, match="through a symlink"):
        publish_page(
            registry,
            _env("priv", detail={"secret": "x"}),
            private_root=private,
            public_root=public,
        )
    assert list(public.rglob("*.json")) == []


def test_symlinked_directory_reads_back_as_blind(
    registry: RouteRegistry, roots: tuple[Path, Path]
) -> None:
    private, public = roots
    elsewhere = private.parent / "elsewhere"
    publish_page(registry, _env("pub"), private_root=elsewhere, public_root=public)
    private.mkdir()
    (private / "pub").symlink_to(elsewhere / "pub")
    route = registry.get_by_slug("pub")
    assert route is not None
    assert load_envelope(route, private) is None


def test_route_turned_private_loses_its_old_public_copy(
    roots: tuple[Path, Path],
) -> None:
    private, public = roots
    was_public = RouteRegistry((_route("page"),))
    publish_page(
        was_public,
        _env("page", lines=("old",)),
        private_root=private,
        public_root=public,
    )
    served = public / "page/summary.json"
    assert served.exists()
    now_private = RouteRegistry((_route("page", visibility=Visibility.PRIVATE),))
    publish_page(now_private, _env("page"), private_root=private, public_root=public)
    assert not served.exists()
    assert (private / "page/summary.json").exists()


def test_publishing_a_private_route_with_no_public_copy_is_fine(
    registry: RouteRegistry, roots: tuple[Path, Path]
) -> None:
    private, public = roots
    publish_page(registry, _env("priv"), private_root=private, public_root=public)
    publish_page(registry, _env("priv"), private_root=private, public_root=public)


def test_outputs_may_not_share_a_summary_source() -> None:
    reg = RouteRegistry((_route("a"), _route("b", summary_source="a/summary.json")))
    with pytest.raises(PublishError, match="more than one output"):
        check_outputs(reg)


@pytest.mark.parametrize(
    "name", ["core.json", "headline.json", "CORE.JSON", "./core.json"]
)
def test_a_page_may_not_write_the_derived_files(name: str) -> None:
    with pytest.raises(PublishError, match="derived file"):
        check_outputs(RouteRegistry((_route("a", summary_source=name),)))


def test_summary_source_may_not_collide_with_the_portal_page() -> None:
    portal = _route("portal", href="/", output_path="index.html", summary_source=None)
    reg = RouteRegistry((_route("a", summary_source="index.html"),), portal=portal)
    with pytest.raises(PublishError, match="portal output_path"):
        check_outputs(reg)


def test_summary_source_may_not_collide_with_another_routes_page() -> None:
    reg = RouteRegistry((_route("a"), _route("b", summary_source="a/index.html")))
    with pytest.raises(PublishError, match="a/index.html"):
        check_outputs(reg)


def test_distinct_outputs_pass(registry: RouteRegistry) -> None:
    check_outputs(registry)


@pytest.mark.parametrize("name", [".gptme-vitals.lock", "./.GPTME-VITALS.LOCK"])
def test_summary_source_cannot_replace_the_build_lock(name: str) -> None:
    with pytest.raises(PublishError, match="lock"):
        check_outputs(RouteRegistry((_route("a", summary_source=name),)))
