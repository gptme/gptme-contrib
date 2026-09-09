"""Tests for the live-voice gptme-rag tool.

The 2026-09-09 standup query is the contract: 'what has Bob been doing in
the last hour' must return a non-empty recent journal snippet well under
the 8s voice budget, without going through the subagent.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from gptme_voice.rag import (
    TOOL_NAME,
    VoiceRag,
    is_last_hour_query,
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


def test_collect_recent_files_spans_timezone_ahead_of_utc(tmp_path: Path) -> None:
    """A recent file in a local-date directory ahead of the server's UTC date.

    Journal dirs are named by the writer's local calendar date.  When the writer
    is ahead of the server's UTC clock (e.g. UTC+1) and writes shortly after
    local midnight, the file lands in a directory dated one day AHEAD of the
    UTC date.  collect_recent_files must scan those dirs and return the file.
    """
    from datetime import timedelta

    now = time.time()
    today = datetime.fromtimestamp(now, tz=timezone.utc).date()
    local_tomorrow = (today + timedelta(days=1)).isoformat()

    # Written 10 min ago (recent, inside the default 24h window) but sitting in
    # a directory dated one day ahead of the server's UTC date.
    _write_journal(
        tmp_path,
        f"journal/{local_tomorrow}/local-midnight-session.md",
        "# Local midnight\n\nShipped the heartbeat widget just now.\n",
        mtime=now - 600,
    )

    rag = VoiceRag(workspace=str(tmp_path), enabled=True)
    found = rag.collect_recent_files(now=now)
    assert any(
        "local-midnight-session.md" in str(p) for p in found
    ), f"expected local-midnight-session.md (in a dir ahead of UTC) in results, got: {found}"


@pytest.mark.asyncio
async def test_backend_is_recency_when_lexical_has_no_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Topic query with no lexical matches must not mislabel the backend.

    When the query has real topic terms but lexical search returns no hits, the
    results fall back to the recency-ordered corpus — the backend field must say
    'recency', not claim a lexical ranking that never happened.
    """
    now = time.time()
    today = datetime.fromtimestamp(now, tz=timezone.utc).date()
    _write_journal(
        tmp_path,
        f"journal/{today}/a-session.md",
        "# Work\n\nShipped the heartbeat widget an hour ago.\n",
        mtime=now - 3600,
    )

    rag = VoiceRag(
        workspace=str(tmp_path),
        enabled=True,
        search_impl=None,
    )

    # Force lexical to return no matches.  With relevance_floor=0.0 the real
    # TfidfIndex returns documents even for an unmatched term, so this empty
    # return is monkeypatched to exercise the no-match fallback branch.
    monkeypatch.setattr(rag, "_search_lexical", lambda *a, **k: [])

    # 'quasar' is a real topic term (survives stop-word stripping); with lexical
    # forced empty the recency order is returned and must be labelled 'recency'.
    result = await rag.search("quasar in the last hour", n_results=3)
    assert result["status"] == "ok"
    assert result["backend"] == "recency", (
        f"backend should be 'recency' when lexical has no matches, got "
        f"{result['backend']!r}"
    )
    assert any("a-session.md" in item["source"] for item in result["results"])


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
async def test_timeout_fires_promptly_even_when_search_executor_is_saturated() -> None:
    """asyncio.wait_for cannot cancel a running executor worker (finding: the
    orphaned search thread keeps executing after the caller times out), so
    search() runs on a small dedicated pool instead of the default
    process-wide executor. That bound must not delay the caller's own
    timeout: with more concurrent searches than pool workers, each search()
    still returns 'timeout' within its own budget rather than being
    serialized behind slots freed by the (still-running) earlier workers.
    """

    def _slow(query: str, n_results: int) -> list[dict]:
        time.sleep(0.5)
        return []

    rags = [
        VoiceRag(workspace=None, enabled=True, timeout_seconds=0.05, search_impl=_slow)
        for _ in range(8)  # more than the search executor's 4 workers
    ]
    started = time.perf_counter()
    results = await asyncio.gather(*(r.search(CALL_QUERY) for r in rags))
    elapsed = time.perf_counter() - started
    assert all(r["status"] == "timeout" for r in results)
    assert elapsed < 0.5, (
        f"timeouts took {elapsed:.2f}s — appear serialized behind the "
        "still-running orphaned workers instead of firing independently"
    )


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


def test_session_config_omits_workspace_search_when_rag_tools_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """include_rag_tools=False suppresses the tool even when GPTME_VOICE_RAG=1."""
    monkeypatch.setenv("GPTME_VOICE_RAG", "1")
    monkeypatch.delenv("GPTME_VOICE_BODY_URL", raising=False)
    from gptme_voice.realtime.server import VoiceServer

    server = VoiceServer(workspace=str(tmp_path))
    cfg = server._build_session_config(
        "You are Bob.", include_body_tools=False, include_rag_tools=False
    )
    assert all(tool["name"] != TOOL_NAME for tool in cfg.extra_tools)
    assert "WORKSPACE SEARCH" not in cfg.instructions


def test_rag_for_websocket_loopback_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loopback WebSocket clients get RAG access."""
    monkeypatch.setenv("GPTME_VOICE_RAG", "1")
    monkeypatch.delenv("GPTME_VOICE_BODY_URL", raising=False)
    monkeypatch.delenv("TWILIO_CALLER_ALLOWLIST", raising=False)
    from unittest.mock import MagicMock

    from gptme_voice.realtime.server import VoiceServer

    server = VoiceServer(workspace=str(tmp_path))
    ws = MagicMock()
    ws.client.host = "127.0.0.1"
    result = server._rag_for_websocket(ws, transport="local")
    assert result is not None
    assert result.enabled


def test_rag_for_websocket_non_loopback_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-loopback WebSocket clients are denied RAG access."""
    monkeypatch.setenv("GPTME_VOICE_RAG", "1")
    monkeypatch.delenv("GPTME_VOICE_BODY_URL", raising=False)
    monkeypatch.delenv("TWILIO_CALLER_ALLOWLIST", raising=False)
    from unittest.mock import MagicMock

    from gptme_voice.realtime.server import VoiceServer

    server = VoiceServer(workspace=str(tmp_path))
    ws = MagicMock()
    ws.client.host = "203.0.113.42"  # external IP
    result = server._rag_for_websocket(ws, transport="browser")
    assert result is None


