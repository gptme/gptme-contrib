"""Watcher moves, atomic saves and directory deletes keep the index in step (#2356 W3-W5, W7)."""

import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

from watchdog.events import DirDeletedEvent, FileDeletedEvent, FileMovedEvent

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
    # Simulate the timer having fired already: it has removed itself from
    # _timers and is waiting to run _process_deletion.
    with handler._lock:
        fired = handler._timers.pop(b)
    fired.cancel()
    handler.on_moved(FileMovedEvent(str(a), str(b)))

    handler._process_deletion(b)  # the late, already-fired timer body

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
