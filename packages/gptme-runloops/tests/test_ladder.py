"""Contract tests for the deepen ladder and the bound-session gate (#2310)."""

from __future__ import annotations

import pytest
from gptme_runloops.ladder import (
    Rung,
    RungOutput,
    bound_session_gate,
    run_ladder,
)


def _rung(name, items, note="", calls=None):
    def produce():
        if calls is not None:
            calls.append(name)
        return RungOutput(items=items, note=note)

    return Rung(name=name, produce=produce)


def _file_all(_name, items):
    return list(items)


def test_stops_once_deficit_is_filled_and_skips_later_rungs():
    calls: list[str] = []
    rungs = [
        _rung("own-prs", ["a", "b"], calls=calls),
        _rung("miners", ["c", "d"], calls=calls),
        _rung("bug-hunt", ["e"], calls=calls),
    ]
    res = run_ladder(rungs, deficit=3, file_items=_file_all)
    assert res.filed == ["a", "b", "c"]
    assert res.produced_by == "own-prs"
    assert calls == ["own-prs", "miners"]  # bug-hunt never paid for
    assert res.rungs["miners"].emitted == 2
    assert res.rungs["miners"].filed == 1
    assert "bug-hunt" not in res.rungs
    assert res.remaining == 0
    assert not res.idle_on_purpose


def test_all_empty_records_zero_per_rung_and_idles_on_purpose():
    rungs = [
        _rung("own-prs", []),
        _rung("judgment-waits", [], note="unavailable: producer not built"),
    ]
    filed_calls: list[str] = []

    def file_items(name, items):
        filed_calls.append(name)
        return list(items)

    res = run_ladder(rungs, deficit=2, file_items=file_items)
    assert res.idle_on_purpose
    assert res.produced_by is None
    assert list(res.rungs) == ["own-prs", "judgment-waits"]
    assert res.rungs["judgment-waits"].note.startswith("unavailable")
    assert filed_calls == []  # never called with an empty list
    assert res.summary() == "produced_by=none own-prs=0/0 judgment-waits=0/0"


def test_dedupe_shortfall_falls_through_to_next_rung():
    def file_items(name, items):
        return [] if name == "own-prs" else list(items)  # all own-prs were dupes

    res = run_ladder(
        [_rung("own-prs", ["a"]), _rung("follow-through", ["b"])],
        deficit=1,
        file_items=file_items,
    )
    assert res.produced_by == "follow-through"
    assert res.rungs["own-prs"].emitted == 1
    assert res.rungs["own-prs"].filed == 0
    assert res.filed == ["b"]


@pytest.mark.parametrize(
    ("candidates", "expected_batches", "expected_filed", "later_runs"),
    [
        (
            ["dup", "dup", "a", "b", "unused"],
            [["dup", "dup"], ["a", "b"]],
            ["a", "b"],
            False,
        ),
        (["a", "dup", "dup", "b"], [["a", "dup"], ["dup"], ["b"]], ["a", "b"], False),
        (["dup", "dup", "a"], [["dup", "dup"], ["a"]], ["a", "fallback"], True),
    ],
)
def test_dedupe_exhausts_current_rung_before_running_heavier_sources(
    candidates, expected_batches, expected_filed, later_runs
):
    calls: list[str] = []
    batches: list[list[str]] = []
    persisted: list[str] = []

    def file_items(name, items):
        assert 0 < len(items) <= 2 - len(persisted)
        if name == "own-prs":
            batches.append(list(items))
        fresh = [item for item in items if item != "dup"]
        persisted.extend(fresh)
        return fresh

    res = run_ladder(
        [
            _rung("own-prs", candidates, calls=calls),
            _rung("heavy", ["fallback"], calls=calls),
        ],
        deficit=2,
        file_items=file_items,
    )
    assert res.filed == persisted == expected_filed
    assert batches == expected_batches
    assert calls == (["own-prs", "heavy"] if later_runs else ["own-prs"])
    assert res.rungs["own-prs"].emitted == len(candidates)
    assert res.rungs["own-prs"].filed == (1 if later_runs else 2)
    assert res.produced_by == "own-prs"
    assert res.remaining == 0
    assert not res.idle_on_purpose


