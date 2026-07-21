"""Regression tests for concurrent issue-cache updates."""

from __future__ import annotations

import multiprocessing
from pathlib import Path

from gptodo.utils import load_cache, save_cache, update_cache


def _update(cache_path: str, key: str) -> None:
    update_cache(Path(cache_path), {key: {"state": "open"}})


def test_update_cache_preserves_concurrent_updates(tmp_path: Path) -> None:
    cache_path = tmp_path / "state" / "issue-cache.json"
    processes = [
        multiprocessing.Process(target=_update, args=(str(cache_path), key))
        for key in ("first", "second", "third")
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0

    assert set(load_cache(cache_path)) == {"first", "second", "third"}


def test_update_cache_applies_remove_and_update_in_one_transaction(tmp_path: Path) -> None:
    cache_path = tmp_path / "state" / "issue-cache.json"
    save_cache(cache_path, {"remove": 1, "keep": 2})

    merged = update_cache(cache_path, {"add": 3}, remove={"remove"})

    assert merged == {"keep": 2, "add": 3}
    assert load_cache(cache_path) == merged
    assert list(cache_path.parent.glob(f".{cache_path.name}.*.tmp")) == []
