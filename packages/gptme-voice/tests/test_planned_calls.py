"""Prepared call availability and explicit, call-bound fulfillment."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from gptme_voice.realtime.planned_calls import (
    fulfill_planned_call,
    is_planned_call_fulfilled,
    load_pending_planned_calls,
    register_planned_call,
)
from gptme_voice.realtime.tool_bridge import GptmeToolBridge

NOW = datetime(2026, 11, 25, 8, tzinfo=timezone.utc)
PHONE = "+15551212"
PURPOSE = "Daily standup"
CONFIRMATION = "That covers the daily standup."


def register(tmp_path, plan_id="standup-one", **changes):
    values = {
        "workspace": str(tmp_path),
        "plan_id": plan_id,
        "caller": PHONE,
        "scheduled_at": (NOW + timedelta(hours=1)).isoformat(),
        "purpose": PURPOSE,
        "context": {
            "generated_at": (NOW - timedelta(minutes=30)).isoformat(),
            "text": "The release shipped. Decide the next customer experiment.",
            "provenance": "agentic",
        },
    }
    values.update(changes)
    return register_planned_call(**values)


def fulfill(tmp_path, plan, **changes):
    values = {
        "workspace": str(tmp_path),
        "offered_plans": [plan],
        "caller": PHONE,
        "call_sid": "CA" + "a" * 32,
        "transcript": [
            SimpleNamespace(
                role="assistant",
                text="The release shipped. Does that cover the daily standup, so the later call is unnecessary?",
            ),
            SimpleNamespace(role="user", text=CONFIRMATION),
        ],
        "plan_id": plan["plan_id"],
        "caller_confirmation": CONFIRMATION,
        "summary": "Delivered outcomes and discussed the next customer experiment.",
        "now": NOW,
    }
    values.update(changes)
    return fulfill_planned_call(**values)


def test_available_brief_is_immutable_and_unfulfilled_until_explicit_receipt(tmp_path):
    plan = register(tmp_path)
    retry = register(
        tmp_path, context={"generated_at": NOW.isoformat(), "text": "Replacement"}
    )
    assert retry == plan
    assert load_pending_planned_calls(str(tmp_path), PHONE, now=NOW) == [plan]
    assert not is_planned_call_fulfilled(str(tmp_path), plan["plan_id"])
    result = fulfill(tmp_path, plan)
    assert result["status"] == "fulfilled"
    assert is_planned_call_fulfilled(str(tmp_path), plan["plan_id"])
    assert load_pending_planned_calls(str(tmp_path), PHONE, now=NOW) == []


def test_reprocessing_is_idempotent_and_preserves_original_receipt(tmp_path):
    plan = register(tmp_path)
    assert fulfill(tmp_path, plan)["status"] == "fulfilled"
    path = tmp_path / "state/voice-calls/fulfilled/standup-one.json"
    first = path.read_bytes()
    assert (
        fulfill(tmp_path, plan, now=NOW + timedelta(minutes=1))["status"] == "fulfilled"
    )
    assert path.read_bytes() == first


def test_fulfills_only_selected_plan(tmp_path):
    first, second = register(tmp_path), register(tmp_path, "standup-two")
    assert fulfill(tmp_path, first)["status"] == "fulfilled"
    assert load_pending_planned_calls(str(tmp_path), PHONE, now=NOW) == [second]


@pytest.mark.parametrize(
    "changes",
    [
        {"offered_plans": []},
        {"plan_id": "unoffered"},
        {"caller": "+19999999"},
        {"call_sid": ""},
        {"transcript": []},
        {"transcript": [SimpleNamespace(role="user", text=CONFIRMATION)]},
        {"summary": ""},
        {
            "transcript": [
                SimpleNamespace(
                    role="assistant",
                    text="Does that cover the daily standup, so the later call is unnecessary?",
                ),
                SimpleNamespace(role="user", text=CONFIRMATION),
                SimpleNamespace(
                    role="user", text="Actually, please keep the later call."
                ),
            ]
        },
        {"caller_confirmation": "Yes"},
        {"caller_confirmation": "I don't think that covers the daily standup."},
        {"caller_confirmation": "That covers only part of the daily standup."},
        {"caller_confirmation": "That covers the daily standup but call me later."},
        {"caller_confirmation": "That covers the weather."},
    ],
)
def test_uncovered_partial_wrong_identity_or_unconfirmed_stays_scheduled(
    tmp_path, changes
):
    plan = register(tmp_path)
    assert "error" in fulfill(tmp_path, plan, **changes)
    assert not is_planned_call_fulfilled(str(tmp_path), plan["plan_id"])
    assert load_pending_planned_calls(str(tmp_path), PHONE, now=NOW) == [plan]


@pytest.mark.parametrize(
    "changes",
    [
        {"scheduled_at": (NOW - timedelta(minutes=1)).isoformat()},
        {"scheduled_at": (NOW + timedelta(hours=25)).isoformat()},
        {
            "context": {
                "generated_at": (NOW - timedelta(hours=25)).isoformat(),
                "text": "Stale",
            }
        },
        {
            "context": {
                "generated_at": (NOW + timedelta(minutes=1)).isoformat(),
                "text": "Future",
            }
        },
    ],
)
def test_stale_future_or_expired_plans_are_not_offered(tmp_path, changes):
    register(tmp_path, **changes)
    assert load_pending_planned_calls(str(tmp_path), PHONE, now=NOW) == []


def test_missing_corrupt_or_other_caller_plan_is_not_offered(tmp_path):
    assert load_pending_planned_calls(str(tmp_path), PHONE, now=NOW) == []
    register(tmp_path)
    assert load_pending_planned_calls(str(tmp_path), "+19999999", now=NOW) == []
    (tmp_path / "state/voice-calls/planned/standup-one.json").write_text("{")
    assert load_pending_planned_calls(str(tmp_path), PHONE, now=NOW) == []


@pytest.mark.parametrize("plan_id", ["../escape", "foo/bar", "", ".", "x" * 129])
def test_plan_id_cannot_escape_storage(tmp_path, plan_id):
    with pytest.raises(ValueError, match="invalid plan_id"):
        register(tmp_path, plan_id)


def test_plan_id_cannot_be_reused_for_another_purpose(tmp_path):
    register(tmp_path)
    with pytest.raises(ValueError, match="different scheduled purpose"):
        register(tmp_path, purpose="Security review")


def test_invalid_context_and_naive_timestamps_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="timezone"):
        register(tmp_path, scheduled_at="2026-11-25T09:00:00")
    with pytest.raises(ValueError, match="text"):
        register(tmp_path, context={"generated_at": NOW.isoformat(), "text": ""})
    with pytest.raises(ValueError, match="size"):
        register(
            tmp_path, context={"generated_at": NOW.isoformat(), "text": "x" * 16001}
        )


def test_corrupt_receipt_does_not_suppress_schedule(tmp_path):
    register(tmp_path)
    path = tmp_path / "state/voice-calls/fulfilled/standup-one.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"plan_id": "standup-one", "fulfilled_at": NOW.isoformat()})
    )
    assert not is_planned_call_fulfilled(str(tmp_path), "standup-one")


@pytest.mark.asyncio
async def test_tool_requires_authenticated_callback():
    bridge = GptmeToolBridge()
    assert "error" in await bridge.handle_function_call("fulfill_planned_call", {})
    bridge.on_fulfill_planned_call = lambda args: {"status": "fulfilled", **args}
    assert await bridge.handle_function_call(
        "fulfill_planned_call", {"plan_id": "one"}
    ) == {
        "status": "fulfilled",
        "plan_id": "one",
    }


def test_atomic_publication_leaves_no_partial_record_on_failure(tmp_path, monkeypatch):
    import gptme_voice.realtime.planned_calls as module

    def fail(_source, _target):
        raise OSError("publication failed")

    monkeypatch.setattr(module.os, "link", fail)
    with pytest.raises(OSError, match="publication failed"):
        register(tmp_path)
    assert not list((tmp_path / "state/voice-calls/planned").iterdir())


def test_plans_sort_by_instant_across_timezone_offsets(tmp_path):
    first = register(tmp_path, "first", scheduled_at="2026-11-25T10:00:00+01:00")
    second = register(tmp_path, "second", scheduled_at="2026-11-25T09:30:00+00:00")
    assert load_pending_planned_calls(str(tmp_path), PHONE, now=NOW) == [first, second]


def test_coverage_without_permission_to_skip_the_later_call_stays_pending(tmp_path):
    plan = register(tmp_path)
    transcript = [
        SimpleNamespace(role="assistant", text="Does that cover the daily standup?"),
        SimpleNamespace(role="user", text=CONFIRMATION),
    ]
    assert "error" in fulfill(tmp_path, plan, transcript=transcript)
    assert not is_planned_call_fulfilled(str(tmp_path), plan["plan_id"])


def test_caller_can_explicitly_confirm_no_later_call_is_needed(tmp_path):
    plan = register(tmp_path)
    quote = "That covers the daily standup, no need for the later call."
    transcript = [
        SimpleNamespace(
            role="assistant", text="The release shipped and we agreed next steps."
        ),
        SimpleNamespace(role="user", text=quote),
    ]
    assert (
        fulfill(tmp_path, plan, transcript=transcript, caller_confirmation=quote)[
            "status"
        ]
        == "fulfilled"
    )


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Hello Erik, ready?", False),
        ("Does that cover the daily standup?", False),
        ("Should I cancel the later call?", False),
        ("Does that cover the daily standup, so I can skip the later call?", True),
        (
            "Does that cover the daily standup, so I should not cancel the later call?",
            False,
        ),
    ],
)
def test_natural_yes_only_answers_explicit_purpose_and_skip_confirmation(
    tmp_path, question, expected
):
    plan = register(tmp_path)
    transcript = [
        SimpleNamespace(role="assistant", text=question),
        SimpleNamespace(role="user", text="Yes."),
    ]
    result = fulfill(tmp_path, plan, transcript=transcript, caller_confirmation="Yes.")
    assert (result.get("status") == "fulfilled") is expected


def test_yes_after_intervening_user_turn_does_not_answer_an_old_question(tmp_path):
    plan = register(tmp_path)
    transcript = [
        SimpleNamespace(
            role="assistant",
            text="Does that cover the daily standup so I can skip the later call?",
        ),
        SimpleNamespace(role="user", text="Wait, what did you say?"),
        SimpleNamespace(role="user", text="Yes."),
    ]
    assert "error" in fulfill(
        tmp_path, plan, transcript=transcript, caller_confirmation="Yes."
    )
