"""Launch-cause inference: neutral ``AGENT_*`` names with ``BOB_*`` aliases."""

import json
from pathlib import Path

import pytest

from gptme_sessions.dispatch_cause import (
    append_start,
    infer_cause,
    last_release,
    main,
    session_trigger,
)


#: Dispatcher variables ``infer_cause`` reads; cleared when a test must not
#: inherit the test runner's own launch provenance.
_PRODUCER_VARS = (
    "AGENT_DISPATCH_KIND",
    "BOB_DISPATCH_KIND",
    "AGENT_DISPATCH_ID",
    "BOB_DISPATCH_ID",
    "AGENT_DISPATCH_CAUSE",
    "BOB_DISPATCH_CAUSE",
    "CASCADE_INTENT",
    "CASCADE_COORDINATION_CLAIMED",
    "CASCADE_COORDINATION_KEY",
    "CASCADE_COORDINATION_SOURCE",
    "CASCADE_SELECTED_ID",
    "EVENT_ID",
    "EVENT_URL",
    "EVENT_TRIGGER_TYPE",
    "PM_DISPATCH_ID",
    "PM_WORK_FILE",
    "INVOCATION_ID",
)


def test_worker_kind_wins_over_inherited_cascade_state():
    cause = infer_cause(
        {
            "AGENT_DISPATCH_KIND": "worker",
            "AGENT_DISPATCH_ID": "worker-7",
            "CASCADE_COORDINATION_CLAIMED": "1",
            "CASCADE_COORDINATION_KEY": "cascade:task:some-task",
            "CASCADE_SELECTED_ID": "some-task",
        }
    )
    # A worker is a new launch; its spawner's selection must not become its task.
    assert cause["kind"] == "worker"
    assert cause["id"] == "worker-7"
    assert cause["task"] is None


@pytest.mark.parametrize("prefix", ["AGENT", "BOB"])
@pytest.mark.parametrize("kind", ["worker", "fanout", "fanout-btb"])
@pytest.mark.parametrize(
    "inherited",
    [
        {"EVENT_ID": "parent-event", "EVENT_URL": "https://github.com/gptme/gptme/pull/42"},
        {"PM_DISPATCH_ID": "parent-slot"},
        {"CASCADE_INTENT": '{"task_id": "selected-task"}'},
    ],
    ids=["event", "pm-slot", "task"],
)
def test_explicit_spawn_wins_over_parent_dispatch(prefix, kind, inherited):
    cause = infer_cause(
        {
            **inherited,
            f"{prefix}_DISPATCH_KIND": kind,
            f"{prefix}_DISPATCH_ID": "child-1",
            f"{prefix}_PARENT_SESSION_ID": "parent-1",
        }
    )
    assert cause["kind"] == kind
    assert cause["id"] == "child-1"
    assert cause["parent_session_id"] == "parent-1"
    assert cause["pr"] is None
    assert "event_type" not in cause
    assert session_trigger(cause) == "spawn"
    # Fanout deliberately carries its selected task; workers must not borrow it.
    expected_task = "selected-task" if kind != "worker" and "CASCADE_INTENT" in inherited else None
    assert cause["task"] == expected_task


def test_legacy_bob_names_still_infer():
    cause = infer_cause({"BOB_DISPATCH_KIND": "fanout", "BOB_DISPATCH_ID": "fanout-3"})
    assert cause["kind"] == "fanout"
    assert cause["id"] == "fanout-3"
    assert session_trigger(cause) == "spawn"


def test_neutral_name_wins_when_both_spellings_are_set():
    cause = infer_cause(
        {
            "AGENT_DISPATCH_KIND": "fanout-btb",
            "AGENT_DISPATCH_ID": "neutral-1",
            "BOB_DISPATCH_KIND": "worker",
            "BOB_DISPATCH_ID": "legacy-1",
        }
    )
    assert cause["id"] == "neutral-1"
    assert cause["kind"] == "fanout-btb"


