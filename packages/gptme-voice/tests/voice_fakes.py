"""Fakes shared by the voice server test modules."""

import json


class _DummyBrowserWebSocket:
    def __init__(self, incoming: list[dict[str, object]]) -> None:
        self.query_params: dict[str, str] = {}
        self._incoming = incoming
        self.accepted = False
        self.text_messages: list[str] = []
        self.binary_messages: list[bytes] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive(self) -> dict[str, object]:
        if self._incoming:
            return self._incoming.pop(0)
        return {"type": "websocket.disconnect"}

    async def send_text(self, message: str) -> None:
        self.text_messages.append(message)

    async def send_bytes(self, message: bytes) -> None:
        self.binary_messages.append(message)


class _PeerWebSocket:
    def __init__(
        self, host: str | None, incoming: list[dict[str, object]] | None = None
    ) -> None:
        self.client = None if host is None else (host, 12345)
        self.query_params: dict[str, str] = {}
        self._incoming = [json.dumps(message) for message in incoming or []]
        self.accepted = False
        self.close_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.close_code = code

    def iter_text(self):
        async def _gen():
            for message in self._incoming:
                yield message

        return _gen()


class _FakeRealtimeClient:
    def __init__(self) -> None:
        self.sent_audio: list[bytes] = []
        self.sent_text: list[str] = []
        self.commit_count = 0
        self.disconnect_kwargs: dict[str, object] | None = None
        self.activate_session_count = 0
        self.on_function_call = None
        self.sent_typed: list[str] = []
        self.typed_interrupts = False
        self.typed_error: Exception | None = None

    async def connect(self) -> None:
        return None

    async def activate_session(self) -> None:
        self.activate_session_count += 1

    async def send_audio(self, audio_data: bytes) -> None:
        self.sent_audio.append(audio_data)

    async def commit_audio(self) -> None:
        self.commit_count += 1

    async def disconnect(self, **kwargs: object) -> None:
        self.disconnect_kwargs = kwargs

    async def inject_message(self, _text: str) -> None:
        return None

    async def send_text_message(self, text: str) -> None:
        self.sent_text.append(text)

    async def send_typed_message(
        self, text: str, before_reply=None, on_delivered=None
    ) -> bool:
        if self.typed_error is not None:  # one-shot, before delivery
            error, self.typed_error = self.typed_error, None
            raise error
        self.sent_typed.append(text)
        if on_delivered is not None:
            on_delivered()
        if before_reply is not None:
            await before_reply()
        return self.typed_interrupts


class _DummyToolBridge:
    def __init__(self, *args, **kwargs) -> None:
        return None

    async def handle_function_call(self, _name: str, _args: dict) -> None:
        return None

    def pending_task_ids(self) -> list[str]:
        return []

    def get_timings(self) -> list[dict[str, object]]:
        return []


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def __aiter__(self) -> "_FakeWebSocket":
        return self

    async def __anext__(self) -> str:
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True
