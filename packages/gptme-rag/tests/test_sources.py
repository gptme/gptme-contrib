"""Tests for source-descriptor document collection (Phase 2.1)."""

from pathlib import Path


from gptme_rag.indexing.document import Document
from gptme_rag.sources import (
    SourceDescriptor,
    SourceRegistry,
    collect_voice_call_documents,
    de_accumulate_transcript,
)


# ---------------------------------------------------------------------------
# de_accumulate_transcript
# ---------------------------------------------------------------------------


def test_de_accumulate_transcript_empty():
    assert de_accumulate_transcript([]) == ""


def test_de_accumulate_transcript_single_turn():
    transcript = [{"role": "user", "text": "hello"}]
    assert de_accumulate_transcript(transcript) == "USER: hello"


def test_de_accumulate_transcript_drops_cumulative_partials():
    # A cumulative transcript: the user role repeats with growing text.
    transcript = [
        {"role": "user", "text": "he"},
        {"role": "user", "text": "hello"},
        {"role": "user", "text": "hello world"},
        {"role": "assistant", "text": "hi"},
        {"role": "assistant", "text": "hi there"},
    ]
    result = de_accumulate_transcript(transcript)
    assert result == "USER: hello world\n\nASSISTANT: hi there"


def test_de_accumulate_transcript_skips_empty_turns():
    transcript = [
        {"role": "user", "text": "   "},
        {"role": "assistant", "text": "real"},
    ]
    assert de_accumulate_transcript(transcript) == "ASSISTANT: real"


def test_de_accumulate_transcript_role_reset_resumes_run():
    # Same role appearing after a different role starts a new run.
    transcript = [
        {"role": "user", "text": "a"},
        {"role": "user", "text": "ab"},
        {"role": "assistant", "text": "x"},
        {"role": "user", "text": "c"},
        {"role": "user", "text": "cd"},
    ]
    result = de_accumulate_transcript(transcript)
    assert result == "USER: ab\n\nASSISTANT: x\n\nUSER: cd"


# ---------------------------------------------------------------------------
# SourceRegistry
# ---------------------------------------------------------------------------


def _mk_doc(text: str, source: str = "s.md") -> Document:
    return Document(content=text, metadata={"type": "test", "source": source})


def test_registry_collect_always_on_only_by_default():
    registry = SourceRegistry()
    registry.add("always", lambda: [_mk_doc("a")])
    registry.add("gated", lambda: [_mk_doc("g")], always_on=False)

    docs = registry.collect()
    assert [d.content for d in docs] == ["a"]


def test_registry_collect_gated_includes_all():
    registry = SourceRegistry()
    registry.add("always", lambda: [_mk_doc("a")])
    registry.add("gated", lambda: [_mk_doc("g")], always_on=False)

    docs = registry.collect(gated=True)
    assert [d.content for d in docs] == ["a", "g"]


def test_registry_collector_exception_does_not_sink_build():
    registry = SourceRegistry()

    def bad() -> list[Document]:
        raise RuntimeError("boom")

    registry.add("bad", bad)
    registry.add("good", lambda: [_mk_doc("ok")])

    docs = registry.collect()
    assert [d.content for d in docs] == ["ok"]


def test_registry_sources_property():
    registry = SourceRegistry()
    registry.add("a", lambda: [_mk_doc("a")])
    assert [s.name for s in registry.sources] == ["a"]


def test_descriptor_defaults_always_on():
    desc = SourceDescriptor("n", lambda: [_mk_doc("x")])
    assert desc.always_on is True
    assert desc.name == "n"


# ---------------------------------------------------------------------------
# collect_voice_call_documents
# ---------------------------------------------------------------------------


def test_collect_voice_calls_missing_dir(tmp_path: Path):
    assert collect_voice_call_documents(tmp_path / "nope") == []


def test_collect_voice_calls_deaccumulates_and_metadata(tmp_path: Path):
    call_dir = tmp_path / "voice"
    call_dir.mkdir()
    (call_dir / "20260811T080814Z-358-twilio-CAabc.json").write_text(
        '{"transcript": [{"role": "user", "text": "h"}, {"role": "user", "text": "hello"}, '
        '{"role": "assistant", "text": "hi"}], "source": "twilio"}',
        encoding="utf-8",
    )

    docs = collect_voice_call_documents(call_dir, repo_root=tmp_path)
    assert len(docs) == 1
    doc = docs[0]
    assert doc.content == "USER: hello\n\nASSISTANT: hi"
    assert doc.doc_id == "voicecall:20260811T080814Z-358-twilio-CAabc"
    assert doc.metadata["type"] == "voicecall"
    assert doc.metadata["title"] == "Voice call 2026-08-11 (twilio)"
    assert doc.metadata["date"] == "2026-08-11"


