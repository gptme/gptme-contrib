"""Tests for subagent tree resolution (read_session_tree)."""

from __future__ import annotations

import json
from pathlib import Path


from gptme_sessions.signals import extract_from_path
from gptme_sessions.transcript import (
    SessionTree,
    SubagentNode,
    read_session_tree,
    subagent_record_files,
)


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _cc_assistant_with_agent(tool_use_id: str, subagent_type: str = "Explore") -> dict:
    """An assistant CC record carrying one ``Agent`` tool use."""
    return {
        "type": "assistant",
        "timestamp": "2026-03-01T10:00:00.000Z",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Agent",
                    "input": {
                        "description": f"spawn {tool_use_id}",
                        "subagent_type": subagent_type,
                        "prompt": "do work",
                    },
                },
            ],
        },
    }


def _cc_write_record(tool_use_id: str, path: str) -> dict:
    """An assistant CC record with a plain ``Write`` tool use."""
    return {
        "type": "assistant",
        "timestamp": "2026-03-01T10:00:01.000Z",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Write",
                    "input": {"file_path": path, "content": "# x"},
                },
            ],
        },
    }


def _meta(tool_use_id: str, agent_type: str, spawn_depth: int, description: str) -> dict:
    return {
        "agentType": agent_type,
        "description": description,
        "toolUseId": tool_use_id,
        "spawnDepth": spawn_depth,
    }


def _build_session(tmp_path: Path) -> Path:
    """Build a CC session with one direct subagent and one nested subagent.

    Layout (Claude Code convention):
        sess-1.jsonl
        sess-1/subagents/agent-a1.jsonl (+ .meta.json)   -> spawned by tool_agent_1
        sess-1/subagents/agent-a2.jsonl (+ .meta.json)   -> spawned by a1 (tool_agent_2)
    """
    session = tmp_path / "sess-1.jsonl"
    parent_records = [
        _cc_assistant_with_agent("tool_agent_1"),
        _cc_write_record("tool_write_parent", "parent.py"),
    ]
    _write_jsonl(session, parent_records)

    sub = tmp_path / "sess-1" / "subagents"
    # a1: direct child, spawns a2 via its own Agent call.
    a1_records = [
        _cc_assistant_with_agent("tool_agent_2"),
        _cc_write_record("tool_write_a1", "a1.py"),
    ]
    _write_jsonl(sub / "agent-a1.jsonl", a1_records)
    _write_jsonl(sub / "agent-a1.meta.json", [_meta("tool_agent_1", "Explore", 1, "top")])

    # a2: nested child (depth 2), no further spawns.
    a2_records = [_cc_write_record("tool_write_a2", "a2.py")]
    _write_jsonl(sub / "agent-a2.jsonl", a2_records)
    _write_jsonl(sub / "agent-a2.meta.json", [_meta("tool_agent_2", "Explore", 2, "nested")])

    return session


def test_read_session_tree_resolves_nested_subagents(tmp_path: Path) -> None:
    session = _build_session(tmp_path)
    tree = read_session_tree(session)

    assert isinstance(tree, SessionTree)
    assert tree.parent.session_id == "sess-1"
    assert tree.total_subagents == 2

    assert len(tree.subagents) == 1
    a1 = tree.subagents[0]
    assert isinstance(a1, SubagentNode)
    assert a1.session_id == "agent-a1"
    assert a1.agent_type == "Explore"
    assert a1.spawn_depth == 1
    assert a1.tool_use_id == "tool_agent_1"

    assert len(a1.children) == 1
    a2 = a1.children[0]
    assert a2.session_id == "agent-a2"
    assert a2.spawn_depth == 2
    assert a2.tool_use_id == "tool_agent_2"
    assert a2.children == []


def test_flatten_records_includes_subagents(tmp_path: Path) -> None:
    session = _build_session(tmp_path)
    tree = read_session_tree(session)

    flat = tree.flatten_records()
    # parent (2) + a1 (2) + a2 (1) = 5 records
    assert len(flat) == 5
    # all child tool writes present
    write_ids = {
        b.get("id")
        for r in flat
        if r.get("type") == "assistant"
        for b in r.get("message", {}).get("content", [])
        if isinstance(b, dict) and b.get("type") == "tool_use"
    }
    assert {"tool_write_parent", "tool_write_a1", "tool_write_a2"} <= write_ids


def test_flatten_messages_is_superset_of_parent(tmp_path: Path) -> None:
    session = _build_session(tmp_path)
    tree = read_session_tree(session)
    assert len(tree.flatten_messages()) > len(tree.parent.messages)


