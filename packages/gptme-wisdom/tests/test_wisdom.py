"""Tests for gptme-wisdom: parsers, indexer, and CLI."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from gptme_wisdom import BookDocument, BookIndex, parse_book_text
from gptme_wisdom.cli import main


SAMPLE_TEXT = """
# Chapter 1: Introduction

This is the introduction to the book. It covers the basics of the subject
and provides an overview of the topics that will be covered in subsequent
chapters. The introduction is designed to help readers understand the
scope of the material.

## 1.1 Background

The background section provides historical context and explains why this
topic is important. Understanding the history helps readers appreciate
the current state of the art and the problems that need to be solved.

## 1.2 Overview

This section gives a brief overview of each chapter's content to help
readers navigate the material and understand how the pieces fit together.

# Chapter 2: Core Concepts

This chapter introduces the core concepts that underpin the entire subject.
These concepts are fundamental and will be referenced throughout the book.
Readers who master these concepts will find the rest of the material much
easier to understand.

## 2.1 Key Definitions

Definitions are essential for precise communication in technical fields.
We provide careful definitions for each term before using it so that
readers can build an accurate mental model.

## 2.2 Fundamental Theorems

The fundamental theorems form the theoretical foundation. Each theorem
is stated precisely and accompanied by an intuitive explanation to help
readers understand why it is true, not just that it is true.

# Exercises

