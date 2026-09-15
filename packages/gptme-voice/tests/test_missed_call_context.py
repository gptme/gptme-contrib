"""Tests for the general missed-call context persistence mechanism."""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from gptme_voice.realtime.missed_call_context import (
    _MAX_PAYLOAD_BYTES,
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


def test_write_context_file_only_omits_inline_snapshot(tmp_path):
    write_missed_call_context(
        str(tmp_path),
        sid=CALL_SID,
        caller=PHONE,
        context_file="state/prepared-context.json",
    )
    note = json.loads(
        (tmp_path / "state" / "voice-calls" / "missed-call-context.json").read_text()
    )
    assert note["context_file"] == "state/prepared-context.json"
    assert "context" not in note


def test_write_rejects_context_file_outside_workspace(tmp_path):
    with pytest.raises(ValueError, match="context_file outside workspace"):
        write_missed_call_context(
            str(tmp_path),
            sid=CALL_SID,
            caller=PHONE,
            context_file="../outside.json",
        )
    note_path = tmp_path / "state" / "voice-calls" / "missed-call-context.json"
    assert not note_path.exists()


def test_write_rejects_outside_context_file_even_with_snapshot(tmp_path):
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="context_file outside workspace"):
        write_missed_call_context(
            str(tmp_path),
            sid=CALL_SID,
            caller=PHONE,
            context={"generated_at": now.isoformat(), "text": MARKER},
            context_file="../outside.json",
        )
    note_path = tmp_path / "state" / "voice-calls" / "missed-call-context.json"
    assert not note_path.exists()


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
        "missing_generated_at",
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
        ctx["text"] = "x" * (_MAX_PAYLOAD_BYTES + 1)
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
    elif mutation == "missing_generated_at":
        del ctx["generated_at"]
    (voice / "missed-call-context.json").write_text(json.dumps(note))
    assert load_callback_candidate(str(ws)) is None


def test_candidate_rejects_caller_mismatch(workspace):
    ws, _, _, _ = workspace
    assert load_callback_candidate(str(ws), caller=PHONE) is not None
    assert load_callback_candidate(str(ws), caller="+19999999") is None


def _write_file_link_note(tmp_path, now, *, context_file, inline=None, extra=None):
    voice = tmp_path / "state" / "voice-calls"
    voice.mkdir(parents=True, exist_ok=True)
    note = {
        "type": "general",
        "sid": CALL_SID,
        "date": now.date().isoformat(),
        "placed_at": (now - timedelta(minutes=5)).isoformat(),
        "caller": PHONE,
        "context_file": context_file,
    }
    if inline is not None:
        note["context"] = inline
    if extra:
        note.update(extra)
    (voice / "missed-call-context.json").write_text(json.dumps(note))
    return note


def test_candidate_loads_referenced_context_file(tmp_path):
    """Inbound reads the linked file; no inlined snapshot is required."""
    now = datetime.now(timezone.utc)
    brief = {
        "generated_at": (now - timedelta(minutes=20)).isoformat(),
        "text": MARKER,
        "conversation_goals": ["ship it"],
    }
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "prepared-context.json").write_text(json.dumps(brief))
    _write_file_link_note(tmp_path, now, context_file="state/prepared-context.json")
    result = load_callback_candidate(str(tmp_path), now=now)
    assert result is not None
    sid, payload, _ = result
    assert sid == CALL_SID
    data = json.loads(payload)
    assert data["text"] == MARKER
    assert data["conversation_goals"] == ["ship it"]
    assert data["context_file"] == "state/prepared-context.json"


def test_candidate_loads_plain_text_context_file(tmp_path):
    """General notes files are not standup-brief JSON; inbound still reads them."""
    now = datetime.now(timezone.utc)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "agenda.md").write_text(f"# Agenda\n\n{MARKER}\n")
    _write_file_link_note(tmp_path, now, context_file="state/agenda.md")
    result = load_callback_candidate(str(tmp_path), now=now)
    assert result is not None
    sid, payload, _ = result
    assert sid == CALL_SID
    data = json.loads(payload)
    assert MARKER in data["text"]
    assert data["context_file"] == "state/agenda.md"


def test_candidate_loads_generic_json_context_file(tmp_path):
    now = datetime.now(timezone.utc)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "prep.json").write_text(
        json.dumps({"agenda": MARKER, "goals": ["ship"]})
    )
    _write_file_link_note(tmp_path, now, context_file="state/prep.json")
    result = load_callback_candidate(str(tmp_path), now=now)
    assert result is not None
    _, payload, _ = result
    data = json.loads(payload)
    assert data["agenda"] == MARKER
    assert data["goals"] == ["ship"]
    assert MARKER in data["text"]
    assert data["context_file"] == "state/prep.json"


def test_candidate_loads_text_json_without_generated_at(tmp_path):
    now = datetime.now(timezone.utc)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "prep.json").write_text(
        json.dumps({"text": MARKER, "goals": ["ship"]})
    )
    _write_file_link_note(tmp_path, now, context_file="state/prep.json")
    result = load_callback_candidate(str(tmp_path), now=now)
    assert result is not None
    _, payload, _ = result
    data = json.loads(payload)
    assert data["text"] == MARKER
    assert data["goals"] == ["ship"]
    assert data["context_file"] == "state/prep.json"


