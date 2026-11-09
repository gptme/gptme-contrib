"""Extraction of gptme's structured session-end block from a transcript."""

import json
from pathlib import Path

from gptme_sessions.transcript import read_transcript


def _write_gptme_log(path: Path, tail: str) -> Path:
    records = [
        {
            "role": "user",
            "content": "do the thing",
            "timestamp": "2026-11-09T10:00:00+00:00",
        },
        {
            "role": "assistant",
            "content": "done",
            "timestamp": "2026-11-09T10:00:05+00:00",
            "metadata": {"usage": {"input_tokens": 350, "output_tokens": 100}},
        },
        {
            "role": "system",
            "content": tail,
            "timestamp": "2026-11-09T10:00:42+00:00",
            "ui_only": True,
        },
    ]
    log = path / "conversation.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return log


def test_extracts_session_end_block(tmp_path):
    payload = {
        "outcome": "completed",
        "policy_blocks": 0,
        "input_tokens": 350,
        "output_tokens": 100,
        "wall_seconds": 42,
    }
    log = _write_gptme_log(tmp_path, f"<!-- gptme:session-end {json.dumps(payload)} -->")

    transcript = read_transcript(log)

    assert transcript.session_end == payload
    assert transcript.to_dict()["session_end"] == payload


def test_missing_block_is_none(tmp_path):
    log = _write_gptme_log(tmp_path, "no structured block here")

    assert read_transcript(log).session_end is None


def test_malformed_block_is_ignored(tmp_path):
    """A truncated tail block must not make the transcript unreadable."""
    log = _write_gptme_log(tmp_path, '<!-- gptme:session-end {"outcome": -->')

    transcript = read_transcript(log)

    assert transcript.session_end is None
    assert transcript.messages