def test_filed_identities_are_the_hosts_actual_dedupe_result():
    persisted: list[str] = []

    def file_items(_name, items):
        fresh = [item for item in items if item != "dup"]
        persisted.extend(fresh)
        return fresh

    res = run_ladder(
        [_rung("own-prs", ["dup", "a", "dup", "b", "c"])],
        deficit=3,
        file_items=file_items,
    )
    assert res.filed == persisted == ["a", "b", "c"]
    assert res.rungs["own-prs"].filed == 3
    assert res.remaining == 0


def test_broken_rung_is_recorded_and_ladder_continues():
    def boom():
        raise RuntimeError("manifest stale")

    res = run_ladder(
        [Rung("own-prs", boom), _rung("miners", ["x"])],
        deficit=1,
        file_items=_file_all,
    )
    assert res.rungs["own-prs"].error == "RuntimeError: manifest stale"
    assert res.produced_by == "miners"


def test_broken_rung_shape_is_recorded_not_raised():
    """A rung that returns the wrong object fails only that rung."""

    class Wrong:
        pass

    res = run_ladder(
        [Rung("own-prs", lambda: Wrong()), _rung("miners", ["x"])],
        deficit=1,
        file_items=_file_all,
    )
    assert res.rungs["own-prs"].error.startswith("AttributeError")
    assert res.produced_by == "miners"
    assert res.filed == ["x"]


def test_rung_named_none_is_not_confused_with_the_no_producer_sentinel():
    """A rung may be called ``none``; a later filer must not take its credit."""
    res = run_ladder(
        [_rung("none", ["a"]), _rung("miners", ["b"])],
        deficit=2,
        file_items=_file_all,
    )
    assert res.produced_by == "none"
    assert res.filed == ["a", "b"]
    assert res.summary().startswith("produced_by=none none=1/1 miners=1/1")


def test_failed_sources_do_not_look_like_a_clean_idle_tick():
    def boom():
        raise RuntimeError("source unavailable")

    res = run_ladder([Rung("broken", boom), _rung("empty", [])], 1, _file_all)
    assert not res.idle_on_purpose
    assert "RuntimeError: source unavailable" in res.summary()
    assert "empty=0/0" in res.summary()


@pytest.mark.parametrize("deficit", [0, 1, 3])
def test_duplicate_names_are_rejected_before_any_side_effects(deficit):
    calls: list[str] = []
    with pytest.raises(ValueError, match="duplicate"):
        run_ladder(
            [_rung("a", ["x"], calls=calls), _rung("a", [], calls=calls)],
            deficit,
            lambda name, items: calls.append("filed") or list(items),
        )
    assert calls == []


def test_zero_deficit_runs_nothing():
    calls: list[str] = []
    res = run_ladder([_rung("own-prs", ["a"], calls=calls)], 0, _file_all)
    assert calls == []
    assert res.rungs == {}
    assert not res.idle_on_purpose


def test_file_items_returning_too_many_items_raises():
    with pytest.raises(ValueError, match="file_items"):
        run_ladder([_rung("own-prs", ["a"])], 1, lambda _n, _i: ["a", "b"])


def test_negative_deficit_and_duplicate_names_raise():
    with pytest.raises(ValueError, match="deficit"):
        run_ladder([], -1, _file_all)
    with pytest.raises(ValueError, match="duplicate"):
        run_ladder([_rung("a", []), _rung("a", [])], 1, _file_all)


@pytest.mark.parametrize(
    ("item", "proceed", "cause"),
    [("task-1", True, "bound"), (None, False, "unbound"), ("  ", False, "unbound")],
)
def test_bound_session_gate(item, proceed, cause):
    d = bound_session_gate(item)
    assert d.proceed is proceed
    assert d.cause == cause
