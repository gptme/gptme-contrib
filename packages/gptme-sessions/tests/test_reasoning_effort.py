"""Reasoning-effort telemetry: record fields, per-harness extraction, CLI plumbing.

Background: ``post-session-grade.sh`` passed ``--reasoning-profile`` whenever a
run set ``GRADE_REASONING_PROFILE``; click rejected the unknown option, the
``|| true`` swallowed the error, and no session record was written at all.
These tests pin the flags, the record fields, and the "never lose a grade over
this field" contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from gptme_sessions.cli import cli
from gptme_sessions.post_session import (
    VALID_REASONING_PROFILES,
    check_reasoning_effort,
    post_session,
)
from gptme_sessions.record import (
    KNOWN_REASONING_EFFORTS,
    REASONING_PROFILES,
    SessionRecord,
    normalize_reasoning_effort,
)
from gptme_sessions.signals import (
    _combine_cc_usage,
    extract_from_path,
    extract_signals,
    extract_signals_cc,
    extract_signals_codex,
    extract_usage_cc,
    extract_usage_codex,
    extract_usage_gptme,
)
from gptme_sessions.store import SessionStore

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _cc_assistant(
    *, effort: str | None = "high", thinking: int | None = 285, ts: str = "2026-09-09T10:00:00.000Z"
) -> dict:
    usage: dict = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 200,
        "output_tokens": 50,
    }
    if thinking is not None:
        usage["output_tokens_details"] = {"thinking_tokens": thinking}
    record: dict = {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "model": "claude-fable-5-1",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "ok"}],
            "usage": usage,
        },
    }
    # ``effort`` is a sibling of ``message`` on real CC trajectories, not inside it.
    if effort is not None:
        record["effort"] = effort
    return record


def _codex_meta() -> dict:
    return {
        "timestamp": "2026-09-09T02:04:18.000Z",
        "type": "session_meta",
        "payload": {"originator": "codex_exec", "id": "abc"},
    }


def _codex_turn_context(effort: str | None = "ultra", *, mirror_only: bool = False) -> dict:
    payload: dict = {"model": "gpt-6-astra", "cwd": "/home/bob/bob"}
    if effort is not None:
        if not mirror_only:
            payload["effort"] = effort
        payload["collaboration_mode"] = {
            "mode": "default",
            "settings": {"model": "gpt-6-astra", "reasoning_effort": effort},
        }
    return {"timestamp": "2026-09-09T02:04:20.197Z", "type": "turn_context", "payload": payload}


def _codex_developer_message(passthrough: object) -> dict:
    return {
        "timestamp": "2026-09-09T02:04:20.194Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "instructions"}],
            "internal_chat_message_metadata_passthrough": passthrough,
        },
    }


def _codex_token_count(reasoning: int = 6415) -> dict:
    return {
        "timestamp": "2026-09-09T02:10:00.000Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {"input_tokens": 1000},
                "total_token_usage": {
                    "input_tokens": 3000,
                    "output_tokens": 400,
                    "cached_input_tokens": 2000,
                    "reasoning_output_tokens": reasoning,
                    "total_tokens": 3400,
                },
                "model_context_window": 400000,
            },
        },
    }


def _gptme_assistant(
    *,
    effort: str | None = "high",
    reasoning_tokens: int | None = 120,
    ts: str = "2026-09-09T10:00:00",
) -> dict:
    usage: dict = {
        "input_tokens": 500,
        "output_tokens": 80,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    if reasoning_tokens is not None:
        usage["reasoning_tokens"] = reasoning_tokens
    metadata: dict = {"model": "anthropic/claude-opus-4-6", "usage": usage}
    if effort is not None:
        metadata["reasoning_effort"] = effort
    return {"role": "assistant", "content": "hello", "timestamp": ts, "metadata": metadata}


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


# ---------------------------------------------------------------------------
# SessionRecord
# ---------------------------------------------------------------------------


class TestSessionRecordFields:
    def test_defaults_are_none(self) -> None:
        r = SessionRecord()
        assert r.reasoning_effort is None
        assert r.reasoning_profile is None
        assert r.reasoning_tokens is None

    def test_round_trip(self) -> None:
        r = SessionRecord(reasoning_effort="High", reasoning_profile="deep", reasoning_tokens=42)
        d = r.to_dict()
        assert d["reasoning_effort"] == "high"
        assert d["reasoning_profile"] == "deep"
        assert d["reasoning_tokens"] == 42
        back = SessionRecord.from_dict(json.loads(json.dumps(d)))
        assert back.reasoning_effort == "high"
        assert back.reasoning_profile == "deep"
        assert back.reasoning_tokens == 42

    def test_effort_is_free_form_but_lowercased(self) -> None:
        # A backend level we have never seen must survive — never be dropped.
        r = SessionRecord(reasoning_effort="  Galactic ")
        assert r.reasoning_effort == "galactic"
        assert SessionRecord(reasoning_effort="").reasoning_effort is None
        assert SessionRecord(reasoning_effort=3).reasoning_effort is None  # type: ignore[arg-type]

    def test_profile_is_a_closed_set(self) -> None:
        assert SessionRecord(reasoning_profile="Routine").reasoning_profile == "routine"
        assert SessionRecord(reasoning_profile="turbo").reasoning_profile is None
        assert REASONING_PROFILES == {"routine", "default", "deep"}
        assert VALID_REASONING_PROFILES == REASONING_PROFILES

    def test_reasoning_tokens_rejects_non_int(self) -> None:
        assert SessionRecord(reasoning_tokens=True).reasoning_tokens is None  # type: ignore[arg-type]
        assert SessionRecord(reasoning_tokens="12").reasoning_tokens is None  # type: ignore[arg-type]

    def test_old_records_without_fields_still_load(self) -> None:
        legacy = {"session_id": "abcd1234", "harness": "claude-code", "model": "opus"}
        r = SessionRecord.from_dict(legacy)
        assert r.reasoning_effort is None
        assert r.reasoning_profile is None
        assert "reasoning_effort" in r.to_dict()

    def test_store_persists_fields(self, tmp_path: Path) -> None:
        store = SessionStore(sessions_dir=tmp_path)
        store.append(SessionRecord(reasoning_effort="xhigh", reasoning_profile="routine"))
        loaded = SessionStore(sessions_dir=tmp_path).load_all()
        assert loaded[0].reasoning_effort == "xhigh"
        assert loaded[0].reasoning_profile == "routine"


def test_normalize_reasoning_effort() -> None:
    assert normalize_reasoning_effort(" XHigh ") == "xhigh"
    assert normalize_reasoning_effort("") is None
    assert normalize_reasoning_effort(None) is None
    assert normalize_reasoning_effort(["high"]) is None


def test_check_reasoning_effort_warns_but_never_raises() -> None:
    assert check_reasoning_effort("claude-code", None) is None
    assert check_reasoning_effort("claude-code", "high") is None
    warning = check_reasoning_effort("claude-code", "ultra")
    assert warning is not None and "ultra" in warning and "claude-code" in warning
    # Harnesses without a documented set are silently accepted.
    assert check_reasoning_effort("copilot-cli", "anything") is None
    assert "ultra" in KNOWN_REASONING_EFFORTS["codex"]


# ---------------------------------------------------------------------------
# Claude Code extractor
# ---------------------------------------------------------------------------


class TestClaudeCodeExtraction:
    def test_signals_read_top_level_effort_and_sum_thinking(self) -> None:
        msgs = [
            _cc_assistant(effort="high", thinking=285),
            _cc_assistant(effort="high", thinking=15, ts="2026-09-09T10:01:00.000Z"),
            _cc_assistant(effort="max", thinking=None, ts="2026-09-09T10:02:00.000Z"),
        ]
        signals = extract_signals_cc(msgs)
        assert signals["reasoning_effort"] == "high"
        assert signals["thinking_tokens"] == 300

    def test_effort_inside_message_is_not_the_contract(self) -> None:
        # Guard against reading a hypothetical ``message.effort`` — the real
        # field is top-level. A record with neither yields None, not a guess.
        rec = _cc_assistant(effort=None, thinking=None)
        rec["message"]["effort"] = "low"
        signals = extract_signals_cc([rec])
        assert signals["reasoning_effort"] is None
        assert signals["thinking_tokens"] == 0

    def test_tie_resolves_to_first_seen(self) -> None:
        msgs = [
            _cc_assistant(effort="low"),
            _cc_assistant(effort="max", ts="2026-09-09T10:01:00.000Z"),
        ]
        assert extract_signals_cc(msgs)["reasoning_effort"] == "low"

    def test_usage_carries_effort_and_reasoning_tokens(self) -> None:
        usage = extract_usage_cc([_cc_assistant(effort="High", thinking=285)])
        assert usage["reasoning_effort"] == "high"
        assert usage["reasoning_tokens"] == 285

    def test_usage_omits_reasoning_keys_when_absent(self) -> None:
        usage = extract_usage_cc([_cc_assistant(effort=None, thinking=None)])
        assert "reasoning_effort" not in usage
        assert "reasoning_tokens" not in usage

    def test_combine_cc_usage_sums_tokens_and_prefers_parent_effort(self) -> None:
        parent = {"input_tokens": 1, "reasoning_effort": "high", "reasoning_tokens": 10}
        child = {"input_tokens": 1, "reasoning_effort": "low", "reasoning_tokens": 5}
        combined = _combine_cc_usage([parent, child])
        assert combined["reasoning_effort"] == "high"
        assert combined["reasoning_tokens"] == 15
        # None stays None when no part observed thinking tokens.
        assert "reasoning_tokens" not in _combine_cc_usage([{"input_tokens": 1}])

    def test_extract_from_path_end_to_end(self, tmp_path: Path) -> None:
        traj = _write_jsonl(tmp_path / "cc.jsonl", [_cc_assistant(effort="high", thinking=285)])
        result = extract_from_path(traj)
        assert result["format"] == "claude_code"
        assert result["reasoning_effort"] == "high"
        assert result["usage"]["reasoning_effort"] == "high"
        assert result["usage"]["reasoning_tokens"] == 285


# ---------------------------------------------------------------------------
# Codex extractor
# ---------------------------------------------------------------------------


class TestCodexExtraction:
    def test_turn_context_effort(self) -> None:
        msgs = [_codex_meta(), _codex_turn_context("ultra"), _codex_token_count(6415)]
        assert extract_signals_codex(msgs)["reasoning_effort"] == "ultra"
        usage = extract_usage_codex(msgs)
        assert usage["reasoning_effort"] == "ultra"
        assert usage["reasoning_tokens"] == 6415
        assert usage["model"] == "gpt-6-astra"

    def test_collaboration_mode_mirror_when_top_level_effort_missing(self) -> None:
        msgs = [_codex_meta(), _codex_turn_context("xhigh", mirror_only=True)]
        assert extract_signals_codex(msgs)["reasoning_effort"] == "xhigh"

    def test_passthrough_json_in_string(self) -> None:
        passthrough = json.dumps(
            {
                "turn_id": "t1",
                "collaboration_mode": {"settings": {"reasoning_effort": "high"}},
            }
        )
        msgs = [_codex_meta(), _codex_developer_message(passthrough), _codex_turn_context(None)]
        assert extract_signals_codex(msgs)["reasoning_effort"] == "high"
        assert extract_usage_codex(msgs)["reasoning_effort"] == "high"

    def test_passthrough_escaped_string_falls_back_to_regex(self) -> None:
        # Doubly-encoded: the string is not valid JSON on its own, but the
        # escaped key/value pair is still visible to the regex.
        passthrough = 'prefix {\\"reasoning_effort\\":\\"medium\\",\\"x\\":1} suffix'
        msgs = [_codex_meta(), _codex_developer_message(passthrough)]
        assert extract_signals_codex(msgs)["reasoning_effort"] == "medium"

    def test_passthrough_dict(self) -> None:
        msgs = [
            _codex_meta(),
            _codex_developer_message({"turn_id": "t1", "reasoning_effort": "Low"}),
        ]
        assert extract_signals_codex(msgs)["reasoning_effort"] == "low"

    def test_no_effort_anywhere(self) -> None:
        msgs = [
            _codex_meta(),
            _codex_turn_context(None),
            _codex_developer_message({"turn_id": "t"}),
        ]
        assert extract_signals_codex(msgs)["reasoning_effort"] is None
        assert "reasoning_effort" not in extract_usage_codex(msgs)

    def test_extract_from_path_end_to_end(self, tmp_path: Path) -> None:
        traj = _write_jsonl(
            tmp_path / "rollout.jsonl",
            [_codex_meta(), _codex_turn_context("ultra"), _codex_token_count(77)],
        )
        result = extract_from_path(traj)
        assert result["format"] == "codex"
        assert result["reasoning_effort"] == "ultra"
        assert result["usage"]["reasoning_tokens"] == 77


# ---------------------------------------------------------------------------
# gptme extractor
# ---------------------------------------------------------------------------


class TestGptmeExtraction:
    def test_metadata_effort_and_usage_reasoning_tokens(self) -> None:
        msgs = [
            {"role": "user", "content": "hi", "timestamp": "2026-09-09T09:59:00"},
            _gptme_assistant(effort="high", reasoning_tokens=120),
            _gptme_assistant(effort="high", reasoning_tokens=30, ts="2026-09-09T10:01:00"),
        ]
        assert extract_signals(msgs)["reasoning_effort"] == "high"
        usage = extract_usage_gptme(msgs)
        assert usage["reasoning_effort"] == "high"
        assert usage["reasoning_tokens"] == 150

    def test_tolerates_absence(self) -> None:
        msgs = [
            {"role": "user", "content": "hi", "timestamp": "2026-09-09T09:59:00"},
            _gptme_assistant(effort=None, reasoning_tokens=None),
        ]
        assert extract_signals(msgs)["reasoning_effort"] is None
        usage = extract_usage_gptme(msgs)
        assert "reasoning_effort" not in usage
        assert "reasoning_tokens" not in usage
        assert usage["input_tokens"] == 500

    def test_legacy_flat_metadata_still_reads_reasoning_tokens(self) -> None:
        msg = {
            "role": "assistant",
            "content": "x",
            "timestamp": "2026-09-09T10:00:00",
            "metadata": {"input_tokens": 10, "output_tokens": 5, "reasoning_tokens": 3},
        }
        assert extract_usage_gptme([msg])["reasoning_tokens"] == 3


# ---------------------------------------------------------------------------
# post_session plumbing
# ---------------------------------------------------------------------------


class TestPostSession:
    def test_fills_effort_from_cc_trajectory(self, tmp_path: Path) -> None:
        traj = _write_jsonl(tmp_path / "cc.jsonl", [_cc_assistant(effort="high", thinking=285)])
        store = SessionStore(sessions_dir=tmp_path / "sessions")
        result = post_session(
            store=store, harness="claude-code", model="unknown", trajectory_path=traj
        )
        assert result.reasoning_effort == "high"
        assert result.record.reasoning_effort == "high"
        assert result.reasoning_tokens == 285
        assert result.record.reasoning_tokens == 285
        assert result.record.reasoning_profile is None
        persisted = SessionStore(sessions_dir=tmp_path / "sessions").load_all()
        assert persisted[0].reasoning_effort == "high"
        assert persisted[0].reasoning_tokens == 285

    def test_fills_effort_from_codex_trajectory(self, tmp_path: Path) -> None:
        traj = _write_jsonl(
            tmp_path / "rollout.jsonl",
            [_codex_meta(), _codex_turn_context("ultra"), _codex_token_count(6415)],
        )
        store = SessionStore(sessions_dir=tmp_path / "sessions")
        result = post_session(store=store, harness="codex", model="unknown", trajectory_path=traj)
        assert result.record.reasoning_effort == "ultra"
        assert result.record.reasoning_tokens == 6415

    def test_caller_effort_wins_over_trajectory(self, tmp_path: Path) -> None:
        traj = _write_jsonl(tmp_path / "cc.jsonl", [_cc_assistant(effort="high")])
        store = SessionStore(sessions_dir=tmp_path / "sessions")
        result = post_session(
            store=store,
            harness="claude-code",
            model="unknown",
            trajectory_path=traj,
            reasoning_effort="MAX",
            reasoning_profile="deep",
        )
        assert result.record.reasoning_effort == "max"
        assert result.record.reasoning_profile == "deep"

    def test_unknown_effort_is_kept_with_warning(self, tmp_path: Path, caplog) -> None:
        store = SessionStore(sessions_dir=tmp_path)
        with caplog.at_level("WARNING", logger="gptme_sessions.post_session"):
            result = post_session(
                store=store, harness="claude-code", model="unknown", reasoning_effort="galactic"
            )
        assert result.record.reasoning_effort == "galactic"
        assert any("galactic" in rec.message for rec in caplog.records)
        # The grade/record must never be lost because of this field.
        assert len(store.load_all()) == 1

    def test_invalid_profile_raises(self, tmp_path: Path) -> None:
        store = SessionStore(sessions_dir=tmp_path)
        with pytest.raises(ValueError, match="reasoning_profile"):
            post_session(store=store, harness="gptme", model="x", reasoning_profile="turbo")

    def test_no_trajectory_no_fields(self, tmp_path: Path) -> None:
        store = SessionStore(sessions_dir=tmp_path)
        result = post_session(store=store, harness="gptme", model="x")
        assert result.record.reasoning_effort is None
        assert result.record.reasoning_tokens is None
        assert result.reasoning_profile is None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _invoke(args: list[str], sessions_dir: Path):
    return CliRunner().invoke(cli, ["--sessions-dir", str(sessions_dir), *args])


class TestPostSessionCli:
    def test_flags_land_on_record(self, tmp_path: Path) -> None:
        # This exact invocation shape (``--reasoning-profile`` present) used to
        # die with "No such option" and silently drop the whole record.
        res = _invoke(
            [
                "post-session",
                "--harness",
                "claude-code",
                "--model",
                "claude-fable-5-1",
                "--run-type",
                "autonomous",
                "--reasoning-profile",
                "deep",
                "--reasoning-effort",
                "High",
                "--json",
            ],
            tmp_path,
        )
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output.strip().splitlines()[-1])
        assert payload["reasoning_effort"] == "high"
        assert payload["reasoning_profile"] == "deep"
        assert payload["reasoning_tokens"] is None
        records = SessionStore(sessions_dir=tmp_path).load_all()
        assert records[0].reasoning_effort == "high"
        assert records[0].reasoning_profile == "deep"

    def test_profile_is_validated(self, tmp_path: Path) -> None:
        res = _invoke(
            ["post-session", "--harness", "gptme", "--reasoning-profile", "turbo"], tmp_path
        )
        assert res.exit_code != 0
        assert "Invalid value for '--reasoning-profile'" in res.output

    def test_unknown_effort_warns_but_records(self, tmp_path: Path) -> None:
        res = _invoke(
            ["post-session", "--harness", "codex", "--reasoning-effort", "galactic"],
            tmp_path,
        )
        assert res.exit_code == 0, res.output
        assert "galactic" in res.output and "not a documented level" in res.output
        assert SessionStore(sessions_dir=tmp_path).load_all()[0].reasoning_effort == "galactic"

    def test_effort_filled_from_trajectory_when_flag_omitted(self, tmp_path: Path) -> None:
        traj = _write_jsonl(
            tmp_path / "rollout.jsonl",
            [_codex_meta(), _codex_turn_context("ultra"), _codex_token_count(9)],
        )
        sessions = tmp_path / "sessions"
        res = _invoke(
            [
                "post-session",
                "--harness",
                "codex",
                "--trajectory",
                str(traj),
                "--reasoning-profile",
                "routine",
                "--json",
            ],
            sessions,
        )
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output.strip().splitlines()[-1])
        assert payload["reasoning_effort"] == "ultra"
        assert payload["reasoning_profile"] == "routine"
        assert payload["reasoning_tokens"] == 9
