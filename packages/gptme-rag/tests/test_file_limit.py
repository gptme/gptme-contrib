"""Tests for the per-directory file_limit cap in Indexer._get_valid_files.

The cap used to default to 1000 and log only a WARNING, which silently
truncated large corpora (Bob's ambient-memory index: ~43k git-visible files
across 7 paths, of which only ~4k were ever collected). See gptme-contrib#1523.
"""

from __future__ import annotations

import logging

import pytest
from click.testing import CliRunner

from gptme_rag.cli import cli
from gptme_rag.indexing.indexer import Indexer


def _write_files(directory, count: int, prefix: str = "doc") -> None:
    for i in range(count):
        (directory / f"{prefix}-{i:03d}.txt").write_text(f"content {i}")


def test_get_valid_files_truncates_and_logs_error(tmp_path, caplog):
    """A directory over file_limit is truncated and the truncation is an ERROR."""
    _write_files(tmp_path, 12)
    indexer = Indexer.__new__(Indexer)

    with caplog.at_level(logging.ERROR, logger="gptme_rag.indexing.indexer"):
        files = indexer._get_valid_files(tmp_path, file_limit=5)

    assert len(files) == 5
    assert any("File limit (5) reached, was 12" in rec.message for rec in caplog.records)
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)


def test_get_valid_files_under_limit_does_not_truncate(tmp_path, caplog):
    """A directory under file_limit returns every file and does not log."""
    _write_files(tmp_path, 3)
    indexer = Indexer.__new__(Indexer)

    with caplog.at_level(logging.ERROR, logger="gptme_rag.indexing.indexer"):
        files = indexer._get_valid_files(tmp_path, file_limit=100)

    assert len(files) == 3
    assert not any("File limit" in rec.message for rec in caplog.records)


def test_get_valid_files_exact_limit_does_not_log(tmp_path, caplog):
    """Hitting the cap exactly is not truncation — no ERROR, no slice."""
    _write_files(tmp_path, 5)
    indexer = Indexer.__new__(Indexer)

    with caplog.at_level(logging.ERROR, logger="gptme_rag.indexing.indexer"):
        files = indexer._get_valid_files(tmp_path, file_limit=5)

    assert len(files) == 5
    assert not any("File limit" in rec.message for rec in caplog.records)


def test_collect_documents_honors_file_limit(tmp_path):
    """collect_documents threads file_limit through to the walker."""
    _write_files(tmp_path, 8)
    indexer = Indexer.__new__(Indexer)
    indexer.processor = None  # skip embedder/chunker; one Document per file
    docs = indexer.collect_documents(tmp_path, file_limit=3)
    sources = {doc.metadata.get("source") for doc in docs}
    assert len(sources) == 3


def test_index_cli_exposes_file_limit_flag():
    runner = CliRunner()
    result = runner.invoke(cli, ["index", "--help"])
    assert result.exit_code == 0, result.output
    assert "--file-limit" in result.output
    assert "100000" in result.output


def test_get_valid_files_truncation_is_deterministic(tmp_path):
    """Over-cap truncation keeps the lexicographically first N paths, stably."""
    _write_files(tmp_path, 12)
    indexer = Indexer.__new__(Indexer)

    first = indexer._get_valid_files(tmp_path, file_limit=5)
    second = indexer._get_valid_files(tmp_path, file_limit=5)

    assert first == second
    expected = set(sorted(tmp_path.resolve().glob("*.txt"))[:5])
    assert first == expected


def test_get_valid_files_rejects_negative_file_limit(tmp_path):
    _write_files(tmp_path, 3)
    indexer = Indexer.__new__(Indexer)
    with pytest.raises(ValueError, match="file_limit must be >= 0"):
        indexer._get_valid_files(tmp_path, file_limit=-1)


def test_get_valid_files_rejects_non_integer_file_limit(tmp_path):
    _write_files(tmp_path, 3)
    indexer = Indexer.__new__(Indexer)
    with pytest.raises(TypeError, match="file_limit must be an integer"):
        indexer._get_valid_files(tmp_path, file_limit=2.5)


def test_index_cli_rejects_negative_file_limit(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["index", "--file-limit", "-1", str(tmp_path)])
    assert result.exit_code != 0
    assert "Invalid value" in result.output
    assert "--file-limit" in result.output
