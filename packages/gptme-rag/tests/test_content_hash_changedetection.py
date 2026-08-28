"""Tests for content-hash-based change detection (gptme-contrib Bug #2).

The change-detection key in the `index` command was mtime-only. A git restore,
checkout, or hot-worktree mtime rewrite would force a full re-embed of unchanged
files. This test verifies that:

1. `Document.from_file` records a stable `content_hash` (sha256 of file bytes).
2. Re-running `index` on unchanged content does not re-embed (only first-run embeds).
3. Touching mtime alone does not re-embed, but changing content does.

See tasks/vitals-regen-index-cpu-burn-loop.md for the parent investigation.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from click.testing import CliRunner

from gptme_rag.cli import cli
from gptme_rag.indexing.document import Document


def test_document_from_file_records_content_hash(tmp_path):
    """from_file stores a sha256 of the file bytes in metadata."""
    f = tmp_path / "a.txt"
    f.write_text("hello world")
    docs = list(Document.from_file(f))
    assert docs, "expected at least one document"
    expected = hashlib.sha256(b"hello world").hexdigest()
    for doc in docs:
        assert doc.metadata["content_hash"] == expected


def test_content_hash_changes_when_content_changes(tmp_path):
    """Editing file content changes the recorded content_hash."""
    f = tmp_path / "a.txt"
    f.write_text("v1")
    docs_v1 = list(Document.from_file(f))
    f.write_text("v2")
    docs_v2 = list(Document.from_file(f))
    h1 = docs_v1[0].metadata["content_hash"]
    h2 = docs_v2[0].metadata["content_hash"]
    assert h1 != h2


def test_content_hash_stable_across_mtime_touch(tmp_path):
    """Touching mtime alone does not change the content_hash."""
    f = tmp_path / "a.txt"
    f.write_text("stable")
    h1 = list(Document.from_file(f))[0].metadata["content_hash"]
    os.utime(f, (f.stat().st_atime + 10, f.stat().st_mtime + 10))
    h2 = list(Document.from_file(f))[0].metadata["content_hash"]
    assert h1 == h2


def _run_index(runner, index_dir, sources, *extra_args):
    """Run the index CLI against a temp persist dir. Returns CliResult."""
    return runner.invoke(
        cli,
        [
            "index",
            "--persist-dir",
            str(index_dir),
            "--embedding-function",
            "default",
            *extra_args,
            *[str(s) for s in sources],
        ],
    )


def test_index_skips_unchanged_mtime_only_rewrite(tmp_path):
    """Second run on unchanged content (mtime touched) does NOT re-embed."""
    runner = CliRunner()
    index_dir = tmp_path / "index"
    src = tmp_path / "src"
    src.mkdir()
    f = src / "doc.txt"
    f.write_text("some stable corpus content")

    first = _run_index(runner, index_dir, [src])
    assert first.exit_code == 0, first.output
    assert "Successfully indexed 1 files" in first.output, first.output

    # Simulate a git-restore style mtime rewrite: content identical, mtime bumped.
    os.utime(f, (f.stat().st_atime + 100, f.stat().st_mtime + 100))
    second = _run_index(runner, index_dir, [src])
    assert second.exit_code == 0, second.output
    assert "No new or modified documents to index" in second.output, second.output


def test_index_reembeds_on_content_change(tmp_path):
    """Changing content re-embeds on the next run."""
    runner = CliRunner()
    index_dir = tmp_path / "index"
    src = tmp_path / "src"
    src.mkdir()
    f = src / "doc.txt"
    f.write_text("version one content")

    first = _run_index(runner, index_dir, [src])
    assert first.exit_code == 0, first.output

    f.write_text("version two content changed")
    second = _run_index(runner, index_dir, [src])
    assert second.exit_code == 0, second.output
    assert "Successfully indexed 1 files" in second.output, second.output


def test_index_replaces_stale_chunks_on_content_change(tmp_path):
    """Re-indexing a changed source replaces its old chunks instead of accumulating."""
    runner = CliRunner()
    index_dir = tmp_path / "index"
    src = tmp_path / "src"
    src.mkdir()
    f = src / "doc.txt"
    f.write_text("one two three four five six seven eight")

    first = _run_index(
        runner,
        index_dir,
        [src],
        "--chunk-size",
        "3",
        "--chunk-overlap",
        "1",
    )
    assert first.exit_code == 0, first.output

    import chromadb

    client = chromadb.PersistentClient(
        path=str(index_dir),
        settings=chromadb.config.Settings(
            allow_reset=True,
            is_persistent=True,
            anonymized_telemetry=False,
        ),
    )
    collection = client.get_collection("default")
    first_count = collection.count()
    assert first_count > 1
    del collection, client
    chromadb.api.shared_system_client.SharedSystemClient._identifier_to_system.clear()

    f.write_text("replacement content")
    second = _run_index(
        runner,
        index_dir,
        [src],
        "--chunk-size",
        "3",
        "--chunk-overlap",
        "1",
    )
    assert second.exit_code == 0, second.output

    client = chromadb.PersistentClient(
        path=str(index_dir),
        settings=chromadb.config.Settings(
            allow_reset=True,
            is_persistent=True,
            anonymized_telemetry=False,
        ),
    )
    collection = client.get_collection("default")
    assert collection.count() < first_count
    stored = collection.get(include=["metadatas"])["metadatas"]
    expected_hash = hashlib.sha256(f.read_bytes()).hexdigest()
    assert {metadata["content_hash"] for metadata in stored} == {expected_hash}


def _strip_content_hash_from_index(index_dir: Path) -> None:
    """Replace stored docs with legacy copies that lack content_hash metadata."""
    import chromadb

    client = chromadb.PersistentClient(
        path=str(index_dir),
        settings=chromadb.config.Settings(
            allow_reset=True,
            is_persistent=True,
            anonymized_telemetry=False,
        ),
    )
    collection = client.get_collection("default")
    results = collection.get(include=["embeddings", "metadatas", "documents"])
    legacy_metadatas = [
        {key: value for key, value in metadata.items() if key != "content_hash"}
        for metadata in results["metadatas"]
    ]
    collection.delete(ids=results["ids"])
    collection.add(
        ids=results["ids"],
        embeddings=results["embeddings"],
        metadatas=legacy_metadatas,
        documents=results["documents"],
    )


def test_index_legacy_mtime_fallback_skips_unchanged(tmp_path):
    """Legacy stored docs (no content_hash) use mtime fallback — unchanged mtime skips."""
    runner = CliRunner()
    index_dir = tmp_path / "index"
    src = tmp_path / "src"
    src.mkdir()
    f = src / "legacy.txt"
    f.write_text("legacy corpus content")

    # First run: index the file, storing content_hash.
    first = _run_index(runner, index_dir, [src])
    assert first.exit_code == 0, first.output
    assert "Successfully indexed 1 files" in first.output, first.output

    # Simulate legacy stored docs by removing content_hash from the index.
    _strip_content_hash_from_index(index_dir)

    # Second run: mtime unchanged, no content_hash → mtime fallback should skip.
    second = _run_index(runner, index_dir, [src])
    assert second.exit_code == 0, second.output
    assert "No new or modified documents to index" in second.output, second.output


def test_index_legacy_mtime_fallback_reembeds_on_mtime_change(tmp_path):
    """Legacy stored docs use mtime fallback — bumped mtime triggers re-embed."""
    runner = CliRunner()
    index_dir = tmp_path / "index"
    src = tmp_path / "src"
    src.mkdir()
    f = src / "legacy.txt"
    f.write_text("legacy corpus content")

    first = _run_index(runner, index_dir, [src])
    assert first.exit_code == 0, first.output

    # Strip content_hash to simulate legacy docs.
    _strip_content_hash_from_index(index_dir)

    # Bump mtime (no content change) → mtime fallback sees newer mtime → re-embed.
    os.utime(f, (f.stat().st_atime + 100, f.stat().st_mtime + 100))
    second = _run_index(runner, index_dir, [src])
    assert second.exit_code == 0, second.output
    assert "Successfully indexed 1 files" in second.output, second.output
