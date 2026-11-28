from __future__ import annotations

from gptme_vitals import snapshot_cache as sc


def test_future_dated_snapshot_is_not_fresh(tmp_path):
    # Written with a clock 1 day ahead (NTP step-back, copied cache): must not be
    # served as fresh for a day + TTL.
    assert sc.write_snapshot(tmp_path, "sig", {"v": "old"}, now=1_000_000.0 + 86_400)
    hit, _ = sc.read_snapshot(tmp_path, "sig", ttl_seconds=900, now=1_000_000.0)
    assert hit is False


def test_name_with_slash_is_cached(tmp_path):
    # _snapshot_path sanitises unsafe names, but mkstemp's prefix uses the raw name.
    calls: list[int] = []

    def compute():
        calls.append(1)
        return {"n": 1}

    sc.cached_signal(
        "github/gptme",
        compute,
        cache_dir=tmp_path,
        ttl_seconds=60,
        now=0.0,
        refresh=False,
    )
    sc.cached_signal(
        "github/gptme",
        compute,
        cache_dir=tmp_path,
        ttl_seconds=60,
        now=1.0,
        refresh=False,
    )
    assert len(calls) == 1
