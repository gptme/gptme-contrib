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
