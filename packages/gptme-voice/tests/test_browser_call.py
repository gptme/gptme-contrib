"""Tests for browser (webui) calls: conversation binding, typed messages, probes."""

import asyncio
import json
from pathlib import Path

import pytest
from gptme_voice.realtime.server import (
    TranscriptTurn,
    VoiceServer,
)

from .voice_fakes import (
    _DummyBrowserWebSocket,
    _DummyToolBridge,
    _FakeRealtimeClient,
    _PeerWebSocket,
)


def test_local_probe_reports_text_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    server = VoiceServer()

    class _RecordingPeer(_PeerWebSocket):
        messages: list[str] = []

        async def send_text(self, message: str) -> None:
            self.messages.append(message)

    websocket = _RecordingPeer(
        "127.0.0.1", incoming=[{"type": "text", "text": "probe hello"}]
    )
    websocket.messages = []
    websocket.query_params = {"probe": "1"}

    async def _build(*, caller_id: str, handoff_id: str | None) -> str:
        return "You are Bob."

    monkeypatch.setattr(server, "_build_session_instructions", _build)
    monkeypatch.setattr(server, "_make_client", lambda *_a, **_k: _FakeRealtimeClient())
    monkeypatch.setattr("gptme_voice.realtime.server.GptmeToolBridge", _DummyToolBridge)
    asyncio.run(server.handle_local_websocket(websocket))

    sent = [json.loads(m) for m in websocket.messages]
    assert {"type": "transcript", "role": "user", "text": "probe hello"} in sent


def _run_browser_call(
    monkeypatch: pytest.MonkeyPatch,
    server: VoiceServer,
    websocket,
    fake_client: _FakeRealtimeClient,
) -> dict:
    """Drive handle_browser_websocket with fakes; return what _on_call_end saw."""
    seen: dict = {}

    async def _fake_build_session_instructions(
        *, caller_id: str, handoff_id: str | None
    ) -> str:
        return "You are Bob."

    async def _fake_on_call_end(caller_id, source, transcript, *args, **kwargs):
        seen.update(caller_id=caller_id, transcript=list(transcript))

    monkeypatch.setattr(
        server, "_build_session_instructions", _fake_build_session_instructions
    )
    monkeypatch.setattr(server, "_make_client", lambda _cfg, **_kw: fake_client)
    monkeypatch.setattr(server, "_on_call_end", _fake_on_call_end)
    monkeypatch.setattr("gptme_voice.realtime.server.GptmeToolBridge", _DummyToolBridge)
    asyncio.run(server.handle_browser_websocket(websocket))
    return seen


