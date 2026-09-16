"""Tests for subagent_summary: classifier, tree aggregation, extract_from_path."""

from __future__ import annotations

import json
from pathlib import Path

from gptme_sessions.signals import extract_from_path
from gptme_sessions.subagent_summary import (
    ChildSpec,
    classify_agent,
    cmd_mutation,
    empty_summary,
    infer_session_kind,
    max_concurrency,
    summarize_subagents,
)
from gptme_sessions.transcript import read_session_tree


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _cc_assistant(
    ts: str,
    *,
    tool_name: str | None = None,
    tool_id: str = "tool-1",
    tool_input: dict | None = None,
    text: str | None = None,
    usage: dict | None = None,
    entrypoint: str | None = None,
) -> dict:
    content: list[dict] = []
    if text is not None:
        content.append({"type": "text", "text": text})
    if tool_name is not None:
        content.append(
            {
                "type": "tool_use",
                "id": tool_id,
                "name": tool_name,
                "input": tool_input or {},
            }
        )
    record: dict = {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": content,
        },
    }
    if usage:
        record["message"]["usage"] = usage
    if entrypoint:
        record["entrypoint"] = entrypoint
    return record


def _cc_user(ts: str, text: str, *, entrypoint: str | None = None) -> dict:
    record: dict = {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    if entrypoint:
        record["entrypoint"] = entrypoint
    return record


def _cc_tool_result(ts: str, tool_id: str, content: str) -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content}],
        },
    }


def _meta(tool_use_id: str, agent_type: str, spawn_depth: int, description: str) -> dict:
    return {
        "agentType": agent_type,
        "description": description,
        "toolUseId": tool_use_id,
        "spawnDepth": spawn_depth,
    }


def _notif(task_id: str, tool_use_id: str, result: str = "done") -> str:
    return (
        "<task-notification>"
        f"<task-id>{task_id}</task-id>"
        f"<tool-use-id>{tool_use_id}</tool-use-id>"
        "<summary>Agent Explore</summary>"
        f"<result>{result}</result>"
        "<duration_ms>1000</duration_ms>"
        "</task-notification>"
    )


# ---------------------------------------------------------------------------
# classifier
# ---------------------------------------------------------------------------


def test_cmd_mutation_strong_verbs_are_acting() -> None:
    label, _ = cmd_mutation("git-safe-commit --scope-only file.py -m 'x'")
    assert label == "acting"
    label, _ = cmd_mutation("gh pr create --title x")
    assert label == "acting"


def test_cmd_mutation_quoted_verbs_are_not_acting() -> None:
    label, _ = cmd_mutation("grep 'git-safe-commit' scripts/foo.py")
    assert label is None
    label, _ = cmd_mutation("git merge-base HEAD origin/master")
    assert label is None


def test_cmd_mutation_scratch_vs_repo_redirects() -> None:
    label, _ = cmd_mutation("echo hi > /tmp/out.txt")
    assert label == "scratch"
    label, _ = cmd_mutation("echo hi > journal/foo.md")
    assert label == "acting"
    label, _ = cmd_mutation("echo hi > so.md", cwd_scratch=True)
    assert label == "scratch"


def test_cmd_mutation_home_dir_is_user_agnostic() -> None:
    label, _ = cmd_mutation("echo hi > /home/alice/repo/out.md")
    assert label == "acting"
    label, _ = cmd_mutation("echo hi > /home/alice/.cache/out.md")
    assert label == "scratch"
    label, _ = cmd_mutation("python3 scripts/memory/agent-remember.py save x")
    assert label == "acting"


def test_cmd_mutation_get_api_is_not_acting() -> None:
    label, _ = cmd_mutation("gh api -X GET -f q=foo repos/x")
    assert label is None


def test_classify_agent_readonly_explore() -> None:
    label, _ = classify_agent({"Read": 3, "Grep": 2}, ["git log --oneline", "gh pr view 1"], [])
    assert label == "readonly"


def test_classify_agent_scratch_write() -> None:
    label, _ = classify_agent({"Write": 1}, [], ["/tmp/scratch/out.md"])
    assert label == "scratch"


def test_classify_agent_acting_write() -> None:
    label, _ = classify_agent({"Write": 1}, [], ["packages/gptme-sessions/src/x.py"])
    assert label == "acting"


