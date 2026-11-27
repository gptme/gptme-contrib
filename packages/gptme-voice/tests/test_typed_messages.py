"""Typed messages during a call: cancel, ordering and delivery (OpenAIRealtimeClient)."""

import asyncio

import pytest
from gptme_voice.realtime.openai_client import (
    OpenAIRealtimeClient,
)

from .voice_fakes import _FakeWebSocket


def test_send_typed_message_interrupts_an_active_reply() -> None:
    """Typing while the model talks cancels that reply before the new turn."""

    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            # The fake provider never acknowledges response.create.
            mp.setattr(
                "gptme_voice.realtime.openai_client._TYPED_CANCEL_WAIT_SECONDS", 0.3
            )
            client = OpenAIRealtimeClient(api_key="test-key")
            await client.connect()
            await client._handle_event({"type": "session.updated"})

            baseline = len(fake_ws.sent)
            assert await client.send_typed_message("idle turn") is False
            assert [e["type"] for e in fake_ws.sent[baseline:]] == [
                "conversation.item.create",
                "response.create",
            ]

            await client._handle_event({"type": "response.created"})
            assert client.responding
            baseline = len(fake_ws.sent)
            typed = asyncio.create_task(client.send_typed_message("wait, not that"))
            await asyncio.sleep(0.05)
            # The new turn waits for the provider to confirm the cancel.
            assert [e["type"] for e in fake_ws.sent[baseline:]] == ["response.cancel"]
            await client._handle_event(
                {"type": "response.done", "response": {"status": "cancelled"}}
            )
            assert await typed is True
            assert [e["type"] for e in fake_ws.sent[baseline:]] == [
                "response.cancel",
                "conversation.item.create",
                "response.create",
            ]
            await client.disconnect()

    asyncio.run(_exercise())


def test_send_typed_message_also_cancels_a_reply_that_starts_meanwhile() -> None:
    """An injected result can start a new reply while the typed message waits
    for the first cancel; that reply is cancelled too before the typed turn."""

    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            # The fake provider never acknowledges response.create.
            mp.setattr(
                "gptme_voice.realtime.openai_client._TYPED_CANCEL_WAIT_SECONDS", 0.3
            )
            client = OpenAIRealtimeClient(api_key="test-key")
            await client.connect()
            await client._handle_event({"type": "session.updated"})
            await client._handle_event({"type": "response.created"})
            baseline = len(fake_ws.sent)
            typed = asyncio.create_task(client.send_typed_message("stop"))
            await asyncio.sleep(0.02)
            done = {"type": "response.done", "response": {"status": "cancelled"}}
            # The first reply ends, but a new one starts before we resume.
            await client._handle_event(done)
            await client._handle_event({"type": "response.created"})
            await asyncio.sleep(0.02)
            await client._handle_event(done)
            assert await typed is True
            assert [e["type"] for e in fake_ws.sent[baseline:]] == [
                "response.cancel",
                "response.cancel",
                "conversation.item.create",
                "response.create",
            ]
            await client.disconnect()

    asyncio.run(_exercise())


def test_send_typed_message_reports_undelivered_if_cancel_is_never_confirmed() -> None:
    """Without response.done, a response.create would be rejected: raise
    instead of sending a turn that would go unanswered."""

    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            mp.setattr(
                "gptme_voice.realtime.openai_client._TYPED_CANCEL_WAIT_SECONDS", 0.05
            )
            client = OpenAIRealtimeClient(api_key="test-key")
            await client.connect()
            await client._handle_event({"type": "session.updated"})
            await client._handle_event({"type": "response.created"})
            baseline = len(fake_ws.sent)
            with pytest.raises(asyncio.TimeoutError):
                await client.send_typed_message("stop")
            assert [e["type"] for e in fake_ws.sent[baseline:]] == ["response.cancel"]
            await client.disconnect()

    asyncio.run(_exercise())


def test_send_typed_message_rechecks_right_before_response_create() -> None:
    """A reply that starts while the typed item is being sent is cancelled
    before response.create, which would otherwise be rejected."""

    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            # The fake provider never acknowledges response.create.
            mp.setattr(
                "gptme_voice.realtime.openai_client._TYPED_CANCEL_WAIT_SECONDS", 0.3
            )
            client = OpenAIRealtimeClient(api_key="test-key")
            await client.connect()
            await client._handle_event({"type": "session.updated"})
            real_send = client._send_event

            async def _send_and_race(event_type: str, data: dict) -> None:
                await real_send(event_type, data)
                if event_type == "conversation.item.create":
                    # An injected result's reply starts during this send.
                    await client._handle_event({"type": "response.created"})

                    async def _confirm() -> None:
                        await asyncio.sleep(0.02)
                        await client._handle_event(
                            {
                                "type": "response.done",
                                "response": {"status": "cancelled"},
                            }
                        )

                    asyncio.ensure_future(_confirm())

            client._send_event = _send_and_race  # type: ignore[method-assign]
            baseline = len(fake_ws.sent)
            # A reply was interrupted (the one that started mid-send).
            assert await client.send_typed_message("stop") is True
            assert [e["type"] for e in fake_ws.sent[baseline:]] == [
                "conversation.item.create",
                "response.cancel",
                "response.create",
            ]
            await client.disconnect()

    asyncio.run(_exercise())


