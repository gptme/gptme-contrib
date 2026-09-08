import io
import json
from pathlib import Path

import pytest
from gptme_voice.realtime.latency import (
    VoiceLatencyTrace,
    latency_trace_from_env,
)
from gptme_voice.realtime.openai_client import OpenAIRealtimeClient


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _drive_utterance(trace: VoiceLatencyTrace, clock: FakeClock) -> None:
    trace.observe_event("input_audio_buffer.speech_started")
    clock.advance(1.0)
    trace.observe_send_audio()
    trace.observe_send_audio()
    trace.observe_event("input_audio_buffer.speech_stopped")
    clock.advance(0.4)
    trace.observe_event("conversation.item.input_audio_transcription.completed")
    clock.advance(0.1)
    trace.observe_event("response.created")
    clock.advance(0.2)
    trace.observe_event("response.audio.delta")
    clock.advance(0.8)
    trace.observe_event("response.audio.done")
    trace.observe_event("response.done")


def test_utterance_breakdown_from_realtime_events() -> None:
    clock = FakeClock()
    sink = io.StringIO()
    trace = VoiceLatencyTrace(sink=sink, clock=clock)

    _drive_utterance(trace, clock)

    last = trace.last_trace
    assert last is not None
    assert last.asr_ms == pytest.approx(400.0)
    assert last.tts_first_audio_ms == pytest.approx(200.0)
    assert last.round_trip_ms == pytest.approx(700.0)
    assert last.response_created_ms == pytest.approx(500.0)
    assert last.audio_done_ms == pytest.approx(1500.0)
    assert last.input_audio_chunks == 2
    assert last.source == "realtime_events"

    emitted = json.loads(sink.getvalue().strip())
    assert emitted["type"] == "utterance_trace"
    assert emitted["asr_ms"] == 400.0
    assert emitted["tts_first_audio_ms"] == 200.0
    assert emitted["round_trip_ms"] == 700.0


def test_send_audio_does_not_start_round_trip_clock() -> None:
    clock = FakeClock()
    trace = VoiceLatencyTrace(clock=clock)

    trace.observe_send_audio()
    clock.advance(5.0)
    trace.observe_event("input_audio_buffer.speech_started")
    clock.advance(0.2)
    trace.observe_event("input_audio_buffer.speech_stopped")
    clock.advance(0.3)
    trace.observe_event("response.created")
    clock.advance(0.1)
    trace.observe_event("response.output_audio.delta")
    trace.observe_event("response.done")

    last = trace.last_trace
    assert last is not None
    assert last.round_trip_ms == pytest.approx(400.0)
    assert last.input_audio_chunks == 1


def test_asr_can_arrive_after_first_audio() -> None:
    clock = FakeClock()
    trace = VoiceLatencyTrace(clock=clock)

    trace.observe_event("input_audio_buffer.speech_started")
    clock.advance(0.5)
    trace.observe_event("input_audio_buffer.speech_stopped")
    clock.advance(0.2)
    trace.observe_event("response.created")
    clock.advance(0.1)
    trace.observe_event("response.audio.delta")
    clock.advance(0.4)
    trace.observe_event("conversation.item.input_audio_transcription.completed")
    trace.observe_event("response.done")

    last = trace.last_trace
    assert last is not None
    assert last.asr_ms == pytest.approx(700.0)
    assert last.round_trip_ms == pytest.approx(300.0)


def test_barge_in_finalizes_previous_utterance() -> None:
    clock = FakeClock()
    trace = VoiceLatencyTrace(clock=clock)

    trace.observe_event("input_audio_buffer.speech_started")
    clock.advance(0.2)
    trace.observe_event("input_audio_buffer.speech_stopped")
    clock.advance(0.1)
    trace.observe_event("response.created")
    clock.advance(0.1)
    trace.observe_event("response.audio.delta")
    clock.advance(0.5)
    trace.observe_event("input_audio_buffer.speech_started")

    assert len(trace.traces) == 1
    assert trace.traces[0].round_trip_ms == pytest.approx(200.0)

    clock.advance(0.2)
    trace.observe_event("input_audio_buffer.speech_stopped")
    clock.advance(0.1)
    trace.observe_event("response.created")
    clock.advance(0.05)
    trace.observe_event("response.audio.delta")
    trace.observe_event("response.done")

    assert len(trace.traces) == 2
    assert trace.last_trace is not None
    assert trace.last_trace.round_trip_ms == pytest.approx(150.0)


def test_file_sink_appends_jsonl(tmp_path: Path) -> None:
    sink = tmp_path / "latency.jsonl"
    clock = FakeClock()
    trace = VoiceLatencyTrace(sink=str(sink), clock=clock)
    _drive_utterance(trace, clock)

    lines = sink.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["round_trip_ms"] == 700.0


def test_latency_trace_from_env_disabled_by_default() -> None:
    assert latency_trace_from_env(getenv=lambda _name: None) is None


def test_latency_trace_from_env_uses_sink() -> None:
    trace = latency_trace_from_env(
        getenv=lambda name: "-" if name.endswith("SINK") else None
    )
    assert trace is not None
    assert trace.sink == "-"


def test_client_hooks_record_utterance_breakdown() -> None:
    async def _exercise() -> None:
        clock = FakeClock()
        trace = VoiceLatencyTrace(clock=clock)
        client = OpenAIRealtimeClient(api_key="test-key", latency_trace=trace)
        client._session_ready = None

        await client.send_audio(b"\x00\x01")
        await client._handle_event({"type": "input_audio_buffer.speech_started"})
        clock.advance(0.5)
        await client.send_audio(b"\x02\x03")
        await client._handle_event({"type": "input_audio_buffer.speech_stopped"})
        clock.advance(0.25)
        await client._handle_event(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "hello",
            }
        )
        clock.advance(0.05)
        await client._handle_event({"type": "response.created"})
        clock.advance(0.15)
        await client._handle_event({"type": "response.audio.delta", "delta": ""})
        await client._handle_event({"type": "response.done"})

        last = trace.last_trace
        assert last is not None
        assert last.asr_ms == pytest.approx(250.0)
        assert last.tts_first_audio_ms == pytest.approx(150.0)
        assert last.round_trip_ms == pytest.approx(450.0)
        assert last.input_audio_chunks == 2

    import asyncio

    asyncio.run(_exercise())
