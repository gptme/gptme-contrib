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


def _run_index(runner, tmp_path, index_dir, sources):
    """Run the index CLI against a temp persist dir. Returns CliResult."""
    return runner.invoke(
        cli,
        [
            "index",
            "--persist-dir",
            str(index_dir),
            "--embedding-function",
            "minilm",
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

    first = _run_index(runner, tmp_path, index_dir, [src])
    assert first.exit_code == 0, first.output
    assert "Successfully indexed 1 files" in first.output, first.output

    # Simulate a git-restore style mtime rewrite: content identical, mtime bumped.
    os.utime(f, (f.stat().st_atime + 100, f.stat().st_mtime + 100))
    second = _run_index(runner, tmp_path, index_dir, [src])
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

    first = _run_index(runner, tmp_path, index_dir, [src])
    assert first.exit_code == 0, first.output

    f.write_text("version two content changed")
    second = _run_index(runner, tmp_path, index_dir, [src])
    assert second.exit_code == 0, second.output
    assert "Successfully indexed 1 files" in second.output, second.output
