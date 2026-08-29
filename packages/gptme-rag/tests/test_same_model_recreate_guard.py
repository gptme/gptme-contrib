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
from unittest.mock import patch

import chromadb
import pytest
from chromadb.api.client import Client
from chromadb.config import Settings
from click.testing import CliRunner

from gptme_rag.cli import cli
from gptme_rag.indexing.document import Document
from gptme_rag.indexing.indexer import Indexer, ModernBERTEmbedding

UUID_DIR = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _stub_modernbert(monkeypatch) -> None:
    """Make ModernBERTEmbedding constructible without loading 768-dim weights.

    Patching the class itself would break the ``isinstance`` checks the indexer
    uses to name the current model, so stub the instance methods instead.
    """
    monkeypatch.setattr(ModernBERTEmbedding, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(
        ModernBERTEmbedding,
        "__call__",
        lambda self, input: [[0.1] * 768 for _ in input],  # noqa: A002
    )
    monkeypatch.setattr(ModernBERTEmbedding, "is_msmarco", False, raising=False)


def _make_default_collection(persist_dir: Path, *, n_docs: int = 1, model: str = "default") -> None:
    settings = Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False)
    client = chromadb.PersistentClient(path=str(persist_dir), settings=settings)
    col = client.create_collection(
        name="default",
        metadata={
            "hnsw:space": "cosine",
            "embedding_model": model,
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


def test_preserved_collection_keeps_custom_embedding_function(tmp_path, monkeypatch):
    """A preserved handle must write with OUR embedder, not Chroma's default.

    Regression: the preserve branch assigned the handle returned by
    ``get_collection(name=...)`` — fetched deliberately without an embedding
    function — straight to ``self.collection``. ``_add_documents`` calls
    ``collection.add(documents=...)`` with no explicit ``embeddings=``, so that
    handle embeds with Chroma's default 384-dim MiniLM and raises
    InvalidDimensionException against 768-dim ModernBERT vectors. Preserving a
    collection you can no longer write to is not preserving it.
    """
    persist_dir = tmp_path / "index"
    persist_dir.mkdir()
    _make_default_collection(persist_dir, model="modernbert")

    _stub_modernbert(monkeypatch)

    real_get = Client.get_collection
    calls = {"n": 0}

    def boom(self, name, embedding_function=None, **kwargs):
        calls["n"] += 1
        if embedding_function is not None:
            raise ValueError("Embedding function conflict")
        return real_get(self, name, embedding_function=None, **kwargs)

    monkeypatch.setattr(Client, "get_collection", boom)

    indexer = Indexer(
        persist_directory=persist_dir,
        enable_persist=True,
        embedding_function="modernbert",
        collection_name="default",
        allow_recreate=True,
        force_recreate=False,
    )

    assert indexer.collection.count() == 1, "matching-model EF conflict wiped the collection"
    assert indexer.collection._embedding_function is indexer.embedding_function, (
        "preserved collection still carries Chroma's default embedder — writes "
        "would embed at the wrong dimension"
    )
    assert indexer._stored_model_name is None, (
        "rebinding succeeded, so the collection is writable; the read-only "
        "fail-loud guard must not be armed"
    )


def test_preserve_fails_loud_when_rebinding_is_impossible(tmp_path, monkeypatch):
    """If the EF cannot be rebound, arm the guard rather than write bad vectors.

    Chroma internals are not API. If a future version drops the attribute we
    bind to, the collection must become read-only with a clear RuntimeError
    instead of silently embedding at the default dimension.
    """
    persist_dir = tmp_path / "index"
    persist_dir.mkdir()
    _make_default_collection(persist_dir, model="modernbert")

    _stub_modernbert(monkeypatch)
    monkeypatch.setattr(Indexer, "_bind_embedding_function", lambda self, col: False)

    real_get = Client.get_collection

    def boom(self, name, embedding_function=None, **kwargs):
        if embedding_function is not None:
            raise ValueError("Embedding function conflict")
        return real_get(self, name, embedding_function=None, **kwargs)

    monkeypatch.setattr(Client, "get_collection", boom)

    indexer = Indexer(
        persist_directory=persist_dir,
        enable_persist=True,
        embedding_function="modernbert",
        collection_name="default",
        allow_recreate=True,
        force_recreate=False,
    )

    assert indexer.collection.count() == 1, "fail-loud path must still preserve the data"
    assert indexer._stored_model_name == "modernbert"
    assert indexer.embedding_function is None
    doc = Document(content="x", metadata={"source": "x.md"}, doc_id="x")
    with pytest.raises(RuntimeError, match="stored embedding model"):
        indexer.add_document(doc)


def test_bind_embedding_function_reports_failure_on_unknown_handle():
    """Version drift must be detectable so callers can fail loud, not silently."""
    indexer = Indexer.__new__(Indexer)
    indexer.embedding_function = object()

    class Opaque:
        __slots__ = ()

    assert indexer._bind_embedding_function(Opaque()) is False


def test_auto_mode_rebinds_preserved_collection(tmp_path, monkeypatch):
    """Auto mode must not leave Chroma's default EF on a matching-model handle.

    The auto exception path used to assign the EF-less peek handle and skip
    ``_bind_embedding_function``. Writes then embedded with MiniLM against a
    ModernBERT collection.
    """
    persist_dir = tmp_path / "index"
    persist_dir.mkdir()
    _make_default_collection(persist_dir, model="modernbert")
    _stub_modernbert(monkeypatch)

    real_get = Client.get_collection

    def boom(self, name, embedding_function=None, **kwargs):
        if embedding_function is not None:
            raise ValueError("Embedding function conflict")
        return real_get(self, name, embedding_function=None, **kwargs)

    monkeypatch.setattr(Client, "get_collection", boom)

    indexer = Indexer(
        persist_directory=persist_dir,
        enable_persist=True,
        embedding_function="auto",
        collection_name="default",
        allow_recreate=True,
        force_recreate=False,
    )

    assert indexer.collection.count() == 1
    assert indexer.collection._embedding_function is indexer.embedding_function
    assert indexer._stored_model_name is None


def test_gc_orphans_refuses_apply_while_writer_lock_is_held(tmp_path):
    """flock is per-open-file-description; same-process double-open does not
    conflict, so the holder must be a child process."""
    import subprocess
    import sys

    from gptme_rag.indexing.gc import gc_orphan_segment_dirs

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()
    _make_default_collection(persist_dir)
    orphan = persist_dir / UUID_DIR
    orphan.mkdir()
    lock_path = persist_dir / ".gptme-rag-writer.lock"

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o644)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "sys.stdout.write('locked\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n",
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == b"locked\n"
        with pytest.raises(RuntimeError, match="index writer is active"):
            gc_orphan_segment_dirs(persist_dir, apply=True)
    finally:
        holder.kill()
        holder.wait()

    assert orphan.exists()


def test_gc_orphans_catalog_error_is_not_reported_as_clean(tmp_path):
    persist_dir = tmp_path / "index"
    persist_dir.mkdir()
    (persist_dir / "chroma.sqlite3").write_bytes(b"not sqlite")

    result = CliRunner().invoke(
        cli,
        ["gc-orphans", "--persist-dir", str(persist_dir)],
    )

    assert result.exit_code != 0
    assert "No orphan segment dirs" not in result.output


def test_gc_orphans_oserror_is_not_a_traceback(tmp_path):
    """Permission errors on persist-dir access must exit 1 with a clean message.

    Regression: gc_orphan_segment_dirs only wrapped sqlite3.Error, so
    iterdir/lock-file OSError leaked as a traceback. The CLI is dry-run
    by default and should report a permission error, not dump frames.
    """
    from gptme_rag.indexing.gc import ChromaCatalogError, gc_orphan_segment_dirs

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()
    _make_default_collection(persist_dir)

    with patch(
        "gptme_rag.indexing.gc._collect_orphans",
        side_effect=PermissionError("read-only persist dir"),
    ):
        with pytest.raises(ChromaCatalogError, match="read-only persist dir"):
            gc_orphan_segment_dirs(persist_dir)
        result = CliRunner().invoke(
            cli,
            ["gc-orphans", "--persist-dir", str(persist_dir)],
            catch_exceptions=False,
        )

    assert result.exit_code != 0
    assert "No orphan segment dirs" not in result.output
    assert "Traceback" not in result.output
    assert "Cannot access" in result.output


def test_allow_recreate_false_peek_failure_does_not_wipe(tmp_path, monkeypatch):
    """Read-only Indexer must not delete a live collection if peek also fails.

    Regression: the ``not allow_recreate`` except branch set
    ``need_recreate=True`` when the EF-less ``get_collection()`` raised
    (transient sqlite lock, permission, missing-looking catalog). The later
    ``if need_recreate`` block then deleted and recreated the collection,
    wiping it from search/status.
    """
    persist_dir = tmp_path / "index"
    persist_dir.mkdir()
    _make_default_collection(persist_dir, n_docs=3)

    real_get = Client.get_collection

    def boom(self, name, embedding_function=None, **kwargs):
        raise ValueError("Embedding function conflict")

    monkeypatch.setattr(Client, "get_collection", boom)

    with pytest.raises(RuntimeError, match="recreation is disabled"):
        Indexer(
            persist_directory=persist_dir,
            enable_persist=True,
            embedding_function="default",
            collection_name="default",
            allow_recreate=False,
        )

    monkeypatch.setattr(Client, "get_collection", real_get)
    verify = chromadb.PersistentClient(
        path=str(persist_dir),
        settings=Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False),
    )
    assert verify.get_collection("default").count() == 3, (
        "allow_recreate=False peek failure wiped the collection — "
        "read-only recreate-guard regression"
    )