def test_browser_typed_message_goes_to_the_call_and_the_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text typed into the chat during a call is a user turn of the call."""
    server = VoiceServer(enable_browser_transport=True)
    websocket = _DummyBrowserWebSocket(
        [
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "text", "text": "  use the other file  "}),
            },
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "text", "text": " "}),
            },
            {"type": "websocket.disconnect"},
        ]
    )
    fake_client = _FakeRealtimeClient()

    seen = _run_browser_call(monkeypatch, server, websocket, fake_client)

    assert fake_client.sent_typed == ["use the other file"]
    assert [(t.role, t.text) for t in seen["transcript"]] == [
        ("user", "use the other file")
    ]
    # Playback is always cleared: the provider may have finished streaming a
    # reply the browser is still playing.
    assert {"type": "clear"} in [json.loads(m) for m in websocket.text_messages]


def test_browser_typed_message_during_a_reply_clears_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = VoiceServer(enable_browser_transport=True)
    websocket = _DummyBrowserWebSocket(
        [
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "text", "text": "stop, wrong file"}),
            },
            {"type": "websocket.disconnect"},
        ]
    )
    fake_client = _FakeRealtimeClient()
    fake_client.typed_interrupts = True

    _run_browser_call(monkeypatch, server, websocket, fake_client)

    assert {"type": "clear"} in [json.loads(m) for m in websocket.text_messages]


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("Not connected to OpenAI Realtime API"),
        asyncio.TimeoutError(),  # session never became ready
    ],
)
def test_browser_typed_message_not_delivered_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """A provider hiccup drops the message, not the call, and nothing is posted."""
    server = VoiceServer(enable_browser_transport=True)
    websocket = _DummyBrowserWebSocket(
        [
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "text", "text": "lost message"}),
            },
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "text", "text": "second try"}),
            },
            {"type": "websocket.disconnect"},
        ]
    )
    fake_client = _FakeRealtimeClient()
    fake_client.typed_error = error

    seen = _run_browser_call(monkeypatch, server, websocket, fake_client)

    # The call survived the failed send: the next message went through, and
    # only that one is in the transcript.
    assert fake_client.sent_typed == ["second try"]
    assert [(t.role, t.text) for t in seen["transcript"]] == [("user", "second try")]


def test_browser_audio_keeps_flowing_while_a_typed_message_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typed delivery can wait for an interrupted reply to end; microphone
    audio sent meanwhile is forwarded at once, not queued behind it."""
    server = VoiceServer(enable_browser_transport=True)
    events: list[str] = []

    class _SlowTypedClient(_FakeRealtimeClient):
        async def send_typed_message(
            self, text: str, before_reply=None, on_delivered=None
        ) -> bool:
            events.append("typed:start")
            await asyncio.sleep(0.2)  # waiting for response.done
            events.append("typed:sent")
            return await super().send_typed_message(text, before_reply, on_delivered)

        async def send_audio(self, audio_data: bytes) -> None:
            events.append("audio")
            await super().send_audio(audio_data)

    class _PacedSocket(_DummyBrowserWebSocket):
        async def receive(self) -> dict[str, object]:
            await asyncio.sleep(0.01)  # frames arrive over time, as in a call
            return await super().receive()

    websocket = _PacedSocket(
        [
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "text", "text": "stop"}),
            },
            {"type": "websocket.receive", "bytes": b"\x00\x00" * 160},
            {"type": "websocket.disconnect"},
        ]
    )
    fake_client = _SlowTypedClient()

    seen = _run_browser_call(monkeypatch, server, websocket, fake_client)

    assert events == ["typed:start", "audio", "typed:sent"]
    # Teardown waited for the in-flight typed message to be delivered.
    assert [(t.role, t.text) for t in seen["transcript"]] == [("user", "stop")]


def test_probe_call_leaves_no_call_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """?probe=1 calls skip all post-call bookkeeping."""
    server = VoiceServer(enable_browser_transport=True)
    websocket = _DummyBrowserWebSocket(
        [
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "text", "text": "hi"}),
            },
            {"type": "websocket.disconnect"},
        ]
    )
    websocket.query_params = {"probe": "1"}
    seen = _run_browser_call(monkeypatch, server, websocket, _FakeRealtimeClient())
    assert seen == {}  # _on_call_end never ran

    normal = _DummyBrowserWebSocket([{"type": "websocket.disconnect"}])
    seen = _run_browser_call(monkeypatch, server, normal, _FakeRealtimeClient())
    assert seen["caller_id"] == "local"


