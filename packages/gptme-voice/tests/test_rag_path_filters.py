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
@pytest.mark.usefixtures("lexical")
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
    # Reads reject symlinks too, so check journal selection directly.
    selected = rag.collect_recent_files(include_paths=("journal",))
    assert [path.name for path in selected] == ["recent.md"]
    result = await rag.search(
        "secret", include_paths=["scripts/", "knowledge/", "journal/"]
    )
    assert result["status"] == "empty"
    assert result["results"] == []


@pytest.mark.asyncio
async def test_unfiltered_recap_follows_a_symlinked_journal_directory(
    tmp_path: Path,
) -> None:
    """Journal directories kept on another repo or volume must keep working."""
    pytest.importorskip("gptme_rag")
    today = datetime.now(timezone.utc).date().isoformat()
    journals = tmp_path / "journals"
    write(journals, f"{today}/work.md", "Shipped the standup brief")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "journal").symlink_to(journals, target_is_directory=True)

    result = await VoiceRag(str(workspace), enabled=True).search(
        "what have you been doing today"
    )

    assert result["status"] == "ok"
    assert [r["source"] for r in result["results"]] == [f"journal/{today}/work.md"]
    assert "standup brief" in result["results"][0]["snippet"]


def test_unfiltered_documents_load_without_a_pinned_root(corpus: Path) -> None:
    """Platforms without no-follow directory descriptors keep working unfiltered."""
    rag = VoiceRag(str(corpus), enabled=True)
    paths = rag.collect_recent_files()
    assert paths
    docs = rag._load_documents(paths, None, pinned=False)
    today = datetime.now(timezone.utc).date().isoformat()
    assert [d.metadata["source"] for d in docs] == [f"journal/{today}/recent.md"]
    assert "Brief standup monitoring work" in docs[0].content


