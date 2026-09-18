"""Exercise callback context through the actual Twilio WebSocket trust boundary."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from gptme_voice.realtime import server as server_mod
from gptme_voice.realtime.missed_call_context import MAX_CONTEXT_AGE

from .test_server import _DummyToolBridge, _DummyTwilioWebSocket, _FakeRealtimeClient

PHONE = "+15551212"
CALL_SID = "CA" + "a" * 32
MARKER = "Prepared standup: the release is ready for review."


@pytest.fixture
def callback_case(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    state = tmp_path / "state"
    voice = state / "voice-calls"
    voice.mkdir(parents=True)
    people = tmp_path / "people"
    people.mkdir()
    (people / "operator.md").write_text(
        f"# Erik\n- Phone: {PHONE}\n- Call role: operator\n- Call name: Erik\n"
    )
    stamp = {
        "sid": CALL_SID,
        "date": now.date().isoformat(),
        "placed_at": (now - timedelta(minutes=2)).isoformat(),
    }
    brief = {
        "text": MARKER,
        "generated_at": (now - timedelta(minutes=30)).isoformat(),
        "conversation_goals": ["Decide release timing"],
    }
    (voice / "last-standup-call-sid.txt").write_text(json.dumps(stamp))
    (state / "standup-brief.json").write_text(json.dumps(brief))
    config = {
        "TWILIO_CALLER_ALLOWLIST": PHONE,
        "TWILIO_ACCOUNT_SID": "AC" + "b" * 32,
        "TWILIO_AUTH_TOKEN": "test-token",
        "GPTME_VOICE_STATE_DIR": str(voice),
    }
    monkeypatch.setattr(server_mod, "_get_config_env", config.get)
    server = server_mod.VoiceServer(workspace=str(tmp_path))
    server._instructions = "You are Bob."
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
    captured = []
    monkeypatch.setattr(
        server,
        "_make_client",
        lambda cfg, **kwargs: captured.append(cfg) or _FakeRealtimeClient(),
    )
    monkeypatch.setattr(server_mod, "GptmeToolBridge", _DummyToolBridge)
    monkeypatch.setattr(server, "_on_call_end", lambda *a, **kw: asyncio.sleep(0))

    def run(
        *,
        grant=True,
        grant_from=PHONE,
        grant_sid="CAinbound",
        from_number=PHONE,
        token=None,
    ):
        custom = {"from_number": from_number}
        if grant:
            custom["body_grant"] = token or server._mint_twilio_body_grant(
                grant_from, grant_sid
            )
        ws = _DummyTwilioWebSocket(
            [
                {
                    "event": "start",
                    "start": {
                        "streamSid": "MZinbound",
                        "callSid": "CAinbound",
                        "customParameters": custom,
                    },
                },
                {"event": "stop"},
            ]
        )
        asyncio.run(server.handle_twilio_websocket(ws))
        assert captured, "handler must create a session"
        return captured[-1]

    return run, server, response, requests, state, stamp, brief


def test_trusted_callback_receives_prepared_plan_before_greeting(callback_case):
    run, _, _, requests, _, _, _ = callback_case
    cfg = run()
    assert MARKER in cfg.instructions
    assert "Decide release timing" in cfg.instructions
    assert "callback" in cfg.initial_response_instructions.lower()
    assert "attempted to reach" in cfg.instructions.lower()
    assert len(requests) == 1


def test_trusted_callback_reads_referenced_context_file(callback_case):
    """General missed-call notes load the linked file, not standup-specific stamps."""
    run, _, _, requests, state, _, _ = callback_case
    (state / "standup-brief.json").unlink()
    (state / "voice-calls/last-standup-call-sid.txt").unlink()
    now = datetime.now(timezone.utc)
    brief = {
        "text": MARKER,
        "generated_at": (now - timedelta(minutes=30)).isoformat(),
        "conversation_goals": ["Decide release timing"],
    }
    (state / "prepared-context.json").write_text(json.dumps(brief))
    note = {
        "type": "general",
        "sid": CALL_SID,
        "date": now.date().isoformat(),
        "placed_at": (now - timedelta(minutes=2)).isoformat(),
        "caller": PHONE,
        "context_file": "state/prepared-context.json",
    }
    (state / "voice-calls/missed-call-context.json").write_text(json.dumps(note))
    cfg = run()
    assert MARKER in cfg.instructions
    assert "Decide release timing" in cfg.instructions
    assert "callback" in cfg.initial_response_instructions.lower()
    assert "attempted to reach" in cfg.instructions.lower()
    assert "standup call" not in cfg.instructions.lower()
    assert len(requests) == 1


def test_trusted_callback_reads_plain_text_context_file(callback_case):
    """A general notes file (not standup-brief JSON) is injected on callback."""
    run, _, _, requests, state, _, _ = callback_case
    (state / "standup-brief.json").unlink()
    (state / "voice-calls/last-standup-call-sid.txt").unlink()
    now = datetime.now(timezone.utc)
    (state / "agenda.md").write_text(f"# Follow-up\n\n{MARKER}\n")
    note = {
        "type": "general",
        "sid": CALL_SID,
        "date": now.date().isoformat(),
        "placed_at": (now - timedelta(minutes=2)).isoformat(),
        "caller": PHONE,
        "context_file": "state/agenda.md",
    }
    (state / "voice-calls/missed-call-context.json").write_text(json.dumps(note))
    cfg = run()
    assert MARKER in cfg.instructions
    assert "state/agenda.md" in cfg.instructions
    assert "callback" in cfg.initial_response_instructions.lower()
    assert "standup call" not in cfg.instructions.lower()
    assert len(requests) == 1


@pytest.mark.parametrize(
    "status", ["completed", "in-progress", "queued", "ringing", None, "unknown"]
)
def test_answered_or_unresolved_call_keeps_normal_inbound(callback_case, status):
    run, _, response, requests, _, _, _ = callback_case
    response["status"] = status
    cfg = run()
    assert MARKER not in cfg.instructions
    assert "callback" not in cfg.initial_response_instructions.lower()
    assert len(requests) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"grant": False},
        {"grant_sid": "CAdifferent"},
        {"grant_from": "+19999999"},
        {"from_number": "+19999999"},
    ],
)
def test_untrusted_websocket_cannot_load_plan(callback_case, kwargs):
    run, _, _, requests, _, _, _ = callback_case
    assert MARKER not in run(**kwargs).instructions
    assert not requests


@pytest.mark.parametrize(
    "field,value",
    [
        ("to", "+19999999"),
        ("sid", "CAdifferent"),
        ("direction", "inbound"),
    ],
)
def test_different_recipient_or_call_keeps_normal_inbound(callback_case, field, value):
    run, _, response, _, _, _, _ = callback_case
    response[field] = value
    assert MARKER not in run().instructions


@pytest.mark.parametrize(
    "mutation",
    [
        "old_brief",
        "future_brief",
        "replacement_brief",
        "old_call",
        "future_call",
        "yesterday",
        "wrong_date",
        "naive_timestamp",
        "bad_timestamp",
        "empty_text",
        "bad_sid",
        "malformed_brief",
        "list_brief",
        "malformed_stamp",
        "list_stamp",
        "missing_brief",
        "missing_stamp",
        "oversized_plan",
    ],
)
def test_invalid_local_evidence_skips_lookup(callback_case, mutation):
    run, _, _, requests, state, stamp, brief = callback_case
    now = datetime.now(timezone.utc)
    if mutation == "old_brief":
        brief["generated_at"] = (now - timedelta(hours=5)).isoformat()
    elif mutation == "future_brief":
        brief["generated_at"] = (now + timedelta(minutes=1)).isoformat()
    elif mutation == "replacement_brief":
        brief["generated_at"] = now.isoformat()
    elif mutation == "old_call":
        stamp["placed_at"] = (now - timedelta(hours=5)).isoformat()
        brief["generated_at"] = (now - timedelta(hours=5)).isoformat()
    elif mutation == "future_call":
        stamp["placed_at"] = (now + timedelta(minutes=1)).isoformat()
    elif mutation == "yesterday":
        stamp["placed_at"] = (now - timedelta(days=1)).isoformat()
        brief["generated_at"] = stamp["placed_at"]
    elif mutation == "wrong_date":
        stamp["date"] = "2000-01-01"
    elif mutation == "naive_timestamp":
        brief["generated_at"] = now.replace(tzinfo=None).isoformat()
    elif mutation == "bad_timestamp":
        brief["generated_at"] = "invalid"
    elif mutation == "empty_text":
        brief["text"] = " "
    elif mutation == "bad_sid":
        stamp["sid"] = "../../Calls"
    elif mutation == "oversized_plan":
        brief["text"] = "x" * 16001
    brief_path = state / "standup-brief.json"
    stamp_path = state / "voice-calls/last-standup-call-sid.txt"
    brief_path.write_text(json.dumps(brief))
    stamp_path.write_text(json.dumps(stamp))
    for kind, path in (("brief", brief_path), ("stamp", stamp_path)):
        if mutation == f"malformed_{kind}":
            path.write_text("{")
        elif mutation == f"list_{kind}":
            path.write_text("[]")
        elif mutation == f"missing_{kind}":
            path.unlink()
    assert MARKER not in run().instructions
    assert not requests


def test_allowlisted_non_operator_cannot_load_private_plan(callback_case):
    run, _, _, requests, state, _, _ = callback_case
    (state.parent / "people/operator.md").write_text(f"# Friend\n- Phone: {PHONE}\n")
    assert MARKER not in run().instructions
    assert not requests


def test_callback_plan_takes_priority_over_generic_prewarm(callback_case, monkeypatch):
    run, server, _, _, _, _, _ = callback_case

    async def forbidden_claim(_number):
        pytest.fail("callback must not use the generic prewarm")

    monkeypatch.setattr(server, "_claim_prewarm", forbidden_claim)
    assert MARKER in run().instructions


@pytest.mark.parametrize("failure", ["http_error", "bad_json", "timeout", "list"])
def test_api_failure_falls_back_to_normal_inbound(callback_case, monkeypatch, failure):
    run, _, _, _, _, _, _ = callback_case

    async def get(self, url, **kwargs):
        request = httpx.Request("GET", url)
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if failure == "http_error":
            return httpx.Response(503, request=request)
        if failure == "list":
            return httpx.Response(200, json=[], request=request)
        return httpx.Response(200, text="invalid", request=request)

    # The fixture wraps AsyncClient; patch the real class's method.
    monkeypatch.setattr(httpx._client.AsyncClient, "get", get)
    assert MARKER not in run().instructions


def test_intervening_call_resumes_instead_of_reoffering_missed_standup(callback_case):
    run, server, _, requests, _, _, _ = callback_case
    server._save_recent_call(
        server_mod.RecentCallRecord(
            caller_id=PHONE,
            source="twilio",
            ended_at=datetime.now(timezone.utc).timestamp(),
            transcript=[
                server_mod.TranscriptTurn(
                    role="user", text="We already covered the standup."
                )
            ],
            metadata={"call_sid": "CAprevious"},
        )
    )
    cfg = run()
    assert MARKER not in cfg.instructions
    assert "We already covered the standup." in cfg.instructions
    assert not cfg.initial_response_instructions
    assert not requests


@pytest.mark.parametrize("candidate", [True, False])
def test_signed_webhook_preserves_callback_evidence_before_stream(
    callback_case, monkeypatch, candidate
):
    from xml.etree import ElementTree

    from twilio.request_validator import RequestValidator

    run, server, _, _, state, _, _ = callback_case
    if not candidate:
        (state / "standup-brief.json").unlink()
    prewarms = []
    monkeypatch.setattr(server, "_register_prewarm_task", prewarms.append)
    params = {"From": PHONE, "CallSid": "CAinbound"}
    signature = RequestValidator("test-token").compute_signature(
        "https://voice.example/incoming", params
    )

    class Request:
        headers = {"host": "voice.example", "X-Twilio-Signature": signature}

        async def form(self):
            return params

    response = asyncio.run(server.handle_incoming_call(Request()))
    assert response.status_code == 200
    assert prewarms == ([] if candidate else [PHONE])
    xml = ElementTree.fromstring(response.body)
    token = next(
        node.attrib["value"]
        for node in xml.iter("Parameter")
        if node.attrib["name"] == "body_grant"
    )
    assert (MARKER in run(token=token).instructions) is candidate


def test_callback_crossing_utc_midnight_is_not_same_day(callback_case):
    from gptme_voice.realtime.standup_callback import load_callback_candidate

    _, _, _, _, state, stamp, brief = callback_case
    stamp.update(date="2026-09-14", placed_at="2026-09-14T23:59:00+00:00")
    brief["generated_at"] = "2026-09-14T23:50:00+00:00"
    (state / "standup-brief.json").write_text(json.dumps(brief))
    (state / "voice-calls/last-standup-call-sid.txt").write_text(json.dumps(stamp))
    assert (
        load_callback_candidate(
            str(state.parent), now=datetime(2026, 9, 15, 0, 1, tzinfo=timezone.utc)
        )
        is None
    )


def test_trusted_callback_at_58_minutes_receives_prepared_brief(callback_case):
    """Regression: same-morning callback 58 min after the missed standup must
    deliver the prepared brief — the 2026-09-17 failure case."""
    run, _, _, requests, state, stamp, brief = callback_case
    now = datetime.now(timezone.utc)
    # Outbound was 58 minutes ago; context was generated 3.5h ago
    stamp["placed_at"] = (now - timedelta(minutes=58)).isoformat()
    stamp["date"] = now.date().isoformat()
    brief["generated_at"] = (now - timedelta(hours=3, minutes=30)).isoformat()
    (state / "standup-brief.json").write_text(json.dumps(brief))
    (state / "voice-calls/last-standup-call-sid.txt").write_text(json.dumps(stamp))
    cfg = run()
    assert (
        MARKER in cfg.instructions
    ), "58-minute same-morning callback must inject the prepared standup brief"
    assert "callback" in cfg.initial_response_instructions.lower()
    assert len(requests) == 1


@pytest.mark.parametrize("source", ["legacy", "note", "history"])
def test_trusted_callback_at_2h_receives_prepared_brief(callback_case, source):
    """Regression: a brief older than MAX_CONTEXT_AGE at callback time is still
    delivered when it was fresh relative to when the missed call was placed.

    Covers all three loaders (legacy stamp, missed-call-context.json,
    callback-history.jsonl): the 2026-09-18 failure had the brief generated
    ~4h52m before the callback but ~2h30m before the call was placed.
    """
    run, _, _, requests, state, stamp, brief = callback_case
    now = datetime.now(timezone.utc)
    placed = now - timedelta(hours=2, minutes=23)
    generated = placed - timedelta(hours=2, minutes=30)
    if generated.date() != now.date():
        pytest.skip("scenario spans UTC midnight; the loaders require a same-day brief")
    assert now - generated > MAX_CONTEXT_AGE >= placed - generated
    brief["generated_at"] = generated.isoformat()
    (state / "standup-brief.json").write_text(json.dumps(brief))
    voice = state / "voice-calls"
    if source == "legacy":
        stamp["placed_at"] = placed.isoformat()
        (voice / "last-standup-call-sid.txt").write_text(json.dumps(stamp))
    else:
        (voice / "last-standup-call-sid.txt").unlink()
        note = {
            "type": "general",
            "sid": CALL_SID,
            "date": now.date().isoformat(),
            "placed_at": placed.isoformat(),
            "caller": PHONE,
            "context_file": "state/standup-brief.json",
        }
        target = (
            "missed-call-context.json" if source == "note" else "callback-history.jsonl"
        )
        (voice / target).write_text(json.dumps(note) + "\n")
    cfg = run()
    assert (
        MARKER in cfg.instructions
    ), "2h+ same-morning callback must inject the prepared brief"
    assert "callback" in cfg.initial_response_instructions.lower()
    assert len(requests) == 1