def test_probe_call_never_touches_the_conversation_or_a_real_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe gets no conversation link (no context fetch, no transcript posts)
    and a caller id of its own (no resume record of a real caller consumed)."""
    server = VoiceServer(enable_browser_transport=True)
    server.browser_secret = "s3cret"
    server.gptme_server_url = "http://127.0.0.1:5700"
    server.gptme_server_key = "k"
    fetched: list[str] = []

    async def _fetch(*args, **kwargs):
        fetched.append(args[2])
        raise AssertionError("probe fetched conversation context")

    monkeypatch.setattr(
        "gptme_voice.realtime.server.fetch_conversation_context", _fetch
    )
    seen_callers: list[str] = []

    async def _build(*, caller_id: str, handoff_id: str | None) -> str:
        seen_callers.append(caller_id)
        return "You are Bob."

    websocket = _DummyBrowserWebSocket(
        [
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "text", "text": "hi"}),
            },
            {"type": "websocket.disconnect"},
        ]
    )
    websocket.query_params = {"probe": "1", "conversation_id": "conv-1"}
    websocket.headers = {"x-gptme-voice-secret": "s3cret"}
    websocket.client = None
    monkeypatch.setattr(server, "_build_session_instructions", _build)
    monkeypatch.setattr(
        server, "_make_client", lambda _cfg, **_kw: _FakeRealtimeClient()
    )
    monkeypatch.setattr("gptme_voice.realtime.server.GptmeToolBridge", _DummyToolBridge)
    asyncio.run(server.handle_browser_websocket(websocket))

    assert fetched == []
    assert len(seen_callers) == 1 and seen_callers[0].startswith("probe-")


def test_probe_keeps_handoffs_and_reports_typed_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe never consumes a real caller's pending handoff, and a typed
    message it sends is reported back like a spoken turn."""
    server = VoiceServer(enable_browser_transport=True)
    handoffs: list[str | None] = []

    async def _build(*, caller_id: str, handoff_id: str | None) -> str:
        handoffs.append(handoff_id)
        return "You are Bob."

    websocket = _DummyBrowserWebSocket(
        [
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "text", "text": "probe question"}),
            },
            {"type": "websocket.disconnect"},
        ]
    )
    websocket.query_params = {"probe": "1", "handoff_id": "real-pending-handoff"}
    monkeypatch.setattr(server, "_build_session_instructions", _build)
    monkeypatch.setattr(
        server, "_make_client", lambda _cfg, **_kw: _FakeRealtimeClient()
    )
    monkeypatch.setattr("gptme_voice.realtime.server.GptmeToolBridge", _DummyToolBridge)
    asyncio.run(server.handle_browser_websocket(websocket))

    assert handoffs == [None]
    sent = [json.loads(m) for m in websocket.text_messages]
    assert {"type": "transcript", "role": "user", "text": "probe question"} in sent


def test_conversation_bound_call_end_is_not_promoted_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turns were posted live to the real conversation; the end-of-call
    promotion (keyed by caller id, i.e. webui:<id>) must not repost them."""
    from gptme_voice.realtime.conversation_link import ConversationContext

    server = VoiceServer(enable_browser_transport=True)
    server.browser_secret = "s3cret"
    server.gptme_server_url = "http://127.0.0.1:5700"
    server.gptme_server_key = "k"

    async def _fetch(*args, **kwargs):
        return ConversationContext(instructions=None, workspace=None)

    ended: list[dict] = []

    async def _on_call_end(caller_id, source, transcript, metadata, **kwargs):
        ended.append(metadata)

    callers: list[str | None] = []

    async def _build(*, caller_id: str | None, handoff_id: str | None) -> str:
        callers.append(caller_id)
        return "You are Bob."

    websocket = _DummyBrowserWebSocket([{"type": "websocket.disconnect"}])
    websocket.query_params = {"conversation_id": "conv-1"}
    websocket.headers = {"x-gptme-voice-secret": "s3cret"}
    websocket.client = None
    monkeypatch.setattr(
        "gptme_voice.realtime.server.fetch_conversation_context", _fetch
    )
    monkeypatch.setattr(server, "_build_session_instructions", _build)
    monkeypatch.setattr(server, "_on_call_end", _on_call_end)
    monkeypatch.setattr(
        server, "_make_client", lambda _cfg, **_kw: _FakeRealtimeClient()
    )
    monkeypatch.setattr("gptme_voice.realtime.server.GptmeToolBridge", _DummyToolBridge)
    asyncio.run(server.handle_browser_websocket(websocket))
    assert ended and ended[0].get("transcript_posted_live") == "1"
    # The conversation already holds the earlier calls' turns: no resume.
    assert callers == [None]

    posted: list[str] = []
    monkeypatch.setattr(
        "gptme_voice.realtime.server.urllib.request.urlopen",
        lambda *a, **k: posted.append("posted"),
    )
    server._promote_transcript_to_gptme(
        "webui:conv-1",
        [TranscriptTurn(role="user", text="hi")],
        {"call_sid": "x", "transcript_posted_live": "1"},
    )
    assert posted == []


def test_teardown_survives_a_failing_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the provider disconnect raises (e.g. a late callback on the closed
    browser socket), the call is still recorded."""
    server = VoiceServer(enable_browser_transport=True)

    class _BrokenDisconnect(_FakeRealtimeClient):
        async def disconnect(self, **kwargs: object) -> None:
            raise RuntimeError("Cannot call send once a close message has been sent")

    websocket = _DummyBrowserWebSocket([{"type": "websocket.disconnect"}])
    seen = _run_browser_call(monkeypatch, server, websocket, _BrokenDisconnect())
    assert seen.get("caller_id") == "local"


