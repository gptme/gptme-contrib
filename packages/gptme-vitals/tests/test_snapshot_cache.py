"""Tests for gptme_vitals.snapshot_cache."""

from __future__ import annotations

import json
import sys

import pytest
from gptme_vitals import snapshot_cache as sc


@pytest.fixture(autouse=True)
def clean_cache_environment(monkeypatch):
    for name in (sc.TTL_ENV, sc.DIR_ENV, sc.REFRESH_ENV):
        monkeypatch.delenv(name, raising=False)


def test_miss_then_hit_within_ttl(tmp_path):
    calls: list[int] = []

    def compute():
        calls.append(1)
        return {"n": len(calls)}

    first = sc.cached_signal(
        "sig", compute, cache_dir=tmp_path, ttl_seconds=60, now=1000.0
    )
    second = sc.cached_signal(
        "sig", compute, cache_dir=tmp_path, ttl_seconds=60, now=1030.0
    )
    assert first == second == {"n": 1}
    assert len(calls) == 1
    assert (tmp_path / "sig.json").exists()


def test_expired_snapshot_recomputes(tmp_path):
    calls: list[int] = []

    def compute():
        calls.append(1)
        return {"n": len(calls)}

    sc.cached_signal("sig", compute, cache_dir=tmp_path, ttl_seconds=60, now=1000.0)
    later = sc.cached_signal(
        "sig", compute, cache_dir=tmp_path, ttl_seconds=60, now=1061.0
    )
    assert later == {"n": 2}
    assert len(calls) == 2


def test_key_mismatch_is_a_miss(tmp_path):
    sc.cached_signal(
        "kpi", lambda: {"days": 7}, cache_dir=tmp_path, ttl_seconds=60, key="7", now=0.0
    )
    value = sc.cached_signal(
        "kpi",
        lambda: {"days": 30},
        cache_dir=tmp_path,
        ttl_seconds=60,
        key="30",
        now=1.0,
    )
    assert value == {"days": 30}


def test_colliding_signal_names_never_serve_each_others_values(tmp_path):
    for name, value in [("a b", 1), ("a_b", 2), ("a b", 3)]:
        assert (
            sc.cached_signal(
                name, lambda: value, cache_dir=tmp_path, ttl_seconds=60, now=0.0
            )
            == value
        )
        assert (
            sc.cached_signal(
                name,
                lambda: pytest.fail("same-name snapshot should hit"),
                cache_dir=tmp_path,
                ttl_seconds=60,
                now=1.0,
            )
            == value
        )


def test_ttl_zero_disables_reads_and_writes(tmp_path):
    calls: list[int] = []

    def compute():
        calls.append(1)
        return {"n": len(calls)}

    sc.cached_signal("sig", compute, cache_dir=tmp_path, ttl_seconds=0)
    sc.cached_signal("sig", compute, cache_dir=tmp_path, ttl_seconds=0)
    assert len(calls) == 2
    assert not list(tmp_path.iterdir())


def test_non_roundtrippable_values_are_never_cached(tmp_path):
    """A value whose JSON round-trip differs (tuple, set, datetime, int keys)
    must be recomputed every time so consumers never see a type change."""
    calls: list[int] = []

    def compute():
        calls.append(1)
        return {"pair": (1, 2), 1: "int-key"}

    first = sc.cached_signal(
        "odd", compute, cache_dir=tmp_path, ttl_seconds=60, now=0.0
    )
    second = sc.cached_signal(
        "odd", compute, cache_dir=tmp_path, ttl_seconds=60, now=1.0
    )
    assert first == second == {"pair": (1, 2), 1: "int-key"}
    assert len(calls) == 2
    assert not (tmp_path / "odd.json").exists()
    assert sc.write_snapshot(tmp_path, "s", {"a": {1, 2}}) is False


@pytest.mark.parametrize("value", [{"pair": (1, 2)}, {"set": {1, 2}}])
def test_uncacheable_refresh_invalidates_previous_snapshot(tmp_path, value):
    assert sc.write_snapshot(tmp_path, "sig", {"n": 1}, now=0.0)
    for now, refresh in [(1.0, True), (2.0, False)]:
        assert (
            sc.cached_signal(
                "sig",
                lambda: value,
                cache_dir=tmp_path,
                ttl_seconds=60,
                now=now,
                refresh=refresh,
            )
            is value
        )
    assert not (tmp_path / "sig.json").exists()


