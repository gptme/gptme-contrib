"""Tests for the live-voice gptme-rag tool.

The 2026-09-09 standup query is the contract: 'what has Bob been doing in
the last hour' must return a non-empty recent journal snippet well under
the 8s voice budget, without going through the subagent.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from gptme_voice.rag import (
    TOOL_NAME,
    VoiceRag,
    is_recency_query,
    rag_instruction_preamble,
    rag_tool_schema,
    topic_terms,
)

CALL_QUERY = "what have you been doing in the last hour"


def _write_journal(root: Path, rel: str, body: str, mtime: float | None = None) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_topic_terms_strips_standup_recency_phrasing() -> None:
    assert topic_terms(CALL_QUERY) == ""
    assert is_recency_query(CALL_QUERY)
    assert topic_terms("training run in the last hour") == "training run"


def test_rag_tool_schema_names_the_standup_query() -> None:
    schema = rag_tool_schema()
    assert schema["name"] == TOOL_NAME
    blob = (
        schema["description"].lower()
        + schema["parameters"]["properties"]["query"]["description"].lower()
    )
    assert "last hour" in blob
    assert "subagent" in blob
    assert "workspace_search" in rag_instruction_preamble()


@pytest.mark.asyncio
async def test_disabled_search_fails_closed() -> None:
    rag = VoiceRag(workspace="/tmp", enabled=False)
    result = await rag.search(CALL_QUERY)
    assert result == {"error": "workspace_search is not enabled on this server."}


@pytest.mark.asyncio
async def test_empty_query_rejected() -> None:
    rag = VoiceRag(workspace="/tmp", enabled=True, search_impl=lambda *_: [])
    assert await rag.search("   ") == {"error": "No query provided"}


@pytest.mark.asyncio
async def test_call_query_shape_returns_recent_answer_under_budget(
    tmp_path: Path,
) -> None:
    """Exact standup query: recent snippet in, stale snippet out, <8s."""
    now = time.time()
    today = datetime.fromtimestamp(now, tz=timezone.utc).date().isoformat()
    # Stale file lives in TODAY's directory but with an old mtime so the
    # mtime cutoff filter is actually exercised (the old test used a 40-day-old
    # directory which collect_recent_files never visits, giving false confidence).
    _write_journal(
        tmp_path,
        f"journal/{today}/autonomous-session-recent.md",
        "# Autonomous Session recent\n\nShipped the heartbeat widget ten minutes ago.\n",
        mtime=now - 600,
    )
    _write_journal(
        tmp_path,
        f"journal/{today}/autonomous-session-stale.md",
        "# Ancient work\n\nRewrote ABOUT.md in June.\n",
        mtime=now - 40 * 86400,
    )

    rag = VoiceRag(
        workspace=str(tmp_path),
        enabled=True,
        timeout_seconds=8.0,
    )
    started = time.perf_counter()
    result = await rag.search(CALL_QUERY, n_results=3)
    elapsed = time.perf_counter() - started

    assert elapsed < 8.0
    assert result["status"] == "ok"
    assert result["query"] == CALL_QUERY
    assert result["elapsed_ms"] < 8000
    snippets = " ".join(item["snippet"] for item in result["results"]).lower()
    sources = " ".join(item["source"] for item in result["results"])
    assert "heartbeat widget" in snippets
    assert "autonomous-session-recent.md" in sources
    assert "ABOUT.md" not in sources
    assert "rewrote about.md" not in snippets


def test_collect_recent_files_spans_full_recency_window(tmp_path: Path) -> None:
    """collect_recent_files must include files from all days inside the window.

    With recency_hours=48 the window covers three calendar days.  A file written
    two calendar days ago (but within 48h) must be returned; the previously
    hardcoded two-directory list silently dropped it.
    """
    from datetime import timedelta

    now = time.time()
    today = datetime.fromtimestamp(now, tz=timezone.utc).date()
    two_days_ago = (today - timedelta(days=2)).isoformat()

    # File written 47h ago — inside a 48h window but in a directory two days
    # before today, i.e. not covered by the old hardcoded [today, yesterday].
    _write_journal(
        tmp_path,
        f"journal/{two_days_ago}/old-session.md",
        "# Old Session\n\nDid some work 47 hours ago.\n",
        mtime=now - 47 * 3600,
    )

    rag = VoiceRag(workspace=str(tmp_path), enabled=True, recency_hours=48)
    found = rag.collect_recent_files(now=now)
    assert any(
        "old-session.md" in str(p) for p in found
    ), f"expected old-session.md in results for recency_hours=48, got: {found}"


@pytest.mark.asyncio
async def test_injected_backend_records_call_query_and_latency() -> None:
    seen: list[str] = []

    def _impl(query: str, n_results: int) -> list[dict]:
        seen.append(query)
        return [
            {
                "source": "journal/2026-09-09/autonomous-session-test.md",
                "mtime": "2026-09-09T08:00:00+00:00",
                "snippet": "Investigated the 30s fast-subagent timeout.",
            }
        ][:n_results]

    rag = VoiceRag(workspace=None, enabled=True, timeout_seconds=8.0, search_impl=_impl)
    result = await rag.search(CALL_QUERY)
    assert seen == [CALL_QUERY]
    assert result["status"] == "ok"
    assert result["elapsed_ms"] < 1000
    assert "30s fast-subagent timeout" in result["results"][0]["snippet"]


@pytest.mark.asyncio
async def test_timeout_is_enforced() -> None:
    def _slow(query: str, n_results: int) -> list[dict]:
        time.sleep(0.3)
        return []

    rag = VoiceRag(
        workspace=None, enabled=True, timeout_seconds=0.05, search_impl=_slow
    )
    result = await rag.search(CALL_QUERY)
    assert result["status"] == "timeout"
    assert "exceeded" in result["error"]


@pytest.mark.asyncio
async def test_handle_function_call_routes_workspace_search() -> None:
    from gptme_voice.realtime.tool_bridge import GptmeToolBridge

    rag = VoiceRag(
        workspace=None,
        enabled=True,
        search_impl=lambda query, n: [
            {"source": "journal/today.md", "mtime": None, "snippet": f"hit:{query}:{n}"}
        ],
    )
    bridge = GptmeToolBridge(workspace="/fake", rag=rag)
    result = await bridge.handle_function_call(
        "workspace_search", {"query": CALL_QUERY, "n_results": 3}
    )
    assert result["status"] == "ok"
    assert result["results"][0]["snippet"] == f"hit:{CALL_QUERY}:3"


@pytest.mark.asyncio
async def test_handle_function_call_without_rag_fails_cleanly() -> None:
    from gptme_voice.realtime.tool_bridge import GptmeToolBridge

    bridge = GptmeToolBridge(workspace="/fake")
    assert await bridge.handle_function_call(
        "workspace_search", {"query": CALL_QUERY}
    ) == {"error": "workspace_search is not enabled on this server."}


def test_session_config_advertises_workspace_search_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GPTME_VOICE_RAG", "1")
    monkeypatch.delenv("GPTME_VOICE_BODY_URL", raising=False)
    from gptme_voice.realtime.server import VoiceServer

    server = VoiceServer(workspace=str(tmp_path))
    cfg = server._build_session_config("You are Bob.", include_body_tools=False)
    names = [tool["name"] for tool in cfg.extra_tools]
    assert TOOL_NAME in names
    assert "workspace_search" in cfg.instructions
    assert (
        "last hour" in cfg.instructions.lower() or "recap" in cfg.instructions.lower()
    )


def test_session_config_omits_workspace_search_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GPTME_VOICE_RAG", "0")
    monkeypatch.delenv("GPTME_VOICE_BODY_URL", raising=False)
    from gptme_voice.realtime.server import VoiceServer

    server = VoiceServer(workspace=str(tmp_path))
    cfg = server._build_session_config("You are Bob.", include_body_tools=False)
    assert all(tool["name"] != TOOL_NAME for tool in cfg.extra_tools)
    assert "WORKSPACE SEARCH" not in cfg.instructions
