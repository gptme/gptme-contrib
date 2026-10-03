"""Native gptme children are siblings joined by explicit parent metadata."""

import json
from pathlib import Path

from gptme_sessions.signals import extract_from_path
from gptme_sessions.transcript import read_session_tree, subagent_record_files


def write_log(path: Path, command: str = "cat README.md") -> Path:
    path.mkdir(parents=True)
    records = [
        {"role": "user", "content": "Inspect the project", "timestamp": "2026-10-01T12:00:00Z"},
        {
            "role": "assistant",
            "content": f"```shell\n{command}\n```",
            "timestamp": "2026-10-01T12:00:01Z",
            "metadata": {"model": "child-model", "usage": {"input_tokens": 10, "output_tokens": 5}},
        },
        {"role": "system", "content": "Ran command; result", "timestamp": "2026-10-01T12:00:05Z"},
    ]
    file = path / "conversation.jsonl"
    file.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return file


def child(root: Path, name: str, parent: Path, command: str = "cat README.md") -> Path:
    file = write_log(root / f"subagent-{name}", command)
    (file.parent / "subagent-meta.json").write_text(
        json.dumps(
            {
                "agent_id": name,
                "parent_logdir": str(parent),
                "prompt": "Inspect",
                "profile": "explorer",
            }
        )
    )
    return file


def test_native_sibling_tree_and_signals(tmp_path: Path) -> None:
    parent = write_log(tmp_path / "run-parent")
    records = [json.loads(line) for line in parent.read_text().splitlines()]
    records[1]["metadata"]["model"] = "parent-model"
    parent.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    a = child(tmp_path, "a", parent.parent)
    b = child(tmp_path, "b", parent.parent, "git commit -m change")
    nested = child(tmp_path, "nested", a.parent)
    unrelated = child(tmp_path, "unrelated", tmp_path / "other-parent")
    # Retained incomplete child still counts, even with no final answer/tools.
    failed = child(tmp_path, "failed", parent.parent)
    failed.write_text(json.dumps({"role": "user", "content": "Never completed"}) + "\n")
    tree = read_session_tree(parent)
    assert {n.session_id for n in tree.subagents} == {"a", "b", "failed"}
    assert tree.subagents[0].agent_type == "explorer"
    assert tree.subagents[0].children[0].session_id == "nested"
    assert tree.subagents[0].children[0].spawn_depth == 2
    assert set(subagent_record_files(parent)) == {a, b, nested, failed}
    assert unrelated not in subagent_record_files(parent)
    result = extract_from_path(parent)
    summary = result["subagent_summary"]
    assert summary["subagents_total"] == 4
    assert summary["spawns_total"] == 3
    assert summary["subagents_depth_max"] == 2
    assert summary["subagent_tokens_total"] == 45
    assert summary["subagent_seconds_total"] == 15
    assert summary["subagents_acting"] == 1
    assert result["usage"]["total_tokens"] == 60
    assert result["usage"]["model"] == "parent-model"
    assert summary["subagent_tool_output_bytes"] > 0
    assert summary["subagent_children"][0]["label"] == "readonly"


def test_native_requires_valid_exact_parent_metadata(tmp_path: Path) -> None:
    parent = write_log(tmp_path / "run-parent")
    same_basename = tmp_path / "other" / "run-parent"
    child(tmp_path, "wrong-parent", same_basename)
    no_meta = write_log(tmp_path / "subagent-no-meta")
    bad = write_log(tmp_path / "subagent-bad-meta")
    (bad.parent / "subagent-meta.json").write_text("[]")
    malformed = write_log(tmp_path / "subagent-malformed")
    (malformed.parent / "subagent-meta.json").write_text("{")
    assert subagent_record_files(parent) == []
    assert read_session_tree(parent).subagents == []
    assert no_meta.exists()


def test_native_cycle_and_directory_entrypoint(tmp_path: Path) -> None:
    parent = write_log(tmp_path / "subagent-parent")
    a = child(tmp_path, "a", parent.parent)
    (parent.parent / "subagent-meta.json").write_text(
        json.dumps(
            {
                "agent_id": "parent",
                "parent_logdir": str(a.parent),
            }
        )
    )
    assert subagent_record_files(parent.parent) == [a]
    tree = read_session_tree(parent.parent)
    assert len(tree.subagents) == 1
    assert tree.subagents[0].children == []


def test_native_string_tool_calls_classified(tmp_path: Path) -> None:
    parent = write_log(tmp_path / "run-parent")
    a = child(tmp_path, "native-tool", parent.parent)
    records = [json.loads(line) for line in a.read_text().splitlines()]
    records[1]["content"] = '@shell(call-1): {"command": "git commit -m change"}'
    a.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    result = extract_from_path(parent)
    assert result["subagent_summary"]["subagents_acting"] == 1
    assert result["tool_calls"]["shell"] == 1