def test_no_subagents_is_byte_identical(tmp_path: Path) -> None:
    session = tmp_path / "solo.jsonl"
    records = [_cc_write_record("tool_write_1", "solo.py")]
    _write_jsonl(session, records)

    tree = read_session_tree(session)
    assert tree.total_subagents == 0
    assert tree.flatten_records() == tree.parent_records
    assert tree.flatten_messages() == tree.parent.messages
    assert subagent_record_files(session) == []


def test_self_loop_does_not_recurse_infinitely(tmp_path: Path) -> None:
    """A subagent whose own jsonl carries its own spawn tool-use must not loop."""
    session = tmp_path / "loop.jsonl"
    _write_jsonl(session, [_cc_assistant_with_agent("tool_loop")])

    sub = tmp_path / "loop" / "subagents"
    # The child's records re-include the parent's Agent call as context, so its
    # own toolUseId matches itself.
    child_records = [_cc_assistant_with_agent("tool_loop")]
    _write_jsonl(sub / "agent-loop.jsonl", child_records)
    _write_jsonl(
        sub / "agent-loop.meta.json",
        [_meta("tool_loop", "Explore", 1, "self-loop")],
    )

    tree = read_session_tree(session)
    # The child resolves once, but its self-reference is skipped.
    assert tree.total_subagents == 1
    assert tree.subagents[0].children == []


def test_extract_from_path_counts_subagent_tool_calls(tmp_path: Path) -> None:
    session = _build_session(tmp_path)
    result = extract_from_path(session)
    # Without subagent routing, only the parent's Write would count; with it,
    # a1/a2's writes are included too.
    tool_calls = result.get("tool_calls", {})
    assert tool_calls.get("Write") == 3  # parent + a1 + a2


def test_extract_from_path_deduplicates_echoed_parent_tool_call(tmp_path: Path) -> None:
    session = tmp_path / "echo.jsonl"
    spawn = _cc_assistant_with_agent("tool_agent_echo")
    _write_jsonl(session, [spawn])

    sub = tmp_path / "echo" / "subagents"
    _write_jsonl(
        sub / "agent-echo.jsonl",
        [spawn, _cc_write_record("tool_write_child", "child.py")],
    )
    _write_jsonl(
        sub / "agent-echo.meta.json",
        [_meta("tool_agent_echo", "Explore", 1, "echo")],
    )

    result = extract_from_path(session)
    assert result["tool_calls"]["Agent"] == 1
    assert result["tool_calls"]["Write"] == 1
    assert result["steps"] == 2


def _cc_usage_record(input_tokens: int, output_tokens: int) -> dict:
    """A complete durable CC assistant turn with token usage."""
    return {
        "type": "assistant",
        "timestamp": "2026-03-01T10:00:02.000Z",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        },
    }


def test_extract_from_path_adds_subagent_usage_to_parent_result(tmp_path: Path) -> None:
    """A parent's stream result must not replace a durable child's usage."""
    session = tmp_path / "usage.jsonl"
    _write_jsonl(
        session,
        [
            _cc_usage_record(1, 1),
            {
                "type": "result",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            _cc_assistant_with_agent("tool_agent_usage"),
        ],
    )
    sub = tmp_path / "usage" / "subagents"
    _write_jsonl(sub / "agent-usage.jsonl", [_cc_usage_record(20, 10)])
    _write_jsonl(
        sub / "agent-usage.meta.json",
        [_meta("tool_agent_usage", "Explore", 1, "usage")],
    )

    usage = extract_from_path(session)["usage"]
    assert usage["input_tokens"] == 120
    assert usage["output_tokens"] == 60


def test_read_session_tree_skips_meta_for_missing_jsonl(tmp_path: Path) -> None:
    session = tmp_path / "missing.jsonl"
    _write_jsonl(session, [_cc_assistant_with_agent("tool_missing")])
    sub = tmp_path / "missing" / "subagents"
    _write_jsonl(
        sub / "agent-missing.meta.json",
        [_meta("tool_missing", "Explore", 1, "missing")],
    )

    tree = read_session_tree(session)
    assert tree.total_subagents == 0


def test_read_session_tree_includes_flat_jsonl_without_meta(tmp_path: Path) -> None:
    session = tmp_path / "unmapped.jsonl"
    _write_jsonl(session, [_cc_write_record("tool_parent", "parent.py")])
    sub = tmp_path / "unmapped" / "subagents"
    _write_jsonl(sub / "agent-unmapped.jsonl", [_cc_write_record("tool_child", "child.py")])

    tree = read_session_tree(session)
    assert tree.total_subagents == 1
    assert tree.subagents[0].session_id == "agent-unmapped"
    assert tree.subagents[0].tool_use_id is None
