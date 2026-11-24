"""Source discovery: deleted, untracked and non-ASCII files (git ls-files)."""

import os
import subprocess
from pathlib import Path

import pytest
from gptme_codegraph import core
from gptme_codegraph.core import SqliteIndexCache, build_index


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CODEGRAPH_STATE_DIR", tmp_path / "state")


def _git(d: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(d), *args],
        check=True,
        capture_output=True,
    )


def _repo(d: Path) -> Path:
    d.mkdir()
    _git(d, "init", "-q")
    return d


# --- Finding 1: tracked-but-deleted file (unstaged rm) crashes build_index ---
def test_build_index_survives_tracked_file_deleted_from_worktree(tmp_path):
    d = _repo(tmp_path / "repo")
    (d / "a.py").write_text("def a():\n    pass\n")
    (d / "b.py").write_text("def b():\n    pass\n")
    _git(d, "add", ".")
    (d / "b.py").unlink()  # user deleted the file, not yet `git rm`-ed
    index = build_index(d)
    assert index.has("a")
    assert not index.has("b")


# --- Finding 1b: broken symlink crashes build_index (non-git dir) ---
def test_build_index_survives_dangling_symlink(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "a.py").write_text("def a():\n    pass\n")
    os.symlink(d / "gone.py", d / "link.py")
    index = build_index(d)
    assert index.has("a")


# --- Finding 2: non-ASCII file names come back C-quoted from git ls-files ---
def test_iter_source_files_handles_non_ascii_names(tmp_path):
    d = _repo(tmp_path / "repo")
    (d / "café.py").write_text("def cafe():\n    pass\n")
    _git(d, "add", ".")
    files = core._iter_source_files(d)
    assert files == [d / "café.py"]
    assert build_index(d).has("cafe")


# A name that is not valid UTF-8 must not crash discovery (-z emits raw bytes).
def test_iter_source_files_survives_undecodable_name(tmp_path):
    d = _repo(tmp_path / "repo")
    (d / "a.py").write_text("def a():\n    pass\n")
    bad = os.fsdecode(b"bad\xff.py")
    try:
        (d / bad).write_text("def bad():\n    pass\n")
    except (OSError, UnicodeEncodeError):
        pytest.skip("filesystem rejects non-UTF-8 names")
    _git(d, "add", ".")
    files = core._iter_source_files(d)
    assert d / "a.py" in files
    assert d / bad in files
    assert build_index(d).has("a")


# --- Finding 3: untracked (not ignored) files are invisible, cache never invalidates ---
def test_untracked_new_file_is_indexed(tmp_path):
    d = _repo(tmp_path / "repo")
    (d / "a.py").write_text("def a():\n    pass\n")
    _git(d, "add", ".")
    (d / "new.py").write_text("def brand_new():\n    pass\n")
    assert build_index(d).has("brand_new")


def test_sqlite_cache_goes_stale_when_untracked_file_added(tmp_path):
    d = _repo(tmp_path / "repo")
    (d / "a.py").write_text("def a():\n    pass\n")
    _git(d, "add", ".")
    cache = SqliteIndexCache(str(d))
    cache.save(build_index(d))
    assert cache.is_fresh()
    (d / "new.py").write_text("def brand_new():\n    pass\n")
    assert not cache.is_fresh(), "new source file must invalidate the cache"
    cache.close()
