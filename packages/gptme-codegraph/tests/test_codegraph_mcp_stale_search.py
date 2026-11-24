"""codegraph_search must not serve documents from an index another process replaced.

Scenario (gptme/gptme-contrib#2356, G4): the MCP server searches a directory,
then a second process (the CLI, another MCP server) rebuilds and saves the
SQLite index after an edit. The server's freshness check then passes, because
the file mtimes match the new rows, so it used to keep both its in-memory
index and the cached search documents from before the edit.
"""

import json
import os
from pathlib import Path

import gptme_codegraph.core as core
import gptme_codegraph.mcp_server as mcp_server
import pytest
from gptme_codegraph.core import SqliteIndexCache, build_index


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_CODEGRAPH_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(mcp_server, "_index_cache", {})
    src = tmp_path / "proj"
    src.mkdir()
    return src


def _search(directory: Path, query: str) -> list[str]:
    raw = mcp_server.codegraph_search(query=query, directory=str(directory))
    data = json.loads(raw)
    assert "error" not in data, data
    return [r["name"] for r in data["results"]]


def _rewrite_from_other_process(src: Path) -> None:
    """Edit the file and save a rebuilt index the way a second process would."""
    target = src / "mod.py"
    old_mtime = target.stat().st_mtime
    target.write_text("def beta_renamed():\n    return 2\n")
    # Guarantee an mtime change even on coarse-grained filesystems.
    os.utime(target, (old_mtime + 10, old_mtime + 10))
    other = SqliteIndexCache(str(src))
    other.save(build_index(src))
    other.close()


def test_search_sees_index_rewritten_by_another_process(isolated):
    src = isolated
    (src / "mod.py").write_text("def alpha_original():\n    return 1\n")
    assert _search(src, "alpha original") == ["alpha_original"]

    _rewrite_from_other_process(src)

    names = _search(src, "beta renamed")
    assert names == ["beta_renamed"]
    assert "alpha_original" not in _search(src, "alpha original")


def test_search_after_restart_ignores_documents_from_old_index(isolated, monkeypatch):
    src = isolated
    (src / "mod.py").write_text("def alpha_original():\n    return 1\n")
    assert _search(src, "alpha original") == ["alpha_original"]

    _rewrite_from_other_process(src)
    # Simulate a server restart: the in-memory cache is gone, SQLite remains.
    monkeypatch.setattr(mcp_server, "_index_cache", {})

    assert _search(src, "beta renamed") == ["beta_renamed"]
    assert "alpha_original" not in _search(src, "alpha original")


def test_unchanged_index_reuses_cached_documents(isolated, monkeypatch):
    src = isolated
    (src / "mod.py").write_text("def alpha_original():\n    return 1\n")
    assert _search(src, "alpha original") == ["alpha_original"]

    calls = []
    real = mcp_server.extract_search_documents

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(mcp_server, "extract_search_documents", counting)
    monkeypatch.setattr(mcp_server, "_index_cache", {})
    assert _search(src, "alpha original") == ["alpha_original"]
    assert calls == []