def test_classify_agent_sticky_cwd_scratch_then_relative_redirect() -> None:
    label, _ = classify_agent(
        {"Bash": 2},
        ["cd /tmp && pwd", "echo hi > so.md"],
        [],
        sticky_cwd=True,
    )
    assert label == "scratch"


def test_max_concurrency_overlap() -> None:
    assert max_concurrency([]) == 0
    assert max_concurrency([(0.0, 10.0), (20.0, 30.0)]) == 1
    assert max_concurrency([(0.0, 10.0), (5.0, 15.0)]) == 2


def test_infer_session_kind_sentinel_and_cli() -> None:
    assert (
        infer_session_kind(
            harness="claude-code",
            entrypoint="cli",
            first_prompt="hello",
        )
        == "interactive"
    )
    assert (
        infer_session_kind(
            harness="claude-code",
            entrypoint="sdk-cli",
            first_prompt="BOB_SESSION_SENTINEL=abc autonomous work session",
        )
        == "autonomous"
    )
    assert (
        infer_session_kind(
            harness="codex",
            entrypoint="",
            first_prompt="",
            originator="codex-tui",
        )
        == "interactive"
    )


# ---------------------------------------------------------------------------
# extract_from_path / tree aggregation
# ---------------------------------------------------------------------------


def test_extract_from_path_nested_count_depth_and_usage(tmp_path: Path) -> None:
    session = tmp_path / "sess-1.jsonl"
    _write_jsonl(
        session,
        [
            _cc_user("2026-03-01T10:00:00.000Z", "do work", entrypoint="sdk-cli"),
            _cc_assistant(
                "2026-03-01T10:00:01.000Z",
                tool_name="Agent",
                tool_id="tool_agent_1",
                tool_input={"description": "top", "subagent_type": "Explore", "prompt": "x"},
                usage={"input_tokens": 1, "output_tokens": 1},
            ),
        ],
    )
    sub = tmp_path / "sess-1" / "subagents"
    _write_jsonl(
        sub / "agent-a1.jsonl",
        [
            _cc_assistant(
                "2026-03-01T10:00:02.000Z",
                tool_name="Agent",
                tool_id="tool_agent_2",
                tool_input={"description": "nested", "subagent_type": "Explore", "prompt": "y"},
                usage={"input_tokens": 20, "output_tokens": 10},
            ),
            _cc_assistant(
                "2026-03-01T10:00:08.000Z",
                tool_name="Write",
                tool_id="tool_write_a1",
                tool_input={"file_path": "a1.py", "content": "# x"},
            ),
        ],
    )
    _write_jsonl(sub / "agent-a1.meta.json", [_meta("tool_agent_1", "Explore", 1, "top")])
    _write_jsonl(
        sub / "agent-a2.jsonl",
        [
            _cc_assistant(
                "2026-03-01T10:00:03.000Z",
                tool_name="Read",
                tool_id="tool_read_a2",
                tool_input={"file_path": "README.md"},
                usage={"input_tokens": 5, "output_tokens": 2},
            ),
        ],
    )
    _write_jsonl(sub / "agent-a2.meta.json", [_meta("tool_agent_2", "Explore", 2, "nested")])

    result = extract_from_path(session)
    summary = result["subagent_summary"]
    assert summary["subagents_total"] == 2
    assert summary["subagents_depth_max"] == 2
    assert summary["subagents_acting"] == 1  # a1 wrote a1.py
    assert summary["subagents_readonly"] == 1  # a2 only Read
    assert summary["subagents_scratch"] == 0
    assert summary["subagent_tokens_total"] == 37  # 30 + 7
    assert summary["spawns_total"] == 1
    assert summary["session_kind"] == "oneshot"


def test_extract_from_path_no_subagents_is_zeroed(tmp_path: Path) -> None:
    session = tmp_path / "solo.jsonl"
    _write_jsonl(
        session,
        [
            _cc_user("2026-03-01T10:00:00.000Z", "hello", entrypoint="cli"),
            _cc_assistant(
                "2026-03-01T10:00:01.000Z",
                tool_name="Read",
                tool_id="r1",
                tool_input={"file_path": "x.py"},
            ),
        ],
    )
    summary = extract_from_path(session)["subagent_summary"]
    expected = empty_summary()
    expected["session_kind"] = "interactive"
    expected["active_seconds"] = 1
    assert summary["subagents_total"] == 0
    assert summary["subagents_depth_max"] == 0
    assert summary["subagents_max_concurrent"] == 0
    assert summary["session_kind"] == "interactive"
    assert set(summary) == set(expected)


