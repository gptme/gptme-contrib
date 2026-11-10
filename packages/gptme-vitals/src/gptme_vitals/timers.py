"""Render one systemd service+timer pair per page from the registry's ``cadence``.

A single whole-tree build either over-refreshes cheap pages or under-refreshes
expensive ones, so each page that emits an envelope gets its own unit pair that
runs ``gptme-vitals build <slug>``. This module only *renders text*; it never
touches systemd. The caller decides where the files go and enables them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from gptme_vitals.routes import DashboardRoute, RefreshCadence, RouteRegistry

#: ``OnCalendar`` expressions. ``MANUAL`` and ``N_A`` have no schedule, so they
#: get no unit rather than one that would silently never (or always) fire.
CALENDAR: dict[RefreshCadence, str] = {
    RefreshCadence.FIVE_MIN: "*:0/5",
    RefreshCadence.TEN_MIN: "*:0/10",
    RefreshCadence.FIFTEEN_MIN: "*:0/15",
    RefreshCadence.TWENTY_MIN: "*:0/20",
    RefreshCadence.THIRTY_MIN: "*:0/30",
    RefreshCadence.HOURLY: "hourly",
}

#: The slug becomes part of a unit file name, so it is restricted to a safe set.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_PREFIX = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class TimerError(ValueError):
    """The registry or arguments cannot be turned into valid unit files."""


@dataclass(frozen=True, slots=True)
class UnitPair:
    """A rendered service and timer for one page."""

    slug: str
    service_name: str
    service: str
    timer_name: str
    timer: str


def _quote(arg: str) -> str:
    """Quote one ``ExecStart`` argument the way systemd parses it.

    Not ``shlex``: systemd expands ``%`` specifiers and ``$`` variables inside
    the value, treats ``\\`` as an escape, and splits on unquoted whitespace.
    A newline would end the directive, so it is refused rather than escaped.
    """
    if "\n" in arg or "\r" in arg or "\0" in arg:
        raise TimerError(f"argument {arg!r} contains a control character")
    escaped = (
        arg.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def render_unit_pair(
    route: DashboardRoute,
    *,
    executable: str,
    build_args: Sequence[str],
    unit_prefix: str = "gptme-vitals",
    timeout_seconds: int = 300,
) -> UnitPair:
    """Render the pair for ``route``; the command is ``executable build <slug> *build_args``."""
    if not PurePosixPath(executable).is_absolute():
        raise TimerError(f"executable must be an absolute path, got {executable!r}")
    if not _PREFIX.fullmatch(unit_prefix):
        raise TimerError(f"unit_prefix {unit_prefix!r} is not a safe unit-name prefix")
    if not _SLUG.fullmatch(route.slug):
        raise TimerError(f"slug {route.slug!r} is not safe to use in a unit name")
    calendar = CALENDAR.get(route.cadence)
    if calendar is None:
        raise TimerError(
            f"route {route.slug!r} has cadence {route.cadence!r}: no schedule"
        )
    if timeout_seconds <= 0:
        raise TimerError("timeout_seconds must be positive")
    name = f"{unit_prefix}-{route.slug}"
    command = " ".join(
        _quote(a) for a in (executable, "build", route.slug, *build_args)
    )
    service = (
        "[Unit]\n"
        f"Description=Refresh the {route.slug} dashboard page\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"TimeoutStartSec={timeout_seconds}\n"
        f"ExecStart={command}\n"
    )
    timer = (
        "[Unit]\n"
        f"Description=Refresh the {route.slug} dashboard page ({route.cadence})\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar={calendar}\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return UnitPair(
        slug=route.slug,
        service_name=f"{name}.service",
        service=service,
        timer_name=f"{name}.timer",
        timer=timer,
    )


def render_units(
    registry: RouteRegistry,
    *,
    executable: str,
    build_args: Sequence[str],
    unit_prefix: str = "gptme-vitals",
    timeout_seconds: int = 300,
) -> tuple[list[UnitPair], list[str]]:
    """Render a pair for every scheduled envelope route.

    Returns ``(pairs, unscheduled)``; ``unscheduled`` lists the envelope routes
    skipped because their cadence is ``manual`` or ``n/a``, so the caller can
    say so instead of leaving a page nobody refreshes.
    """
    pairs: list[UnitPair] = []
    unscheduled: list[str] = []
    for route in registry.envelope_routes():
        if route.cadence not in CALENDAR:
            unscheduled.append(route.slug)
            continue
        pairs.append(
            render_unit_pair(
                route,
                executable=executable,
                build_args=build_args,
                unit_prefix=unit_prefix,
                timeout_seconds=timeout_seconds,
            )
        )
    return pairs, unscheduled