def test_allow_recreate_true_peek_failure_does_not_wipe(tmp_path, monkeypatch):
    """index-command Indexer must not delete a live collection if peek also fails.

    Regression: the allow_recreate=True except branch treated a failed
    EF-less get_collection() as "collection missing" and set
    need_recreate=True. Recreate deletes first, so a transient sqlite
    lock during a concurrent write would wipe a matching live collection
    (the original 84k-chunk incident, still open on the write path after
    the read-only guard).
    """
    persist_dir = tmp_path / "index"
    persist_dir.mkdir()
    _make_default_collection(persist_dir, n_docs=3)

    real_get = Client.get_collection

    def boom(self, name, embedding_function=None, **kwargs):
        raise ValueError("database is locked")

    monkeypatch.setattr(Client, "get_collection", boom)

    with pytest.raises(RuntimeError, match="not confirmed missing"):
        Indexer(
            persist_directory=persist_dir,
            enable_persist=True,
            embedding_function="default",
            collection_name="default",
            allow_recreate=True,
            force_recreate=False,
        )

    monkeypatch.setattr(Client, "get_collection", real_get)
    verify = chromadb.PersistentClient(
        path=str(persist_dir),
        settings=Settings(allow_reset=True, is_persistent=True, anonymized_telemetry=False),
    )
    assert verify.get_collection("default").count() == 3, (
        "allow_recreate=True peek failure wiped the collection — "
        "write-path recreate-guard regression"
    )