def test_rag_for_websocket_twilio_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Twilio callers on TWILIO_CALLER_ALLOWLIST get RAG; others are denied."""
    monkeypatch.setenv("GPTME_VOICE_RAG", "1")
    monkeypatch.delenv("GPTME_VOICE_BODY_URL", raising=False)
    monkeypatch.setenv("TWILIO_CALLER_ALLOWLIST", "+46765784797")
    from unittest.mock import MagicMock

    from gptme_voice.realtime.server import VoiceServer

    server = VoiceServer(workspace=str(tmp_path))
    ws = MagicMock()

    allowed = server._rag_for_websocket(
        ws, transport="twilio", caller_id="+46765784797"
    )
    assert allowed is not None

    denied = server._rag_for_websocket(ws, transport="twilio", caller_id="+10000000000")
    assert denied is None


def test_is_last_hour_query_matches_only_sub_hour_phrases() -> None:
    """is_last_hour_query must not fire on broader recency phrases.

    Queries about "today", "this morning", "recently" use the 24h recency
    window and must NOT trigger the last-hour bucket sort — that would push
    morning or afternoon work out of the top results when more than n_results
    files were written in the most recent hour.
    """
    # Sub-hour — should match
    assert is_last_hour_query("what have you been doing in the last hour")
    assert is_last_hour_query("what happened in the past hour")
    assert is_last_hour_query("recap this hour")

    # Broader recency — must NOT match
    assert not is_last_hour_query("what did you work on today")
    assert not is_last_hour_query("what have you been doing this morning")
    assert not is_last_hour_query("what did you do this afternoon")
    assert not is_last_hour_query("what have you been working on recently")
    assert not is_last_hour_query("what did you do lately")

    # Pure topic — must not match
    assert not is_last_hour_query("training run status")


@pytest.mark.asyncio
async def test_recency_bucket_sort_not_applied_for_today_query(
    tmp_path: Path,
) -> None:
    """'What did you work on today?' must not bias results toward the last hour.

    If n_results or more files were written in the last hour, the recency-bucket
    sort would push morning work off the top-N page.  For a 'today' query the
    plain newest-first order should be returned without any sub-hour re-sorting.
    """
    now = time.time()
    today = datetime.fromtimestamp(now, tz=timezone.utc).date()

    # Morning file (written 6h ago — within the 24h window, outside the 1h window)
    _write_journal(
        tmp_path,
        f"journal/{today}/morning-session.md",
        "# Morning\n\nDeep-dive on the RAG pipeline — six hours of work.\n",
        mtime=now - 6 * 3600,
    )
    # Three recent files written in the last hour — more than n_results below.
    # If the recency-bucket sort incorrectly fires for a 'today' query, it would
    # re-rank these three ahead of the morning file, pushing it off the top-3 page.
    for i, offset in enumerate([600, 900, 1200]):
        _write_journal(
            tmp_path,
            f"journal/{today}/recent-session-{i}.md",
            f"# Recent {i}\n\nQuick CI fix {i}.\n",
            mtime=now - offset,
        )

    rag = VoiceRag(workspace=str(tmp_path), enabled=True, search_impl=None)
    # n_results=3: fewer than the 4 total files, so if the morning file is incorrectly
    # sorted to position 4 by the recency-bucket sort, it will be absent from results.
    result = await rag.search("what did you work on today", n_results=3)

    assert result["status"] == "ok"
    sources = [item["source"] for item in result["results"]]
    # The morning file must appear in the top-3 — plain newest-first keeps it there;
    # an erroneous recency-bucket sort on a 'today' query would drop it to position 4.
    assert any(
        "morning-session.md" in s for s in sources
    ), f"morning-session.md missing from 'today' query results; got {sources}"


def test_voice_rag_disabled_when_gptme_rag_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VoiceRag must set enabled=False at init when gptme-rag cannot be imported.

    An operator who sets GPTME_VOICE_RAG=1 on a server without gptme-rag
    installed should see workspace_search omitted from the session tool schema,
    not a non-functional tool that returns 'could not be read' on every call.
    """
    import builtins
    import sys

    real_import = builtins.__import__

    def _no_gptme_rag(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("gptme_rag"):
            raise ImportError(f"No module named '{name}' (test stub)")
        return real_import(name, *args, **kwargs)

    # Remove any cached gptme_rag modules so the import check actually fires.
    for key in list(sys.modules.keys()):
        if key.startswith("gptme_rag"):
            del sys.modules[key]

    monkeypatch.setattr(builtins, "__import__", _no_gptme_rag)

    rag = VoiceRag(workspace=None, enabled=True)
    assert not rag.enabled, (
        "VoiceRag.enabled must be False when gptme-rag is not installed and "
        "no search_impl override is provided"
    )

    # An explicit search_impl override bypasses the import check — the caller
    # has provided the search implementation themselves.
    rag_with_impl = VoiceRag(workspace=None, enabled=True, search_impl=lambda q, n: [])
    assert rag_with_impl.enabled, (
        "VoiceRag with an explicit search_impl must remain enabled even when "
        "gptme-rag is not installed"
    )
