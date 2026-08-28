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
import re
from pathlib import Path

from click.testing import CliRunner

from gptme_rag.cli import cli
from gptme_rag.indexing.document import Document

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _plain(output: str) -> str:
    """Strip spinner/markup so assertions work when stdout is a TTY."""
    return _ANSI_RE.sub("", output)


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
    assert "Successfully indexed 1 files" in _plain(first.output), first.output

    # Simulate a git-restore style mtime rewrite: content identical, mtime bumped.
    os.utime(f, (f.stat().st_atime + 100, f.stat().st_mtime + 100))
    second = _run_index(runner, index_dir, [src])
    assert second.exit_code == 0, second.output
    assert "No new or modified documents to index" in _plain(second.output), second.output


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
    assert "Successfully indexed 1 files" in _plain(second.output), second.output


def test_index_preserves_old_chunks_when_replacement_add_fails(tmp_path, monkeypatch):
    """A failed replacement embed must not delete the source's old chunks."""
    runner = CliRunner()
    index_dir = tmp_path / "index"
    src = tmp_path / "src"
    src.mkdir()
    f = src / "doc.txt"
    f.write_text("version one content")

    first = _run_index(runner, index_dir, [src])
    assert first.exit_code == 0, first.output

    f.write_text("version two content")

    import chromadb

    chromadb.api.shared_system_client.SharedSystemClient._identifier_to_system.clear()
    original_add = chromadb.Collection.add
    replacement_hash = hashlib.sha256(b"version two content").hexdigest()

    def fail_replacement_add(self, *args, **kwargs):
        metadatas = kwargs.get("metadatas")
        if metadatas is None and len(args) >= 3:
            metadatas = args[2]
        metadatas = metadatas or []
        if any(metadata.get("content_hash") == replacement_hash for metadata in metadatas):
            raise RuntimeError("simulated embedding failure")
        return original_add(self, *args, **kwargs)

    monkeypatch.setattr(chromadb.Collection, "add", fail_replacement_add)
    second = _run_index(runner, index_dir, [src])
    assert second.exit_code == 0, second.output
    assert "simulated embedding failure" in second.output

    chromadb.api.shared_system_client.SharedSystemClient._identifier_to_system.clear()
    client = chromadb.PersistentClient(
        path=str(index_dir),
        settings=chromadb.config.Settings(
            allow_reset=True,
            is_persistent=True,
            anonymized_telemetry=False,
        ),
    )
    collection = client.get_collection("default")
    stored = collection.get(include=["metadatas"])["metadatas"]
    assert stored is not None
    old_hash = hashlib.sha256(b"version one content").hexdigest()
    assert {metadata["content_hash"] for metadata in stored} == {old_hash}


def test_index_retry_after_crash_does_not_delete_replacement(tmp_path):
    """If add succeeded and delete did not, retry must keep the new generation."""
    runner = CliRunner()
    index_dir = tmp_path / "index"
    src = tmp_path / "src"
    src.mkdir()
    f = src / "doc.txt"
    f.write_text("version one content")

    first = _run_index(runner, index_dir, [src])
    assert first.exit_code == 0, first.output

    import chromadb

    chromadb.api.shared_system_client.SharedSystemClient._identifier_to_system.clear()
    f.write_text("version two content")
    v2_hash = hashlib.sha256(b"version two content").hexdigest()
    abs_source = str(f.resolve())

    client = chromadb.PersistentClient(
        path=str(index_dir),
        settings=chromadb.config.Settings(
            allow_reset=True,
            is_persistent=True,
            anonymized_telemetry=False,
        ),
    )
    collection = client.get_collection("default")
    existing = collection.get(include=["embeddings", "metadatas", "documents"])
    embeddings = existing["embeddings"]
    assert embeddings is not None and len(embeddings) > 0
    collection.add(
        ids=[f"{abs_source}@{v2_hash}#chunk0-0"],
        documents=["version two content"],
        metadatas=[
            {
                "source": abs_source,
                "filename": f.name,
                "extension": f.suffix,
                "content_hash": v2_hash,
                "last_modified": "2026-08-28T00:00:00",
                "is_chunk": True,
            }
        ],
        embeddings=[existing["embeddings"][0]],
    )
    del collection, client
    chromadb.api.shared_system_client.SharedSystemClient._identifier_to_system.clear()

    second = _run_index(runner, index_dir, [src])
    assert second.exit_code == 0, second.output
    assert "Removed leftover stale chunks" in _plain(second.output), second.output
    assert "Successfully indexed" not in _plain(second.output), second.output

    chromadb.api.shared_system_client.SharedSystemClient._identifier_to_system.clear()
    client = chromadb.PersistentClient(
        path=str(index_dir),
        settings=chromadb.config.Settings(
            allow_reset=True,
            is_persistent=True,
            anonymized_telemetry=False,
        ),
    )
    collection = client.get_collection("default")
    stored = collection.get(include=["metadatas"])["metadatas"]
    assert stored is not None
    assert {metadata["content_hash"] for metadata in stored} == {v2_hash}


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
    assert "Successfully indexed 1 files" in _plain(first.output), first.output

    # Simulate legacy stored docs by removing content_hash from the index.
    _strip_content_hash_from_index(index_dir)

    # Second run: mtime unchanged, no content_hash → mtime fallback should skip.
    second = _run_index(runner, index_dir, [src])
    assert second.exit_code == 0, second.output
    assert "No new or modified documents to index" in _plain(second.output), second.output


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
    assert "Successfully indexed 1 files" in _plain(second.output), second.output


