"""Browser (webui) voice calls on ``/voice``.

A browser call streams PCM audio both ways over one websocket. When
gptme-server runs gptme-voice as its sidecar, the call is bound to a
conversation: its history and workspace shape the session, transcript turns
are posted to it live, typed messages join the call, and probe calls
(``?probe=1``) report what happened without leaving a trace.

Split out of ``server.py``; :class:`~gptme_voice.realtime.server.VoiceServer`
delegates to :func:`handle_browser_websocket`. Server-module names are reached
through ``_srv`` at call time (the import is circular), which also keeps them
patchable in tests via ``gptme_voice.realtime.server``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import websockets
from starlette.websockets import WebSocketDisconnect

from . import server as _srv

if TYPE_CHECKING:
    from .conversation_link import LiveTranscriptSink
    from .openai_client import OpenAIRealtimeClient
    from .server import TranscriptTurn, VoiceServer


def _same_dir(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return a == b
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except OSError:
        return False


def _is_probe_call(websocket) -> bool:
    """``?probe=1``: a test call that leaves no trace.

    Probe calls get transcript and tool-result events on the websocket (so a
    probe client can check what happened) and skip all post-call bookkeeping:
    no call record, call history, post-call unit or transcript promotion.
    """
    query_params = getattr(websocket, "query_params", {}) or {}
    return str(query_params.get("probe", "")).lower() in ("1", "true", "yes")


def _probe_caller_id() -> str:
    """A fresh caller id for a probe call, never shared with a real caller."""
    return f"probe-{uuid.uuid4().hex[:12]}"


def _probe_reporting(websocket, on_ai_transcript, on_user_transcript):
    """Wrap transcript callbacks to also report each turn to a probe client."""

    async def _send(event: dict) -> None:
        try:
            await websocket.send_text(json.dumps(event))
        except Exception:  # client gone: reporting must never break the call
            pass

    async def _ai(text: str) -> None:
        await on_ai_transcript(text)
        await _send({"type": "transcript", "role": "assistant", "text": text})

    pending: set[asyncio.Task[None]] = set()

    def _user(text: str, item_id: str | None = None) -> None:
        on_user_transcript(text, item_id)
        # Keep a reference until sent (a bare task can be garbage-collected).
        task = asyncio.ensure_future(
            _send({"type": "transcript", "role": "user", "text": text})
        )
        pending.add(task)
        task.add_done_callback(pending.discard)

    def _wrap_result(inject):
        async def _on_result(text: str) -> None:
            await _send({"type": "tool_result", "text": text[:2000]})
            await inject(text)

        return _on_result

    return _ai, _user, _wrap_result


def _notify_after(sink, on_ai_transcript, on_user_transcript):
    """Wrap transcript callbacks so *sink* hears about every change."""

    async def _ai(text: str) -> None:
        await on_ai_transcript(text)
        sink.notify()

    def _user(text: str, item_id: str | None = None) -> None:
        on_user_transcript(text, item_id)
        sink.notify()

    return _ai, _user


async def handle_browser_websocket(server: VoiceServer, websocket):
    """
    Handle WebSocket connection for browser voice transport.

    Accepts raw binary PCM16 audio frames at 16kHz from the browser side.
    Control messages remain JSON text frames.

    When run as gptme-server's sidecar (``GPTME_VOICE_BROWSER_SECRET``
    set), the server's tunnel must present the secret, and the call is
    bound to the ``conversation_id`` it passes: the realtime model gets
    the conversation's recent history, tools run in its workspace, and
    transcript turns are appended to it live.
    """
    headers = getattr(websocket, "headers", None) or {}
    # Bytes: compare_digest raises TypeError on non-ASCII str arguments.
    if server.browser_secret and not hmac.compare_digest(
        str(headers.get(_srv._BROWSER_SECRET_HEADER, "")).encode(),
        server.browser_secret.encode(),
    ):
        _srv.logger.warning(
            "Rejecting /voice client %s: missing or wrong sidecar secret",
            _srv._websocket_peer_host(websocket) or "unknown",
        )
        await websocket.close(code=1008)
        return

    await websocket.accept()

    conversation_id = (
        str(websocket.query_params.get("conversation_id") or "") or None
        if server.browser_secret
        else None
    )
    probe = _is_probe_call(websocket)
    caller_id = (
        _probe_caller_id()
        if probe
        else f"webui:{conversation_id}"
        if conversation_id
        else server._get_local_caller_id(websocket)
    )
    # A probe must not consume a real caller's pending handoff bootstrap.
    handoff_id = None if probe else server._get_local_handoff_id(websocket)
    realtime_client: OpenAIRealtimeClient | None = None
    tool_bridge: _srv.GptmeToolBridge | None = None
    transcript: list[TranscriptTurn] = []
    audio_converter = _srv.AudioConverter()
    live_sink: LiveTranscriptSink | None = None
    typed_lock = asyncio.Lock()
    typed_tasks: set[asyncio.Task[None]] = set()
    unknown_workspace: str | None = None  # private dir when the workspace is unknown
    same_workspace = True
    tool_workspace = server.workspace

    try:
        context = None
        if (
            conversation_id
            and server.gptme_server_url
            and server.gptme_server_key
            and not probe  # a probe must not write into a real conversation
        ):
            context = await _srv.fetch_conversation_context(
                server.gptme_server_url, server.gptme_server_key, conversation_id
            )
            if context.workspace:
                tool_workspace = context.workspace
        # An unknown workspace (config unreadable) is not assumed to be the
        # server's: fail closed for the server workspace's context.
        workspace_known = context is None or context.workspace_known
        same_workspace = workspace_known and _same_dir(tool_workspace, server.workspace)
        if not workspace_known:
            # Lookups must not run in (or read) the server's workspace on a
            # guess: give them an empty private directory instead.
            unknown_workspace = tempfile.mkdtemp(prefix="gptme-voice-unknown-ws-")
            tool_workspace = unknown_workspace
        # A call about another workspace gets that workspace's project prompt
        # (or the default if unknown), and none of the server workspace's
        # digest or call history. Passed only when needed, so the common case
        # calls the builder exactly as other transports do.
        other_workspace: dict[str, Any] = (
            {}
            if same_workspace
            else {
                "workspace_context": False,
                "base_instructions": server._base_instructions(
                    tool_workspace if workspace_known else None
                ),
            }
        )
        instructions = await server._build_session_instructions(
            caller_id=caller_id, handoff_id=handoff_id, **other_workspace
        )
        if context is not None and context.loaded and conversation_id:
            if context.instructions:
                instructions = f"{instructions}\n\n{context.instructions}"
            live_sink = _srv.LiveTranscriptSink(
                server_url=server.gptme_server_url,
                api_key=server.gptme_server_key,
                conversation_id=conversation_id,
                call_sid=f"webui-{uuid.uuid4().hex[:12]}",
                transcript=transcript,
                call_metadata={"source": "webui", "provider": server.provider},
            )
        body_adapter = server._body_adapter_for_websocket(
            websocket, transport="browser"
        )
        rag_for_ws = server._rag_for_websocket(websocket, transport="browser")
        if rag_for_ws is not None and not same_workspace:
            # The index covers the server's workspace, not the conversation's:
            # searching it would answer (and leak) from an unrelated workspace.
            _srv.logger.info(
                "workspace_search disabled: call workspace %s is not the indexed %s",
                tool_workspace,
                server.workspace,
            )
            rag_for_ws = None
        session_cfg = server._build_session_config(
            instructions=instructions,
            include_body_tools=body_adapter is not None,
            include_rag_tools=rag_for_ws is not None,
        )
        on_ai_transcript, on_user_transcript, _browser_hangup = (
            server._make_transcript_callbacks(
                transcript=transcript,
                websocket=websocket,
                source="browser",
                call_sid=None,
            )
        )
        if live_sink is not None:
            on_ai_transcript, on_user_transcript = _notify_after(
                live_sink, on_ai_transcript, on_user_transcript
            )
        wrap_result = None
        if probe:
            on_ai_transcript, on_user_transcript, wrap_result = _probe_reporting(
                websocket, on_ai_transcript, on_user_transcript
            )
        realtime_client = server._make_client(
            session_cfg,
            on_audio=lambda audio: server._send_browser_audio(websocket, audio),
            on_audio_end=lambda: server._send_browser_audio_end(websocket),
            on_ai_transcript=on_ai_transcript,
            on_user_transcript=on_user_transcript,
            # Barge-in: the caller started talking, so drop queued playback.
            # (audio_end only means the provider finished *streaming* the
            # reply, which is long before the browser finished playing it.)
            on_speech_started=lambda: send_browser_clear(websocket),
        )

        tool_bridge = _srv.GptmeToolBridge(
            workspace=tool_workspace,
            on_result=(
                wrap_result(realtime_client.inject_message)
                if wrap_result
                else realtime_client.inject_message
            ),
            on_dispatch=server._make_sound_cue_callback(websocket, "dispatch"),
            on_timeout=server._make_sound_cue_callback(websocket, "timeout"),
            on_cancel=server._make_sound_cue_callback(websocket, "cancel"),
            on_hangup=_browser_hangup,
            on_handoff=server._make_handoff_callback([caller_id], transcript),
            transcript_provider=lambda: transcript,
            body_adapter=body_adapter,
            rag=rag_for_ws,
        )
        server._seed_bridge_from_prior_leg(tool_bridge, caller_id)
        realtime_client.on_function_call = tool_bridge.handle_function_call
        # A rejected session.update must end the call instead of leaving
        # the caller with an unconfigured (or silent) provider session.
        realtime_client.on_session_failed = _srv._hangup_on_session_failure(
            _browser_hangup
        )

        await realtime_client.connect()
        await websocket.send_text(
            json.dumps(
                {
                    "type": "ready",
                    "input_sample_rate": _srv.AudioConverter.BROWSER_RATE,
                    "output_sample_rate": _srv.AudioConverter.OPENAI_RATE,
                }
            )
        )

        while True:
            message = await websocket.receive()
            message_type = message.get("type")
            if message_type == "websocket.disconnect":
                break

            audio_data = message.get("bytes")
            if isinstance(audio_data, bytes):
                await realtime_client.send_audio(
                    audio_converter.browser_to_openai(audio_data)
                )
                continue

            text = message.get("text")
            if not isinstance(text, str):
                continue

            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                _srv.logger.warning(
                    "Browser websocket: ignoring malformed JSON text frame"
                )
                continue
            if not isinstance(data, dict):
                continue  # valid JSON but not an object: ignore, like bad JSON
            if data.get("type") == "commit":
                await realtime_client.commit_audio()
            elif data.get("type") == "text":
                # Off the receive loop: delivering a typed message can wait
                # for an interrupted reply to end, and microphone audio must
                # keep flowing meanwhile. The lock keeps typed turns in order.
                task = asyncio.create_task(
                    _typed_in_order(
                        server,
                        typed_lock,
                        websocket,
                        realtime_client,
                        transcript,
                        live_sink,
                        data,
                        probe,
                    )
                )
                typed_tasks.add(task)
                task.add_done_callback(typed_tasks.discard)

    except WebSocketDisconnect:
        pass
    except RuntimeError as exc:
        if "not connected" not in str(exc).lower():
            raise
        _srv.logger.debug("Browser websocket already closed before receive: %s", exc)
    except Exception as e:
        _srv.logger.exception("Error handling browser connection: %s", e)
    finally:
        if typed_tasks:
            # A message typed just before hanging up is still delivered
            # and recorded; bounded, so a stuck provider can't hold teardown.
            _done, pending = await asyncio.wait(
                set(typed_tasks), timeout=_srv._TYPED_DRAIN_TIMEOUT_SECONDS
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if realtime_client:
            try:
                await server._disconnect_realtime_client(realtime_client)
            except Exception:
                # Still flush the transcript and record the call.
                _srv.logger.exception("Error disconnecting the realtime client")
        if live_sink is not None:
            await live_sink.close()
        if unknown_workspace is not None:
            if tool_bridge is not None and getattr(tool_bridge, "_pending_tasks", None):
                # A lookup may still run in it (mkdtemp has no finalizer, so it
                # really stays); the OS temp cleanup removes it later.
                _srv.logger.info(
                    "Leaving %s for a lookup still running", unknown_workspace
                )
            else:
                shutil.rmtree(unknown_workspace, ignore_errors=True)
        if probe or not same_workspace:
            # Probes leave no trace; a call about another workspace must not
            # land in this server workspace's call history or post-call runs.
            _srv.logger.info(
                "%s call ended (%d turns); no call record",
                "Probe" if probe else "Other-workspace",
                len(transcript),
            )
            return
        await server._on_call_end(
            caller_id,
            "browser",
            transcript,
            {
                "caller_id": caller_id,
                "provider": server.provider,
                **({"handoff_id": handoff_id} if handoff_id else {}),
                # Already posted turn by turn to the real conversation; the
                # end-of-call promotion (keyed by caller id) must not repost.
                **({"transcript_posted_live": "1"} if live_sink else {}),
            },
            tool_bridge=tool_bridge,
        )


async def _typed_in_order(server, lock: asyncio.Lock, *args) -> None:
    async with lock:
        try:
            await handle_typed_message(server, *args)
        except Exception:
            # A browser that went away mid-delivery must not leak an
            # unretrieved task exception; the call itself carries on.
            _srv.logger.exception("Typed message handling failed")


async def handle_typed_message(
    server: VoiceServer,
    websocket,
    realtime_client: OpenAIRealtimeClient,
    transcript: list[TranscriptTurn],
    live_sink: LiveTranscriptSink | None,
    data: dict,
    probe: bool = False,
) -> None:
    """A message the user typed into the chat during the call.

    It goes to the realtime model as the user's turn (answered by voice),
    and into the transcript, so it is added to the conversation like a
    spoken turn rather than starting a text-agent step.
    """
    text = str(data.get("text") or "").strip()[: _srv._MAX_TYPED_MESSAGE_CHARS]
    if not text:
        return
    # Deliver first: a message the model never got must not be recorded in
    # the conversation as if it had been part of the call, and a provider
    # hiccup must not end the call (like the /local text path).
    delivered = False

    def _record() -> None:
        nonlocal delivered
        delivered = True
        # A distinct item id keeps it from being merged with a speech partial.
        _srv._append_transcript_turn(
            transcript,
            "user",
            text,
            item_id=f"typed-{uuid.uuid4().hex[:8]}",
            allow_continuation=False,
        )
        if live_sink is not None:
            live_sink.notify(immediate=True)

    try:
        # Recorded the moment the provider has it (before any wait), and
        # playback is cleared right before the new reply is requested, so
        # it never clips the start of the answer this message triggers.
        # Always clear: the provider may have finished streaming a long
        # reply the browser is still playing, and the message supersedes it.
        await realtime_client.send_typed_message(
            text,
            before_reply=lambda: send_browser_clear(websocket),
            on_delivered=_record,
        )
    except (
        RuntimeError,
        asyncio.TimeoutError,  # session never became ready
        websockets.ConnectionClosed,
    ) as exc:
        # Not delivered: a message the model never got must not be
        # recorded, and a provider hiccup must not end the call.
        if not delivered:
            _srv.logger.warning("Typed message not delivered to the provider: %r", exc)
            return
        _srv.logger.warning("Typed message delivered, reply not requested: %r", exc)
    if probe:
        await send_probe_event(
            websocket, {"type": "transcript", "role": "user", "text": text}
        )


async def send_probe_event(websocket, event: dict) -> None:
    try:
        await websocket.send_text(json.dumps(event))
    except Exception:  # client gone: reporting must never break the call
        pass


async def send_browser_clear(websocket) -> None:
    """Tell the browser to drop queued assistant audio (caller barge-in).

    Best effort: a barge-in during the teardown drain arrives after the
    browser socket closed, and must not abort the rest of teardown.
    """
    try:
        await websocket.send_text(json.dumps({"type": "clear"}))
    except Exception as exc:
        _srv.logger.debug("Could not send clear to the browser: %r", exc)
