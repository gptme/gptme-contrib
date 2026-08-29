"""Tests for gptme-rag KnowledgeEntrySource (gptme/gptme#3596).

The JSONL store shipped in gptme/gptme#3622 is the source of truth.
This source is a rebuildable index over that file — it must not invent
a second store, and metadata values must stay Chroma-safe (scalars only).
"""

from __future__ import annotations

import json
from pathlib import Path

from gptme_rag.indexing.document import Document
from gptme_rag.knowledge_source import (
    KnowledgeEntrySource,
    collect_knowledge_entry_documents,
    default_knowledge_entries_path,
)
from gptme_rag.sources import SourceRegistry


def _write_entries(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def _valid_entry(**overrides: object) -> dict:
    entry = {
        "id": "11111111-1111-1111-1111-111111111111",
        "problem": "zlib failure",
        "resolution": "rebuild the archive",
        "tags": ["git", "pytest"],
        "keywords": ["zlib", "failure", "rebuild", "archive"],
        "created_at": "2026-08-28T12:00:00+00:00",
        "memory_type": "knowledge_entry",
    }
    entry.update(overrides)
    return entry


def test_missing_file_returns_empty(tmp_path: Path):
    docs = collect_knowledge_entry_documents(tmp_path / "missing.jsonl")
    assert docs == []


def test_collects_valid_jsonl_as_knowledge_entry_documents(tmp_path: Path):
    path = tmp_path / "entries.jsonl"
    _write_entries(path, [_valid_entry()])

    docs = collect_knowledge_entry_documents(path)

    assert len(docs) == 1
    doc = docs[0]
    assert isinstance(doc, Document)
    assert doc.doc_id == "knowledge_entry:11111111-1111-1111-1111-111111111111"
    assert "zlib failure" in doc.content
    assert "rebuild the archive" in doc.content
    assert doc.metadata["memory_type"] == "knowledge_entry"
    assert doc.metadata["type"] == "knowledge_entry"
    assert doc.metadata["problem"] == "zlib failure"
    assert doc.metadata["resolution"] == "rebuild the archive"
    assert doc.metadata["created_at"] == "2026-08-28T12:00:00+00:00"
    assert doc.source_path == path


def test_metadata_values_are_chroma_scalars(tmp_path: Path):
    path = tmp_path / "entries.jsonl"
    _write_entries(path, [_valid_entry()])

    doc = collect_knowledge_entry_documents(path)[0]
    for key, value in doc.metadata.items():
        assert isinstance(
            value, str | int | float | bool
        ), f"{key}={value!r} is not a chroma scalar"

    assert doc.metadata["tags"] == "git,pytest"
    assert "zlib" in doc.metadata["keywords"]


def test_skips_malformed_and_invalid_lines(tmp_path: Path):
    path = tmp_path / "entries.jsonl"
    good = _valid_entry()
    path.write_text(
        "\n".join(
            [
                "{not json",
                json.dumps(
                    {
                        "id": "not-a-uuid",
                        "problem": "x",
                        "resolution": "y",
                        "tags": [],
                        "created_at": "t",
                    }
                ),
                json.dumps(_valid_entry(problem="")),
                json.dumps(
                    _valid_entry(id="22222222-2222-2222-2222-222222222222", problem="keep me")
                ),
                json.dumps(["not", "an", "object"]),
                json.dumps(good),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    docs = collect_knowledge_entry_documents(path)
    ids = {doc.doc_id for doc in docs}
    assert ids == {
        "knowledge_entry:22222222-2222-2222-2222-222222222222",
        "knowledge_entry:11111111-1111-1111-1111-111111111111",
    }


def test_default_path_uses_xdg_data_home(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_knowledge_entries_path() == tmp_path / "gptme" / "knowledge" / "entries.jsonl"


def test_default_path_without_xdg(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    path = default_knowledge_entries_path()
    assert path == Path.home() / ".local" / "share" / "gptme" / "knowledge" / "entries.jsonl"


def test_knowledge_entry_source_collects_from_explicit_path(tmp_path: Path):
    path = tmp_path / "entries.jsonl"
    _write_entries(path, [_valid_entry()])

    source = KnowledgeEntrySource(entries_path=path)
    docs = source.collect()
    assert len(docs) == 1
    assert docs[0].metadata["memory_type"] == "knowledge_entry"


def test_source_is_registerable(tmp_path: Path):
    path = tmp_path / "entries.jsonl"
    _write_entries(path, [_valid_entry()])

    registry = SourceRegistry()
    registry.add("knowledge entries", KnowledgeEntrySource(entries_path=path).collect)
    docs = registry.collect()
    assert len(docs) == 1
    assert docs[0].metadata["memory_type"] == "knowledge_entry"


def test_tags_in_content_for_lexical_search(tmp_path: Path):
    path = tmp_path / "entries.jsonl"
    _write_entries(path, [_valid_entry(tags=["flock", "jsonl"])])

    doc = collect_knowledge_entry_documents(path)[0]
    assert "flock" in doc.content
    assert "jsonl" in doc.content


def test_skips_keywords_that_are_not_a_string_list(tmp_path: Path):
    path = tmp_path / "entries.jsonl"
    keep = _valid_entry(id="33333333-3333-3333-3333-333333333333", problem="keep me")
    _write_entries(
        path,
        [
            _valid_entry(keywords="failure"),
            _valid_entry(keywords=["ok", 1]),
            keep,
        ],
    )

    docs = collect_knowledge_entry_documents(path)
    assert [doc.doc_id for doc in docs] == ["knowledge_entry:33333333-3333-3333-3333-333333333333"]
    assert "f, a, i, l, u, r, e" not in docs[0].content
    assert docs[0].metadata["keywords"] != "f,a,i,l,u,r,e"


def test_missing_keywords_is_valid(tmp_path: Path):
    path = tmp_path / "entries.jsonl"
    entry = _valid_entry()
    del entry["keywords"]
    _write_entries(path, [entry])

    docs = collect_knowledge_entry_documents(path)
    assert len(docs) == 1
    assert docs[0].metadata["keywords"] == ""
