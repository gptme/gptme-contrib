"""Regression tests for same-model recreate wipes and orphan segment GC.

Bob's ambient-memory index was deleted and rebuilt from zero when logs showed
``Model mismatch (stored: modernbert, current: modernbert) or force recreate``.
The index command uses allow_recreate=True, so two paths can wipe a live
collection without an operator asking:

1. The exception fallback: get_collection() raises (Chroma EF-object conflict
   even when stored_model == current_model) and the handler sets
   need_recreate=True.
2. force_recreate=True is logged as "Model mismatch" even when the models
   match, hiding that the wipe was requested rather than detected.

These tests pin both: matching-model EF conflicts must preserve the
collection, and force-recreate logs must not say "mismatch".
"""

from __future__ import annotations

import logging
from pathlib import Path

import chromadb
from chromadb.api.client import Client
from chromadb.config import Settings
from click.testing import CliRunner

from gptme_rag.cli import cli
from gptme_rag.indexing.indexer import Indexer

UUID_DIR = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _make_default_collection(persist_dir: Path, *, n_docs: int = 1) -> None:
    settings = Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False)
    client = chromadb.PersistentClient(path=str(persist_dir), settings=settings)
    col = client.create_collection(
        name="default",
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": "default",
            "embedding_backend": "default",
        },
    )
    col.add(
        ids=[f"doc-{i}" for i in range(n_docs)],
        embeddings=[[0.1] * 384 for _ in range(n_docs)],
        documents=[f"keep me {i}" for i in range(n_docs)],
    )
    assert col.count() == n_docs
    del client


def test_same_model_get_collection_error_does_not_wipe(tmp_path, monkeypatch):
    """EF-object conflict on a matching-model collection must not delete it.

    Regression: Indexer.__init__ caught get_collection() failures and set
    need_recreate=True whenever allow_recreate=True (the index command
    default). Chroma can raise on embedding-function identity even when
    stored_model == current_model; that wiped an 84k-chunk index.
    """
    persist_dir = tmp_path / "index"
    persist_dir.mkdir()
    _make_default_collection(persist_dir)

    real_get = Client.get_collection
    calls = {"n": 0}

    def boom(self, name, embedding_function=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("Embedding function conflict")
        return real_get(self, name, embedding_function=embedding_function, **kwargs)

    monkeypatch.setattr(Client, "get_collection", boom)

    indexer = Indexer(
        persist_directory=persist_dir,
        enable_persist=True,
        embedding_function="default",
        collection_name="default",
        allow_recreate=True,
        force_recreate=False,
    )

    assert indexer.collection.count() == 1, (
        "Matching-model get_collection error wiped the collection — "
        "same-model recreate-guard regression"
    )
    verify = chromadb.PersistentClient(
        path=str(persist_dir),
        settings=Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False),
    )
    assert verify.get_collection("default").count() == 1


def test_force_recreate_same_model_does_not_log_mismatch(tmp_path, caplog):
    """force_recreate with stored == current must not claim a model mismatch.

    The combined log line hid that the wipe was requested, not detected —
    operators saw ``Model mismatch (stored: X, current: X)`` and chased a
    comparison bug that was actually --force-recreate.
    """
    persist_dir = tmp_path / "index"
    persist_dir.mkdir()
    _make_default_collection(persist_dir)

    with caplog.at_level(logging.INFO, logger="gptme_rag.indexing.indexer"):
        indexer = Indexer(
            persist_directory=persist_dir,
            enable_persist=True,
            embedding_function="default",
            collection_name="default",
            force_recreate=True,
        )

    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "Model mismatch" not in messages, (
        "force_recreate logged as a model mismatch — operators cannot tell "
        "a requested wipe from a detected one"
    )
    assert "Force recreate" in messages
    # force_recreate is allowed to wipe; this test only pins the log.
    assert indexer.collection is not None


def test_gc_orphan_segment_dirs_dry_run_and_apply(tmp_path):
    """UUID dirs not listed in chroma.sqlite3 segments are orphans; others stay."""
    from gptme_rag.indexing.gc import gc_orphan_segment_dirs

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()
    _make_default_collection(persist_dir)

    orphan = persist_dir / UUID_DIR
    orphan.mkdir()
    (orphan / "data_level0.bin").write_bytes(b"stale")

    live_dirs = {p.name for p in persist_dir.iterdir() if p.is_dir()}
    assert UUID_DIR in live_dirs

    dry = gc_orphan_segment_dirs(persist_dir, apply=False)
    assert any(p.name == UUID_DIR for p in dry)
    assert orphan.exists(), "dry-run must not delete"

    applied = gc_orphan_segment_dirs(persist_dir, apply=True)
    assert any(p.name == UUID_DIR for p in applied)
    assert not orphan.exists(), "apply must delete the orphan UUID dir"
    assert (persist_dir / "chroma.sqlite3").exists()
    remaining = {p.name for p in persist_dir.iterdir() if p.is_dir()}
    assert UUID_DIR not in remaining
    assert remaining, "must not delete live segment dirs"


def test_gc_orphans_cli_defaults_to_dry_run(tmp_path):
    persist_dir = tmp_path / "index"
    persist_dir.mkdir()
    _make_default_collection(persist_dir)
    orphan = persist_dir / UUID_DIR
    orphan.mkdir()
    (orphan / "data_level0.bin").write_bytes(b"stale")

    result = CliRunner().invoke(
        cli,
        ["gc-orphans", "--persist-dir", str(persist_dir)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert orphan.exists(), "CLI default is dry-run"
    assert UUID_DIR in result.output

    result = CliRunner().invoke(
        cli,
        ["gc-orphans", "--persist-dir", str(persist_dir), "--apply"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert not orphan.exists()
