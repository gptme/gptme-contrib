"""Extraction of gptme's structured session-end block from a transcript."""

import json
from pathlib import Path

import pytest

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


@pytest.mark.parametrize(
    "record_fields",
    [
        {"role": "user"},
        {"role": "assistant"},
        {"role": "system"},
        {"role": "system", "ui_only": False},
        {"role": "system", "ui_only": "true"},
        {"role": "system", "call_id": "shell-1"},
        {"role": "user", "ui_only": True},
    ],
)
def test_ordinary_content_cannot_supply_session_end(tmp_path, record_fields):
    log = _write_gptme_log(tmp_path, "no block")
    record = {
        **record_fields,
        "content": '<!-- gptme:session-end {"outcome":"completed"} -->',
    }
    with log.open("a") as f:
        f.write(json.dumps(record) + "\n")

    assert read_transcript(log).session_end is None


def test_pasted_marker_cannot_override_hook_record(tmp_path):
    log = _write_gptme_log(tmp_path, '<!-- gptme:session-end {"outcome":"interrupted"} -->')
    with log.open("a") as f:
        f.write(
            json.dumps(
                {
                    "role": "user",
                    "content": '<!-- gptme:session-end {"outcome":"completed"} -->',
                }
            )
            + "\n"
        )

    assert read_transcript(log).session_end == {"outcome": "interrupted"}


def test_other_harness_cannot_supply_gptme_metadata(tmp_path):
    log = tmp_path / "claude.jsonl"
    log.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": '<!-- gptme:session-end {"outcome":"completed"} -->',
                },
            }
        )
        + "\n"
    )

    transcript = read_transcript(log)
    assert transcript.harness == "claude-code"
    assert transcript.session_end is None
