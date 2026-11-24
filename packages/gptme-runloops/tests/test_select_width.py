"""Shadow width: one linear target per account, rank as a deadband."""

from __future__ import annotations

from datetime import date

import pytest
from gptme_runloops.select_policy import PoolPolicy, parse_routing
from gptme_runloops.select_rules import compute_width, format_shell
from gptme_runloops.select_usage import WEEK_SECONDS, PoolReading, week_elapsed


def pool(**kw) -> PoolPolicy:
    return PoolPolicy(name="claude-alice", **{"max_width": 16, **kw})


def at(used: float | None, elapsed: float | None) -> PoolReading:
    return PoolReading(used, None, 10.0, "fake", elapsed=elapsed)


@pytest.mark.parametrize(
    ("kw", "used", "elapsed", "width"),
    [
        # 2026-11-24 alice: 29% used at 96% of the week, owner reserve 0.95
        ({"end_target": 0.95}, 0.29, 0.96, 16),
        # erik: 30% used at 58%, Bob is rank 1 -> far past the band, near full
        ({"rank": 1}, 0.30, 0.58, 16),
        # owner slightly behind: widens a little
        ({}, 0.45, 0.50, 1 + round(0.04 / 0.10 * 15)),
        # overflow inside its 2-point band stays shut
        ({"rank": 1}, 0.48, 0.50, 0),
        # ahead of target: overflow takes nothing
        ({"rank": 1}, 0.60, 0.50, 0),
        # at end target: nobody widens
        ({}, 0.98, 0.70, 0),
    ],
)
def test_width(kw, used, elapsed, width):
    assert compute_width(pool(**kw), at(used, elapsed))[0] == width


@pytest.mark.parametrize("rank", [0, 1, 2])
@pytest.mark.parametrize("offset", [-0.000001, 0.0, 0.000001])
def test_rank_band_boundary(rank, offset):
    # At half-week the target is 0.49; equality must not open the consumer.
    used = 0.49 - rank * 0.02 + offset
    width, _ = compute_width(pool(rank=rank), at(used, 0.5))
    if offset < 0:
        assert width is not None and width > 0
    else:
        assert width == (None if rank == 0 else 0)


def test_owner_on_pace_has_no_opinion():
    width, why = compute_width(pool(), at(0.50, 0.50))
    assert width is None and "owner width unchanged" in why


@pytest.mark.parametrize("reading", [at(None, 0.5), at(0.2, None)])
def test_unknown_usage_or_reset_has_no_opinion(reading):
    assert compute_width(pool(), reading)[0] is None


def test_no_max_width_has_no_opinion():
    assert compute_width(PoolPolicy(name="p"), at(0.1, 0.9)) == (
        None,
        "no width policy",
    )


def test_week_elapsed():
    data = {"seven_day": {"utilization": 0.3, "resets_in_seconds": WEEK_SECONDS / 4}}
    assert week_elapsed(data) == 0.75
    assert week_elapsed({"seven_day": {"utilization": 0.3}}) is None
    assert week_elapsed(None) is None


def test_pool_fields_parse_and_validate():
    raw = {
        "routing": {"schema_version": 1},
        "pools": {
            "a": {"end_target": 0.95, "rank": 1, "max_width": 16},
            "b": {"rank": -1, "max_width": 0, "end_target": 1.5},
        },
    }
    policy = parse_routing(raw, date(2026, 11, 24))
    a = policy.pools["a"]
    assert (a.end_target, a.rank, a.max_width) == (0.95, 1, 16)
    errors = {i.where for i in policy.issues if i.level == "error"}
    assert {"pools.b.rank", "pools.b.max_width", "pools.b.end_target"} <= errors


def test_shell_output_carries_width():
    assert "ROUTE_WIDTH=3" in format_shell({"width": 3})
    assert "ROUTE_WIDTH=''" in format_shell({"width": None})