Solve the following exercises to test your understanding.
1. Derive the key formula.
2. Prove the main theorem.
"""


def make_docs(source: str = "testbook") -> list[BookDocument]:
    return parse_book_text(
        SAMPLE_TEXT,
        source=source,
        title="Test Book",
        url="https://example.org",
        license="CC BY 4.0",
        target_tokens=100,
        overlap_tokens=10,
        min_chunk_tokens=10,
    )


def test_parse_produces_chunks() -> None:
    docs = make_docs()
    assert len(docs) > 0


def test_parse_chapter_tracking() -> None:
    docs = make_docs()
    chapters = {d.chapter for d in docs}
    # Chapter 1 and Chapter 2 should appear
    assert any("Chapter 1" in c or "Introduction" in c for c in chapters)
    assert any("Chapter 2" in c or "Core Concepts" in c for c in chapters)


def test_parse_section_tracking() -> None:
    docs = make_docs()
    # h2-level headers (##) are treated as chapter boundaries (depth ≤ 2),
    # so numbered subsections like "## 1.1 Background" appear in chapters.
    chapters = {d.chapter for d in docs}
    assert any("1.1" in c or "Background" in c for c in chapters)


def test_parse_skips_exercises() -> None:
    docs = make_docs()
    # Exercises section should be skipped
    assert not any("Solve the following" in d.content for d in docs)
    assert not any("Derive the key formula" in d.content for d in docs)


def test_parse_provenance_fields() -> None:
    docs = make_docs()
    for doc in docs:
        assert doc.source == "testbook"
        assert doc.title == "Test Book"
        assert doc.url == "https://example.org"
        assert doc.license == "CC BY 4.0"


def test_parse_empty_text() -> None:
    docs = parse_book_text(
        "   \n\n   ",
        source="empty",
        title="Empty",
        url="",
    )
    assert docs == []


def test_book_index_add_and_search(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    docs = make_docs()
    with BookIndex(db_path=db) as idx:
        added = idx.add_many(iter(docs))
        assert added == len(docs)
        results = idx.search("core concepts fundamental")
        assert len(results) > 0
        assert all("score" in r for r in results)


def test_book_index_dedup(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    docs = make_docs()
    with BookIndex(db_path=db) as idx:
        first = idx.add_many(iter(docs))
        second = idx.add_many(iter(docs))
    assert first == len(docs)
    assert second == 0  # all duplicates


def test_book_index_count(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    docs = make_docs()
    with BookIndex(db_path=db) as idx:
        idx.add_many(iter(docs))
        total = idx.count()
        by_source = idx.count(source="testbook")
        missing = idx.count(source="nonexistent")
    assert total == len(docs)
    assert by_source == len(docs)
    assert missing == 0


def test_book_index_sources(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    docs_a = make_docs("booka")
    docs_b = make_docs("bookb")
    with BookIndex(db_path=db) as idx:
        idx.add_many(iter(docs_a))
        idx.add_many(iter(docs_b))
        sources = idx.sources()
    slugs = {s["source"] for s in sources}
    assert "booka" in slugs
    assert "bookb" in slugs


def test_book_index_remove(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    docs = make_docs()
    with BookIndex(db_path=db) as idx:
        idx.add_many(iter(docs))
        removed = idx.remove_source("testbook")
        remaining = idx.count()
    assert removed == len(docs)
    assert remaining == 0


def test_book_index_source_filter_in_search(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    docs_a = make_docs("booka")
    docs_b = make_docs("bookb")
    with BookIndex(db_path=db) as idx:
        idx.add_many(iter(docs_a))
        idx.add_many(iter(docs_b))
        results = idx.search("core concepts", source="booka")
    assert all(r["source"] == "booka" for r in results)


# CLI tests
def _make_db(tmp_path: Path) -> Path:
    """Helper: create a populated DB and return its path."""
    db = tmp_path / "test.db"
    docs = make_docs("sicp")
    with BookIndex(db_path=db) as idx:
        idx.add_many(iter(docs))
    return db


def test_cli_list_empty(tmp_path: Path) -> None:
    runner = CliRunner()
    db = tmp_path / "nonexistent.db"
    result = runner.invoke(main, ["--db", str(db), "list"])
    assert result.exit_code == 0
    assert "no wisdom index" in result.output.lower() or "ingest" in result.output.lower()


def test_cli_list(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--db", str(db), "list"])
    assert result.exit_code == 0
    assert "sicp" in result.output


def test_cli_list_json(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--db", str(db), "list", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert any(s["source"] == "sicp" for s in data)


def test_cli_search(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--db", str(db), "search", "core concepts"])
    assert result.exit_code == 0


def test_cli_search_json(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--db", str(db), "search", "--json", "chapter"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)


def test_cli_search_context(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--db", str(db), "search", "--context", "definitions"])
    assert result.exit_code == 0
    assert "## Wisdom:" in result.output


def test_cli_search_no_index(tmp_path: Path) -> None:
    runner = CliRunner()
    db = tmp_path / "missing.db"
    result = runner.invoke(main, ["--db", str(db), "search", "anything"])
    assert result.exit_code != 0


def test_cli_ingest(tmp_path: Path) -> None:
    db = tmp_path / "new.db"
    book_file = tmp_path / "book.txt"
    book_file.write_text(SAMPLE_TEXT)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--db",
            str(db),
            "ingest",
            "--source",
            "sicp",
            "--file",
            str(book_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "ingested" in result.output


def test_cli_ingest_custom_metadata(tmp_path: Path) -> None:
    db = tmp_path / "new.db"
    book_file = tmp_path / "book.txt"
    book_file.write_text(SAMPLE_TEXT)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--db",
            str(db),
            "ingest",
            "--source",
            "custom",
            "--title",
            "Custom Book",
            "--file",
            str(book_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "custom" in result.output


def test_cli_ingest_missing_title(tmp_path: Path) -> None:
    db = tmp_path / "new.db"
    book_file = tmp_path / "book.txt"
    book_file.write_text(SAMPLE_TEXT)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--db",
            str(db),
            "ingest",
            "--source",
            "unknownslug",
            "--file",
            str(book_file),
        ],
    )
    assert result.exit_code != 0
    assert "title" in result.output.lower() or "error" in result.output.lower()


def test_cli_remove(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--db", str(db), "remove", "--yes", "sicp"])
    assert result.exit_code == 0
    assert "removed" in result.output
