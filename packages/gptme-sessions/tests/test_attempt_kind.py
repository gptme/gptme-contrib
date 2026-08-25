"""Tests for the ``attempt_kind`` eval-attempt classification.

The session runner already computes an infra-failure verdict (rate limit,
auth failure, dead backend, zero-token exit) and uses it to skip bandit and
grade updates — but never persisted it, leaving every read-side consumer to
re-derive infra-ness from duration/token heuristics.  These tests cover the
write side: the field, its validation, and the second-pass stamp.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from gptme_sessions import SessionRecord, SessionStore
from gptme_sessions.cli import cli
from gptme_sessions.record import ATTEMPT_KINDS


def _store(tmp_path: Path) -> SessionStore:
    return SessionStore(sessions_dir=tmp_path)


def test_attempt_kind_defaults_to_none() -> None:
    """Records written before the field existed must parse unchanged."""
    record = SessionRecord.from_dict({"session_id": "old1", "outcome": "productive"})
    assert record.attempt_kind is None


def test_infra_failure_session_reads_back_as_infra_retry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(SessionRecord(session_id="dead", outcome="failed", duration_seconds=12))

    assert store.stamp_attempt_kind("dead", "infra_retry") is True

    (loaded,) = store.load_all()
    assert loaded.attempt_kind == "infra_retry"


def test_normal_session_reads_back_as_repetition(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(SessionRecord(session_id="good", outcome="productive", duration_seconds=1800))

    assert store.stamp_attempt_kind("good", "repetition") is True

    (loaded,) = store.load_all()
    assert loaded.attempt_kind == "repetition"


def test_stamp_persists_to_jsonl(tmp_path: Path) -> None:
    """The value must survive to disk, not just to the in-memory record."""
    store = _store(tmp_path)
    store.append(SessionRecord(session_id="s1", outcome="noop"))
    store.stamp_attempt_kind("s1", "infra_retry")

    lines = [json.loads(x) for x in store.path.read_text().splitlines() if x.strip()]
    assert [x["attempt_kind"] for x in lines] == ["infra_retry"]


def test_stamp_leaves_other_records_untouched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for sid in ("a", "b", "c"):
        store.append(SessionRecord(session_id=sid, outcome="productive"))

    store.stamp_attempt_kind("b", "infra_retry")

    by_id = {r.session_id: r for r in store.load_all()}
    assert by_id["b"].attempt_kind == "infra_retry"
    assert by_id["a"].attempt_kind is None
    assert by_id["c"].attempt_kind is None
    assert len(by_id) == 3


def test_stamp_missing_session_returns_false(tmp_path: Path) -> None:
    """post_session() can fail; the runner must not crash stamping a ghost."""
    store = _store(tmp_path)
    store.append(SessionRecord(session_id="present", outcome="productive"))

    assert store.stamp_attempt_kind("absent", "repetition") is False


@pytest.mark.parametrize("bad", ["", "retry", "INFRA_RETRY", "continuation"])
def test_stamp_rejects_unknown_kind(tmp_path: Path, bad: str) -> None:
    """A typo would persist as an unread value forever — fail loudly instead."""
    store = _store(tmp_path)
    store.append(SessionRecord(session_id="s1", outcome="productive"))

    with pytest.raises(ValueError):
        store.stamp_attempt_kind("s1", bad)

    assert store.load_all()[0].attempt_kind is None


def test_stamp_requires_session_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.stamp_attempt_kind("", "repetition")


def test_attempt_kinds_vocabulary_is_closed() -> None:
    assert ATTEMPT_KINDS == frozenset({"repetition", "infra_retry", "unknown"})


def test_invalid_attempt_kind_discarded_at_load_time() -> None:
    """A hand-edited or buggy JSONL with an invalid attempt_kind must not reach consumers."""
    record = SessionRecord.from_dict(
        {"session_id": "s1", "outcome": "productive", "attempt_kind": "infra-retry"}
    )
    assert record.attempt_kind is None


def test_valid_attempt_kind_preserved_at_load_time() -> None:
    """A valid attempt_kind must survive the round-trip through from_dict."""
    for kind in ATTEMPT_KINDS:
        record = SessionRecord.from_dict(
            {"session_id": "s1", "outcome": "productive", "attempt_kind": kind}
        )
        assert record.attempt_kind == kind


def test_stamp_handles_duplicate_rows(tmp_path: Path) -> None:
    """stamp_attempt_kind must stamp all rows when a session_id appears twice."""
    store = _store(tmp_path)
    store.append(SessionRecord(session_id="dup", outcome="failed", duration_seconds=5))
    store.append(SessionRecord(session_id="dup", outcome="failed", duration_seconds=5))

    assert store.stamp_attempt_kind("dup", "infra_retry") is True

    records = store.load_all()
    assert len(records) == 2
    assert all(r.attempt_kind == "infra_retry" for r in records)


def test_cli_stamp_handles_duplicate_rows(tmp_path: Path) -> None:
    """CLI stamp-attempt-kind must succeed even when the store has duplicate rows."""
    store = _store(tmp_path)
    store.append(SessionRecord(session_id="dup", outcome="failed", duration_seconds=5))
    store.append(SessionRecord(session_id="dup", outcome="failed", duration_seconds=5))

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--sessions-dir", str(tmp_path), "stamp-attempt-kind", "dup", "infra_retry"],
    )
    assert result.exit_code == 0, result.output
    assert "infra_retry" in result.output

    records = store.load_all()
    assert all(r.attempt_kind == "infra_retry" for r in records)


def test_cli_stamp_prefix_resolution(tmp_path: Path) -> None:
    """CLI stamp-attempt-kind resolves partial session ID prefixes."""
    store = _store(tmp_path)
    store.append(SessionRecord(session_id="abcdef", outcome="productive"))

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--sessions-dir", str(tmp_path), "stamp-attempt-kind", "abc", "repetition"],
    )
    assert result.exit_code == 0, result.output
    assert store.load_all()[0].attempt_kind == "repetition"


def test_cli_stamp_ambiguous_prefix_raises(tmp_path: Path) -> None:
    """CLI stamp-attempt-kind raises on a prefix matching two different session IDs."""
    store = _store(tmp_path)
    store.append(SessionRecord(session_id="abc1", outcome="productive"))
    store.append(SessionRecord(session_id="abc2", outcome="failed"))

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--sessions-dir", str(tmp_path), "stamp-attempt-kind", "abc", "repetition"],
    )
    assert result.exit_code != 0
    assert "Ambiguous" in result.output