def test_browser_clear_on_a_closed_socket_is_harmless() -> None:
    server = VoiceServer(enable_browser_transport=True)

    class _Closed:
        async def send_text(self, _message: str) -> None:
            raise RuntimeError("Cannot call send once a close message has been sent")

    asyncio.run(server._send_browser_clear(_Closed()))  # does not raise


def test_conversation_in_another_workspace_gets_no_workspace_search_or_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """workspace_search covers the server's workspace; a call bound to another
    workspace must not search (and leak) it."""
    from gptme_voice.realtime.conversation_link import ConversationContext

    server = VoiceServer(enable_browser_transport=True, workspace=str(tmp_path / "a"))
    server.browser_secret = "s3cret"
    server.gptme_server_url = "http://127.0.0.1:5700"
    server.gptme_server_key = "k"
    sentinel_rag = object()
    monkeypatch.setattr(server, "_rag_for_websocket", lambda *a, **k: sentinel_rag)

    async def _fetch(*args, **kwargs):
        return ConversationContext(instructions=None, workspace=str(tmp_path / "b"))

    bridges: list[dict] = []

    class _RecordingBridge(_DummyToolBridge):
        def __init__(self, *args, **kwargs) -> None:
            bridges.append(kwargs)

    built: list[dict] = []

    async def _build(**kwargs) -> str:
        built.append(kwargs)
        return "You are Bob."

    websocket = _DummyBrowserWebSocket([{"type": "websocket.disconnect"}])
    websocket.query_params = {"conversation_id": "conv-1"}
    websocket.headers = {"x-gptme-voice-secret": "s3cret"}
    websocket.client = None
    monkeypatch.setattr(
        "gptme_voice.realtime.server.fetch_conversation_context", _fetch
    )
    monkeypatch.setattr(server, "_build_session_instructions", _build)
    monkeypatch.setattr(
        server, "_make_client", lambda _cfg, **_kw: _FakeRealtimeClient()
    )
    monkeypatch.setattr("gptme_voice.realtime.server.GptmeToolBridge", _RecordingBridge)
    asyncio.run(server.handle_browser_websocket(websocket))
    assert bridges[0]["rag"] is None
    assert bridges[0]["workspace"] == str(tmp_path / "b")
    # Nor the server workspace's activity digest / call history.
    assert built[0].get("workspace_context") is False


def test_bootstrap_without_workspace_context_skips_digest_and_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = VoiceServer(enable_browser_transport=True)
    monkeypatch.setattr(
        "gptme_voice.realtime.server._load_voice_digest", lambda _ws: "DIGEST-A"
    )
    monkeypatch.setattr(
        "gptme_voice.realtime.server.load_callback_history_index", lambda _ws: "CALLS-A"
    )

    async def _run(workspace_context: bool) -> str:
        return await server._build_session_instructions(
            caller_id=None, workspace_context=workspace_context
        )

    with_ctx = asyncio.run(_run(True))
    without = asyncio.run(_run(False))
    assert "DIGEST-A" in with_ctx and "CALLS-A" in with_ctx
    assert "DIGEST-A" not in without and "CALLS-A" not in without


