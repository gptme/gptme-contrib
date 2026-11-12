from __future__ import annotations

from gptme_sessions.session_identity import (
    SessionIndex,
    canonical_session_key,
    id_format,
)

UUID = "7d2d5a9b-d8d1-4d02-9c6a-b926ad461947"


def test_id_format():
    assert id_format(UUID) == "uuid"
    assert id_format(UUID.upper()) == "uuid"
    assert id_format("937ce54a") == "hex8"
    assert id_format("4033") == "label4"
    assert id_format("bob-x") == "other"
    assert id_format(None) == "missing"


def test_canonical_key_qualifies_labels_with_date():
    row = {"session_id": "4033", "start_time": "2026-03-12T12:52:32+00:00"}
    assert canonical_session_key(row) == "4033@2026-03-12"
    # falls back to timestamp, and passes unique ids through lower-cased
    assert (
        canonical_session_key({"session_id": "4033", "timestamp": "2026-03-11T23:59:00Z"})
        == "4033@2026-03-11"
    )
    assert canonical_session_key({"session_id": UUID.upper()}) == UUID


def test_unique_id_resolves_without_time():
    row = {"session_id": "937ce54a", "timestamp": "2026-04-13T19:03:11+00:00"}
    idx = SessionIndex([row])
    assert idx.resolve("937CE54A") is row
    # a merge days later still names the same session
    assert idx.resolve("937ce54a", at="2026-05-01T00:00:00Z") is row


def test_recurring_label_needs_time_and_picks_nearest_in_window():
    old = {"session_id": "4033", "start_time": "2026-02-01T10:00:00+00:00"}
    new = {"session_id": "4033", "start_time": "2026-03-12T12:52:00+00:00"}
    idx = SessionIndex([old, new])
    assert idx.resolve("4033") is None
    assert idx.stats["ambiguous_no_time"] == 1
    assert idx.resolve("4033", at="2026-03-12T13:30:00Z") is new
    assert idx.resolve("4033", at="2026-02-01T11:00:00Z") is old
    # an event far from any run does not get a guessed session
    assert idx.resolve("4033", at="2026-02-20T00:00:00Z") is None
    assert idx.stats["out_of_window"] == 1


def test_equidistant_sessions_stay_unresolved_regardless_of_order():
    earlier = {"session_id": "4033", "start_time": "2026-03-12T10:00:00Z"}
    later = {"session_id": "4033", "start_time": "2026-03-12T12:00:00Z"}
    for rows in ([earlier, later], [later, earlier]):
        idx = SessionIndex(rows)
        assert idx.resolve("4033", at="2026-03-12T11:00:00Z") is None
        assert idx.stats["ambiguous_time"] == 1


def test_single_label_is_still_time_checked():
    row = {"session_id": "4033", "start_time": "2026-02-01T10:00:00+00:00"}
    idx = SessionIndex([row])
    assert idx.resolve("4033", at="2026-03-12T00:00:00Z") is None


def test_alias_fields_resolve_to_the_record():
    row = {
        "session_id": UUID,
        "session_label": "d829",
        "cc_session_uuid": "2e88335f-0652-4a7a-9e5d-5085ac2620f5",
        "start_time": "2026-03-12T11:00:00+00:00",
    }
    idx = SessionIndex([row])
    assert idx.resolve("2e88335f-0652-4a7a-9e5d-5085ac2620f5") is row
    assert idx.resolve("d829", at="2026-03-12T12:00:00Z") is row
    assert idx.stats["alias:label4"] == 1


def test_owner_fields_are_not_aliases():
    worker = {"session_id": UUID, "trajectory_owner_session_id": "aaaabbbb"}
    idx = SessionIndex([worker])
    assert idx.resolve("aaaabbbb") is None
    assert idx.stats["unresolved:hex8"] == 1


def test_out_of_window_id_falls_through_to_in_window_alias():
    old = {"session_id": "4033", "start_time": "2026-02-01T10:00:00+00:00"}
    new = {
        "session_id": "abcd1234",
        "session_label": "4033",
        "start_time": "2026-03-12T12:00:00+00:00",
    }
    idx = SessionIndex([old, new])
    assert idx.resolve("4033", at="2026-03-12T13:00:00Z") is new


def test_dispatch_identity_hoists_nested_fields_and_joins_decision():
    from gptme_sessions.session_identity import (
        dispatch_identity,
        load_decisions_by_session,
    )

    sid = "8727b520-4afa-48a3-8e41-37230792bb71"
    decisions = load_decisions_by_session(
        [{"session_id": sid.upper(), "execution_id": "t1"}, "junk", {}]
    )
    row = {
        "session_id": sid,
        "reasoning_effort": "high",
        "cascade_intent": {
            "task_id": "t1",
            "pool": "frontier",
            "cascade_lane": "open",
            "experiment_cell": "reserve",
        },
    }
    assert dispatch_identity(row, decisions) == {
        "session_key": sid,
        "decision_id": sid,
        "task_id": "t1",
        "pool": "frontier",
        "lane": "open",
        "effort": "high",
        "experiment_cell": "reserve",
        "dispatch_kind": "selector",
    }


def test_dispatch_identity_event_dispatch_has_no_decision():
    from gptme_sessions.session_identity import dispatch_identity

    row = {
        "session_id": "54252a01-9949-46de-abbf-147b8cf9ede8",
        "trigger": "dispatch",
        "dispatch_cause": {"event_type": "completion_followup", "task": None},
    }
    ident = dispatch_identity(row, {})
    assert ident["decision_id"] is None
    assert ident["task_id"] is None
    assert ident["pool"] is None
    assert ident["dispatch_kind"] == "completion_followup"


def test_lone_label_without_time_stays_unresolved():
    # one occurrence in the loaded window does not make a 4-hex label unique
    # across history, so a time-less lookup must not attribute it
    row = {"session_id": "4033", "start_time": "2026-02-01T10:00:00+00:00"}
    idx = SessionIndex([row])
    assert idx.resolve("4033") is None
    assert idx.stats["ambiguous_no_time"] == 1
    assert idx.resolve("4033", at="2026-02-01T11:00:00Z") is row
