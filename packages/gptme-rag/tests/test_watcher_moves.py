"""Watcher moves, atomic saves and directory deletes keep the index in step (#2356 W3-W5, W7)."""

import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

from watchdog.events import DirDeletedEvent, FileDeletedEvent, FileModifiedEvent, FileMovedEvent

from gptme_rag.indexing.watcher import IndexEventHandler


def _contents_for(indexer, source: Path) -> list[str]:
    got = indexer.collection.get(where={"source": str(source)})
    return list(got["documents"] or [])


def _all_sources(indexer) -> set[str]:
    got = indexer.collection.get()
    return {m["source"] for m in got["metadatas"] or []}


def test_move_over_existing_file_replaces_its_content(tmp_path, indexer):
    """An atomic save (write temp, rename over target) replaces the target's
    indexed content instead of adding the new chunks next to the old ones."""
    watched = tmp_path / "w"
    watched.mkdir()
    target = (watched / "notes.md").resolve()
    target.write_text("old content about bananas")
    indexer.index_file(target)
    assert any("bananas" in c for c in _contents_for(indexer, target))

    tmp = watched / "notes.md.tmp"
    tmp.write_text("new content about cherries")
    os.replace(tmp, target)
    IndexEventHandler(indexer).on_moved(FileMovedEvent(str(tmp), str(target)))

    contents = _contents_for(indexer, target)
    assert any("cherries" in c for c in contents)
    assert not any("bananas" in c for c in contents), contents


def test_rename_out_of_pattern_clears_search_cache(tmp_path, indexer):
    """Renaming a watched file to a name outside the pattern must not leave
    search serving it from the result cache."""
    watched = tmp_path / "w"
    watched.mkdir()
    src = (watched / "doc.md").resolve()
    src.write_text("zebra stripes savanna")
    indexer.index_file(src)
    docs, _, _ = indexer.search("zebra stripes savanna", n_results=1)
    assert docs and docs[0].metadata["source"] == str(src)

    dest = (watched / "doc.txt").resolve()
    src.rename(dest)
    IndexEventHandler(indexer, pattern="**/*.md").on_moved(FileMovedEvent(str(src), str(dest)))

    assert str(src) not in _all_sources(indexer)
    docs, _, _ = indexer.search("zebra stripes savanna", n_results=1)
    assert [d.metadata["source"] for d in docs] == [], "search served a stale cached result"


def test_directory_moved_out_of_watch_is_removed_from_index(tmp_path, indexer):
    """`mv watched/sub elsewhere/` yields only a DirDeletedEvent under inotify;
    the files under it leave the index, and a sibling with a shared name
    prefix stays."""
    watched = tmp_path / "w"
    (watched / "sub").mkdir(parents=True)
    (watched / "sub2").mkdir()
    gone = (watched / "sub" / "a.md").resolve()
    gone.write_text("directory move payload")
    kept = (watched / "sub2" / "b.md").resolve()
    kept.write_text("sibling directory payload")
    indexer.index_file(gone)
    indexer.index_file(kept)

    shutil.move(str(watched / "sub"), str(tmp_path / "elsewhere"))
    IndexEventHandler(indexer).on_deleted(DirDeletedEvent(str(watched / "sub")))

    sources = _all_sources(indexer)
    assert str(gone) not in sources
    assert str(kept) in sources


class _Cache:
    def clear(self) -> None:
        pass


class _SourceTrackingIndexer:
    """Indexer double that tracks which sources are present."""

    def __init__(self, persist_directory):
        self.cache = _Cache()
        self.persist_directory = persist_directory
        self.sources: set[str] = set()

    def delete_documents(self, where: dict) -> None:
        self.sources.discard(where["source"])

    def index_file(self, path) -> int:
        self.sources.add(str(path))
        return 1

    def search(self, query: str, n_results: int = 5):
        docs = [SimpleNamespace(metadata={"source": s}) for s in sorted(self.sources)]
        return docs, [], None


def test_move_onto_path_with_pending_deletion(tmp_path):
    """`rm b.md; mv a.md b.md` inside the debounce window: the pending deletion
    timer for b.md must not remove the file that was just moved there."""
    a = (tmp_path / "a.md").resolve()
    b = (tmp_path / "b.md").resolve()
    a.write_text("A")
    b.write_text("B")
    indexer = _SourceTrackingIndexer(tmp_path / "index")
    indexer.sources = {str(a), str(b)}
    handler = IndexEventHandler(indexer)
    handler._update_delay = 0.1

    b.unlink()
    handler.on_deleted(FileDeletedEvent(str(b)))
    os.replace(a, b)
    handler.on_moved(FileMovedEvent(str(a), str(b)))
    assert str(b) in indexer.sources
    time.sleep(0.5)  # past the debounce window of the stale deletion

    assert str(b) in indexer.sources, "pending deletion removed the moved-in file"
    assert str(a) not in indexer.sources


