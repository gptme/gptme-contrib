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