def test_candidate_loads_generic_json_with_generated_at_but_no_text(tmp_path):
    """A generated_at key alone is not standup-brief shape; load as generic."""
    now = datetime.now(timezone.utc)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "prep.json").write_text(
        json.dumps(
            {
                "generated_at": (now - timedelta(days=2)).isoformat(),
                "agenda": MARKER,
            }
        )
    )
    note = _write_file_link_note(tmp_path, now, context_file="state/prep.json")
    result = load_callback_candidate(str(tmp_path), now=now)
    assert result is not None
    _, payload, _ = result
    data = json.loads(payload)
    assert data["agenda"] == MARKER
    assert MARKER in data["text"]
    assert data["generated_at"] == note["placed_at"]
    assert data["context_file"] == "state/prep.json"


def test_candidate_loads_generic_json_with_unparseable_generated_at(tmp_path):
    now = datetime.now(timezone.utc)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "prep.json").write_text(
        json.dumps({"generated_at": "yesterday", "agenda": MARKER})
    )
    _write_file_link_note(tmp_path, now, context_file="state/prep.json")
    result = load_callback_candidate(str(tmp_path), now=now)
    assert result is not None
    _, payload, _ = result
    data = json.loads(payload)
    assert data["agenda"] == MARKER
    assert MARKER in data["text"]


def test_candidate_loads_long_lived_text_file(tmp_path):
    """A notes file older than MAX_CONTEXT_AGE still restores on a fresh missed call."""
    now = datetime.now(timezone.utc)
    (tmp_path / "state").mkdir()
    notes = tmp_path / "state" / "agenda.md"
    notes.write_text(MARKER)
    old = (now - timedelta(days=2)).timestamp()
    os.utime(notes, (old, old))
    _write_file_link_note(tmp_path, now, context_file="state/agenda.md")
    result = load_callback_candidate(str(tmp_path), now=now)
    assert result is not None
    _, payload, _ = result
    data = json.loads(payload)
    assert data["text"] == MARKER


def test_candidate_falls_back_to_inline_when_text_file_empty(tmp_path):
    now = datetime.now(timezone.utc)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "agenda.md").write_text("   \n")
    _write_file_link_note(
        tmp_path,
        now,
        context_file="state/agenda.md",
        inline={
            "generated_at": (now - timedelta(minutes=20)).isoformat(),
            "text": MARKER,
        },
    )
    result = load_callback_candidate(str(tmp_path), now=now)
    assert result is not None
    _, payload, _ = result
    assert MARKER in payload


def test_candidate_falls_back_to_inline_when_json_text_is_non_string(tmp_path):
    """A non-string JSON `text` field is not overwritten with a dump of the file."""
    now = datetime.now(timezone.utc)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "prep.json").write_text(
        json.dumps({"text": 123, "goals": ["ship"]})
    )
    _write_file_link_note(
        tmp_path,
        now,
        context_file="state/prep.json",
        inline={
            "generated_at": (now - timedelta(minutes=20)).isoformat(),
            "text": MARKER,
        },
    )
    result = load_callback_candidate(str(tmp_path), now=now)
    assert result is not None
    _, payload, _ = result
    data = json.loads(payload)
    assert data["text"] == MARKER
    assert data.get("goals") != ["ship"]


def test_candidate_prefers_live_file_over_inline_snapshot(tmp_path):
    now = datetime.now(timezone.utc)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "prepared-context.json").write_text(
        json.dumps(
            {
                "generated_at": (now - timedelta(minutes=20)).isoformat(),
                "text": MARKER,
            }
        )
    )
    _write_file_link_note(
        tmp_path,
        now,
        context_file="state/prepared-context.json",
        inline={
            "generated_at": (now - timedelta(minutes=25)).isoformat(),
            "text": "stale snapshot",
        },
    )
    result = load_callback_candidate(str(tmp_path), now=now)
    assert result is not None
    _, payload, _ = result
    assert MARKER in payload
    assert "stale snapshot" not in payload


def test_candidate_stale_standup_brief_falls_back_to_inline(tmp_path):
    """Standup-shaped JSON still honors freshness; stale live file → inline."""
    now = datetime.now(timezone.utc)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "standup-brief.json").write_text(
        json.dumps(
            {
                "generated_at": (now - timedelta(hours=5)).isoformat(),
                "text": "stale live brief",
            }
        )
    )
    _write_file_link_note(
        tmp_path,
        now,
        context_file="state/standup-brief.json",
        inline={
            "generated_at": (now - timedelta(minutes=20)).isoformat(),
            "text": MARKER,
        },
    )
    result = load_callback_candidate(str(tmp_path), now=now)
    assert result is not None
    _, payload, _ = result
    assert MARKER in payload
    assert "stale live brief" not in payload


