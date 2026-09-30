"""served_model: the model the provider reported serving, per harness.

``model`` on a session record is the requested model. ``served_model`` comes
from the provider response as the trajectory recorded it, so a silent
substitution (alias float, fallback, reroute) is visible downstream.
"""

from __future__ import annotations

import json
from pathlib import Path

from gptme_sessions.post_session import post_session
from gptme_sessions.record import SessionRecord
from gptme_sessions.signals import (
    extract_from_path,
    extract_usage_cc,
    extract_usage_gptme,
    extract_usage_pi,
)
from gptme_sessions.store import SessionStore


def _gptme_assistant(served: str | None, model: str = "openai-subscription/gpt-5.6-sol") -> dict:
    metadata: dict = {"model": model, "usage": {"input_tokens": 10, "output_tokens": 2}}
    if served is not None:
        metadata["served_model"] = served
    return {"role": "assistant", "content": "ok", "metadata": metadata}


def _cc_assistant(model: str, msg_id: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


class TestGptme:
    def test_served_model_recorded_even_when_equal(self) -> None:
        usage = extract_usage_gptme([_gptme_assistant("gpt-5.6-sol")])
        assert usage["model"] == "openai-subscription/gpt-5.6-sol"
        assert usage["served_model"] == "gpt-5.6-sol"
        assert "served_models" not in usage

    def test_absent_when_provider_did_not_report(self) -> None:
        usage = extract_usage_gptme([_gptme_assistant(None)])
        assert "served_model" not in usage

    def test_mixed_served_models_listed(self) -> None:
        usage = extract_usage_gptme(
            [
                _gptme_assistant("gpt-5.6-sol"),
                _gptme_assistant("gpt-5.6-luna"),
                _gptme_assistant("gpt-5.6-sol"),
            ]
        )
        assert usage["served_model"] == "gpt-5.6-sol"  # last seen
        assert usage["served_models"] == ["gpt-5.6-luna", "gpt-5.6-sol"]


class TestClaudeCode:
    def test_message_model_is_served(self) -> None:
        usage = extract_usage_cc(
            [
                _cc_assistant("claude-opus-5", "m1"),
                _cc_assistant("<synthetic>", "m2"),
            ]
        )
        assert usage["served_model"] == "claude-opus-5"
        assert "served_models" not in usage

    def test_subagent_models_do_not_leak_into_parent_served(self, tmp_path: Path) -> None:
        parent = tmp_path / "sess.jsonl"
        _write_jsonl(parent, [_cc_assistant("claude-opus-5", "p1")])
        sub_dir = tmp_path / "sess" / "subagents"
        sub_dir.mkdir(parents=True)
        _write_jsonl(sub_dir / "agent-a1.jsonl", [_cc_assistant("claude-haiku-4-5-20251001", "c1")])

        usage = extract_from_path(parent)["usage"]
        assert usage["served_model"] == "claude-opus-5"
        assert "served_models" not in usage


class TestPi:
    def _records(self, **message_extra: object) -> list[dict]:
        return [
            {
                "type": "session",
                "version": 3,
                "id": "pi-served",
                "timestamp": "2026-09-30T12:00:00Z",
                "cwd": "/workspace",
            },
            {
                "type": "message",
                "id": "root",
                "parentId": None,
                "timestamp": "2026-09-30T12:00:01Z",
                "message": {
                    "role": "assistant",
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-flash",
                    "content": "done",
                    "stopReason": "stop",
                    **message_extra,
                },
            },
        ]

    def test_response_model_is_served(self) -> None:
        usage = extract_usage_pi(self._records(responseModel="deepseek/deepseek-v4-flash-0731"))
        assert usage["model"] == "deepseek/deepseek-v4-flash"
        assert usage["served_model"] == "deepseek/deepseek-v4-flash-0731"

    def test_absent_response_model_is_not_reported(self) -> None:
        assert "served_model" not in extract_usage_pi(self._records())


def test_post_session_persists_served_model(tmp_path: Path) -> None:
    session_dir = tmp_path / "gptme-session"
    session_dir.mkdir()
    _write_jsonl(
        session_dir / "conversation.jsonl",
        [
            {"role": "user", "content": "hi"},
            _gptme_assistant("gpt-5.6-sol"),
            _gptme_assistant("gpt-5.6-luna"),
        ],
    )
    store = SessionStore(sessions_dir=tmp_path / "sessions")
    result = post_session(
        store=store,
        harness="gptme",
        model="gpt-5.6-sol",
        exit_code=0,
        duration_seconds=5,
        trajectory_path=session_dir / "conversation.jsonl",
    )
    assert result.served_model == "gpt-5.6-luna"
    assert result.record.served_model == "gpt-5.6-luna"
    assert result.record.served_models == ["gpt-5.6-luna", "gpt-5.6-sol"]
    assert result.record.model == "gpt-5.6-sol"

    persisted = store.load_all()[-1]
    assert persisted.served_model == "gpt-5.6-luna"
    assert persisted.served_models == ["gpt-5.6-luna", "gpt-5.6-sol"]


def test_record_round_trip_and_legacy_default() -> None:
    record = SessionRecord(harness="gptme", model="gpt-5.6-sol", served_model="gpt-5.6-sol")
    assert SessionRecord.from_dict(record.to_dict()).served_model == "gpt-5.6-sol"
    legacy = SessionRecord.from_dict({"harness": "gptme", "model": "gpt-5.6-sol"})
    assert legacy.served_model is None
    assert legacy.served_models is None