def test_deeply_nested_value_is_returned_without_caching(tmp_path):
    value: list[object] = []
    for _ in range(sys.getrecursionlimit() + 100):
        value = [value]
    assert (
        sc.cached_signal("deep", lambda: value, cache_dir=tmp_path, ttl_seconds=60)
        is value
    )
    assert not list(tmp_path.iterdir())


def test_served_value_is_identical_to_computed_value(tmp_path):
    value = {"a": [1, 2.5, "x", None, True], "b": {"c": {}}}
    sc.cached_signal("v", lambda: value, cache_dir=tmp_path, ttl_seconds=60, now=0.0)
    served = sc.cached_signal(
        "v",
        lambda: pytest.fail("hit expected"),
        cache_dir=tmp_path,
        ttl_seconds=60,
        now=1.0,
    )
    assert served == value


def test_env_ttl_default_and_override(monkeypatch):
    monkeypatch.delenv(sc.TTL_ENV, raising=False)
    assert sc.signal_cache_ttl_seconds() == sc.DEFAULT_TTL_SECONDS
    monkeypatch.setenv(sc.TTL_ENV, "0")
    assert sc.signal_cache_ttl_seconds() == 0
    monkeypatch.setenv(sc.TTL_ENV, "junk")
    assert sc.signal_cache_ttl_seconds() == sc.DEFAULT_TTL_SECONDS


def test_cache_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.delenv(sc.DIR_ENV, raising=False)
    assert sc.signal_cache_dir(tmp_path / "default") == tmp_path / "default"
    monkeypatch.setenv(sc.DIR_ENV, str(tmp_path / "elsewhere"))
    assert sc.signal_cache_dir(tmp_path) == tmp_path / "elsewhere"


def test_corrupt_snapshot_is_a_miss(tmp_path):
    (tmp_path / "sig.json").write_text("{not json")
    assert sc.cached_signal("sig", lambda: 42, cache_dir=tmp_path, ttl_seconds=60) == 42
    payload = json.loads((tmp_path / "sig.json").read_text())
    assert payload["value"] == 42


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_snapshot_timestamp_is_a_miss(tmp_path, timestamp):
    (tmp_path / "sig.json").write_text(
        json.dumps(
            {
                "name": "sig",
                "key": "",
                "collected_at_epoch": timestamp,
                "value": 1,
            }
        )
    )
    assert (
        sc.cached_signal(
            "sig",
            lambda: 42,
            cache_dir=tmp_path,
            ttl_seconds=60,
            now=1000.0,
        )
        == 42
    )
    assert (
        json.loads((tmp_path / "sig.json").read_text())["collected_at_epoch"] == 1000.0
    )


def test_deeply_nested_snapshot_is_a_miss(tmp_path):
    (tmp_path / "sig.json").write_text("[" * 10000 + "0" + "]" * 10000)
    assert sc.cached_signal("sig", lambda: 42, cache_dir=tmp_path, ttl_seconds=60) == 42
    assert json.loads((tmp_path / "sig.json").read_text())["value"] == 42


def test_unwritable_cache_dir_degrades_to_compute(tmp_path):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    assert sc.cached_signal(
        "sig", lambda: {"ok": 1}, cache_dir=blocker, ttl_seconds=60
    ) == {"ok": 1}


def test_refresh_bypasses_read_but_writes(tmp_path, monkeypatch):
    sc.cached_signal(
        "sig", lambda: {"n": 1}, cache_dir=tmp_path, ttl_seconds=60, now=0.0
    )
    refreshed = sc.cached_signal(
        "sig",
        lambda: {"n": 2},
        cache_dir=tmp_path,
        ttl_seconds=60,
        now=1.0,
        refresh=True,
    )
    assert refreshed == {"n": 2}
    served = sc.cached_signal(
        "sig",
        lambda: pytest.fail("hit expected"),
        cache_dir=tmp_path,
        ttl_seconds=60,
        now=2.0,
    )
    assert served == {"n": 2}
    monkeypatch.setenv(sc.REFRESH_ENV, "1")
    assert sc.signal_cache_refresh() is True
    assert sc.cached_signal(
        "sig", lambda: {"n": 3}, cache_dir=tmp_path, ttl_seconds=60, now=3.0
    ) == {"n": 3}