def test_from_file_hash_matches_decoded_bytes(tmp_path):
    """Hash and document content must come from the same read of the file."""
    f = tmp_path / "a.txt"
    payload = "hash and content share one read"
    f.write_bytes(payload.encode())
    docs = list(Document.from_file(f))
    assert docs[0].content == payload
    assert docs[0].metadata["content_hash"] == hashlib.sha256(payload.encode()).hexdigest()


def _make_legacy_index_with_invalid_last_chunk_mtime(index_dir: Path) -> None:
    """Legacy docs: drop content_hash, then poison only the last chunk's mtime."""
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
    assert results["ids"] and len(results["ids"]) > 1
    metadatas = []
    for i, metadata in enumerate(results["metadatas"]):
        legacy = {key: value for key, value in metadata.items() if key != "content_hash"}
        if i == len(results["ids"]) - 1:
            legacy["last_modified"] = "not-a-timestamp"
        metadatas.append(legacy)
    collection.delete(ids=results["ids"])
    collection.add(
        ids=results["ids"],
        embeddings=results["embeddings"],
        metadatas=metadatas,
        documents=results["documents"],
    )


def test_index_legacy_mtime_fallback_ignores_invalid_chunk_timestamp(tmp_path):
    """A single invalid last_modified must not zero the stored mtime for a source."""
    runner = CliRunner()
    index_dir = tmp_path / "index"
    src = tmp_path / "src"
    src.mkdir()
    f = src / "legacy.txt"
    f.write_text("one two three four five six seven eight nine ten")

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

    _make_legacy_index_with_invalid_last_chunk_mtime(index_dir)

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
    assert "No new or modified documents to index" in _plain(second.output), second.output


def test_index_reports_success_when_stale_delete_fails(tmp_path, monkeypatch):
    """A failed stale-chunk delete after a successful add must not hide the add."""
    runner = CliRunner()
    index_dir = tmp_path / "index"
    src = tmp_path / "src"
    src.mkdir()
    f = src / "doc.txt"
    f.write_text("version one content")

    first = _run_index(runner, index_dir, [src])
    assert first.exit_code == 0, first.output

    f.write_text("version two content")

    import chromadb

    chromadb.api.shared_system_client.SharedSystemClient._identifier_to_system.clear()
    original_delete = chromadb.Collection.delete

    def fail_delete(self, *args, **kwargs):
        raise RuntimeError("simulated delete failure")

    monkeypatch.setattr(chromadb.Collection, "delete", fail_delete)
    second = _run_index(runner, index_dir, [src])
    monkeypatch.setattr(chromadb.Collection, "delete", original_delete)
    assert second.exit_code == 0, second.output
    assert "Successfully indexed 1 files" in _plain(second.output), second.output
    assert "leftover stale chunks" in _plain(second.output), second.output

    chromadb.api.shared_system_client.SharedSystemClient._identifier_to_system.clear()
    client = chromadb.PersistentClient(
        path=str(index_dir),
        settings=chromadb.config.Settings(
            allow_reset=True,
            is_persistent=True,
            anonymized_telemetry=False,
        ),
    )
    collection = client.get_collection("default")
    stored = collection.get(include=["metadatas"])["metadatas"]
    assert stored is not None
    new_hash = hashlib.sha256(b"version two content").hexdigest()
    assert new_hash in {metadata["content_hash"] for metadata in stored}