def test_gc_orphans_preserves_live_uuid_dir_case_insensitive(tmp_path):
    """Uppercase UUID dir whose lowercase id is in the catalog is live, not orphan.

    Regression: _UUID_DIR is IGNORECASE but membership was exact-case. On a
    case-insensitive filesystem (macOS default) that combination would rmtree
    a live segment whose on-disk name used uppercase hex.
    """
    import sqlite3

    from gptme_rag.indexing.gc import gc_orphan_segment_dirs

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()
    live_id = UUID_DIR
    upper_live = persist_dir / live_id.upper()
    upper_live.mkdir()
    (upper_live / "data_level0.bin").write_bytes(b"live")
    real_orphan = persist_dir / "11111111-2222-3333-4444-555555555555"
    real_orphan.mkdir()

    con = sqlite3.connect(str(persist_dir / "chroma.sqlite3"))
    con.execute("CREATE TABLE segments (id TEXT)")
    con.execute("INSERT INTO segments (id) VALUES (?)", (live_id,))
    con.commit()
    con.close()

    dry = gc_orphan_segment_dirs(persist_dir, apply=False)
    names = {p.name for p in dry}
    assert upper_live.name not in names
    assert real_orphan.name in names

    gc_orphan_segment_dirs(persist_dir, apply=True)
    assert upper_live.exists(), "case-variant of a catalogued UUID must not be deleted"
    assert not real_orphan.exists()


def test_gc_orphans_handles_question_mark_in_persist_path(tmp_path):
    from gptme_rag.indexing.gc import gc_orphan_segment_dirs

    persist_dir = tmp_path / "index?literal"
    persist_dir.mkdir()
    _make_default_collection(persist_dir)
    orphan = persist_dir / UUID_DIR
    orphan.mkdir()

    assert orphan in gc_orphan_segment_dirs(persist_dir)


def test_gc_orphans_continues_after_one_delete_failure(tmp_path):
    from gptme_rag.indexing.gc import gc_orphan_segment_dirs

    persist_dir = tmp_path / "index"
    persist_dir.mkdir()
    _make_default_collection(persist_dir)
    first = persist_dir / UUID_DIR
    second = persist_dir / "11111111-2222-3333-4444-555555555555"
    first.mkdir()
    second.mkdir()

    real_rmtree = __import__("shutil").rmtree

    def fail_first(path, *args, **kwargs):
        if Path(path) == first:
            raise PermissionError("read-only")
        return real_rmtree(path, *args, **kwargs)

    with patch("gptme_rag.indexing.gc.shutil.rmtree", side_effect=fail_first):
        removed = gc_orphan_segment_dirs(persist_dir, apply=True)

    assert first.exists()
    assert not second.exists()
    assert removed == [second]