def test_failed_atomic_save_clears_destination_cache(tmp_path, indexer, monkeypatch):
    """Deleting replaced chunks must invalidate search even if the new file is unreadable."""
    target = (tmp_path / "notes.md").resolve()
    target.write_text("old cached bananas")
    indexer.index_file(target)
    docs, _, _ = indexer.search("old cached bananas", n_results=1)
    assert docs and docs[0].metadata["source"] == str(target)

    temp = tmp_path / "notes.md.tmp"
    temp.write_bytes(b"\xff\xfe")
    os.replace(temp, target)
    monkeypatch.setattr("gptme_rag.indexing.watcher.time.sleep", lambda _: None)
    IndexEventHandler(indexer, pattern="**/*.md").on_moved(FileMovedEvent(str(temp), str(target)))

    assert _contents_for(indexer, target) == []
    docs, _, _ = indexer.search("old cached bananas", n_results=1)
    assert docs == [], "failed replacement left deleted content in the search cache"


class _CountingCollection:
    """Collection double that records full metadata scans."""

    def __init__(self) -> None:
        self.gets = 0

    def get(self, **kwargs):
        self.gets += 1
        return {"ids": [], "metadatas": []}


def test_ignored_directory_delete_does_not_scan_collection(tmp_path):
    """Directory churn under ignored trees must not trigger a collection scan.

    ``Path.match`` only looks at the tail, so nested dirs such as
    ``.git/objects/ab`` need an ancestor-aware check.
    """
    indexer = _SourceTrackingIndexer(tmp_path / "index")
    indexer.collection = _CountingCollection()
    handler = IndexEventHandler(
        indexer, ignore_patterns=[".git", "__pycache__", "node_modules", "*.pyc"]
    )

    for name in ["__pycache__", ".git/objects/ab", "node_modules/pkg", "pkg/__pycache__"]:
        handler.on_deleted(DirDeletedEvent(str(tmp_path / name)))

    assert indexer.collection.gets == 0


def test_already_fired_deletion_cannot_remove_moved_file(tmp_path):
    """A deletion timer that fired before the move but reaches its critical
    section afterwards must not delete the file that replaced it."""
    a = (tmp_path / "a.md").resolve()
    b = (tmp_path / "b.md").resolve()
    a.write_text("A")
    b.write_text("B")
    indexer = _SourceTrackingIndexer(tmp_path / "index")
    indexer.sources = {str(a), str(b)}
    handler = IndexEventHandler(indexer)
    handler._update_delay = 0.05

    b.unlink()
    handler.on_deleted(FileDeletedEvent(str(b)))
    os.replace(a, b)
    # Cancellation cannot stop a callback that already started. Save its
    # actual invocation so it can resume after the move's critical section.
    fired = handler._timers[b]
    fired.cancel()
    handler.on_moved(FileMovedEvent(str(a), str(b)))

    fired.function(*fired.args, **fired.kwargs)

    assert str(b) in indexer.sources, "stale deletion removed the moved-in file"
    assert str(a) not in indexer.sources


def test_deletion_after_move_is_still_honoured(tmp_path):
    """Invalidating a move destination must not block a later real deletion."""
    a = (tmp_path / "a.md").resolve()
    b = (tmp_path / "b.md").resolve()
    a.write_text("A")
    b.write_text("B")
    indexer = _SourceTrackingIndexer(tmp_path / "index")
    indexer.sources = {str(a), str(b)}
    handler = IndexEventHandler(indexer)
    handler._update_delay = 0.05

    os.replace(a, b)
    handler.on_moved(FileMovedEvent(str(a), str(b)))
    assert str(b) in indexer.sources

    b.unlink()
    handler.on_deleted(FileDeletedEvent(str(b)))
    time.sleep(0.3)

    assert str(b) not in indexer.sources


def test_old_deletion_stays_superseded_after_fresh_update(tmp_path):
    """A fresh modification must not re-enable a deletion from before a move."""
    a = (tmp_path / "a.md").resolve()
    b = (tmp_path / "b.md").resolve()
    a.write_text("A")
    b.write_text("B")
    indexer = _SourceTrackingIndexer(tmp_path / "index")
    indexer.sources = {str(a), str(b)}
    handler = IndexEventHandler(indexer)
    handler._update_delay = 60
    try:
        b.unlink()
        handler.on_deleted(FileDeletedEvent(str(b)))
        old = handler._timers[b]
        old.cancel()
        os.replace(a, b)
        handler.on_moved(FileMovedEvent(str(a), str(b)))
        handler.on_modified(FileModifiedEvent(str(b)))
        fresh = handler._timers[b]
        fresh.cancel()

        # The obsolete callback must neither delete the replacement nor pop
        # the fresh timer's registration, even if it runs before that timer.
        old.function(*old.args, **old.kwargs)
        assert str(b) in indexer.sources, "old deletion removed the replacement"
        assert handler._timers[b] is fresh
        fresh.function(*fresh.args, **fresh.kwargs)
        old.function(*old.args, **old.kwargs)
        assert indexer.sources == {str(b)}
        assert not handler._timers
    finally:
        handler.cancel_pending_updates()


