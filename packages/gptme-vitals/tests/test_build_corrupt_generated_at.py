"""A stored envelope whose generated_at is not a string crashes the whole build."""

from __future__ import annotations

import json
from pathlib import Path

from gptme_vitals.cli import run_build
from gptme_vitals.envelope import PageEnvelope, Status
from gptme_vitals.routes import DashboardRoute, RefreshCadence, RouteKind, RouteRegistry


def _route(slug: str, **kw: object) -> DashboardRoute:
    base: dict[str, object] = dict(
        slug=slug,
        label=slug.title(),
        href=f"/{slug}/",
        kind=RouteKind.STATIC,
        section="d",
        cadence=RefreshCadence.HOURLY,
        description=slug,
        output_path=f"{slug}/index.html",
        summary_source=f"{slug}/summary.json",
    )
    base.update(kw)
    return DashboardRoute(**base)  # type: ignore[arg-type]


REG = RouteRegistry(
    (
        _route("services", core_dimension="services", is_headline=True),
        _route("sessions", core_dimension="activity"),
    )
)


def _emit(slug: str, status: Status, generated_at: object = "2026-11-24T00:00:00Z"):
    return lambda: PageEnvelope(
        slug=slug, status=status, headline=slug, generated_at=generated_at
    )  # type: ignore[arg-type]


def _build(tmp_path: Path, emitters: dict, pages=()) -> int:
    return run_build(
        REG,
        emitters,
        pages=pages,
        private_root=tmp_path / "priv",
        public_root=tmp_path / "pub",
        agent="a",
        base_url="https://x.example/",
        out=lambda _l: None,
    )


def test_corrupt_private_generated_at_is_a_blind_page_not_a_crash(
    tmp_path: Path,
) -> None:
    emitters = {
        "services": _emit("services", Status.HEALTHY),
        "sessions": _emit("sessions", Status.HEALTHY),
    }
    assert _build(tmp_path, emitters) == 0
    # Corrupt one stored private envelope: generated_at is a number.
    p = tmp_path / "priv/sessions/summary.json"
    data = json.loads(p.read_text())
    data["generated_at"] = 1732406400
    p.write_text(json.dumps(data))
    # A partial rebuild of the OTHER page: services just went down.
    emitters["services"] = _emit("services", Status.DOWN)
    _build(tmp_path, emitters, pages=("services",))  # must not raise
    core = json.loads((tmp_path / "pub/core.json").read_text())
    assert core["services"]["status"] == "down"


def test_emitter_with_epoch_generated_at_does_not_wedge_every_later_build(
    tmp_path: Path,
) -> None:
    emitters = {
        "services": _emit("services", Status.HEALTHY),
        "sessions": _emit("sessions", Status.HEALTHY, generated_at=1732406400),
    }
    _build(tmp_path, emitters)  # must not raise
    assert (tmp_path / "pub/core.json").exists()