def test_collect_voice_calls_skips_bad_json(tmp_path: Path):
    call_dir = tmp_path / "voice"
    call_dir.mkdir()
    (call_dir / "good.json").write_text(
        '{"transcript": [{"role": "user", "text": "hello"}]}', encoding="utf-8"
    )
    (call_dir / "bad.json").write_text("{not json", encoding="utf-8")

    docs = collect_voice_call_documents(call_dir)
    assert len(docs) == 1


def test_collect_voice_calls_skips_empty_transcript(tmp_path: Path):
    call_dir = tmp_path / "voice"
    call_dir.mkdir()
    (call_dir / "empty.json").write_text('{"transcript": []}', encoding="utf-8")

    docs = collect_voice_call_documents(call_dir)
    assert docs == []


def test_collect_voice_calls_repo_root_guard(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "call.json").write_text(
        '{"transcript": [{"role": "user", "text": "hello"}]}', encoding="utf-8"
    )
    # repo_root is the `repo` dir; `outside` is a sibling, NOT under it.
    assert collect_voice_call_documents(outside, repo_root=repo) == []


def test_collect_voice_calls_skips_non_dict_json(tmp_path: Path):
    """Valid JSON that is not a dict (e.g. an array) must be skipped gracefully."""
    call_dir = tmp_path / "voice"
    call_dir.mkdir()
    (call_dir / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
    (call_dir / "good.json").write_text(
        '{"transcript": [{"role": "user", "text": "hello"}]}', encoding="utf-8"
    )
    docs = collect_voice_call_documents(call_dir)
    assert len(docs) == 1


def test_collect_voice_calls_skips_non_utf8_file(tmp_path: Path):
    """Files with invalid UTF-8 bytes must be skipped, not crash the whole collection."""
    call_dir = tmp_path / "voice"
    call_dir.mkdir()
    (call_dir / "bad_encoding.json").write_bytes(b"\xff\xfe{not utf-8}")
    (call_dir / "good.json").write_text(
        '{"transcript": [{"role": "user", "text": "hello"}]}', encoding="utf-8"
    )
    docs = collect_voice_call_documents(call_dir)
    assert len(docs) == 1


def test_collect_voice_calls_repo_root_guard_resolves_symlinks(tmp_path: Path):
    """Symlinks pointing outside repo_root must be caught by the guard."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "call.json").write_text(
        '{"transcript": [{"role": "user", "text": "hello"}]}', encoding="utf-8"
    )
    # Symlink inside repo pointing outside
    symlink = repo / "calls"
    symlink.symlink_to(outside)
    # The guard must catch this even though the symlink path is under repo
    assert collect_voice_call_documents(symlink, repo_root=repo) == []


def test_de_accumulate_transcript_skips_non_dict_entries():
    """Non-dict entries in the transcript list must be skipped, not crash."""
    transcript = [
        {"role": "user", "text": "first"},
        "not a dict",
        42,
        {"role": "assistant", "text": "second"},
    ]
    result = de_accumulate_transcript(transcript)  # type: ignore[arg-type]
    assert "first" in result
    assert "second" in result


def test_collect_voice_calls_repo_root_guard_relative_vs_absolute(tmp_path: Path):
    """Guard works when voice_calls_dir is absolute but repo_root is also absolute.

    Regression for the P2 finding: Path.is_relative_to without .resolve() fails
    when paths are given in different forms (one relative, one absolute).  Using
    .resolve() on both normalises them so the comparison is spelling-independent.
    """
    repo = tmp_path / "repo"
    call_dir = repo / "calls"
    call_dir.mkdir(parents=True)
    (call_dir / "call.json").write_text(
        '{"transcript": [{"role": "user", "text": "hello"}]}', encoding="utf-8"
    )
    # Both absolute — must succeed (call_dir IS under repo)
    docs = collect_voice_call_documents(call_dir, repo_root=repo)
    assert len(docs) == 1
    # Absolute call_dir, absolute repo_root pointing to sibling — must be empty
    outside = tmp_path / "other"
    outside.mkdir()
    assert collect_voice_call_documents(call_dir, repo_root=outside) == []


def test_de_accumulate_transcript_preserves_non_cumulative_consecutive_turns():
    """Genuine consecutive same-role turns (non-cumulative) must NOT be silently dropped.

    Regression for P1 finding: de_accumulate_transcript always took max(run_texts,
    key=len), discarding shorter turns even when they weren't cumulative partials.
    Non-cumulative runs now join all turns instead of picking the longest.
    """
    transcript = [
        {"role": "user", "text": "Hello world"},
        {"role": "user", "text": "How are you?"},
        {"role": "assistant", "text": "I'm well"},
    ]
    result = de_accumulate_transcript(transcript)
    assert "Hello world" in result
    assert "How are you?" in result
    assert "I'm well" in result


def test_de_accumulate_transcript_still_collapses_cumulative_runs():
    """Cumulative partial-utterance runs still deduplicate to the longest entry."""
    transcript = [
        {"role": "user", "text": "I"},
        {"role": "user", "text": "I want"},
        {"role": "user", "text": "I want to discuss"},
        {"role": "assistant", "text": "ok"},
    ]
    result = de_accumulate_transcript(transcript)
    # Only the final cumulative entry survives; partials are dropped.
    assert result == "USER: I want to discuss\n\nASSISTANT: ok"


def test_collect_voice_calls_skips_file_symlink_outside_repo(tmp_path: Path):
    """A per-file symlink inside a valid call_dir that resolves outside repo_root
    must be skipped without crashing the whole collection.

    Regression for P1 finding: source_path computation used relative_to() without
    guarding per-file symlinks, so a single stray symlink raised ValueError and
    aborted collection for all remaining files.
    """
    repo = tmp_path / "repo"
    call_dir = repo / "calls"
    call_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    # A real file outside the repo
    target = outside / "real_call.json"
    target.write_text(
        '{"transcript": [{"role": "user", "text": "secret"}]}', encoding="utf-8"
    )
    # Symlink inside call_dir pointing outside repo_root
    link = call_dir / "escaped.json"
    link.symlink_to(target)
    # A legitimate in-repo file that must still be collected
    (call_dir / "good.json").write_text(
        '{"transcript": [{"role": "user", "text": "hello"}]}', encoding="utf-8"
    )
    docs = collect_voice_call_documents(call_dir, repo_root=repo)
    # Symlinked file outside repo must be skipped; good file must be collected.
    assert len(docs) == 1
    assert docs[0].content == "USER: hello"


def test_collect_voice_calls_source_path_resolves_before_relative_to(tmp_path: Path):
    """source_path computation must use path.resolve() so relative voice_calls_dir
    does not raise ValueError when repo_root is absolute.

    Regression for P1 finding: line 216 used path.relative_to(repo_root) with an
    unresolved (relative) path while the guard used resolved paths, causing a
    ValueError crash instead of returning collected documents.
    """
    import os

    repo = tmp_path / "repo"
    call_dir = repo / "calls"
    call_dir.mkdir(parents=True)
    (call_dir / "call.json").write_text(
        '{"transcript": [{"role": "user", "text": "hello world"}]}', encoding="utf-8"
    )
    # Change to the repo dir so a relative "calls" resolves under repo_root
    orig_cwd = os.getcwd()
    os.chdir(repo)
    try:
        # Relative voice_calls_dir, absolute repo_root — must not raise ValueError
        docs = collect_voice_call_documents(Path("calls"), repo_root=repo)
        assert len(docs) == 1
        # source metadata must be a relative path (relative to repo_root)
        assert not Path(docs[0].metadata["source"]).is_absolute()
    finally:
        os.chdir(orig_cwd)


def test_de_accumulate_transcript_whitespace_only_entry_loses_to_real_content():
    """A whitespace-only entry must not win the max() over real content.

    Regression for P1 finding: max(run_texts, key=len) picked a 10-space string
    over "hello" (raw lengths 10 vs 5).  The cumulative prefix check passes
    because "hello".startswith("") is True, so the run was treated as cumulative
    and the spaces won — resulting in best.strip() == "" and the turn being dropped.
    Fix: max(key=lambda t: len(t.strip())) strips first so only non-whitespace
    length counts.
    """
    transcript = [
        {"role": "user", "text": "          "},  # 10 spaces — longer raw, empty stripped
        {"role": "user", "text": "hello"},        # shorter raw, has real content
    ]
    result = de_accumulate_transcript(transcript)
    # "hello" must appear; empty-stripped entry must NOT cause the turn to disappear
    assert "hello" in result.lower()


def test_de_accumulate_transcript_non_dict_mid_run_does_not_break_cumulative():
    """A non-dict entry in the middle of a same-role run must be skipped, not split the run.

    Regression for P1 finding: the inner while loop checked isinstance() as a
    loop condition, so a non-dict at position j in a same-role run would end the
    run at j, then the outer loop would skip the non-dict and start a *new* run
    on the next same-role dict — causing a cumulative sequence to be treated as
    two independent turns (first partial kept, final longer text also kept, but
    reported as separate speaker blocks).
    """
    # A cumulative sequence interrupted by a non-dict junk entry mid-run
    transcript = [
        {"role": "user", "text": "I"},
        "junk",  # non-dict in the middle — must be skipped, not end the run
        {"role": "user", "text": "I want"},
        {"role": "user", "text": "I want to talk"},
        {"role": "assistant", "text": "sure"},
    ]
    result = de_accumulate_transcript(transcript)
    # The cumulative user run must collapse to the longest entry (last one)
    assert "I want to talk" in result
    # The partial "I" must NOT appear as a separate block
    assert result.count("USER:") == 1
    assert "ASSISTANT: sure" in result
