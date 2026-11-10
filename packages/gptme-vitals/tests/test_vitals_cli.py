"""``gptme-vitals build``: partial rebuilds, failure isolation, usage errors."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from gptme_vitals.cli import UsageError, main, run_build
from gptme_vitals.envelope import PageEnvelope, Status
from gptme_vitals.routes import (
    DashboardRoute,
    RefreshCadence,
    RouteKind,
    RouteRegistry,
    RouteRegistryError,
    Visibility,
)

NOW = "2026-11-07T20:00:00Z"
BASE_URL = "https://bob.example/"


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


def _registry() -> RouteRegistry:
    return RouteRegistry(
        (
            _route("services", core_dimension="services", is_headline=True),
            _route("sessions", core_dimension="activity"),
            _route("cost", visibility=Visibility.PRIVATE, core_dimension="spend"),
        )
    )


def _emitter(slug: str, status: Status, **kw: object):  # noqa: ANN202
    def emit() -> PageEnvelope:
        return PageEnvelope(
            slug=slug,
            status=status,
            headline=f"{slug}",
            generated_at=NOW,
            **kw,  # type: ignore[arg-type]
        )

    return emit


def _run(
    tmp_path: Path, emitters: dict, pages: tuple[str, ...] = ()
) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = run_build(
        _registry(),
        emitters,
        pages=pages,
        private_root=tmp_path / "private",
        public_root=tmp_path / "public",
        agent="bob",
        base_url=BASE_URL,
        out=lines.append,
    )
    return code, lines


def _core(tmp_path: Path) -> dict[str, Any]:
    core: dict[str, Any] = json.loads((tmp_path / "public/core.json").read_text())
    return core


def _all(**overrides: object) -> dict:
    base = {
        "services": _emitter("services", Status.HEALTHY),
        "sessions": _emitter("sessions", Status.HEALTHY, core={"n": 1}),
        "cost": _emitter("cost", Status.HEALTHY),
    }
    base.update(overrides)
    return base


def test_full_build_writes_pages_and_derived_files(tmp_path: Path) -> None:
    code, _ = _run(tmp_path, _all())
    assert code == 0
    core = _core(tmp_path)
    assert core["status"] == "healthy"
    assert core["activity"] == {
        "n": 1,
        "status": "healthy",
        "pages_total": 1,
        "pages_unknown": 0,
    }
    assert (
        json.loads((tmp_path / "public/headline.json").read_text())["value"]
        == "services"
    )
    assert (tmp_path / "private/cost/summary.json").exists()
    assert not (tmp_path / "public/cost").exists()


def test_partial_build_keeps_the_other_pages_in_core(tmp_path: Path) -> None:
    _run(tmp_path, _all(sessions=_emitter("sessions", Status.DOWN)))
    assert _core(tmp_path)["status"] == "down"
    # Rebuild only `services`: the stored `sessions: down` must still count.
    code, lines = _run(tmp_path, _all(), pages=("services",))
    assert code == 0
    assert not any(
        "sessions" in line for line in lines if line.startswith("ok   sessions")
    )
    assert _core(tmp_path)["status"] == "down"
    assert _core(tmp_path)["activity"]["status"] == "down"


def test_private_page_counts_the_same_on_a_partial_rebuild(tmp_path: Path) -> None:
    emitters = _all(cost=_emitter("cost", Status.DEGRADED))
    _run(tmp_path, emitters)
    full = _core(tmp_path)["spend"]
    _run(tmp_path, emitters, pages=("services",))
    assert _core(tmp_path)["spend"] == full
    assert full["status"] == "degraded"


def test_page_never_built_is_blind_not_healthy(tmp_path: Path) -> None:
    code, _ = _run(tmp_path, _all(), pages=("services",))
    assert code == 0
    core = _core(tmp_path)
    assert core["activity"]["status"] == "unknown"
    assert core["activity"]["pages_unknown"] == 1


def test_failing_emitter_is_isolated_and_exits_nonzero(tmp_path: Path) -> None:
    def boom() -> PageEnvelope:
        raise RuntimeError("collector down")

    code, lines = _run(tmp_path, _all(sessions=boom))
    assert code == 1
    assert "FAIL sessions: RuntimeError: collector down" in lines
    assert "ok   services: healthy" in lines
    assert (tmp_path / "public/services/summary.json").exists()
    assert not (tmp_path / "public/sessions").exists()


def test_failed_rebuild_keeps_the_previous_page(tmp_path: Path) -> None:
    _run(tmp_path, _all(sessions=_emitter("sessions", Status.DEGRADED)))

    def boom() -> PageEnvelope:
        raise RuntimeError("x")

    code, _ = _run(tmp_path, _all(sessions=boom), pages=("sessions",))
    assert code == 1
    assert _core(tmp_path)["activity"]["status"] == "degraded"


def test_invalid_status_from_an_emitter_is_a_failure_not_a_crash(
    tmp_path: Path,
) -> None:
    def legacy() -> PageEnvelope:
        return PageEnvelope(slug="sessions", status="critical", headline="x")  # type: ignore[arg-type]

    code, lines = _run(tmp_path, _all(sessions=legacy))
    assert code == 1
    assert any(line.startswith("FAIL sessions: ValueError") for line in lines)


def test_envelope_for_the_wrong_slug_is_refused(tmp_path: Path) -> None:
    code, lines = _run(tmp_path, _all(sessions=_emitter("services", Status.HEALTHY)))
    assert code == 1
    assert any("returned envelope for 'services'" in line for line in lines)


def test_headline_page_that_failed_before_ever_building_fails_core(
    tmp_path: Path,
) -> None:
    def boom() -> PageEnvelope:
        raise RuntimeError("x")

    code, lines = _run(tmp_path, _all(services=boom))
    assert code == 1
    assert any(line.startswith("FAIL core:") for line in lines)
    assert not (tmp_path / "public/core.json").exists()


def test_unknown_page_is_a_usage_error_before_anything_is_written(
    tmp_path: Path,
) -> None:
    with pytest.raises(UsageError, match="no emitter registered for page 'nope'"):
        _run(tmp_path, _all(), pages=("services", "nope"))
    assert not (tmp_path / "private").exists()


def test_emitter_for_a_route_without_summary_source_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="no summary_source"):
        _run(tmp_path, {**_all(), "ghost": _emitter("ghost", Status.HEALTHY)})


def test_overlapping_roots_are_refused(tmp_path: Path) -> None:
    from gptme_vitals.publish import PublishError

    with pytest.raises(PublishError, match="overlap"):
        run_build(
            _registry(),
            _all(),
            pages=(),
            private_root=tmp_path,
            public_root=tmp_path / "web",
            agent="bob",
            base_url=BASE_URL,
            out=lambda _: None,
        )


APP = textwrap.dedent(
    """
    from gptme_vitals import *

    def _route(slug, **kw):
        return DashboardRoute(
            slug=slug, label=slug, href=f"/{slug}/", kind=RouteKind.STATIC,
            section="s", cadence=RefreshCadence.HOURLY, description=slug,
            output_path=f"{slug}/index.html", summary_source=f"{slug}/summary.json", **kw)

    REGISTRY = RouteRegistry((_route("a", core_dimension="x", is_headline=True),))
    EMITTERS = {"a": lambda: PageEnvelope(slug="a", status=Status.HEALTHY, headline="A")}
    NOT_A_REGISTRY = 3
    """
)


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    mod = tmp_path / "mods"
    mod.mkdir()
    (mod / "vitals_app.py").write_text(APP)
    monkeypatch.syspath_prepend(str(mod))
    sys.modules.pop("vitals_app", None)
    return tmp_path


def _argv(tmp: Path, **kw: str) -> list[str]:
    opts = {
        "--registry": "vitals_app:REGISTRY",
        "--emitters": "vitals_app:EMITTERS",
        "--private-root": str(tmp / "private"),
        "--public-root": str(tmp / "public"),
        "--agent": "bob",
        "--base-url": BASE_URL,
    }
    opts.update(kw)
    return ["build", *[x for pair in opts.items() for x in pair]]


def test_main_end_to_end(app: Path) -> None:
    assert main(_argv(app)) == 0
    assert (
        json.loads((app / "public/core.json").read_text())["x"]["status"] == "healthy"
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"--registry": "vitals_app"},
        {"--registry": "vitals_app:MISSING"},
        {"--registry": "no_such_module:REGISTRY"},
        {"--registry": "vitals_app:NOT_A_REGISTRY"},
        {"--emitters": "vitals_app:REGISTRY"},
    ],
)
def test_main_usage_errors_exit_2(
    app: Path, bad: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_argv(app, **bad)) == 2
    assert capsys.readouterr().err.startswith("gptme-vitals:")
    assert not (app / "public").exists()


def _timers_argv(tmp: Path, **kw: str) -> list[str]:
    opts = {
        "--registry": "vitals_app:REGISTRY",
        "--emitters": "vitals_app:EMITTERS",
        "--private-root": str(tmp / "private"),
        "--public-root": str(tmp / "public"),
        "--agent": "bob",
        "--base-url": BASE_URL,
        "--out": str(tmp / "units"),
        "--executable": "/usr/bin/gptme-vitals",
    }
    opts.update(kw)
    return ["timers", *[x for pair in opts.items() for x in pair]]


def test_timers_writes_one_pair_per_scheduled_page(app: Path) -> None:
    assert main(_timers_argv(app)) == 0
    service = (app / "units/gptme-vitals-a.service").read_text()
    assert '"build" "a" "--registry" "vitals_app:REGISTRY"' in service
    assert f'"--private-root" "{app / "private"}"' in service
    assert "OnCalendar=hourly" in (app / "units/gptme-vitals-a.timer").read_text()
    # Rendering units must not create the data roots.
    assert not (app / "public").exists()


def test_timers_relative_executable_exits_2(
    app: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(_timers_argv(app, **{"--executable": "gptme-vitals"})) == 2
    assert "absolute" in capsys.readouterr().err
    assert not (app / "units").exists()


def test_timers_refuses_overlapping_roots(app: Path) -> None:
    argv = _timers_argv(app, **{"--public-root": str(app / "private" / "web")})
    assert main(argv) == 2
    assert not (app / "units").exists()


def _portal_registry(output_path: str | None = "index.html") -> RouteRegistry:
    portal = _route(
        "portal", href="/", output_path=output_path, summary_source=None, section="root"
    )
    return RouteRegistry(_registry().routes, portal=portal)


def _run_with(
    tmp_path: Path, registry: RouteRegistry, *, title: str | None = None
) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = run_build(
        registry,
        _all(),
        pages=(),
        private_root=tmp_path / "private",
        public_root=tmp_path / "public",
        agent="bob",
        base_url=BASE_URL,
        title=title,
        out=lines.append,
    )
    return code, lines


def test_build_renders_the_portal_from_public_pages_only(tmp_path: Path) -> None:
    code, lines = _run_with(tmp_path, _portal_registry(), title="Bob <vitals>")
    assert code == 0
    assert "ok   portal" in lines
    html = (tmp_path / "public/index.html").read_text()
    assert "<title>Bob &lt;vitals&gt;</title>" in html
    assert 'data-route="services"' in html
    assert 'data-route="cost"' not in html  # private route


def test_portal_title_defaults_to_the_agent(tmp_path: Path) -> None:
    _run_with(tmp_path, _portal_registry())
    assert "<title>bob</title>" in (tmp_path / "public/index.html").read_text()


def test_no_portal_route_means_no_portal_file(tmp_path: Path) -> None:
    code, lines = _run_with(tmp_path, _registry())
    assert code == 0
    assert "ok   portal" not in lines
    assert not (tmp_path / "public/index.html").exists()


def test_portal_output_path_cannot_escape_the_public_root(tmp_path: Path) -> None:
    # The route registry now refuses an escaping output_path at registration,
    # before the build CLI can see it.
    with pytest.raises(RouteRegistryError, match="contained relative path"):
        _portal_registry("../outside.html")
    assert not (tmp_path / "outside.html").exists()


def test_partial_build_keeps_blind_pages_visible_on_the_portal(tmp_path: Path) -> None:
    run_build(
        _portal_registry(),
        _all(),
        pages=("services",),
        private_root=tmp_path / "private",
        public_root=tmp_path / "public",
        agent="bob",
        base_url=BASE_URL,
        out=lambda _: None,
    )
    html = (tmp_path / "public/index.html").read_text()
    sessions_card = html.split('data-route="sessions"')[1].split("</a>")[0]
    assert "no data" in sessions_card


def test_relative_headline_without_base_url_builds_nothing(tmp_path: Path) -> None:
    called: list[str] = []
    inner = _emitter("services", Status.HEALTHY)

    def emit() -> PageEnvelope:
        called.append("services")
        envelope: PageEnvelope = inner()
        return envelope

    with pytest.raises(UsageError, match="supply base_url"):
        run_build(
            _registry(),
            _all(services=emit),
            pages=(),
            private_root=tmp_path / "private",
            public_root=tmp_path / "public",
            agent="bob",
            out=lambda _: None,
        )
    assert called == []
    assert not (tmp_path / "public").exists()


def test_main_headline_link_is_absolute(app: Path) -> None:
    assert main(_argv(app)) == 0
    headline = json.loads((app / "public/headline.json").read_text())
    assert headline["link"] == "https://bob.example/a/"


def test_main_without_base_url_exits_2(
    app: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [x for x in _argv(app) if x not in ("--base-url", BASE_URL)]
    assert main(argv) == 2
    assert "base_url" in capsys.readouterr().err
    assert not (app / "public").exists()


def test_timers_pass_the_base_url_through(app: Path) -> None:
    assert main(_timers_argv(app)) == 0
    service = (app / "units/gptme-vitals-a.service").read_text()
    assert f'"--base-url" "{BASE_URL}"' in service


def test_timers_without_base_url_for_relative_headline_exit_2(app: Path) -> None:
    argv = [x for x in _timers_argv(app) if x not in ("--base-url", BASE_URL)]
    assert main(argv) == 2
    assert not (app / "units").exists()


def test_timers_pass_the_title_through(app: Path) -> None:
    assert main(_timers_argv(app, **{"--title": "My Portal"})) == 0
    assert '"--title" "My Portal"' in (app / "units/gptme-vitals-a.service").read_text()


def test_served_files_are_world_readable(tmp_path: Path) -> None:
    import stat

    code, _ = _run_with(tmp_path, _portal_registry())
    assert code == 0
    for name in ("core.json", "headline.json", "index.html", "services/summary.json"):
        mode = stat.S_IMODE((tmp_path / "public" / name).stat().st_mode)
        assert mode == 0o644, name
    assert (
        stat.S_IMODE((tmp_path / "private/cost/summary.json").stat().st_mode) == 0o600
    )


def test_core_write_failure_is_reported_not_raised(tmp_path: Path) -> None:
    (tmp_path / "public/core.json").mkdir(parents=True)  # os.replace onto a directory
    code, lines = _run(tmp_path, _all())
    assert code == 1
    assert any(line.startswith("FAIL core:") for line in lines)


def test_registry_without_a_headline_fails_before_any_page_is_built(
    tmp_path: Path,
) -> None:
    registry = RouteRegistry((_route("services", core_dimension="services"),))
    with pytest.raises(UsageError, match="is_headline"):
        run_build(
            registry,
            {"services": _emitter("services", Status.HEALTHY)},
            pages=(),
            private_root=tmp_path / "private",
            public_root=tmp_path / "public",
            agent="bob",
            base_url=BASE_URL,
            out=lambda _: None,
        )
    assert not (tmp_path / "private").exists()


def test_colliding_outputs_fail_before_any_page_is_built(tmp_path: Path) -> None:
    from gptme_vitals.publish import PublishError

    registry = RouteRegistry(
        (
            _route("services", core_dimension="services", is_headline=True),
            _route("sessions", summary_source="services/summary.json"),
        )
    )
    with pytest.raises(PublishError, match="more than one output"):
        run_build(
            registry,
            {"services": _emitter("services", Status.HEALTHY)},
            pages=(),
            private_root=tmp_path / "private",
            public_root=tmp_path / "public",
            agent="bob",
            base_url=BASE_URL,
            out=lambda _: None,
        )
    assert not (tmp_path / "private").exists()


def test_timers_resolve_relative_roots_to_absolute_paths(
    app: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(app)
    argv = _timers_argv(
        app, **{"--private-root": "rel-private", "--public-root": "rel-public"}
    )
    assert main(argv) == 0
    service = (app / "units/gptme-vitals-a.service").read_text()
    assert f'"--private-root" "{app / "rel-private"}"' in service
    assert f'"--public-root" "{app / "rel-public"}"' in service


def test_collection_and_shared_writes_happen_under_the_build_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib

    from gptme_vitals import cli

    held = False
    seen: dict[str, bool] = {}
    real_lock = cli.build_lock
    real_publish = cli.publish_page
    real_core = cli.write_core_files
    real_text = cli.write_text

    @contextlib.contextmanager
    def tracking_lock(root: Path):  # noqa: ANN202
        nonlocal held
        with real_lock(root):
            held = True
            try:
                yield
            finally:
                held = False

    def spy(name: str, fn):  # noqa: ANN001, ANN202
        def wrapped(*a, **kw):  # noqa: ANN002, ANN003, ANN202
            seen[name] = held
            return fn(*a, **kw)

        return wrapped

    monkeypatch.setattr(cli, "build_lock", tracking_lock)
    monkeypatch.setattr(cli, "publish_page", spy("publish", real_publish))
    monkeypatch.setattr(cli, "write_core_files", spy("core", real_core))
    monkeypatch.setattr(cli, "write_text", spy("portal", real_text))

    def emitting() -> PageEnvelope:
        seen["emitter"] = held
        return PageEnvelope(
            slug="services",
            status=Status.HEALTHY,
            headline="services",
            generated_at=NOW,
        )

    code, _ = _run_with_emitters(tmp_path, _portal_registry(), _all(services=emitting))
    assert code == 0
    assert seen == {"emitter": True, "publish": True, "core": True, "portal": True}


def _run_with_emitters(
    tmp_path: Path, registry: RouteRegistry, emitters: dict
) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = run_build(
        registry,
        emitters,
        pages=(),
        private_root=tmp_path / "private",
        public_root=tmp_path / "public",
        agent="bob",
        base_url=BASE_URL,
        out=lines.append,
    )
    return code, lines


def test_a_second_build_waits_for_the_first_to_release_the_lock(
    tmp_path: Path,
) -> None:
    import threading
    import time

    from gptme_vitals.cli import build_lock

    result: list[int] = []
    with build_lock(tmp_path / "private"):
        worker = threading.Thread(
            target=lambda: result.append(_run(tmp_path, _all())[0])
        )
        worker.start()
        time.sleep(0.3)
        assert result == []  # still blocked on the lock
        assert not (tmp_path / "public/core.json").exists()
    worker.join(timeout=10)
    assert result == [0]
    assert (tmp_path / "public/core.json").exists()


def test_unwritable_private_root_is_reported_not_raised(tmp_path: Path) -> None:
    (tmp_path / "private").write_text("a file where the directory should be")
    code, lines = _run(tmp_path, _all())
    assert code == 1
    assert any(line.startswith("FAIL lock:") for line in lines)
    assert not (tmp_path / "public").exists()


@pytest.mark.parametrize("failed_cost", [False, True])
def test_visibility_cleanup_does_not_depend_on_emitter_success_or_selection(
    tmp_path: Path, failed_cost: bool
) -> None:
    from gptme_vitals.publish import publish_page

    public_registry = RouteRegistry((_route("cost"),))
    publish_page(
        public_registry,
        _emitter("cost", Status.HEALTHY)(),
        private_root=tmp_path / "private",
        public_root=tmp_path / "public",
    )

    def boom() -> PageEnvelope:
        raise RuntimeError("cost collector unavailable")

    code, _ = _run(
        tmp_path,
        _all(cost=boom),
        pages=() if failed_cost else ("services",),
    )
    assert code == (1 if failed_cost else 0)
    assert not (tmp_path / "public/cost/summary.json").exists()
    assert (tmp_path / "private/cost/summary.json").exists()


def test_failed_public_write_keeps_previous_private_and_derived_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gptme_vitals import publish

    assert _run(tmp_path, _all())[0] == 0
    private = tmp_path / "private/sessions/summary.json"
    public = tmp_path / "public/sessions/summary.json"
    previous_private = private.read_text()
    previous_public = public.read_text()
    real_write = publish._atomic_write

    def fail_public(path: Path, content: str | bytes, *, mode: int) -> None:
        if path == public:
            raise PermissionError("served directory unwritable")
        real_write(path, content, mode=mode)

    monkeypatch.setattr(publish, "_atomic_write", fail_public)
    code, lines = _run(
        tmp_path, _all(sessions=_emitter("sessions", Status.DOWN)), pages=("sessions",)
    )
    assert code == 1
    assert any("FAIL sessions: PermissionError" in line for line in lines)
    assert private.read_text() == previous_private
    assert public.read_text() == previous_public
    assert _core(tmp_path)["activity"]["status"] == "healthy"


@pytest.mark.parametrize("root_name", ["private", "public"])
def test_aliased_output_is_refused_before_collection_or_writes(
    tmp_path: Path,
    root_name: str,
) -> None:
    root = tmp_path / root_name
    root.mkdir()
    (root / "alias").symlink_to(root, target_is_directory=True)
    registry = RouteRegistry(
        (
            _route("services", is_headline=True),
            _route("sessions", summary_source="alias/.gptme-vitals.lock"),
        )
    )
    collected: list[bool] = []

    def emit() -> PageEnvelope:
        collected.append(True)
        return PageEnvelope(
            slug="services",
            status=Status.HEALTHY,
            headline="services",
            generated_at=NOW,
        )

    with pytest.raises(ValueError, match="symlink"):
        run_build(
            registry,
            {"services": emit},
            pages=(),
            private_root=tmp_path / "private",
            public_root=tmp_path / "public",
            agent="bob",
            base_url=BASE_URL,
        )
    assert not collected
    assert not (tmp_path / "private/.gptme-vitals.lock").exists()
    assert not (tmp_path / "private/services").exists()


@pytest.mark.parametrize("failure", ["derive", "write"])
def test_private_transition_revokes_portal_even_when_core_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from dataclasses import replace

    from gptme_vitals import cli
    from gptme_vitals.build import BuildError

    private_registry = _portal_registry()
    public_registry = RouteRegistry(
        tuple(
            replace(route, visibility=Visibility.PUBLIC)
            for route in private_registry.routes
        ),
        portal=private_registry.portal,
    )
    emitters = _all(
        cost=_emitter("cost", Status.HEALTHY, lines=("sensitive cost line",))
    )
    assert _run_with_emitters(tmp_path, public_registry, emitters)[0] == 0
    portal = tmp_path / "public/index.html"
    assert "sensitive cost line" in portal.read_text()

    def fail(*args: object, **kwargs: object) -> None:
        if failure == "derive":
            raise BuildError("injected derivation failure")
        raise PermissionError("injected core write failure")

    monkeypatch.setattr(
        cli, "derive_core" if failure == "derive" else "write_core_files", fail
    )
    code, lines = _run_with_emitters(tmp_path, private_registry, emitters)
    assert code == 1
    assert any(line.startswith("FAIL core:") for line in lines)
    assert not (tmp_path / "public/cost/summary.json").exists()
    assert not portal.exists() or "sensitive cost line" not in portal.read_text()


def test_timers_refuse_scheduled_routes_without_emitters_before_writing(
    app: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib

    vitals_app = importlib.import_module("vitals_app")
    setattr(
        vitals_app,
        "REGISTRY",
        RouteRegistry((*vitals_app.REGISTRY.routes, _route("b"))),
    )
    assert main(_timers_argv(app)) == 2
    assert "no emitter registered for scheduled page 'b'" in capsys.readouterr().err
    assert not (app / "units").exists()


def test_private_headline_revokes_previous_shared_headline(tmp_path: Path) -> None:
    from dataclasses import replace

    registry = _portal_registry()
    assert _run_with_emitters(tmp_path, registry, _all())[0] == 0
    headline = tmp_path / "public/headline.json"
    assert headline.exists()
    private = RouteRegistry(
        tuple(
            replace(route, visibility=Visibility.PRIVATE)
            if route.is_headline
            else route
            for route in registry.routes
        ),
        portal=registry.portal,
    )
    assert _run_with_emitters(tmp_path, private, _all())[0] == 1
    assert not headline.exists()


@pytest.mark.parametrize("root_name", ["private", "public"])
def test_timers_refuse_directory_alias_before_writing(
    app: Path, root_name: str
) -> None:
    root = app / root_name
    root.mkdir()
    (root / "a").symlink_to(root, target_is_directory=True)
    assert main(_timers_argv(app)) == 2
    assert not (app / "units").exists()


def test_reassigned_headline_cannot_leave_former_private_text(tmp_path: Path) -> None:
    from dataclasses import replace

    registry = RouteRegistry((_route("services", is_headline=True), _route("sessions")))
    assert (
        _run_with_emitters(
            tmp_path,
            registry,
            {
                "services": _emitter(
                    "services", Status.HEALTHY, lines=("former public secret",)
                )
            },
        )[0]
        == 0
    )
    headline = tmp_path / "public/headline.json"
    assert "former public secret" in headline.read_text()
    changed = RouteRegistry(
        (
            replace(
                registry.routes[0], visibility=Visibility.PRIVATE, is_headline=False
            ),
            replace(registry.routes[1], is_headline=True),
        )
    )

    def boom() -> PageEnvelope:
        raise RuntimeError("new headline collector unavailable")

    assert _run_with_emitters(tmp_path, changed, {"sessions": boom})[0] == 1
    assert not headline.exists()


def test_timers_refuse_emitter_without_envelope_route(
    app: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib

    vitals_app = importlib.import_module("vitals_app")
    vitals_app.EMITTERS["ghost"] = _emitter("ghost", Status.HEALTHY)
    assert main(_timers_argv(app)) == 2
    assert "no summary_source route" in capsys.readouterr().err
    assert not (app / "units").exists()


@pytest.mark.parametrize("has_summary", [False, True])
def test_partial_build_revokes_private_html_even_without_an_envelope(
    tmp_path: Path, has_summary: bool
) -> None:
    html = tmp_path / "public/cost/index.html"
    html.parent.mkdir(parents=True)
    html.write_text("formerly public cost page")
    registry = RouteRegistry(
        (
            _route("services", is_headline=True),
            _route(
                "cost",
                visibility=Visibility.PRIVATE,
                summary_source="cost/summary.json" if has_summary else None,
            ),
        )
    )
    code, _ = _run_with_emitters(
        tmp_path, registry, {"services": _emitter("services", Status.HEALTHY)}
    )
    assert code == 0
    assert not html.exists()


def test_private_route_losing_envelope_still_revokes_shared_text(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    registry = _portal_registry()
    assert (
        _run_with_emitters(
            tmp_path,
            registry,
            {
                "services": _emitter(
                    "services", Status.HEALTHY, lines=("old public text",)
                )
            },
        )[0]
        == 0
    )
    changed = RouteRegistry(
        tuple(
            replace(
                route,
                visibility=Visibility.PRIVATE,
                summary_source=None,
                core_dimension=None,
                is_headline=False,
            )
            if route.slug in ("services", "cost")
            else replace(route, is_headline=True)
            for route in registry.routes
        ),
        portal=registry.portal,
    )

    def boom() -> PageEnvelope:
        raise RuntimeError("new headline unavailable")

    assert _run_with_emitters(tmp_path, changed, {"sessions": boom})[0] == 1
    assert not (tmp_path / "public/headline.json").exists()
    assert not (tmp_path / "public/index.html").exists()


@pytest.mark.parametrize("new_summary", [None, "moved/summary.json"])
def test_private_transition_revokes_previous_summary_path(
    tmp_path: Path, new_summary: str | None
) -> None:
    from dataclasses import replace

    registry = _portal_registry()
    assert _run_with_emitters(tmp_path, registry, _all())[0] == 0
    changed = RouteRegistry(
        tuple(
            replace(
                route,
                visibility=Visibility.PRIVATE,
                summary_source=new_summary,
                core_dimension=None,
                is_headline=False,
            )
            if route.slug == "services"
            else replace(route, is_headline=True)
            if route.slug == "sessions"
            else route
            for route in registry.routes
        ),
        portal=registry.portal,
    )
    assert (
        _run_with_emitters(
            tmp_path, changed, {"sessions": _emitter("sessions", Status.HEALTHY)}
        )[0]
        == 0
    )
    assert not (tmp_path / "public/services/summary.json").exists()


@pytest.mark.parametrize("change", ["manual", "removed", "no-envelope"])
def test_timer_regeneration_removes_obsolete_generated_pairs(
    app: Path, change: str
) -> None:
    import importlib
    from dataclasses import replace

    vitals_app = importlib.import_module("vitals_app")
    setattr(
        vitals_app,
        "REGISTRY",
        RouteRegistry((*vitals_app.REGISTRY.routes, _route("b"))),
    )
    vitals_app.EMITTERS["b"] = _emitter("b", Status.HEALTHY)
    assert main(_timers_argv(app)) == 0
    unrelated = app / "units/gptme-vitals-custom.service"
    unrelated.write_text("[Service]\nExecStart=/bin/true\n")
    other_prefix = app / "units/other-b.timer"
    other_prefix.write_text("keep")
    b = vitals_app.REGISTRY.routes[1]
    if change == "manual":
        b = replace(b, cadence=RefreshCadence.MANUAL)
    elif change == "no-envelope":
        b = replace(b, summary_source=None)
    setattr(
        vitals_app,
        "REGISTRY",
        RouteRegistry(
            (vitals_app.REGISTRY.routes[0],)
            if change == "removed"
            else (vitals_app.REGISTRY.routes[0], b)
        ),
    )
    vitals_app.EMITTERS.pop("b")
    assert main(_timers_argv(app)) == 0
    assert not (app / "units/gptme-vitals-b.timer").exists()
    assert not (app / "units/gptme-vitals-b.service").exists()
    assert (app / "units/gptme-vitals-a.timer").exists()
    assert unrelated.read_text() == "[Service]\nExecStart=/bin/true\n"
    assert other_prefix.read_text() == "keep"


@pytest.mark.parametrize("failure", ["mkdir", "write"])
def test_timer_output_errors_return_operational_failure(
    app: Path,
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if failure == "mkdir":
        (app / "units").write_text("not a directory")
    else:
        real_write = Path.write_text

        def fail_write(path: Path, *args: Any, **kwargs: Any) -> int:
            if path.suffix == ".timer":
                raise PermissionError("timer destination unavailable")
            return real_write(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", fail_write)
    assert main(_timers_argv(app)) == 1
    assert "gptme-vitals:" in capsys.readouterr().err