def test_parent_session_id_reads_neutral_then_legacy():
    neutral = infer_cause({"AGENT_PARENT_SESSION_ID": "parent-a"})
    assert neutral["parent_session_id"] == "parent-a"
    legacy = infer_cause({"BOB_PARENT_SESSION_ID": "parent-b"})
    assert legacy["parent_session_id"] == "parent-b"


def test_event_cause_carries_pr_and_type():
    cause = infer_cause(
        {
            "EVENT_ID": "evt-9",
            "EVENT_URL": "https://github.com/gptme/gptme/pull/42",
            "EVENT_TRIGGER_TYPE": "review",
        }
    )
    assert cause == {
        "kind": "event",
        "id": "evt-9",
        "parent_session_id": None,
        "task": None,
        "pr": "gptme/gptme#42",
        "event_type": "review",
    }
    assert session_trigger(cause) == "dispatch"


def test_pm_slot_never_credits_one_pr_of_several(tmp_path: Path):
    work_file = tmp_path / "work.jsonl"
    work_file.write_text(
        "\n".join(
            json.dumps(item)
            for item in (
                {"repo": "gptme/gptme", "number": 1, "types": ["pr_update"]},
                {"repo": "gptme/gptme", "number": 2, "types": ["pr_update"]},
            )
        )
    )
    cause = infer_cause({"PM_DISPATCH_ID": "slot-4", "PM_WORK_FILE": str(work_file)})
    assert cause["kind"] == "pm-slot"
    assert cause["pr"] is None


def test_pm_slot_single_pr_is_credited(tmp_path: Path):
    work_file = tmp_path / "work.jsonl"
    work_file.write_text(json.dumps({"repo": "Aw/aw-core", "number": 7, "types": ["merge_ready"]}))
    cause = infer_cause({"PM_DISPATCH_ID": "slot-5", "PM_WORK_FILE": str(work_file)})
    assert cause["pr"] == "Aw/aw-core#7"


def test_agent_msg_reply_is_its_own_kind(tmp_path: Path):
    work_file = tmp_path / "work.jsonl"
    work_file.write_text(json.dumps({"type": "agent_msg_reply"}))
    cause = infer_cause({"PM_DISPATCH_ID": "slot-6", "PM_WORK_FILE": str(work_file)})
    assert cause["kind"] == "agent-msg"


def test_cascade_task_cause_uses_coordination_key():
    cause = infer_cause(
        {
            "CASCADE_COORDINATION_CLAIMED": "1",
            "CASCADE_COORDINATION_KEY": "cascade:task:pick-me",
            "CASCADE_SELECTED_ID": "pick-me",
        }
    )
    assert cause["kind"] == "cascade-task"
    assert cause["id"] == "cascade:task:pick-me"
    assert cause["task"] == "pick-me"
    assert session_trigger(cause) == "dispatch"


def test_systemd_invocation_is_not_a_timer():
    cause = infer_cause({"INVOCATION_ID": "inv-1"})
    assert cause["kind"] == "systemd"
    # An invocation ID proves a service ran, not that a timer fired it.
    assert session_trigger(cause) == "unknown"


def test_unknown_when_nothing_is_set():
    cause = infer_cause({})
    assert cause["kind"] == "unknown"
    assert cause["id"] is None
    assert cause["task"] is None
    assert session_trigger(cause) == "unknown"


def test_systemd_trigger_only_for_explicit_timer_kind():
    assert session_trigger({"kind": "timer"}) == "timer"


def test_last_release_returns_most_recent_row(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "wait-resolutions.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {"task_id": "other", "ts": "2026-01-01T00:00:00Z"},
                {"task_id": "t1", "ts": "2026-01-02T00:00:00Z", "evidence_type": "a"},
                {"task_id": "t1", "ts": "2026-01-03T00:00:00Z", "evidence_type": "b"},
            )
        )
    )
    release = last_release(tmp_path, "t1")
    assert release == {
        "id": "wait:t1:2026-01-03T00:00:00Z",
        "ts": "2026-01-03T00:00:00Z",
        "kind": "b",
    }
    assert last_release(tmp_path, "missing") is None


def test_last_release_tolerates_missing_ledger(tmp_path: Path):
    assert last_release(tmp_path, "t1") is None