def test_typed_turn_is_recorded_before_its_answer_and_survives_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The typed turn is recorded when the provider has it: before an answer
    that streams in during the acknowledgement wait, and even if teardown
    cancels that wait."""
    monkeypatch.setattr("gptme_voice.realtime.server._TYPED_DRAIN_TIMEOUT_SECONDS", 0.1)
    server = VoiceServer(enable_browser_transport=True)
    callbacks: dict = {}

    class _AnswersDuringAck(_FakeRealtimeClient):
        async def send_typed_message(
            self, text: str, before_reply=None, on_delivered=None
        ) -> bool:
            on_delivered()
            await callbacks["on_ai_transcript"]("the answer")
            await asyncio.sleep(10)  # stuck waiting for response.created
            return False

    client = _AnswersDuringAck()

    def _make(_cfg, **kwargs):
        callbacks.update(kwargs)
        return client

    seen: dict = {}

    async def _on_call_end(caller_id, source, transcript, *args, **kwargs):
        seen["transcript"] = list(transcript)

    async def _build(**_kwargs) -> str:
        return "You are Bob."

    websocket = _DummyBrowserWebSocket(
        [
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "text", "text": "the question"}),
            },
            {"type": "websocket.disconnect"},
        ]
    )
    monkeypatch.setattr(server, "_build_session_instructions", _build)
    monkeypatch.setattr(server, "_make_client", _make)
    monkeypatch.setattr(server, "_on_call_end", _on_call_end)
    monkeypatch.setattr("gptme_voice.realtime.server.GptmeToolBridge", _DummyToolBridge)

    asyncio.run(server.handle_browser_websocket(websocket))
    assert [(t.role, t.text) for t in seen["transcript"]] == [
        ("user", "the question"),
        ("assistant", "the answer"),
    ]


def test_unknown_conversation_workspace_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Config unreadable: the workspace is unknown, not assumed to be the
    server's, so no digest/history/workspace_search from the server's."""
    from gptme_voice.realtime.conversation_link import ConversationContext

    server = VoiceServer(enable_browser_transport=True, workspace=str(tmp_path))
    server.browser_secret = "s3cret"
    server.gptme_server_url = "http://127.0.0.1:5700"
    server.gptme_server_key = "k"
    monkeypatch.setattr(server, "_rag_for_websocket", lambda *a, **k: object())

    async def _fetch(*args, **kwargs):
        return ConversationContext(
            instructions=None, workspace=None, workspace_known=False
        )

    built: list[dict] = []
    bridges: list[dict] = []

    async def _build(**kwargs) -> str:
        built.append(kwargs)
        return "You are Bob."

    class _RecordingBridge(_DummyToolBridge):
        def __init__(self, *args, **kwargs) -> None:
            bridges.append(kwargs)

    websocket = _DummyBrowserWebSocket([{"type": "websocket.disconnect"}])
    websocket.query_params = {"conversation_id": "conv-1"}
    websocket.headers = {"x-gptme-voice-secret": "s3cret"}
    websocket.client = None
    monkeypatch.setattr(
        "gptme_voice.realtime.server.fetch_conversation_context", _fetch
    )
    monkeypatch.setattr(server, "_build_session_instructions", _build)
    monkeypatch.setattr(
        server, "_make_client", lambda _cfg, **_kw: _FakeRealtimeClient()
    )
    monkeypatch.setattr("gptme_voice.realtime.server.GptmeToolBridge", _RecordingBridge)
    asyncio.run(server.handle_browser_websocket(websocket))
    assert built[0].get("workspace_context") is False
    assert bridges[0]["rag"] is None
    # Lookups get an empty private directory, not the server's workspace,
    # and the default prompt rather than the server workspace's project files.
    tools_dir = bridges[0]["workspace"]
    assert tools_dir != str(tmp_path) and "gptme-voice-unknown-ws-" in tools_dir
    assert not Path(tools_dir).exists()  # cleaned up at the end of the call
    assert built[0]["base_instructions"] == server._base_instructions(None)


