"""Utterance-level latency tracing for gptme-voice realtime sessions.

Measures the three Phase 2 stages from OpenAI/xAI realtime events:

- ASR: ``speech_stopped`` → user transcript completed
- TTS first audio: ``response.created`` → first ``response.audio.delta``
- Round-trip: ``speech_stopped`` → first audio chunk

``send_audio`` is instrumented as an input-chunk counter, not a turn clock.
Twilio and the local client stream PCM continuously, so using those frames as
t0 would report ~20ms of "latency". Server VAD events are the turn boundary.

Set ``GPTME_VOICE_LATENCY_SINK`` to a file path or ``-`` (stdout) to emit
JSONL ``utterance_trace`` events from a live call.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, TextIO

_SPEECH_STARTED = "input_audio_buffer.speech_started"
_SPEECH_STOPPED = "input_audio_buffer.speech_stopped"
_ASR_DONE = "conversation.item.input_audio_transcription.completed"
_RESPONSE_CREATED = "response.created"
_AUDIO_DELTA = frozenset({"response.audio.delta", "response.output_audio.delta"})
_AUDIO_DONE = frozenset({"response.audio.done", "response.output_audio.done"})
_RESPONSE_DONE = "response.done"


def _round_ms(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 2)


@dataclass
class UtteranceTrace:
    """Timing breakdown for one user utterance → agent reply."""

    asr_ms: float | None = None
    tts_first_audio_ms: float | None = None
    round_trip_ms: float | None = None
    response_created_ms: float | None = None
    audio_done_ms: float | None = None
    input_audio_chunks: int = 0
    source: str = "realtime_events"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "utterance_trace",
            "asr_ms": _round_ms(self.asr_ms),
            "tts_first_audio_ms": _round_ms(self.tts_first_audio_ms),
            "round_trip_ms": _round_ms(self.round_trip_ms),
            "response_created_ms": _round_ms(self.response_created_ms),
            "audio_done_ms": _round_ms(self.audio_done_ms),
            "input_audio_chunks": self.input_audio_chunks,
            "source": self.source,
        }

    def to_jsonl(self) -> str:
        return json.dumps(self.to_dict())


class VoiceLatencyTrace:
    """Collects per-utterance latency from realtime client hooks/events.

    Usage::

        trace = VoiceLatencyTrace(sink="voice-latency.jsonl")
        client = OpenAIRealtimeClient(..., latency_trace=trace)
        ...
        print(trace.last_trace.to_jsonl())
    """

    def __init__(
        self,
        sink: str | TextIO | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.sink = sink
        self._clock = clock or time.perf_counter
        self.traces: list[UtteranceTrace] = []
        self._reset_open()

    def _now(self) -> float:
        return self._clock()

    def _reset_open(self) -> None:
        self._t_speech_started: float | None = None
        self._t_speech_stopped: float | None = None
        self._t_asr_done: float | None = None
        self._t_response_created: float | None = None
        self._t_first_audio: float | None = None
        self._t_audio_done: float | None = None
        self._input_chunks = 0

    def _turn_anchor(self) -> float | None:
        """User-finished-speaking time, falling back to speech start."""
        if self._t_speech_stopped is not None:
            return self._t_speech_stopped
        return self._t_speech_started

    def observe_send_audio(self) -> None:
        """Hook for ``OpenAIRealtimeClient.send_audio``.

        Counts inbound PCM chunks. Does not start the round-trip clock —
        continuous media frames are not utterance boundaries.
        """
        self._input_chunks += 1

    def observe_event(self, event_type: str) -> None:
        """Hook for ``OpenAIRealtimeClient._handle_event``."""
        now = self._now()
        if event_type == _SPEECH_STARTED:
            if self._t_first_audio is not None:
                self._finalize()
            elif self._t_speech_started is not None:
                self._reset_open()
            self._t_speech_started = now
            return
        if event_type == _SPEECH_STOPPED:
            self._t_speech_stopped = now
            return
        if event_type == _ASR_DONE:
            self._t_asr_done = now
            return
        if event_type == _RESPONSE_CREATED:
            self._t_response_created = now
            return
        if event_type in _AUDIO_DELTA:
            if self._t_first_audio is None:
                self._t_first_audio = now
            return
        if event_type in _AUDIO_DONE:
            self._t_audio_done = now
            return
        if event_type == _RESPONSE_DONE:
            self._finalize()

    def _finalize(self) -> None:
        anchor = self._turn_anchor()
        if anchor is None and self._t_first_audio is None and self._t_asr_done is None:
            self._reset_open()
            return

        asr_anchor = (
            self._t_speech_stopped
            if self._t_speech_stopped is not None
            else self._t_speech_started
        )
        trace = UtteranceTrace(input_audio_chunks=self._input_chunks)
        if self._t_asr_done is not None and asr_anchor is not None:
            trace.asr_ms = (self._t_asr_done - asr_anchor) * 1000
        if self._t_first_audio is not None and self._t_response_created is not None:
            trace.tts_first_audio_ms = (
                self._t_first_audio - self._t_response_created
            ) * 1000
        if self._t_first_audio is not None and anchor is not None:
            trace.round_trip_ms = (self._t_first_audio - anchor) * 1000
        if self._t_response_created is not None and anchor is not None:
            trace.response_created_ms = (self._t_response_created - anchor) * 1000
        if self._t_audio_done is not None and anchor is not None:
            trace.audio_done_ms = (self._t_audio_done - anchor) * 1000

        if any(
            value is not None
            for value in (trace.asr_ms, trace.tts_first_audio_ms, trace.round_trip_ms)
        ):
            self.traces.append(trace)
            self._emit(trace)
        self._reset_open()

    def _emit(self, trace: UtteranceTrace) -> None:
        if self.sink is None:
            return
        line = trace.to_jsonl() + "\n"
        if isinstance(self.sink, str):
            if self.sink == "-":
                sys.stdout.write(line)
                sys.stdout.flush()
            else:
                with open(self.sink, "a", encoding="utf-8") as handle:
                    handle.write(line)
        else:
            self.sink.write(line)
            self.sink.flush()

    @property
    def last_trace(self) -> UtteranceTrace | None:
        return self.traces[-1] if self.traces else None


def latency_trace_from_env(
    getenv: Callable[[str], str | None] | None = None,
) -> VoiceLatencyTrace | None:
    """Build a sink-enabled tracer from ``GPTME_VOICE_LATENCY_SINK``."""
    import os

    resolver = getenv or (lambda name: os.environ.get(name))
    sink = resolver("GPTME_VOICE_LATENCY_SINK")
    if not sink:
        return None
    return VoiceLatencyTrace(sink=sink)
