"""Tests for the general missed-call context persistence mechanism."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from gptme_voice.realtime.missed_call_context import (
    load_callback_brief,
    load_callback_candidate,
    write_missed_call_context,
)

PHONE = "+15551212"
CALL_SID = "CA" + "c" * 32
MARKER = "Context prepared for this call: the feature is ready to ship."


@pytest.fixture
def workspace(tmp_path):
    """Minimal workspace with a fresh missed-call context note."""
    now = datetime.now(timezone.utc)
    voice = tmp_path / "state" / "voice-calls"
    voice.mkdir(parents=True)
    note = {
        "type": "general",
        "sid": CALL_SID,
        "date": now.date().isoformat(),
        "placed_at": (now - timedelta(minutes=5)).isoformat(),
        "caller": PHONE,
        "context": {
            "generated_at": (now - timedelta(minutes=30)).isoformat(),
            "text": MARKER,
        },
    }
    (voice / "missed-call-context.json").write_text(json.dumps(note))
    return tmp_path, now, note, voice


# ---------------------------------------------------------------------------
# write_missed_call_context
# ---------------------------------------------------------------------------


def test_write_creates_note(tmp_path):
    now = datetime.now(timezone.utc)
    write_missed_call_context(
        str(tmp_path),
        sid=CALL_SID,
        caller=PHONE,
        context={"generated_at": now.isoformat(), "text": MARKER},
    )
    note_path = tmp_path / "state" / "voice-calls" / "missed-call-context.json"
    assert note_path.exists()
    note = json.loads(note_path.read_text())
    assert note["sid"] == CALL_SID
    assert note["caller"] == PHONE
    assert note["context"]["text"] == MARKER
    assert note["type"] == "general"


def test_write_includes_context_file_when_given(tmp_path):
    now = datetime.now(timezone.utc)
    write_missed_call_context(
        str(tmp_path),
        type="standup",
        sid=CALL_SID,
        caller=PHONE,
        context={"generated_at": now.isoformat(), "text": MARKER},
        context_file="state/standup-brief.json",
    )
    note = json.loads(
        (tmp_path / "state" / "voice-calls" / "missed-call-context.json").read_text()
    )
    assert note["type"] == "standup"
    assert note["context_file"] == "state/standup-brief.json"


def test_write_idempotent(tmp_path):
    now = datetime.now(timezone.utc)
    write_missed_call_context(
        str(tmp_path),
        sid=CALL_SID,
        caller=PHONE,
        context={"generated_at": now.isoformat(), "text": "first"},
    )
    write_missed_call_context(
        str(tmp_path),
        sid=CALL_SID,
        caller=PHONE,
        context={"generated_at": now.isoformat(), "text": "second"},
    )
    note = json.loads(
        (tmp_path / "state" / "voice-calls" / "missed-call-context.json").read_text()
    )
    assert note["context"]["text"] == "second"


def test_write_none_workspace_is_noop():
    write_missed_call_context(
        None, sid=CALL_SID, caller=PHONE, context={"text": MARKER}
    )


# ---------------------------------------------------------------------------
# load_callback_candidate — new format
# ---------------------------------------------------------------------------


def test_candidate_reads_new_format(workspace):
    ws, _, _, _ = workspace
    result = load_callback_candidate(str(ws))
    assert result is not None
    sid, payload, placed_at = result
    assert sid == CALL_SID
    data = json.loads(payload)
    assert data["text"] == MARKER


def test_candidate_none_for_missing_workspace():
    assert load_callback_candidate(None) is None


def test_candidate_none_when_file_absent(tmp_path):
    (tmp_path / "state" / "voice-calls").mkdir(parents=True)
    assert load_callback_candidate(str(tmp_path)) is None


@pytest.mark.parametrize(
    "mutation",
    [
        "old_call",
        "future_call",
        "yesterday",
        "wrong_date",
        "old_context",
        "future_context",
        "replacement_context",
        "naive_timestamp",
        "bad_timestamp",
        "empty_text",
        "bad_sid",
        "oversized",
        "malformed",
        "not_dict",
        "missing_context",
        "context_not_dict",
    ],
)
def test_invalid_note_returns_none(workspace, mutation):
    ws, now, note, voice = workspace
    ctx = note["context"]
    if mutation == "old_call":
        note["placed_at"] = (now - timedelta(minutes=31)).isoformat()
        ctx["generated_at"] = (now - timedelta(hours=1)).isoformat()
    elif mutation == "future_call":
        note["placed_at"] = (now + timedelta(minutes=1)).isoformat()
    elif mutation == "yesterday":
        note["placed_at"] = (now - timedelta(days=1)).isoformat()
        ctx["generated_at"] = note["placed_at"]
    elif mutation == "wrong_date":
        note["date"] = "2000-01-01"
    elif mutation == "old_context":
        ctx["generated_at"] = (now - timedelta(hours=5)).isoformat()
    elif mutation == "future_context":
        ctx["generated_at"] = (now + timedelta(minutes=1)).isoformat()
    elif mutation == "replacement_context":
        # context generated AFTER call was placed
        ctx["generated_at"] = now.isoformat()
    elif mutation == "naive_timestamp":
        ctx["generated_at"] = now.replace(tzinfo=None).isoformat()
    elif mutation == "bad_timestamp":
        ctx["generated_at"] = "invalid"
    elif mutation == "empty_text":
        ctx["text"] = " "
    elif mutation == "bad_sid":
        note["sid"] = "../../Calls"
    elif mutation == "oversized":
        ctx["text"] = "x" * (_MAX_PAYLOAD_BYTES_THRESHOLD + 1)
    elif mutation == "malformed":
        (voice / "missed-call-context.json").write_text("{")
        assert load_callback_candidate(str(ws)) is None
        return
    elif mutation == "not_dict":
        (voice / "missed-call-context.json").write_text("[]")
        assert load_callback_candidate(str(ws)) is None
        return
    elif mutation == "missing_context":
        del note["context"]
    elif mutation == "context_not_dict":
        note["context"] = "a string"
    (voice / "missed-call-context.json").write_text(json.dumps(note))
    assert load_callback_candidate(str(ws)) is None


_MAX_PAYLOAD_BYTES_THRESHOLD = 16000


# ---------------------------------------------------------------------------
# Backward compat: legacy standup files
# ---------------------------------------------------------------------------


def test_candidate_falls_back_to_legacy_standup_files(tmp_path):
    now = datetime.now(timezone.utc)
    voice = tmp_path / "state" / "voice-calls"
    voice.mkdir(parents=True)
    stamp = {
        "sid": CALL_SID,
        "date": now.date().isoformat(),
        "placed_at": (now - timedelta(minutes=5)).isoformat(),
    }
    brief = {
        "text": MARKER,
        "generated_at": (now - timedelta(minutes=30)).isoformat(),
    }
    (voice / "last-standup-call-sid.txt").write_text(json.dumps(stamp))
    (tmp_path / "state" / "standup-brief.json").write_text(json.dumps(brief))
    result = load_callback_candidate(str(tmp_path))
    assert result is not None
    sid, payload, _ = result
    assert sid == CALL_SID
    assert MARKER in payload


def test_legacy_multibyte_payload_respects_byte_cap(tmp_path):
    """Legacy path must measure UTF-8 bytes, matching the new-format path."""
    now = datetime.now(timezone.utc)
    voice = tmp_path / "state" / "voice-calls"
    voice.mkdir(parents=True)
    stamp = {
        "sid": CALL_SID,
        "date": now.date().isoformat(),
        "placed_at": (now - timedelta(minutes=5)).isoformat(),
    }
    # "é" is 2 UTF-8 bytes. 9000 of them stay under 16000 characters but
    # exceed 16000 bytes once JSON-encoded with ensure_ascii=False.
    brief = {
        "text": "é" * 9000,
        "generated_at": (now - timedelta(minutes=30)).isoformat(),
    }
    (voice / "last-standup-call-sid.txt").write_text(json.dumps(stamp))
    (tmp_path / "state" / "standup-brief.json").write_text(
        json.dumps(brief, ensure_ascii=False)
    )
    assert load_callback_candidate(str(tmp_path)) is None


def test_new_format_takes_priority_over_legacy(tmp_path):
    now = datetime.now(timezone.utc)
    voice = tmp_path / "state" / "voice-calls"
    voice.mkdir(parents=True)
    # Write legacy files with a different SID
    legacy_sid = "CA" + "d" * 32
    stamp = {
        "sid": legacy_sid,
        "date": now.date().isoformat(),
        "placed_at": (now - timedelta(minutes=5)).isoformat(),
    }
    brief = {
        "text": "legacy content",
        "generated_at": (now - timedelta(minutes=30)).isoformat(),
    }
    (voice / "last-standup-call-sid.txt").write_text(json.dumps(stamp))
    (tmp_path / "state" / "standup-brief.json").write_text(json.dumps(brief))
    # Write new format with the expected SID
    note = {
        "type": "general",
        "sid": CALL_SID,
        "date": now.date().isoformat(),
        "placed_at": (now - timedelta(minutes=3)).isoformat(),
        "caller": PHONE,
        "context": {
            "generated_at": (now - timedelta(minutes=20)).isoformat(),
            "text": MARKER,
        },
    }
    (voice / "missed-call-context.json").write_text(json.dumps(note))
    result = load_callback_candidate(str(tmp_path))
    assert result is not None
    sid, payload, _ = result
    assert sid == CALL_SID
    assert MARKER in payload


# ---------------------------------------------------------------------------
# load_callback_brief — Twilio API verification
# ---------------------------------------------------------------------------


@pytest.fixture
def twilio_case(workspace, monkeypatch):
    ws, _, _, _ = workspace
    account_sid = "AC" + "b" * 32
    auth_token = "test-token"
    response = {
        "sid": CALL_SID,
        "to": PHONE,
        "status": "no-answer",
        "direction": "outbound-api",
    }
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=response)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    return ws, account_sid, auth_token, response, requests


def test_load_callback_brief_returns_payload(twilio_case):
    ws, account_sid, auth_token, _, requests = twilio_case
    result = asyncio.run(
        load_callback_brief(
            str(ws), PHONE, account_sid=account_sid, auth_token=auth_token
        )
    )
    assert result is not None
    assert MARKER in result
    assert len(requests) == 1


def test_load_callback_brief_none_without_credentials(twilio_case):
    ws, _, _, _, requests = twilio_case
    result = asyncio.run(
        load_callback_brief(str(ws), PHONE, account_sid=None, auth_token=None)
    )
    assert result is None
    assert not requests


@pytest.mark.parametrize("status", ["completed", "in-progress", None])
def test_answered_call_returns_none(twilio_case, status):
    ws, account_sid, auth_token, response, requests = twilio_case
    response["status"] = status
    result = asyncio.run(
        load_callback_brief(
            str(ws), PHONE, account_sid=account_sid, auth_token=auth_token
        )
    )
    assert result is None
    assert len(requests) == 1


def test_wrong_recipient_returns_none(twilio_case):
    ws, account_sid, auth_token, response, _ = twilio_case
    response["to"] = "+19999999"
    result = asyncio.run(
        load_callback_brief(
            str(ws), PHONE, account_sid=account_sid, auth_token=auth_token
        )
    )
    assert result is None


def test_intervening_call_blocks_brief(twilio_case):
    ws, account_sid, auth_token, _, requests = twilio_case
    # last_call_ended_at is at or after placed_at
    candidate = load_callback_candidate(str(ws))
    assert candidate is not None
    _, _, placed_at = candidate
    result = asyncio.run(
        load_callback_brief(
            str(ws),
            PHONE,
            account_sid=account_sid,
            auth_token=auth_token,
            last_call_ended_at=placed_at + 1,
        )
    )
    assert result is None
    assert not requests


# ---------------------------------------------------------------------------
# write + load round-trip
# ---------------------------------------------------------------------------


def test_write_then_load_round_trip(tmp_path):
    now = datetime.now(timezone.utc)
    context = {
        "generated_at": (now - timedelta(minutes=20)).isoformat(),
        "text": MARKER,
        "goals": ["ship it"],
    }
    write_missed_call_context(
        str(tmp_path),
        type="standup",
        sid=CALL_SID,
        caller=PHONE,
        context=context,
        context_file="state/standup-brief.json",
        now=now - timedelta(minutes=5),
    )
    result = load_callback_candidate(str(tmp_path), now=now)
    assert result is not None
    sid, payload, _ = result
    assert sid == CALL_SID
    data = json.loads(payload)
    assert data["text"] == MARKER
    assert data["goals"] == ["ship it"]


def test_utc_midnight_crossing_is_not_same_day(tmp_path):
    voice = tmp_path / "state" / "voice-calls"
    voice.mkdir(parents=True)
    note = {
        "type": "general",
        "sid": CALL_SID,
        "date": "2026-09-14",
        "placed_at": "2026-09-14T23:59:00+00:00",
        "caller": PHONE,
        "context": {
            "generated_at": "2026-09-14T23:50:00+00:00",
            "text": MARKER,
        },
    }
    (voice / "missed-call-context.json").write_text(json.dumps(note))
    assert (
        load_callback_candidate(
            str(tmp_path), now=datetime(2026, 9, 15, 0, 1, tzinfo=timezone.utc)
        )
        is None
    )
