"""Tests for gptme-lessons MCP server memory tools."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gptme-lessons-mcp.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("gptme_lessons_mcp", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _load_module()


def _run(coro):
    return asyncio.run(coro)


def _content(response) -> str:
    if isinstance(response, tuple):
        content_list = response[0]
    else:
        content_list = response.content

    parts = []
    for item in content_list:
        if hasattr(item, "text"):
            parts.append(item.text)
        elif isinstance(item, dict) and "text" in item:
            parts.append(item["text"])
    return "\n".join(parts)


def _find_agent_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "scripts" / "memory" / "memory_retrieval.py").exists():
            return candidate
    raise AssertionError(
        "Could not locate agent root with scripts/memory/memory_retrieval.py"
    )


def _write_memory_fixture(tmp_path: Path) -> tuple[Path, Path]:
    agent_root = tmp_path / "agent"
    memory_dir = agent_root / "memory"
    retrieval_dir = agent_root / "scripts" / "memory"
    state_dir = agent_root / "state" / "cc-memory"
    memory_dir.mkdir(parents=True)
    retrieval_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)

    source_retrieval = _find_agent_root() / "scripts" / "memory" / "memory_retrieval.py"
    shutil.copy2(source_retrieval, retrieval_dir / "memory_retrieval.py")

    (memory_dir / "feedback_greptile_review_loop.md").write_text(
        "---\n"
        "name: greptile_review_loop\n"
        "description: Must complete full Greptile review cycle before self-merging\n"
        "type: feedback\n"
        "aliases:\n"
        "  - greptile review loop\n"
        "---\n\n"
        "Never self-merge until Greptile has reviewed the final diff.\n",
        encoding="utf-8",
    )
    (memory_dir / "reference_release_checklist.md").write_text(
        "---\n"
        "name: release_checklist\n"
        "description: Lightweight release checklist\n"
        "type: reference\n"
        "aliases:\n"
        "  - release checklist\n"
        "---\n\n"
        "Run tests, check docs, then ship.\n",
        encoding="utf-8",
    )
    state_file = state_dir / "metadata.json"
    state_file.write_text(
        json.dumps(
            {
                "feedback_greptile_review_loop.md": {
                    "confidence": 0.96,
                    "last_verified": "2026-05-11T00:00:00+00:00",
                },
                "reference_release_checklist.md": {
                    "confidence": 0.65,
                    "last_verified": "2026-04-01T00:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    return memory_dir, state_file


def test_tools_registered_with_memory_support(tmp_path):
    memory_dir, state_file = _write_memory_fixture(tmp_path)
    mcp = MOD.build_server([], memory_dir=memory_dir, memory_state_file=state_file)

    async def _check():
        tools = await mcp.list_tools()
        return {tool.name for tool in tools}

    names = _run(_check())
    assert "memory_search" in names
    assert "memory_get" in names


def test_memory_search_returns_ranked_match(tmp_path):
    memory_dir, state_file = _write_memory_fixture(tmp_path)
    mcp = MOD.build_server([], memory_dir=memory_dir, memory_state_file=state_file)

    async def _check():
        response = await mcp.call_tool(
            "memory_search",
            {"query": "greptile review loop", "limit": 3},
        )
        return _content(response)

    text = _run(_check())
    assert "feedback_greptile_review_loop.md" in text
    assert "score=" in text
    assert (
        "Match: greptile, loop, review" in text
        or "Match: greptile, review, loop" in text
    )
    assert "Never self-merge until Greptile has reviewed the final diff." in text


def test_memory_get_accepts_stem_and_returns_body(tmp_path):
    memory_dir, state_file = _write_memory_fixture(tmp_path)
    mcp = MOD.build_server([], memory_dir=memory_dir, memory_state_file=state_file)

    async def _check():
        response = await mcp.call_tool(
            "memory_get",
            {"name": "feedback_greptile_review_loop"},
        )
        return _content(response)

    text = _run(_check())
    assert text.startswith("# greptile_review_loop")
    assert "File: feedback_greptile_review_loop.md" in text
    assert "Never self-merge until Greptile has reviewed the final diff." in text
    assert "name: greptile_review_loop" not in text


def test_memory_get_accepts_alias_lookup(tmp_path):
    memory_dir, state_file = _write_memory_fixture(tmp_path)
    mcp = MOD.build_server([], memory_dir=memory_dir, memory_state_file=state_file)

    async def _check():
        response = await mcp.call_tool(
            "memory_get",
            {"name": "greptile review loop"},
        )
        return _content(response)

    text = _run(_check())
    assert "File: feedback_greptile_review_loop.md" in text
    assert "Aliases: greptile review loop" in text
