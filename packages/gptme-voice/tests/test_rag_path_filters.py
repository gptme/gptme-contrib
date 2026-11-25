"""Source selection must narrow a declared voice corpus before retrieval."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from gptme_voice.rag import VoiceRag, rag_tool_schema


def write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def lexical() -> None:
    pytest.importorskip("sklearn")


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pytest.importorskip("gptme_rag")
    today = datetime.now(timezone.utc).date().isoformat()
    write(tmp_path, f"journal/{today}/recent.md", "Brief standup monitoring work")
    script = write(
        tmp_path,
        "scripts/voice/brief.py",
        "generate_standup_brief writes state/standup-brief.json",
    )
    os.utime(script, (time.time() - 90 * 86400,) * 2)
    write(tmp_path, "knowledge/voice.md", "Prepared standup brief mechanism")
    write(tmp_path, "state/standup-brief.json", '{"summary": "Delivered the release"}')
    write(tmp_path, "private/ungranted.md", "Ungrantable secret")
    monkeypatch.setenv(
        "GPTME_VOICE_RAG_SOURCE_PATHS",
        "scripts/voice/*.py:knowledge/voice.md:state/standup-brief.json",
    )
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.usefixtures("lexical")
async def test_targeted_non_journal_lookup(corpus: Path) -> None:
    rag = VoiceRag(str(corpus), enabled=True)
    result = await rag.search(
        "generate_standup_brief", include_paths=["scripts/"], exclude_paths=["journal/"]
    )
    assert result["status"] == "ok"
    assert [r["source"] for r in result["results"]] == ["scripts/voice/brief.py"]
    assert "standup-brief.json" in result["results"][0]["snippet"]
    assert result["elapsed_ms"] < 8000
    assert result["coverage"]["mode"] == "filtered"


@pytest.mark.asyncio
@pytest.mark.usefixtures("lexical")
async def test_exclusion_wins_and_no_match_reports_coverage(corpus: Path) -> None:
    rag = VoiceRag(str(corpus), enabled=True)
    result = await rag.search(
        "brief", include_paths=["scripts", "knowledge"], exclude_paths=["scripts/"]
    )
    assert [r["source"] for r in result["results"]] == ["knowledge/voice.md"]
    for includes, excludes in [
        (["private/"], []),
        (["scripts/"], ["scripts/"]),
        (["missing/"], []),
    ]:
        empty = await rag.search(
            "brief", include_paths=includes, exclude_paths=excludes
        )
        assert empty["status"] == "empty"
        assert empty["results"] == []
        assert "coverage" in empty
        assert "not proof" in empty["message"]


@pytest.mark.asyncio
async def test_default_recaps_stay_recent_journals_only(corpus: Path) -> None:
    result = await VoiceRag(str(corpus), enabled=True).search(
        "what have you been doing in the last hour"
    )
    assert result["status"] == "ok"
    assert all(r["source"].startswith("journal/") for r in result["results"])
    assert result["coverage"]["mode"] == "recent_journals"


@pytest.mark.asyncio
@pytest.mark.parametrize("injected", [False, True])
@pytest.mark.parametrize(
    "query", ["what have you been doing today", "what have you been doing today?"]
)
@pytest.mark.parametrize(
    "filters", [{"include_paths": ["scripts/"]}, {"exclude_paths": ["journal/"]}]
)
async def test_filtered_recaps_require_topic_terms(
    corpus: Path, injected: bool, query: str, filters: dict[str, list[str]]
) -> None:
    def search_impl(query: str, n_results: int) -> list[dict[str, str]]:
        pytest.fail("A filtered query without topic terms must not call the backend")

    rag = VoiceRag(
        str(corpus), enabled=True, search_impl=search_impl if injected else None
    )
    result = await rag.search(query, **filters)
    assert result["status"] == "error"
    assert result["results"] == []
    assert "topic" in result["error"].lower()
    assert result["coverage"]["mode"] == "filtered"


@pytest.mark.asyncio
@pytest.mark.usefixtures("lexical")
async def test_exclude_journals_uses_configured_sources(corpus: Path) -> None:
    result = await VoiceRag(str(corpus), enabled=True).search(
        "brief", exclude_paths=["journal/"]
    )
    assert result["results"]
    assert all(not r["source"].startswith("journal/") for r in result["results"])


@pytest.mark.asyncio
@pytest.mark.usefixtures("lexical")
async def test_filters_apply_before_file_cap(corpus: Path) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    for n in range(90):
        write(corpus, f"journal/{today}/{n}.md", "Newer journal brief mechanism")
    result = await VoiceRag(str(corpus), enabled=True).search(
        "brief", include_paths=["scripts/"]
    )
    assert result["results"][0]["source"] == "scripts/voice/brief.py"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters",
    [
        "journal/",
        [1],
        ["../"],
        ["/etc/"],
        ["scripts/../private"],
        [""],
        ["scripts\\"],
        ["*"],
    ],
)
async def test_invalid_filters_return_tool_errors(
    corpus: Path, filters: object
) -> None:
    from gptme_voice.realtime.tool_bridge import GptmeToolBridge

    bridge = GptmeToolBridge(
        workspace=str(corpus), rag=VoiceRag(str(corpus), enabled=True)
    )
    result = await bridge.handle_function_call(
        "workspace_search", {"query": "brief", "include_paths": filters}
    )
    assert "error" in result
    assert "results" not in result


@pytest.mark.asyncio
@pytest.mark.usefixtures("lexical")
async def test_bridge_forwards_both_filters(corpus: Path) -> None:
    from gptme_voice.realtime.tool_bridge import GptmeToolBridge

    bridge = GptmeToolBridge(
        workspace=str(corpus), rag=VoiceRag(str(corpus), enabled=True)
    )
    result = await bridge.handle_function_call(
        "workspace_search",
        {
            "query": "brief",
            "include_paths": ["scripts/", "knowledge/"],
            "exclude_paths": ["scripts/"],
        },
    )
    assert [r["source"] for r in result["results"]] == ["knowledge/voice.md"]


@pytest.mark.asyncio
async def test_symlinks_cannot_extend_declared_corpus(
    corpus: Path, tmp_path: Path
) -> None:
    private = corpus / "private/ungranted.md"
    (corpus / "scripts/voice/leak.py").symlink_to(private)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("Outside secret")
    (corpus / "knowledge/voice.md").unlink()
    (corpus / "knowledge/voice.md").symlink_to(outside)
    today = datetime.now(timezone.utc).date().isoformat()
    (corpus / f"journal/{today}/leak.md").symlink_to(private)
    rag = VoiceRag(str(corpus), enabled=True)
    for kwargs in [{}, {"include_paths": ["scripts/", "knowledge/"]}]:
        result = await rag.search("secret", **kwargs)
        assert all("secret" not in r["snippet"].lower() for r in result["results"])
        assert all("leak" not in r["source"] for r in result["results"])


@pytest.mark.parametrize("swap_parent", [False, True])
def test_symlink_swap_between_validation_and_read_is_rejected(
    corpus: Path, monkeypatch: pytest.MonkeyPatch, swap_parent: bool
) -> None:
    rag = VoiceRag(str(corpus), enabled=True)
    source = corpus / "scripts/voice/brief.py"
    safe_source = rag._safe_source

    def swap_after_check(path: Path) -> bool:
        safe = safe_source(path)
        if swap_parent:
            parent = source.parent
            parent.rename(parent.with_name("original"))
            write(corpus, "private/brief.py", "Ungrantable secret")
            parent.symlink_to(corpus / "private", target_is_directory=True)
        else:
            source.unlink()
            source.symlink_to(corpus / "private/ungranted.md")
        return safe

    monkeypatch.setattr(rag, "_safe_source", swap_after_check)
    assert rag._load_documents([source]) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("lexical")
async def test_prefix_is_root_anchored_and_segment_bounded(corpus: Path) -> None:
    rag = VoiceRag(str(corpus), enabled=True)
    for prefix in ["voice/", "script", "knowledge/voice"]:
        result = await rag.search("brief", include_paths=[prefix])
        assert result["results"] == []
    result = await rag.search("release", include_paths=["state/standup-brief.json"])
    assert result["results"][0]["source"] == "state/standup-brief.json"


def test_schema_describes_filter_scope() -> None:
    schema = rag_tool_schema()["parameters"]["properties"]
    assert schema["include_paths"]["type"] == "array"
    assert schema["exclude_paths"]["items"]["type"] == "string"


@pytest.mark.asyncio
@pytest.mark.usefixtures("lexical")
async def test_unmatched_topic_is_empty_instead_of_unrelated_recency(
    corpus: Path,
) -> None:
    result = await VoiceRag(str(corpus), enabled=True).search(
        "quasars", include_paths=["scripts/"]
    )
    assert result["status"] == "empty"
    assert result["coverage"]["selected_files"] == 1
    assert result["results"] == []


@pytest.mark.asyncio
async def test_no_extra_sources_is_truthful_about_coverage(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GPTME_VOICE_RAG_SOURCE_PATHS")
    result = await VoiceRag(str(corpus), enabled=True).search(
        "brief", exclude_paths=["journal/"]
    )
    assert result["status"] == "empty"
    assert result["coverage"]["source_paths"] == []


@pytest.mark.asyncio
async def test_invalid_operator_paths_fail_closed(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GPTME_VOICE_RAG_SOURCE_PATHS", "../*.md")
    result = await VoiceRag(str(corpus), enabled=True).search(
        "secret", exclude_paths=["journal/"]
    )
    assert result["status"] == "empty"
    assert result["coverage"]["source_paths"] == []


@pytest.mark.asyncio
async def test_symlink_parent_is_not_a_corpus_grant(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (corpus / "alias").symlink_to(corpus / "private", target_is_directory=True)
    monkeypatch.setenv("GPTME_VOICE_RAG_SOURCE_PATHS", "alias/*.md")
    result = await VoiceRag(str(corpus), enabled=True).search(
        "secret", include_paths=["alias/"]
    )
    assert result["status"] == "empty"


@pytest.mark.asyncio
@pytest.mark.usefixtures("lexical")
async def test_broad_filter_cannot_starve_configured_sources(corpus: Path) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    for n in range(90):
        write(corpus, f"journal/{today}/{n}.md", "Unrelated work")
    result = await VoiceRag(str(corpus), enabled=True).search(
        "generate_standup_brief", include_paths=["scripts/", "journal/"]
    )
    assert result["results"][0]["source"] == "scripts/voice/brief.py"


@pytest.mark.asyncio
async def test_missing_lexical_backend_does_not_return_unrelated_files(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rag = VoiceRag(str(corpus), enabled=True)

    def unavailable(*args, **kwargs):
        raise ImportError("lexical dependencies missing")

    monkeypatch.setattr(rag, "_search_lexical", unavailable)
    result = await rag.search("quasars", include_paths=["scripts/"])
    assert result["status"] == "error"
    assert result["results"] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("lexical")
async def test_filtered_snippet_shows_match_beyond_file_header(corpus: Path) -> None:
    write(
        corpus,
        "scripts/voice/brief.py",
        "# Generic header\n" * 40
        + "cancel_planned_call requires purpose_fulfilled = True\n"
        + "# unrelated footer\n" * 40,
    )
    result = await VoiceRag(str(corpus), enabled=True).search(
        "cancel_planned_call", include_paths=["scripts/"]
    )
    assert result["status"] == "ok"
    assert (
        "cancel_planned_call requires purpose_fulfilled"
        in result["results"][0]["snippet"]
    )
    assert len(result["results"][0]["snippet"]) <= 402


@pytest.mark.asyncio
@pytest.mark.usefixtures("lexical")
async def test_snippet_anchor_matches_whole_lexical_tokens(corpus: Path) -> None:
    write(
        corpus,
        "scripts/voice/brief.py",
        "# implementation_notes\n" + "# filler\n" * 100 + "brief delivery details\n",
    )
    result = await VoiceRag(str(corpus), enabled=True).search(
        "implementation brief", include_paths=["scripts/"]
    )
    assert result["status"] == "ok"
    assert "brief delivery details" in result["results"][0]["snippet"]