def test_call_about_another_workspace_gets_that_workspaces_project_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from gptme_voice.realtime.conversation_link import ConversationContext

    for name in ("a", "b"):
        ws = tmp_path / name
        ws.mkdir()
        (ws / "gptme.toml").write_text('files = ["ABOUT.md"]\n')
        (ws / "ABOUT.md").write_text(f"PROJECT-{name.upper()}-SECRET\n")
    server = VoiceServer(enable_browser_transport=True, workspace=str(tmp_path / "a"))
    assert "PROJECT-A-SECRET" in server._instructions
    server.browser_secret = "s3cret"
    server.gptme_server_url = "http://127.0.0.1:5700"
    server.gptme_server_key = "k"

    async def _fetch(*args, **kwargs):
        return ConversationContext(instructions=None, workspace=str(tmp_path / "b"))

    built: list[dict] = []

    async def _build(**kwargs) -> str:
        built.append(kwargs)
        return "x"

    websocket = _DummyBrowserWebSocket([{"type": "websocket.disconnect"}])
    websocket.query_params = {"conversation_id": "conv-1"}
    websocket.headers = {"x-gptme-voice-secret": "s3cret"}
    websocket.client = None
    monkeypatch.setattr(
        "gptme_voice.realtime.server.fetch_conversation_context", _fetch
    )
    monkeypatch.setattr(server, "_build_session_instructions", _build)
    monkeypatch.setattr(
        server, "_make_client", lambda _cfg, **_kw: _FakeRealtimeClient()
    )
    monkeypatch.setattr("gptme_voice.realtime.server.GptmeToolBridge", _DummyToolBridge)
    asyncio.run(server.handle_browser_websocket(websocket))

    base = built[0]["base_instructions"]
    assert "PROJECT-B-SECRET" in base and "PROJECT-A-SECRET" not in base


def test_probe_reporting_sends_transcript_and_tool_events() -> None:
    from gptme_voice.realtime.server import _probe_reporting

    sent: list[dict] = []
    ai_turns: list[str] = []
    user_turns: list[str] = []
    injected: list[str] = []

    class _Socket:
        async def send_text(self, message: str) -> None:
            sent.append(json.loads(message))

    async def _on_ai(text: str) -> None:
        ai_turns.append(text)

    def _on_user(text: str, item_id: str | None = None) -> None:
        user_turns.append(text)

    async def _inject(text: str) -> None:
        injected.append(text)

    async def _exercise() -> None:
        ai, user, wrap_result = _probe_reporting(_Socket(), _on_ai, _on_user)
        user("what time is it")
        await ai("Checking.")
        await wrap_result(_inject)("It is noon.")
        await asyncio.sleep(0)

    asyncio.run(_exercise())
    assert ai_turns == ["Checking."] and user_turns == ["what time is it"]
    assert injected == ["It is noon."]
    assert {"type": "transcript", "role": "assistant", "text": "Checking."} in sent
    assert {"type": "transcript", "role": "user", "text": "what time is it"} in sent
    assert {"type": "tool_result", "text": "It is noon."} in sent


