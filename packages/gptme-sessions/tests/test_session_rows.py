from __future__ import annotations

import json

import pytest

from gptme_sessions.session_rows import (
    dedupe_monitoring_pairs,
    load_session_rows,
    read_jsonl_rows,
)


def _pm_pair(controller_id: str, worker_id: str, tokens: int = 1000) -> list[dict]:
    return [
        {
            "session_id": controller_id,
            "execution_role": "controller",
            "child_session_ids": [worker_id],
            "token_count": tokens,
            "dispatch_cause": {"id": "pm-slot-gptme-gptme-4856"},
        },
        {
            "session_id": worker_id,
            "execution_role": "worker",
            "parent_session_id": controller_id,
            "token_count": tokens,
            "cost_usd": 0.3,
        },
    ]


def test_keeps_controller_by_default():
    rows = [{"session_id": "auto1", "token_count": 5}, *_pm_pair("c1", "w1")]
    out = dedupe_monitoring_pairs(rows)
    assert [r["session_id"] for r in out] == ["auto1", "c1"]
    assert sum(r["token_count"] for r in out) == 1005


def test_keep_worker():
    out = dedupe_monitoring_pairs(_pm_pair("c1", "w1"), keep="worker")
    assert [r["session_id"] for r in out] == ["w1"]
    assert out[0]["cost_usd"] == 0.3


def test_keeps_unpaired_rows():
    orphan_worker = {"session_id": "w9", "execution_role": "worker"}
    childless = {
        "session_id": "c9",
        "execution_role": "controller",
        "child_session_ids": [],
    }
    lone = _pm_pair("c2", "w2")[0]
    assert dedupe_monitoring_pairs([orphan_worker, childless]) == [
        orphan_worker,
        childless,
    ]
    # a controller whose worker rotated out of the window stays under keep=worker
    assert dedupe_monitoring_pairs([lone], keep="worker") == [lone]


def test_collapses_roleless_child():
    controller, _ = _pm_pair("c1", "w1")
    roleless = {"session_id": "w1", "run_type": "interactive", "token_count": 1000}
    assert [r["session_id"] for r in dedupe_monitoring_pairs([controller, roleless])] == ["c1"]
    assert [
        r["session_id"] for r in dedupe_monitoring_pairs([controller, roleless], keep="worker")
    ] == ["w1"]


def test_rejects_bad_keep():
    with pytest.raises(ValueError):
        dedupe_monitoring_pairs([], keep="both")


def test_load_session_rows_one_row_per_session(tmp_path):
    p = tmp_path / "session-records.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in _pm_pair("c1", "w1")) + "\n")
    assert [r["session_id"] for r in load_session_rows(p)] == ["c1"]


def test_load_session_rows_missing_file(tmp_path):
    assert load_session_rows(tmp_path / "nope.jsonl") == []


def test_read_jsonl_rows_skips_bad_lines(tmp_path):
    p = tmp_path / "r.jsonl"
    p.write_text('{"session_id": "a"}\n\nnot json\n[1, 2]\n{"session_id": "b"}\n')
    assert [r["session_id"] for r in read_jsonl_rows(p)] == ["a", "b"]