def test_index_mixed_legacy_and_current_hash_cleans_legacy_without_reembed(tmp_path):
    """A leftover unhashed chunk beside the current hash must not re-embed."""
    runner = CliRunner()
    index_dir = tmp_path / "index"
    src = tmp_path / "src"
    src.mkdir()
    f = src / "doc.txt"
    f.write_text("stable mixed-generation content")

    first = _run_index(runner, index_dir, [src])
    assert first.exit_code == 0, first.output

    import chromadb

    chromadb.api.shared_system_client.SharedSystemClient._identifier_to_system.clear()
    abs_source = str(f.resolve())
    current_hash = hashlib.sha256(b"stable mixed-generation content").hexdigest()
    client = chromadb.PersistentClient(
        path=str(index_dir),
        settings=chromadb.config.Settings(
            allow_reset=True,
            is_persistent=True,
            anonymized_telemetry=False,
        ),
    )
    collection = client.get_collection("default")
    existing = collection.get(include=["embeddings", "metadatas", "documents"])
    embeddings = existing["embeddings"]
    assert embeddings is not None and len(embeddings) > 0
    collection.add(
        ids=[f"{abs_source}#legacy-unhashed"],
        documents=["stable mixed-generation content"],
        metadatas=[
            {
                "source": abs_source,
                "filename": f.name,
                "extension": f.suffix,
                "last_modified": "2026-08-28T00:00:00",
                "is_chunk": True,
            }
        ],
        embeddings=[embeddings[0]],
    )
    del collection, client
    chromadb.api.shared_system_client.SharedSystemClient._identifier_to_system.clear()

    second = _run_index(runner, index_dir, [src])
    assert second.exit_code == 0, second.output
    assert "Successfully indexed" not in _plain(second.output), second.output
    assert "Removed leftover stale chunks" in _plain(second.output), second.output

    chromadb.api.shared_system_client.SharedSystemClient._identifier_to_system.clear()
    client = chromadb.PersistentClient(
        path=str(index_dir),
        settings=chromadb.config.Settings(
            allow_reset=True,
            is_persistent=True,
            anonymized_telemetry=False,
        ),
    )
    collection = client.get_collection("default")
    stored = collection.get(include=["metadatas"])
    metadatas = stored["metadatas"]
    assert metadatas is not None
    assert {metadata.get("content_hash") for metadata in metadatas} == {current_hash}
    assert f"{abs_source}#legacy-unhashed" not in stored["ids"]


def test_index_emptied_file_deletes_stale_chunks(tmp_path):
    """Emptying a previously indexed file must drop its leftover chunks."""
    runner = CliRunner()
    index_dir = tmp_path / "index"
    src = tmp_path / "src"
    src.mkdir()
    f = src / "doc.txt"
    f.write_text("content that will be emptied")

    first = _run_index(runner, index_dir, [src])
    assert first.exit_code == 0, first.output

    import chromadb

    chromadb.api.shared_system_client.SharedSystemClient._identifier_to_system.clear()
    f.write_text("   \n")
    second = _run_index(runner, index_dir, [src])
    assert second.exit_code == 0, second.output
    assert "Successfully indexed" not in _plain(second.output), second.output
    assert "Removed leftover stale chunks" in _plain(second.output), second.output

    chromadb.api.shared_system_client.SharedSystemClient._identifier_to_system.clear()
    client = chromadb.PersistentClient(
        path=str(index_dir),
        settings=chromadb.config.Settings(
            allow_reset=True,
            is_persistent=True,
            anonymized_telemetry=False,
        ),
    )
    collection = client.get_collection("default")
    stored = collection.get(include=["metadatas"])
    abs_source = str(f.resolve())
    leftover = [
        metadata for metadata in (stored["metadatas"] or []) if metadata.get("source") == abs_source
    ]
    assert leftover == []