def test_directory_delete_through_symlink_stops_at_watch_root(tmp_path):
    """Resolved roots must bound raw watchdog paths, even below ignored ancestors."""
    real_root = tmp_path / "real" / "project"
    real_root.mkdir(parents=True)
    alias = tmp_path / "node_modules"
    alias.symlink_to(real_root, target_is_directory=True)
    indexer = _SourceTrackingIndexer(tmp_path / "index")
    indexer.collection = _CountingCollection()
    handler = IndexEventHandler(indexer, watch_roots=[str(alias)], ignore_patterns=["node_modules"])

    handler.on_deleted(DirDeletedEvent(str(alias / "removed")))
    assert indexer.collection.gets == 1, "ancestor above the watch root silenced deletion"
    handler.on_deleted(DirDeletedEvent(str(alias / "node_modules" / "pkg")))
    assert indexer.collection.gets == 1, "ignored directory inside the root was scanned"


def test_atomic_saves_do_not_retain_unique_temp_paths(tmp_path):
    """Finished moves must not accumulate path tombstones for unique temp files."""
    indexer = _SourceTrackingIndexer(tmp_path / "index")
    handler = IndexEventHandler(indexer, pattern="**/*.md")
    target = tmp_path / "notes.md"
    for i in range(30):
        temp = tmp_path / f"notes.md.tmp.{i}"
        temp.write_text(f"save {i}")
        os.replace(temp, target)
        handler.on_moved(FileMovedEvent(str(temp), str(target)))

    assert indexer.sources == {str(target)}
    assert not handler._timers
    assert not getattr(handler, "_invalidated", set()), "unique save paths retained forever"


def test_move_waits_for_update_that_already_started_indexing(tmp_path):
    """An in-flight update must finish before a move removes its source."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    a = (tmp_path / "a.md").resolve()
    b = (tmp_path / "b.md").resolve()
    a.write_text("A")
    indexer = _SourceTrackingIndexer(tmp_path / "index")
    entered = Event()
    release = Event()
    moving = Event()
    moved = Event()
    original_index_file = indexer.index_file

    def blocking_index_file(path):
        if path == a:
            entered.set()
            assert release.wait(5), "test did not release the in-flight update"
        return original_index_file(path)

    indexer.index_file = blocking_index_file
    handler = IndexEventHandler(indexer)
    handler._update_delay = 60
    handler.on_modified(FileModifiedEvent(str(a)))
    timer = handler._timers[a]
    timer.cancel()

    def move():
        moving.set()
        handler.on_moved(FileMovedEvent(str(a), str(b)))
        moved.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        update = pool.submit(timer.function, *timer.args, **timer.kwargs)
        try:
            assert entered.wait(5)
            os.replace(a, b)
            movement = pool.submit(move)
            assert moving.wait(5)
            assert not moved.wait(0.1), "move interleaved with an active index mutation"
        finally:
            release.set()
        update.result(timeout=5)
        movement.result(timeout=5)

    assert indexer.sources == {str(b)}, "an old update reintroduced the moved-away source"
    assert not handler._timers


def test_old_update_cannot_consume_new_timer_after_move(tmp_path):
    """The identity check applies to stale updates as well as stale deletes."""
    a = (tmp_path / "a.md").resolve()
    b = (tmp_path / "b.md").resolve()
    a.write_text("A")
    b.write_text("B")
    indexer = _SourceTrackingIndexer(tmp_path / "index")
    handler = IndexEventHandler(indexer)
    handler._update_delay = 60
    try:
        handler.on_modified(FileModifiedEvent(str(b)))
        old = handler._timers[b]
        old.cancel()
        os.replace(a, b)
        handler.on_moved(FileMovedEvent(str(a), str(b)))
        handler.on_modified(FileModifiedEvent(str(b)))
        fresh = handler._timers[b]
        fresh.cancel()
        old.function(*old.args, **old.kwargs)
        assert handler._timers[b] is fresh, "obsolete update consumed the fresh timer"
        handler.cancel_pending_updates()
        fresh.function(*fresh.args, **fresh.kwargs)
        assert not handler._timers
        assert indexer.sources == {str(b)}
    finally:
        handler.cancel_pending_updates()