@pytest.mark.asyncio
@pytest.mark.usefixtures("lexical")
@pytest.mark.parametrize(
    "phrase",
    ["standup brief", "standup-brief", "standup\nbrief", "Standup of the Brief"],
)
async def test_snippet_anchors_on_a_phrase_not_an_earlier_single_word(
    corpus: Path, phrase: str
) -> None:
    write(
        corpus,
        "scripts/voice/brief.py",
        "# standup\n" + "# filler\n" * 100 + f"{phrase} delivery plan\n",
    )
    result = await VoiceRag(str(corpus), enabled=True).search(
        "standup brief", include_paths=["scripts/"]
    )
    assert f"{phrase} delivery plan" in result["results"][0]["snippet"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("lexical")
async def test_broad_configured_glob_cannot_starve_recent_journals(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    write(corpus, f"journal/{today}/recap.md", "standup brief mechanism notes")
    for n in range(90):
        write(corpus, f"scripts/voice/gen_{n}.py", "unrelated generator code")
    monkeypatch.setenv("GPTME_VOICE_RAG_SOURCE_PATHS", "scripts/voice/*.py")
    result = await VoiceRag(str(corpus), enabled=True).search(
        "standup brief", include_paths=["scripts/", "journal/"]
    )
    sources = [r["source"] for r in result["results"]]
    assert f"journal/{today}/recap.md" in sources


@pytest.mark.asyncio
async def test_injected_backend_hits_intersect_the_approved_corpus(
    corpus: Path,
) -> None:
    """A custom backend may only surface files inside the selected corpus."""
    journal = next((corpus / "journal").rglob("*.md"))
    requested: list[int] = []

    def search_impl(query: str, n_results: int) -> list[dict[str, str]]:
        requested.append(n_results)
        # Non-approved and excluded sources rank above the approved one.
        return [
            {"source": "private/ungranted.md", "snippet": "Ungrantable secret"},
            {"source": journal.relative_to(corpus).as_posix(), "snippet": "brief"},
            {"source": "state/standup-brief.json", "snippet": "standup brief"},
            {"source": "knowledge/voice.md", "snippet": "standup brief"},
        ]

    rag = VoiceRag(str(corpus), enabled=True, search_impl=search_impl)
    result = await rag.search(
        "brief", n_results=1, include_paths=["knowledge/"], exclude_paths=[]
    )

    assert [r["source"] for r in result["results"]] == ["knowledge/voice.md"]
    assert result["coverage"]["selected_files"] == 1
    assert requested == [80]


@pytest.mark.parametrize("swap_parent", [False, True])
def test_symlink_swap_between_validation_and_read_is_rejected(
    corpus: Path, monkeypatch: pytest.MonkeyPatch, swap_parent: bool
) -> None:
    rag = VoiceRag(str(corpus), enabled=True)
    source = corpus / "scripts/voice/brief.py"
    safe_source = rag._safe_source

    def swap_after_check(path: Path, root: Path | None = None) -> bool:
        safe = safe_source(path, root)
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
    with rag._pinned_root() as root_fd:
        assert root_fd is not None
        assert rag._load_documents([source], root_fd) == []


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
@pytest.mark.parametrize(
    ("header", "query"),
    [
        # Substring of a longer identifier, not a token the ranker saw.
        ("# implementation_notes", "implementation brief"),
        # An English stop word: the ranker ignores it, so the snippet must too.
        ("# system", "system brief"),
    ],
)
async def test_snippet_anchor_matches_ranked_lexical_tokens(
    corpus: Path, header: str, query: str
) -> None:
    write(
        corpus,
        "scripts/voice/brief.py",
        f"{header}\n" + "# filler\n" * 100 + "brief delivery details\n",
    )
    result = await VoiceRag(str(corpus), enabled=True).search(
        query, include_paths=["scripts/"]
    )
    assert result["status"] == "ok"
    assert "brief delivery details" in result["results"][0]["snippet"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("lexical")
async def test_retargeted_workspace_root_cannot_redirect_reads(
    corpus: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = tmp_path_factory.mktemp("other")
    write(other, "scripts/voice/brief.py", "brief from an unsearched tree")
    link = tmp_path_factory.mktemp("links") / "active"
    link.symlink_to(corpus, target_is_directory=True)
    rag = VoiceRag(str(link), enabled=True)
    collect = rag._collect_filtered_files

    def retarget_after_selection(
        includes: tuple[str, ...], excludes: tuple[str, ...]
    ) -> list[Path]:
        paths = collect(includes, excludes)
        link.unlink()
        link.symlink_to(other, target_is_directory=True)
        return paths

    monkeypatch.setattr(rag, "_collect_filtered_files", retarget_after_selection)
    result = await rag.search("brief", include_paths=["scripts/"])
    assert link.resolve() == other.resolve()
    assert result["status"] == "ok"
    assert "standup-brief.json" in result["results"][0]["snippet"]
    assert "unsearched" not in result["results"][0]["snippet"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("lexical")
async def test_validation_uses_the_pinned_root_not_the_retargeted_one(
    corpus: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation must check the tree that is read, not a re-resolved root.

    Retargeting the workspace symlink after selection must not make validation
    inspect the new tree while the read still happens under the pinned one: if
    the new tree lacks the selected source, the current behaviour would reject
    a file it then reads from the pinned tree (or, worse, validate a different
    file than the one returned).
    """
    other = tmp_path_factory.mktemp("other")
    write(other, "unrelated/notes.md", "nothing here")
    link = tmp_path_factory.mktemp("links") / "active"
    link.symlink_to(corpus, target_is_directory=True)
    rag = VoiceRag(str(link), enabled=True)
    collect = rag._collect_filtered_files

    def retarget_after_selection(
        includes: tuple[str, ...], excludes: tuple[str, ...]
    ) -> list[Path]:
        paths = collect(includes, excludes)
        link.unlink()
        link.symlink_to(other, target_is_directory=True)
        return paths

    monkeypatch.setattr(rag, "_collect_filtered_files", retarget_after_selection)
    result = await rag.search("brief", include_paths=["scripts/"])
    assert link.resolve() == other.resolve()
    assert result["status"] == "ok"
    assert "standup-brief.json" in result["results"][0]["snippet"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("lexical")
async def test_journal_glob_does_not_spend_the_reservation_twice(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A glob that already collects the journals must not trim other sources.

    The reservation counted ``len(recent)`` before deduplication, so a
    configured glob covering ``journal/**/*.md`` reserved room for files it had
    already collected, dropping configured source files that still fit.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    for n in range(45):
        write(corpus, f"journal/{today}/entry_{n}.md", f"standup brief note {n}")
    monkeypatch.setenv(
        "GPTME_VOICE_RAG_SOURCE_PATHS",
        "journal/**/*.md:scripts/voice/*.py",
    )
    result = await VoiceRag(str(corpus), enabled=True).search(
        "generate_standup_brief", include_paths=["scripts/", "journal/"]
    )
    sources = [r["source"] for r in result["results"]]
    assert "scripts/voice/brief.py" in sources


@pytest.mark.asyncio
@pytest.mark.usefixtures("lexical")
async def test_bridge_logs_a_correlatable_search_receipt(
    corpus: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A call's lookup must be identifiable from the service log afterwards."""
    import logging

    from gptme_voice.realtime.tool_bridge import GptmeToolBridge

    bridge = GptmeToolBridge(
        workspace=str(corpus), rag=VoiceRag(str(corpus), enabled=True)
    )
    with caplog.at_level(logging.INFO, logger="gptme_voice.realtime.tool_bridge"):
        result = await bridge.handle_function_call(
            "workspace_search",
            {
                "query": "Delivered the release",
                "include_paths": ["state/standup-brief.json"],
                "exclude_paths": ["journal/"],
            },
        )

    assert result["status"] == "ok"
    receipts = [r.message for r in caplog.records if "workspace_search(" in r.message]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert "Delivered the release" in receipt
    assert "status=ok" in receipt
    assert "backend=lexical" in receipt
    assert "mode=filtered" in receipt
    assert "selected=1" in receipt
    assert "results=1" in receipt
    assert "elapsed=" in receipt
