"""Tests for the per-directory file_limit cap in Indexer._get_valid_files.

The cap used to default to 1000 and log only a WARNING, which silently
truncated large corpora (Bob's ambient-memory index: ~43k git-visible files
across 7 paths, of which only ~4k were ever collected). See gptme-contrib#1523.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

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


def test_index_cli_threads_pattern_to_collect_documents(tmp_path, monkeypatch):
    """The index command passes --pattern to document collection."""
    observed_patterns = []

    monkeypatch.setattr(Indexer, "__init__", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(Indexer, "get_all_documents", lambda self: [])

    def collect_documents(self, path, glob_pattern="**/*.*", file_limit=100_000):
        observed_patterns.append(glob_pattern)
        return []

    monkeypatch.setattr(Indexer, "collect_documents", collect_documents)

    runner = CliRunner()
    result = runner.invoke(cli, ["index", "--pattern", "*.md", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert observed_patterns == ["*.md"]


@pytest.mark.skipif(shutil.which("git") is None, reason="requires the git executable")
def test_get_valid_files_honors_pattern_in_git_repo(tmp_path):
    """Git-backed discovery filters files with the requested pattern."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "nested").mkdir()
    (tmp_path / "root.md").write_text("root")
    (tmp_path / "named.md").write_text("named")
    (tmp_path / "docs" / "nested.md").write_text("nested")
    (tmp_path / "docs" / "named.md").write_text("nested named")
    (tmp_path / "docs" / "ignored.txt").write_text("ignored")
    (tmp_path / "src" / "root.py").write_text("root")
    (tmp_path / "src" / "nested" / "child.py").write_text("nested")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)

    indexer = Indexer.__new__(Indexer)
    markdown_files = indexer._get_valid_files(tmp_path, glob_pattern="**/*.md")
    named_files = indexer._get_valid_files(tmp_path, glob_pattern="**/named.md")
    python_files = indexer._get_valid_files(tmp_path, glob_pattern="src/**/*.py")
    character_class_files = indexer._get_valid_files(tmp_path, glob_pattern="src/**/*.[p]y")

    assert markdown_files == {
        (tmp_path / "root.md").resolve(),
        (tmp_path / "named.md").resolve(),
        (tmp_path / "docs" / "nested.md").resolve(),
        (tmp_path / "docs" / "named.md").resolve(),
    }
    assert named_files == {
        (tmp_path / "named.md").resolve(),
        (tmp_path / "docs" / "named.md").resolve(),
    }
    expected_python_files = {
        (tmp_path / "src" / "root.py").resolve(),
        (tmp_path / "src" / "nested" / "child.py").resolve(),
    }
    assert python_files == expected_python_files
    assert character_class_files == expected_python_files


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
