"""Tests for the SQLite search-document cache helpers in search.py.

These cover ``save_search_documents`` / ``load_search_documents`` /
``_ensure_search_tables`` — the wired-but-not-yet-integrated cache layer
(see the ``TODO(phase-2)`` in search.py). Locking the round-trip contract
now means the eventual CLI/MCP integration can't silently regress field
fidelity, directory isolation, or replace-on-resave semantics.
"""

import sqlite3

from gptme_codegraph.search import (
    SearchDocument,
    load_search_documents,
    save_search_documents,
)


def _doc(qualified_id: str, **overrides) -> SearchDocument:
    base = dict(
        qualified_id=qualified_id,
        kind="function",
        name=qualified_id.rsplit(".", 1)[-1],
        file="pkg/mod.py",
        start_line=10,
        end_line=20,
        parent_class=None,
        docstring="does a thing",
        calls=["helper", "other"],
        snippet="def f():\n    return helper()",
    )
    base.update(overrides)
    return SearchDocument(**base)


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


class TestSearchDocumentCacheRoundtrip:
    def test_roundtrip_preserves_all_fields(self):
        conn = _conn()
        doc = _doc(
            "pkg.mod.f",
            parent_class="Klass",
            docstring="multi\nline doc",
            calls=["a", "b", "c"],
            snippet="line1\nline2",
        )
        save_search_documents(conn, "/proj", [doc])
        loaded = load_search_documents(conn, "/proj")

        assert len(loaded) == 1
        got = loaded[0]
        # Field-by-field: a json (de)serialization bug or a constructor
        # argument-order slip would surface here.
        assert got.qualified_id == doc.qualified_id
        assert got.kind == doc.kind
        assert got.name == doc.name
        assert got.file == doc.file
        assert got.start_line == doc.start_line
        assert got.end_line == doc.end_line
        assert got.parent_class == doc.parent_class
        assert got.docstring == doc.docstring
        assert got.calls == doc.calls
        assert got.snippet == doc.snippet

    def test_roundtrip_multiple_docs(self):
        conn = _conn()
        docs = [_doc(f"pkg.mod.f{i}", start_line=i) for i in range(5)]
        save_search_documents(conn, "/proj", docs)
        loaded = load_search_documents(conn, "/proj")

        assert {d.qualified_id for d in loaded} == {d.qualified_id for d in docs}

    def test_defaults_survive_roundtrip(self):
        # parent_class=None, empty calls — make sure load's .get() defaults
        # don't corrupt an explicitly-empty document.
        conn = _conn()
        doc = _doc(
            "pkg.mod.bare", parent_class=None, docstring="", calls=[], snippet=""
        )
        save_search_documents(conn, "/proj", [doc])
        got = load_search_documents(conn, "/proj")[0]

        assert got.parent_class is None
        assert got.docstring == ""
        assert got.calls == []
        assert got.snippet == ""


class TestSearchDocumentCacheSemantics:
    def test_load_empty_returns_empty_list(self):
        conn = _conn()
        # _ensure_search_tables must create the table on the read path too,
        # so loading from a fresh DB yields [] rather than raising.
        assert load_search_documents(conn, "/never-saved") == []

    def test_resave_replaces_directory_contents(self):
        conn = _conn()
        save_search_documents(
            conn, "/proj", [_doc("pkg.mod.old1"), _doc("pkg.mod.old2")]
        )
        save_search_documents(conn, "/proj", [_doc("pkg.mod.new")])

        loaded = load_search_documents(conn, "/proj")
        assert [d.qualified_id for d in loaded] == ["pkg.mod.new"]

    def test_directories_are_isolated(self):
        conn = _conn()
        save_search_documents(conn, "/a", [_doc("a.mod.f")])
        save_search_documents(conn, "/b", [_doc("b.mod.f")])

        assert [d.qualified_id for d in load_search_documents(conn, "/a")] == [
            "a.mod.f"
        ]
        assert [d.qualified_id for d in load_search_documents(conn, "/b")] == [
            "b.mod.f"
        ]

    def test_content_hash_is_populated_and_stable(self):
        conn = _conn()
        doc = _doc("pkg.mod.f")
        save_search_documents(conn, "/proj", [doc])
        first = conn.execute(
            "SELECT content_hash FROM search_documents WHERE qualified_id = ?",
            (doc.qualified_id,),
        ).fetchone()[0]

        assert first and len(first) == 16  # 16-char truncated sha256

        # Re-saving identical content yields the same hash (deterministic).
        save_search_documents(conn, "/proj", [doc])
        second = conn.execute(
            "SELECT content_hash FROM search_documents WHERE qualified_id = ?",
            (doc.qualified_id,),
        ).fetchone()[0]
        assert first == second

    def test_content_hash_changes_with_content(self):
        conn = _conn()
        save_search_documents(conn, "/proj", [_doc("pkg.mod.f", docstring="v1")])
        h1 = conn.execute(
            "SELECT content_hash FROM search_documents WHERE directory = ? AND qualified_id = ?",
            ("/proj", "pkg.mod.f"),
        ).fetchone()[0]
        save_search_documents(conn, "/proj", [_doc("pkg.mod.f", docstring="v2")])
        h2 = conn.execute(
            "SELECT content_hash FROM search_documents WHERE directory = ? AND qualified_id = ?",
            ("/proj", "pkg.mod.f"),
        ).fetchone()[0]

        assert h1 != h2