def test_append_start_writes_ledger_row(tmp_path: Path):
    cause = {"kind": "worker", "id": "w1"}
    append_start(tmp_path, {"AGENT_SESSION_ID": "sess-9"}, cause)
    rows = [
        json.loads(line)
        for line in (tmp_path / "state/sessions/dispatch-starts.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["session_id"] == "sess-9"
    assert rows[0]["dispatch_cause"] == cause
    assert rows[0]["timestamp"]


def test_append_start_empty_receipt_selects_no_session(tmp_path: Path):
    append_start(
        tmp_path,
        {"AGENT_SESSION_ID": "inherited", "AGENT_RECEIPT_SESSION_ID": ""},
        {"kind": "unknown"},
    )
    row = json.loads((tmp_path / "state/sessions/dispatch-starts.jsonl").read_text().strip())
    # An explicit empty receipt must not borrow another backend's native ID.
    assert row["session_id"] is None


def test_cli_refreshes_inherited_cause(tmp_path: Path, capsys):
    monkeypatch_env = {
        "AGENT_DISPATCH_CAUSE": '{"kind": "stale-parent"}',
        "AGENT_DISPATCH_KIND": "fanout",
        "AGENT_DISPATCH_ID": "fresh",
        "EVENT_ID": "parent-event",
        "PM_DISPATCH_ID": "parent-slot",
        "CASCADE_INTENT": '{"task_id": "selected-task"}',
    }
    with pytest.MonkeyPatch.context() as mp:
        # Inference reads the whole environment, so the runner's own dispatcher
        # variables must not leak into the assertion.
        for key in _PRODUCER_VARS:
            mp.delenv(key, raising=False)
        for key, value in monkeypatch_env.items():
            mp.setenv(key, value)
        assert main(["--workspace", str(tmp_path)]) == 0
        inherited = json.loads(capsys.readouterr().out)
        assert inherited == {"kind": "stale-parent"}

        assert main(["--workspace", str(tmp_path), "--refresh"]) == 0
        refreshed = json.loads(capsys.readouterr().out)
        assert refreshed["kind"] == "fanout"
        assert refreshed["id"] == "fresh"
        assert refreshed["task"] == "selected-task"
        assert main(["--workspace", str(tmp_path), "--refresh", "--trigger"]) == 0
        assert capsys.readouterr().out.strip() == "spawn"


def test_cascade_task_with_pr_coordination_key_uses_task_id():
    """When a task is inferred from CASCADE_INTENT but the coordination key is a PR key,
    the cause ID must be the task id, not the unrelated PR coordination key."""
    cause = infer_cause(
        {
            "CASCADE_INTENT": '{"task_id": "t1"}',
            "CASCADE_COORDINATION_CLAIMED": "1",
            "CASCADE_COORDINATION_SOURCE": "pr",
            "CASCADE_COORDINATION_KEY": "cascade:pr:repo:42",
        }
    )
    assert cause["kind"] == "cascade-task"
    assert cause["task"] == "t1"
    # The PR coordination key must not bleed into the task cause ID.
    assert cause["id"] == "t1"


def test_pm_ci_failure_slot_is_credited_to_pr(tmp_path: Path):
    """PM CI failure items use type=ci_failure (not ci_failure_pr); the PR must be credited."""
    work_file = tmp_path / "work.jsonl"
    work_file.write_text(json.dumps({"repo": "gptme/gptme", "number": 99, "type": "ci_failure"}))
    cause = infer_cause({"PM_DISPATCH_ID": "slot-ci", "PM_WORK_FILE": str(work_file)})
    assert cause["kind"] == "pm-slot"
    assert cause["pr"] == "gptme/gptme#99"


def test_cli_trigger_prints_classification(tmp_path: Path, capsys):
    with pytest.MonkeyPatch.context() as mp:
        for key in _PRODUCER_VARS:
            mp.delenv(key, raising=False)
        mp.setenv("EVENT_ID", "evt-1")
        assert main(["--workspace", str(tmp_path), "--trigger"]) == 0
        assert capsys.readouterr().out.strip() == "dispatch"
