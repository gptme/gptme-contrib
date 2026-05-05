"""Tests for the SQLite-backed index cache in codegraph.

Verifies that SqliteIndexCache saves, loads, detects staleness,
and falls through to rebuild when files change.
"""

from pathlib import Path

import pytest
from gptme_codegraph.core import SqliteIndexCache, build_index


@pytest.fixture
def sample_dir(tmp_path):
    """Create a minimal multi-file project."""
    (tmp_path / "utils.py").write_text("""\
def add(a, b):
    return a + b

def multiply(a, b):
    return a * b
""")
    (tmp_path / "main.py").write_text("""\
from utils import add, multiply

def compute(x):
    return multiply(add(x, 1), 2)
""")
    return str(tmp_path)


def test_sqlite_save_load_roundtrip(sample_dir):
    """Test that index saved to SQLite can be loaded back identically."""
    cache = SqliteIndexCache(sample_dir)

    index = build_index(Path(sample_dir))
    assert index.has("add")
    assert index.has("compute")
    cache.save(index)

    loaded = cache.load()
    assert loaded is not None
    assert loaded.has("add")
    assert loaded.has("compute")
    assert loaded.has("multiply")

    add_entries = loaded.lookup("add")
    assert len(add_entries) >= 1
    assert add_entries[0].kind == "function"
    assert "utils.py" in add_entries[0].file
    assert add_entries[0].module_path == "utils"
    assert add_entries[0].qualified_id() == "utils::add"

    cache.close()


def test_sqlite_freshness_detects_mtime_change(sample_dir):
    """Test that is_fresh returns False after a file is modified."""
    cache = SqliteIndexCache(sample_dir)

    index = build_index(Path(sample_dir))
    cache.save(index)
    assert cache.is_fresh()

    # Modify a file
    (Path(sample_dir) / "utils.py").write_text("""\
def add(a, b):
    return a + b + 1  # modified
""")

    assert not cache.is_fresh()
    cache.close()


def test_sqlite_freshness_detects_new_file(sample_dir):
    """Test that is_fresh returns False after a new file is added."""
    cache = SqliteIndexCache(sample_dir)

    index = build_index(Path(sample_dir))
    cache.save(index)
    assert cache.is_fresh()

    (Path(sample_dir) / "new.py").write_text("def new_func(): pass")
    assert not cache.is_fresh()

    cache.close()


def test_sqlite_freshness_detects_deleted_file(sample_dir):
    """Test that is_fresh returns False after a file is removed."""
    cache = SqliteIndexCache(sample_dir)

    index = build_index(Path(sample_dir))
    cache.save(index)
    assert cache.is_fresh()

    (Path(sample_dir) / "utils.py").unlink()
    assert not cache.is_fresh()

    cache.close()


def test_sqlite_empty_cache_returns_none(tmp_path):
    """Test that loading from an empty/new cache returns None."""
    cache = SqliteIndexCache(str(tmp_path))
    loaded = cache.load()
    assert loaded is None
    cache.close()


def test_sqlite_autorebuild_on_stale(sample_dir):
    """Test that load() returns None when stale, forcing rebuild."""
    cache = SqliteIndexCache(sample_dir)

    index = build_index(Path(sample_dir))
    cache.save(index)
    assert cache.is_fresh()

    (Path(sample_dir) / "utils.py").write_text("""\
def add(a, b):
    return a + b + 1
""")

    loaded = cache.load()
    assert loaded is None

    rebuilt = build_index(Path(sample_dir))
    cache.save(rebuilt)
    assert cache.is_fresh()

    cache.close()


def test_sqlite_multiple_directories(tmp_path):
    """Test that indices for different directories don't conflict."""
    dir_a = tmp_path / "project_a"
    dir_b = tmp_path / "project_b"
    dir_a.mkdir()
    dir_b.mkdir()

    (dir_a / "mod.py").write_text("def alpha(): pass")
    (dir_b / "mod.py").write_text("def beta(): pass")

    cache_a = SqliteIndexCache(str(dir_a))
    cache_b = SqliteIndexCache(str(dir_b))

    index_a = build_index(dir_a)
    index_b = build_index(dir_b)

    cache_a.save(index_a)
    cache_b.save(index_b)

    loaded_a = cache_a.load()
    loaded_b = cache_b.load()

    assert loaded_a is not None
    assert loaded_b is not None
    assert loaded_a.lookup("alpha")[0].module_path == "mod"
    assert loaded_a.has("alpha")
    assert not loaded_a.has("beta")
    assert loaded_b.has("beta")
    assert not loaded_b.has("alpha")

    cache_a.close()
    cache_b.close()


def test_sqlite_db_path_unique(tmp_path):
    """Test that different directories get different db paths."""
    from gptme_codegraph.core import _db_path

    path_a = _db_path(str(tmp_path / "proj_a"))
    path_b = _db_path(str(tmp_path / "proj_b"))
    assert path_a != path_b


def test_sqlite_invalidates_on_schema_bump(sample_dir, monkeypatch):
    """Cache rebuilds when SCHEMA_VERSION moves forward.

    Simulates an upgrade where the data schema changes between codegraph
    versions. The cache must drop the old entries rather than silently serve
    stale rows that no longer match the current parser semantics.
    """
    cache = SqliteIndexCache(sample_dir)
    index = build_index(Path(sample_dir))
    cache.save(index)
    # Sanity check: data is there.
    loaded = cache.load()
    assert loaded is not None and loaded.has("add")
    cache.close()

    # Simulate a code upgrade that bumped the schema and reopen the cache.
    bumped = SqliteIndexCache(sample_dir)
    monkeypatch.setattr(bumped, "SCHEMA_VERSION", SqliteIndexCache.SCHEMA_VERSION + 1)
    # First call after the bump must observe an empty data store.
    bumped._init_schema()
    reloaded = bumped.load()
    assert reloaded is None or not reloaded.has(
        "add"
    ), "Expected cache to be invalidated after SCHEMA_VERSION bump"
    bumped.close()
