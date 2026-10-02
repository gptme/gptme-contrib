from gptme_voice.realtime.probe import _compare, _summarize_session


def test_summarize_session_reads_ga_shape() -> None:
    summary = _summarize_session(
        {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": "be bob",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "turn_detection": {"type": "server_vad"},
                    "transcription": {"model": "whisper-1"},
                },
                "output": {"voice": "cedar", "speed": 1.0},
            },
            "tools": [{"type": "function", "name": "hangup"}],
        }
    )
    assert summary["type"] == "realtime"
    assert summary["voice"] == "cedar"
    assert summary["speed"] == 1.0
    assert summary["turn_detection"] == "server_vad"
    assert summary["transcription"] == {"model": "whisper-1"}
    assert summary["instructions_chars"] == 6
    assert summary["tools"] == ["hangup"]


def test_summarize_session_reads_legacy_shape() -> None:
    summary = _summarize_session(
        {
            "modalities": ["text", "audio"],
            "voice": "rex",
            "input_audio_format": "pcm16",
            "output": {"speed": 1.2},
            "turn_detection": {"type": "server_vad"},
            "tools": [{"type": "function", "function": {"name": "hangup"}}],
        }
    )
    assert summary["output_modalities"] == ["text", "audio"]
    assert summary["voice"] == "rex"
    assert summary["input_format"] == "pcm16"
    assert summary["speed"] == 1.2
    assert summary["tools"] == ["hangup"]


def test_summarize_session_tolerates_null_audio() -> None:
    assert _summarize_session({"audio": None, "voice": "rex"}) == {"voice": "rex"}


def test_compare_flags_unapplied_voice_and_dropped_instructions() -> None:
    requested = {"voice": "cedar", "instructions_chars": 4000, "tools": ["hangup"]}
    applied = {"voice": "marin", "instructions_chars": 0, "tools": ["hangup"]}
    mismatches = _compare(requested, applied)
    assert any(m.startswith("voice:") for m in mismatches)
    assert any(m.startswith("instructions:") for m in mismatches)
    assert not any(m.startswith("tools:") for m in mismatches)


def test_compare_ignores_fields_the_provider_does_not_echo() -> None:
    assert _compare({"voice": "rex", "instructions_chars": 10}, {}) == []