def test_concurrency_and_parent_kept_working(tmp_path: Path) -> None:
    session = tmp_path / "par.jsonl"
    parent = [
        _cc_user("2026-03-01T10:00:00.000Z", "parallel please", entrypoint="cli"),
        _cc_assistant(
            "2026-03-01T10:00:01.000Z",
            tool_name="Agent",
            tool_id="tool_a",
            tool_input={"description": "a", "subagent_type": "Explore", "prompt": "a"},
        ),
        _cc_assistant(
            "2026-03-01T10:00:02.000Z",
            tool_name="Agent",
            tool_id="tool_b",
            tool_input={"description": "b", "subagent_type": "Explore", "prompt": "b"},
        ),
        # non-spawn parent turn after both children have started
        _cc_assistant(
            "2026-03-01T10:00:08.000Z",
            tool_name="Read",
            tool_id="tool_read",
            tool_input={"file_path": "notes.md"},
        ),
        _cc_user("2026-03-01T10:00:12.000Z", _notif("a", "tool_a", "alpha-result")),
        _cc_user("2026-03-01T10:00:16.000Z", _notif("b", "tool_b", "beta-result")),
    ]
    _write_jsonl(session, parent)
    sub = tmp_path / "par" / "subagents"
    _write_jsonl(
        sub / "agent-a.jsonl",
        [
            _cc_assistant(
                "2026-03-01T10:00:03.000Z",
                tool_name="Grep",
                tool_id="g1",
                tool_input={"pattern": "x"},
            ),
            _cc_tool_result("2026-03-01T10:00:04.000Z", "g1", "x" * 50),
            _cc_assistant("2026-03-01T10:00:11.000Z", text="done a"),
        ],
    )
    _write_jsonl(sub / "agent-a.meta.json", [_meta("tool_a", "Explore", 1, "a")])
    _write_jsonl(
        sub / "agent-b.jsonl",
        [
            _cc_assistant(
                "2026-03-01T10:00:06.000Z",
                tool_name="Grep",
                tool_id="g2",
                tool_input={"pattern": "y"},
            ),
            _cc_tool_result("2026-03-01T10:00:07.000Z", "g2", "y" * 20),
            _cc_assistant("2026-03-01T10:00:15.000Z", text="done b"),
        ],
    )
    _write_jsonl(sub / "agent-b.meta.json", [_meta("tool_b", "Explore", 1, "b")])

    summary = extract_from_path(session)["subagent_summary"]
    assert summary["subagents_total"] == 2
    assert summary["subagents_max_concurrent"] == 2
    assert summary["subagents_readonly"] == 2
    assert summary["spawns_total"] == 2
    assert summary["spawns_parent_kept_working"] == 2
    assert summary["subagent_tool_output_bytes"] == 70
    assert summary["subagent_report_bytes"] > 0
    assert summary["subagent_seconds_total"] >= 8
    assert summary["session_kind"] == "interactive"
    assert summary["parent_idle_max_seconds"] >= 0


def test_summarize_subagents_direct_without_tree() -> None:
    parent = [
        _cc_user("2026-03-01T10:00:00.000Z", "BOB_SESSION_SENTINEL=deadbeef"),
        _cc_assistant(
            "2026-03-01T10:00:01.000Z",
            tool_name="Agent",
            tool_id="t1",
            tool_input={"subagent_type": "general-purpose", "prompt": "x"},
            entrypoint="sdk-cli",
        ),
    ]
    child = [
        _cc_assistant(
            "2026-03-01T10:00:02.000Z",
            tool_name="Write",
            tool_id="w1",
            tool_input={"file_path": "/tmp/out.md", "content": "x"},
            usage={"input_tokens": 4, "output_tokens": 1},
        )
    ]
    summary = summarize_subagents(
        parent,
        [ChildSpec(records=child, spawn_depth=1, session_id="agent-x", tool_use_id="t1")],
        harness="claude-code",
    )
    assert summary["subagents_total"] == 1
    assert summary["subagents_scratch"] == 1
    assert summary["subagents_acting"] == 0
    assert summary["subagent_tokens_total"] == 5
    assert summary["session_kind"] == "autonomous"