def test_candidate_falls_back_to_inline_when_file_is_stale(tmp_path):
    now = datetime.now(timezone.utc)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "prepared-context.json").write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "text": "replacement brief",
            }
        )
    )
    _write_file_link_note(
        tmp_path,
        now,
        context_file="state/prepared-context.json",
        inline={
            "generated_at": (now - timedelta(minutes=20)).isoformat(),
            "text": MARKER,
        },
    )
    result = load_callback_candidate(str(tmp_path), now=now)
    assert result is not None
    _, payload, _ = result
    assert MARKER in payload
    assert "replacement brief" not in payload


def test_candidate_falls_back_to_inline_when_file_missing(tmp_path):
    now = datetime.now(timezone.utc)
    _write_file_link_note(
        tmp_path,
        now,
        context_file="state/prepared-context.json",
        inline={
            "generated_at": (now - timedelta(minutes=20)).isoformat(),
            "text": MARKER,
        },
    )
    result = load_callback_candidate(str(tmp_path), now=now)
    assert result is not None
    _, payload, _ = result
    assert MARKER in payload


def test_candidate_rejects_context_file_path_traversal(tmp_path):
    now = datetime.now(timezone.utc)
    secret = tmp_path.parent / "secret.json"
    secret.write_text(
        json.dumps(
            {
                "generated_at": (now - timedelta(minutes=20)).isoformat(),
                "text": MARKER,
            }
        )
    )
    _write_file_link_note(tmp_path, now, context_file="../secret.json")
    assert load_callback_candidate(str(tmp_path), now=now) is None


def test_candidate_skips_oversized_context_file_before_read(tmp_path, monkeypatch):
    """Cap is enforced on the opened fd so a huge linked file is never parsed."""
    import os

    now = datetime.now(timezone.utc)
    (tmp_path / "state").mkdir()
    huge = tmp_path / "state" / "prepared-context.json"
    huge.write_bytes(b"x" * (_MAX_PAYLOAD_BYTES + 1))
    _write_file_link_note(tmp_path, now, context_file="state/prepared-context.json")

    real_read = os.read

    def guarded(fd, n, *args, **kwargs):
        # fstat already rejected oversized files; a read of the payload
        # would mean the size cap did not hold the fd.
        if n > _MAX_PAYLOAD_BYTES:
            raise AssertionError("os.read must not buffer more than the payload cap")
        return real_read(fd, n, *args, **kwargs)

    monkeypatch.setattr(os, "read", guarded)
    assert load_callback_candidate(str(tmp_path), now=now) is None


def test_candidate_falls_back_to_inline_when_file_is_oversized(tmp_path):
    now = datetime.now(timezone.utc)
    (tmp_path / "state").mkdir()
    huge = tmp_path / "state" / "prepared-context.json"
    huge.write_bytes(b"x" * (_MAX_PAYLOAD_BYTES + 1))
    _write_file_link_note(
        tmp_path,
        now,
        context_file="state/prepared-context.json",
        inline={
            "generated_at": (now - timedelta(minutes=20)).isoformat(),
            "text": MARKER,
        },
    )
    result = load_callback_candidate(str(tmp_path), now=now)
    assert result is not None
    _, payload, _ = result
    assert MARKER in payload


def test_candidate_does_not_follow_symlink_at_read(tmp_path, monkeypatch):
    """O_NOFOLLOW must refuse a symlink even if the resolver is raced."""
    now = datetime.now(timezone.utc)
    (tmp_path / "state").mkdir()
    secret = tmp_path.parent / "secret.json"
    secret.write_text(
        json.dumps(
            {
                "generated_at": (now - timedelta(minutes=20)).isoformat(),
                "text": MARKER,
            }
        )
    )
    link = tmp_path / "state" / "prepared-context.json"
    link.symlink_to(secret)
    _write_file_link_note(tmp_path, now, context_file="state/prepared-context.json")
    monkeypatch.setattr(
        "gptme_voice.realtime.missed_call_context._resolve_workspace_file",
        lambda workspace, relative: link,
    )
    assert load_callback_candidate(str(tmp_path), now=now) is None


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


def test_load_callback_brief_skips_twilio_on_caller_mismatch(twilio_case):
    ws, account_sid, auth_token, _, requests = twilio_case
    result = asyncio.run(
        load_callback_brief(
            str(ws), "+19999999", account_sid=account_sid, auth_token=auth_token
        )
    )
    assert result is None
    assert not requests


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


def test_write_file_link_then_load_round_trip(tmp_path):
    now = datetime.now(timezone.utc)
    brief = {
        "generated_at": (now - timedelta(minutes=20)).isoformat(),
        "text": MARKER,
        "goals": ["ship it"],
    }
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "prepared-context.json").write_text(json.dumps(brief))
    write_missed_call_context(
        str(tmp_path),
        type="general",
        sid=CALL_SID,
        caller=PHONE,
        context_file="state/prepared-context.json",
        now=now - timedelta(minutes=5),
    )
    result = load_callback_candidate(str(tmp_path), now=now)
    assert result is not None
    sid, payload, _ = result
    assert sid == CALL_SID
    data = json.loads(payload)
    assert data["text"] == MARKER
    assert data["goals"] == ["ship it"]
    assert data["context_file"] == "state/prepared-context.json"


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