def test_typed_item_already_sent_counts_as_delivered_on_a_late_cancel_timeout() -> None:
    """If a reply starts while the item is sent and is never confirmed
    cancelled, the message still counts as delivered (the provider has it):
    no exception, so the caller records it; no rejected response.create."""

    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            mp.setattr(
                "gptme_voice.realtime.openai_client._TYPED_CANCEL_WAIT_SECONDS", 0.05
            )
            client = OpenAIRealtimeClient(api_key="test-key")
            await client.connect()
            await client._handle_event({"type": "session.updated"})
            real_send = client._send_event

            async def _send_and_race(event_type: str, data: dict) -> None:
                await real_send(event_type, data)
                if event_type == "conversation.item.create":
                    await client._handle_event({"type": "response.created"})

            client._send_event = _send_and_race  # type: ignore[method-assign]
            baseline = len(fake_ws.sent)
            assert await client.send_typed_message("stop") is True
            types = [e["type"] for e in fake_ws.sent[baseline:]]
            assert types[0] == "conversation.item.create"
            assert "response.create" not in types
            await client.disconnect()

    asyncio.run(_exercise())


def test_back_to_back_typed_messages_dont_send_a_rejected_response_create() -> None:
    """The second typed message waits for the first's reply to be acknowledged,
    then cancels it, instead of sending a response.create that is rejected."""

    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            client = OpenAIRealtimeClient(api_key="test-key")
            await client.connect()
            await client._handle_event({"type": "session.updated"})
            real_send = client._send_event

            async def _provider(event_type: str, data: dict) -> None:
                await real_send(event_type, data)

                async def _later(event: dict) -> None:
                    await asyncio.sleep(0.02)
                    await client._handle_event(event)

                if event_type == "response.create":
                    asyncio.ensure_future(_later({"type": "response.created"}))
                elif event_type == "response.cancel":
                    asyncio.ensure_future(
                        _later(
                            {
                                "type": "response.done",
                                "response": {"status": "cancelled"},
                            }
                        )
                    )

            client._send_event = _provider  # type: ignore[method-assign]
            baseline = len(fake_ws.sent)
            assert await client.send_typed_message("first") is False
            assert await client.send_typed_message("second") is True
            assert [e["type"] for e in fake_ws.sent[baseline:]] == [
                "conversation.item.create",
                "response.create",
                "response.cancel",
                "conversation.item.create",
                "response.create",
            ]
            await client.disconnect()

    asyncio.run(_exercise())


def test_typed_message_clears_playback_right_before_requesting_the_reply() -> None:
    """The browser clear goes out after the old reply is cancelled and before
    response.create, so it can't clip the start of the new answer."""

    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            mp.setattr(
                "gptme_voice.realtime.openai_client._TYPED_CANCEL_WAIT_SECONDS", 0.2
            )
            client = OpenAIRealtimeClient(api_key="test-key")
            await client.connect()
            await client._handle_event({"type": "session.updated"})
            order: list[str] = []

            async def _clear() -> None:
                order.append("clear")

            real_send = client._send_event

            async def _record(event_type: str, data: dict) -> None:
                order.append(event_type)
                await real_send(event_type, data)

            client._send_event = _record  # type: ignore[method-assign]
            await client.send_typed_message("hi", before_reply=_clear)
            assert order == ["conversation.item.create", "clear", "response.create"]
            await client.disconnect()

    asyncio.run(_exercise())


def test_a_reply_starting_during_the_clear_is_cancelled_before_response_create() -> (
    None
):
    async def _exercise() -> None:
        fake_ws = _FakeWebSocket()

        async def _fake_connect(*_args, **_kwargs):
            return fake_ws

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "gptme_voice.realtime.openai_client.websockets.connect", _fake_connect
            )
            mp.setattr(
                "gptme_voice.realtime.openai_client._TYPED_CANCEL_WAIT_SECONDS", 0.3
            )
            client = OpenAIRealtimeClient(api_key="test-key")
            await client.connect()
            await client._handle_event({"type": "session.updated"})
            order: list[str] = []

            async def _clear() -> None:
                order.append("clear")
                # An injected lookup result starts a reply while we clear.
                await client._handle_event({"type": "response.created"})

                async def _confirm() -> None:
                    await asyncio.sleep(0.02)
                    await client._handle_event(
                        {"type": "response.done", "response": {"status": "cancelled"}}
                    )

                asyncio.ensure_future(_confirm())

            real_send = client._send_event

            async def _record(event_type: str, data: dict) -> None:
                order.append(event_type)
                await real_send(event_type, data)

            client._send_event = _record  # type: ignore[method-assign]
            await client.send_typed_message("hi", before_reply=_clear)
            assert order == [
                "conversation.item.create",
                "clear",
                "response.cancel",
                "response.create",
            ]
            await client.disconnect()

    asyncio.run(_exercise())
