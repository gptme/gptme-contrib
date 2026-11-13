"""A caller's native Claude UUID, not a log filename, owns extracted evidence."""

import json
from pathlib import Path

from gptme_sessions.post_session import post_session
from gptme_sessions.store import SessionStore

EXPECTED = "2821c3d3-f167-4c25-a80e-c17254299e64"
FOREIGN = "95c51db5-168f-4334-a45d-6c3ffee0be08"


def stream(path: Path, session_id: str) -> Path:
    rows = [
        {"type": "system", "subtype": "init", "session_id": session_id, "model": "claude-opus-5-5"},
        {
            "type": "assistant",
            "session_id": session_id,
            "message": {
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        },
        {
            "type": "result",
            "session_id": session_id,
            "num_turns": 1,
            "duration_ms": 2000,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def test_post_session_rejects_foreign_claude_stream(tmp_path, monkeypatch):
    monkeypatch.delenv("CC_SESSION_ID", raising=False)
    path = stream(tmp_path / "foreign.log", FOREIGN)
    result = post_session(
        store=SessionStore(sessions_dir=tmp_path / "records"),
        harness="claude-code",
        model="claude-sonnet-5-5",
        session_id="outer-run",
        harness_session_id=EXPECTED,
        trajectory_path=path,
        exit_code=1,
        failure_reason="local_predispatch_failure",
        duration_seconds=87,
    )
    assert result.record.served_model is None
    assert result.record.token_count is None
    assert result.signals is None
    assert result.record.trajectory_path is None
    assert result.record.cc_session_uuid == EXPECTED
    assert result.record.trajectory_rejection["path"] == str(path)
    assert result.record.trajectory_rejection["observed_session_ids"] == [FOREIGN]
    assert result.record.failure_reason == "local_predispatch_failure"


def test_post_session_accepts_matching_claude_stream(tmp_path, monkeypatch):
    monkeypatch.delenv("CC_SESSION_ID", raising=False)
    result = post_session(
        store=SessionStore(sessions_dir=tmp_path / "records"),
        harness="claude-code",
        model="claude-opus-5-5",
        harness_session_id=EXPECTED,
        trajectory_path=stream(tmp_path / "matching.log", EXPECTED),
        duration_seconds=2,
    )
    assert result.record.served_model == "claude-opus-5-5"
    assert result.record.token_count == 120
    assert result.record.trajectory_rejection is None


def test_regrade_does_not_consume_foreign_stream(tmp_path):
    from click.testing import CliRunner
    from gptme_sessions.cli import cli
    from gptme_sessions.record import SessionRecord

    store = SessionStore(sessions_dir=tmp_path / "records")
    record = SessionRecord.from_dict(
        {
            "session_id": "outer-run",
            "harness": "claude-code",
            "model": "claude-sonnet-5-5",
            "cc_session_uuid": EXPECTED,
            "trajectory_path": str(stream(tmp_path / "foreign.log", FOREIGN)),
            "outcome": "failed",
            "failure_reason": "local_predispatch_failure",
        }
    )
    store.append(record)
    result = CliRunner().invoke(
        cli, ["--sessions-dir", str(store.sessions_dir), "regrade", "--outcome", "all"]
    )
    assert result.exit_code == 0, result.output
    refreshed = store.load_all()[0]
    assert refreshed.served_model is None
    assert refreshed.token_count is None
    assert "identity mismatch" in result.output


def test_sync_does_not_consume_foreign_stream(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from gptme_sessions.cli import cli
    from gptme_sessions.record import SessionRecord

    path = stream(tmp_path / "foreign.log", FOREIGN)
    store = SessionStore(sessions_dir=tmp_path / "records")
    store.append(
        SessionRecord.from_dict(
            {
                "session_id": "outer-run",
                "harness": "claude-code",
                "model": "claude-sonnet-5-5",
                "cc_session_uuid": EXPECTED,
                "trajectory_path": str(path),
                "outcome": "unknown",
            }
        )
    )
    monkeypatch.setattr(
        "gptme_sessions.cli._discover_all",
        lambda **_: [
            {
                "path": str(path),
                "harness": "claude-code",
                "model": "claude-opus-5-5",
            }
        ],
    )
    result = CliRunner().invoke(
        cli, ["--sessions-dir", str(store.sessions_dir), "sync", "--signals"]
    )
    assert result.exit_code == 0, result.output
    refreshed = store.load_all()[0]
    assert refreshed.served_model is None
    assert refreshed.token_count is None
    assert "identity mismatch" in result.output


def test_native_persistence_uuid_and_missing_or_mixed_ids(tmp_path):
    import pytest
    from gptme_sessions.trajectory_identity import (
        TrajectoryIdentityError,
        validate_claude_trajectory,
    )

    path = tmp_path / "native.jsonl"
    path.write_text(json.dumps({"type": "assistant", "sessionId": EXPECTED}) + "\n")
    validate_claude_trajectory(path, EXPECTED)
    for rows in ([], [{"session_id": FOREIGN}], [{"sessionId": EXPECTED}, {"session_id": FOREIGN}]):
        path.write_text("\n".join(json.dumps(row) for row in rows))
        with pytest.raises(TrajectoryIdentityError):
            validate_claude_trajectory(path, EXPECTED)
    # Compatibility: a legacy caller with no native identity cannot validate it.
    validate_claude_trajectory(path, None)


def test_environment_native_uuid_is_persisted_and_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_SESSION_ID", EXPECTED)
    result = post_session(
        store=SessionStore(sessions_dir=tmp_path / "records"),
        harness="claude-code",
        model="claude-sonnet-5-5",
        trajectory_path=stream(tmp_path / "foreign.log", FOREIGN),
        exit_code=1,
    )
    assert result.record.cc_session_uuid == EXPECTED
    assert result.record.trajectory_path is None
    assert result.record.served_model is None