def test_browser_websocket_requires_sidecar_secret_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As gptme-server's sidecar, /voice only accepts the server's tunnel."""
    server = VoiceServer(enable_browser_transport=True)
    server.browser_secret = "s3cret"

    class _SecretSocket(_DummyBrowserWebSocket):
        def __init__(self, secret: str | None) -> None:
            super().__init__([{"type": "websocket.disconnect"}])
            self.headers = {"x-gptme-voice-secret": secret} if secret else {}
            self.close_code: int | None = None
            self.client = None

        async def close(self, code: int = 1000) -> None:
            self.close_code = code

    for bad in (None, "wrong"):
        sock = _SecretSocket(bad)
        asyncio.run(server.handle_browser_websocket(sock))
        assert sock.close_code == 1008
        assert not sock.accepted

    good = _SecretSocket("s3cret")
    _run_browser_call(monkeypatch, server, good, _FakeRealtimeClient())
    assert good.accepted


def test_sidecar_secret_check_handles_non_ascii_and_missing_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = VoiceServer(enable_browser_transport=True)
    server.browser_secret = "s3cret"
    for headers in ({"x-gptme-voice-secret": "s\u00e9cret"}, None):
        websocket = _DummyBrowserWebSocket([{"type": "websocket.disconnect"}])
        if headers is not None:
            websocket.headers = headers
        closed: list[int] = []

        async def _close(code: int = 1000) -> None:
            closed.append(code)

        websocket.close = _close
        asyncio.run(server.handle_browser_websocket(websocket))  # no TypeError
        assert closed == [1008] and websocket.accepted is False


def test_speech_completion_after_a_typed_turn_updates_the_partial() -> None:
    """Typing while a speech partial is last: the later completion of that
    speech still updates the partial in place instead of adding a duplicate."""
    from gptme_voice.realtime.server import _append_transcript_turn

    transcript: list[TranscriptTurn] = []
    _append_transcript_turn(transcript, "user", "Hello, use", item_id="speech-1")
    _append_transcript_turn(
        transcript, "user", "stop", item_id="typed-1", allow_continuation=False
    )
    _append_transcript_turn(
        transcript, "user", "Hello, use the other file", item_id="speech-1"
    )
    assert [(t.text, t.item_id) for t in transcript] == [
        ("Hello, use the other file", "speech-1"),
        ("stop", "typed-1"),
    ]


def test_unloaded_conversation_gets_no_transcript_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gptme_voice.realtime.conversation_link import ConversationContext

    server = VoiceServer(enable_browser_transport=True)
    server.browser_secret = "s3cret"
    server.gptme_server_url = "http://127.0.0.1:5700"
    server.gptme_server_key = "k"

    async def _fetch(*args, **kwargs):
        return ConversationContext(
            instructions=None, workspace=None, workspace_known=False, loaded=False
        )

    sinks: list[object] = []

    class _NoSink:
        def __init__(self, *args, **kwargs) -> None:
            sinks.append(self)

    async def _build(**_kwargs) -> str:
        return "x"

    websocket = _DummyBrowserWebSocket([{"type": "websocket.disconnect"}])
    websocket.query_params = {"conversation_id": "missing"}
    websocket.headers = {"x-gptme-voice-secret": "s3cret"}
    websocket.client = None
    monkeypatch.setattr(
        "gptme_voice.realtime.server.fetch_conversation_context", _fetch
    )
    monkeypatch.setattr("gptme_voice.realtime.server.LiveTranscriptSink", _NoSink)
    monkeypatch.setattr(server, "_build_session_instructions", _build)
    monkeypatch.setattr(
        server, "_make_client", lambda _cfg, **_kw: _FakeRealtimeClient()
    )
    monkeypatch.setattr("gptme_voice.realtime.server.GptmeToolBridge", _DummyToolBridge)
    asyncio.run(server.handle_browser_websocket(websocket))
    assert sinks == []


def test_non_object_json_frames_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """`null`, `[]`, `1`, `"commit"`: ignored like malformed JSON, call goes on."""
    server = VoiceServer(enable_browser_transport=True)
    frames = [
        {"type": "websocket.receive", "text": text}
        for text in ("null", "[]", "1", '"commit"')
    ]
    frames.append(
        {
            "type": "websocket.receive",
            "text": json.dumps({"type": "text", "text": "hi"}),
        }
    )
    frames.append({"type": "websocket.disconnect"})
    fake_client = _FakeRealtimeClient()
    seen = _run_browser_call(
        monkeypatch, server, _DummyBrowserWebSocket(frames), fake_client
    )
    assert fake_client.sent_typed == ["hi"]
    assert [(t.role, t.text) for t in seen["transcript"]] == [("user", "hi")]


def test_call_about_another_workspace_leaves_no_call_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from gptme_voice.realtime.conversation_link import ConversationContext

    server = VoiceServer(enable_browser_transport=True, workspace=str(tmp_path / "a"))
    server.browser_secret = "s3cret"
    server.gptme_server_url = "http://127.0.0.1:5700"
    server.gptme_server_key = "k"
    (tmp_path / "b").mkdir()

    async def _fetch(*args, **kwargs):
        return ConversationContext(instructions=None, workspace=str(tmp_path / "b"))

    ended: list[str] = []

    async def _on_call_end(caller_id, *args, **kwargs):
        ended.append(caller_id)

    async def _build(**_kwargs) -> str:
        return "x"

    websocket = _DummyBrowserWebSocket([{"type": "websocket.disconnect"}])
    websocket.query_params = {"conversation_id": "conv-1"}
    websocket.headers = {"x-gptme-voice-secret": "s3cret"}
    websocket.client = None
    monkeypatch.setattr(
        "gptme_voice.realtime.server.fetch_conversation_context", _fetch
    )
    monkeypatch.setattr(server, "_build_session_instructions", _build)
    monkeypatch.setattr(server, "_on_call_end", _on_call_end)
    monkeypatch.setattr(
        server, "_make_client", lambda _cfg, **_kw: _FakeRealtimeClient()
    )
    monkeypatch.setattr("gptme_voice.realtime.server.GptmeToolBridge", _DummyToolBridge)
    asyncio.run(server.handle_browser_websocket(websocket))
    assert ended == []


@pytest.mark.parametrize(
    ("mode", "expect_runner"),
    [(None, True), ("conversation", True), ("subagent", False)],
)
def test_conversation_bound_call_runs_lookups_in_the_conversation(
    monkeypatch: pytest.MonkeyPatch, mode: str | None, expect_runner: bool
) -> None:
    from gptme_voice.realtime.conversation_link import (
        ConversationContext,
        ConversationLookup,
    )

    if mode is None:
        monkeypatch.delenv("GPTME_VOICE_LOOKUP_MODE", raising=False)
    else:
        monkeypatch.setenv("GPTME_VOICE_LOOKUP_MODE", mode)
    server = VoiceServer(enable_browser_transport=True)
    server.browser_secret = "s3cret"
    server.gptme_server_url = "http://127.0.0.1:5700"
    server.gptme_server_key = "k"

    async def _fetch(*args, **kwargs):
        return ConversationContext(instructions=None, workspace=None)

    bridges: list[dict] = []

    class _RecordingBridge(_DummyToolBridge):
        def __init__(self, *args, **kwargs) -> None:
            bridges.append(kwargs)

    async def _build(*, caller_id: str, handoff_id: str | None) -> str:
        return "You are Bob."

    websocket = _DummyBrowserWebSocket([{"type": "websocket.disconnect"}])
    websocket.query_params = {"conversation_id": "conv-1"}
    websocket.headers = {"x-gptme-voice-secret": "s3cret"}
    websocket.client = None
    monkeypatch.setattr(
        "gptme_voice.realtime.server.fetch_conversation_context", _fetch
    )
    monkeypatch.setattr(server, "_build_session_instructions", _build)
    monkeypatch.setattr(
        server, "_make_client", lambda _cfg, **_kw: _FakeRealtimeClient()
    )
    monkeypatch.setattr("gptme_voice.realtime.server.GptmeToolBridge", _RecordingBridge)
    asyncio.run(server.handle_browser_websocket(websocket))

    runner = bridges[0]["lookup_runner"]
    if expect_runner:
        assert isinstance(runner, ConversationLookup)
        assert runner._conversation_id == "conv-1"
    else:
        assert runner is None