def test_active_seconds_caps_long_gaps() -> None:
    parent = [
        _cc_user("2026-03-01T10:00:00.000Z", "hello", entrypoint="cli"),
        _cc_assistant("2026-03-01T10:00:10.000Z", text="ok"),
        # 2-hour resume gap should cap at 15 minutes
        _cc_assistant("2026-03-01T12:00:10.000Z", text="back"),
    ]
    summary = summarize_subagents(parent, [], harness="claude-code")
    assert summary["active_seconds"] == 10 + 15 * 60
    assert summary["session_kind"] == "interactive"


def test_read_session_tree_summary_matches_extract(tmp_path: Path) -> None:
    session = tmp_path / "match.jsonl"
    _write_jsonl(
        session,
        [
            _cc_user("2026-03-01T10:00:00.000Z", "x"),
            _cc_assistant(
                "2026-03-01T10:00:01.000Z",
                tool_name="Agent",
                tool_id="tool_z",
                tool_input={"description": "z", "subagent_type": "Explore", "prompt": "z"},
            ),
        ],
    )
    sub = tmp_path / "match" / "subagents"
    _write_jsonl(
        sub / "agent-z.jsonl",
        [_cc_assistant("2026-03-01T10:00:02.000Z", tool_name="Read", tool_id="r", tool_input={})],
    )
    _write_jsonl(sub / "agent-z.meta.json", [_meta("tool_z", "Explore", 1, "z")])
    tree = read_session_tree(session)
    via_extract = extract_from_path(session)["subagent_summary"]
    from gptme_sessions.subagent_summary import summarize_session_tree

    via_tree = summarize_session_tree(tree)
    assert via_extract["subagents_total"] == via_tree["subagents_total"] == 1
    assert via_extract["subagents_readonly"] == 1


def test_max_concurrency_single_record_is_one() -> None:
    # A child with only one timestamp (start == end) must count as 1, not 0.
    assert max_concurrency([(100.0, 100.0)]) == 1
    assert max_concurrency([(100.0, 100.0), (100.0, 100.0)]) == 2


def test_scan_gptme_child_writes_are_seen() -> None:
    from gptme_sessions.subagent_summary import _scan_gptme

    records = [
        {"role": "user", "content": "do the thing", "timestamp": "2026-03-01T10:00:00Z"},
        {
            "role": "assistant",
            "content": [
                {"type": "code", "lang": "save", "content": "/home/bob/repo/out.md\ncontent here"},
                {
                    "type": "code",
                    "lang": "bash",
                    "content": "curl https://example.com > /dev/null",
                },
            ],
            "timestamp": "2026-03-01T10:00:01Z",
        },
        {
            "role": "system",
            "content": [{"type": "console", "content": "ok" * 20}],
            "timestamp": "2026-03-01T10:00:02Z",
        },
    ]
    scan = _scan_gptme(records)
    assert scan.write_paths == ["/home/bob/repo/out.md"]
    assert scan.result_bytes == 40
    assert any("curl" in cmd for cmd in scan.bash_cmds)


def test_summarize_subagents_gptme_child_not_readonly() -> None:
    parent = [
        {"role": "user", "content": "spawn", "timestamp": "2026-03-01T10:00:00Z"},
        {
            "role": "assistant",
            "content": [{"type": "code", "lang": "bash", "content": "gptme child prompt"}],
            "timestamp": "2026-03-01T10:00:01Z",
        },
    ]
    child = [
        {
            "role": "assistant",
            "content": [
                {"type": "code", "lang": "save", "content": "/home/bob/repo/file.py\nprint('hi')"}
            ],
            "timestamp": "2026-03-01T10:00:02Z",
        }
    ]
    summary = summarize_subagents(
        parent,
        [ChildSpec(records=child, spawn_depth=1, session_id="agent-x")],
        harness="gptme",
    )
    assert summary["subagents_total"] == 1
    assert summary["subagents_acting"] == 1
    assert summary["subagents_readonly"] == 0
