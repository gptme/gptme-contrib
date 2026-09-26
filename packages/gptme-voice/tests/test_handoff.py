"""Tests for the cross-agent voice handoff library (protocol v1)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from gptme_voice.handoff import (
    PROTOCOL_VERSION,
    STATE_SUBDIRS,
    HandoffWriter,
    archive_filename,
    atomic_move,
    atomic_write,
    build_handoff,
    caller_hash,
    compute_hmac,
    get_valid_agents,
    make_state_dirs,
    validate,
)

SECRET = b"bob-alice-handoff-test-secret-v1!"
SAMPLE_NOW = datetime(2026, 4, 21, 10, 0, 0, tzinfo=timezone.utc)
SAMPLE_VALIDATION_NOW = SAMPLE_NOW + timedelta(seconds=30)


@pytest.fixture(autouse=True)
def _default_test_roster(monkeypatch):
    """The roster is env-only (no built-in default), so tests that exercise the
    classic two-agent flow set it here; roster-specific tests override it."""
    monkeypatch.setenv("GPTME_VOICE_AGENTS", "bob,alice,gordon,sven")


# ---------- compute_hmac / validate ----------


def _sample_payload(
    *,
    now: datetime | None = None,
    secret: bytes = SECRET,
    **overrides,
) -> dict:
    now = now or SAMPLE_NOW
    return build_handoff(
        from_agent="bob",
        to_agent="alice",
        caller_id="test-caller",
        reason="scheduling_capability",
        secret=secret,
        transcript=[{"role": "user", "text": "hello", "ts": "2026-04-21T10:00:00Z"}],
        now=now,
        **overrides,
    )


def test_valid_payload_roundtrips():
    payload = _sample_payload()
    result = validate(
        payload,
        secret=SECRET,
        now=datetime(2026, 4, 21, 10, 0, 30, tzinfo=timezone.utc),
    )
    assert result.ok, result.reason
    # Re-computing the HMAC on an unchanged payload should match.
    assert compute_hmac(payload, SECRET) == payload["hmac"]


def test_unsupported_protocol_version_rejected():
    payload = _sample_payload()
    payload["protocol_version"] = 2
    # Re-sign so the failure is version-based, not HMAC-based.
    payload["hmac"] = compute_hmac(payload, SECRET)
    result = validate(payload, secret=SECRET, now=SAMPLE_VALIDATION_NOW)
    assert not result.ok
    assert "protocol_version" in result.reason


def test_missing_required_field_rejected():
    payload = _sample_payload()
    del payload["transcript"]
    result = validate(payload, secret=SECRET, now=SAMPLE_VALIDATION_NOW)
    assert not result.ok
    assert "transcript" in result.reason


def test_expired_payload_rejected():
    now = datetime(2026, 4, 21, 10, 0, 0, tzinfo=timezone.utc)
    payload = _sample_payload(now=now)
    past = now + timedelta(seconds=120)  # 60s after expires_at
    result = validate(payload, secret=SECRET, now=past)
    assert not result.ok
    assert "expired" in result.reason


def test_tampered_transcript_rejected():
    payload = _sample_payload()
    payload["transcript"].append({"role": "user", "text": "injected"})
    result = validate(payload, secret=SECRET, now=SAMPLE_VALIDATION_NOW)
    assert not result.ok
    assert "HMAC" in result.reason


def test_self_handoff_rejected_at_build_time():
    with pytest.raises(ValueError, match="differ"):
        build_handoff(
            from_agent="bob",
            to_agent="bob",
            caller_id="x",
            reason="r",
            secret=SECRET,
        )


def test_unknown_agent_rejected_at_build_time():
    with pytest.raises(ValueError, match="not in"):
        build_handoff(
            from_agent="bob",
            to_agent="mallory",
            caller_id="x",
            reason="r",
            secret=SECRET,
        )


def test_naive_datetime_raises_value_error():
    """Timezone-naive now must be rejected; otherwise validate() raises TypeError."""
    naive_now = datetime(2026, 4, 21, 10, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware"):
        build_handoff(
            from_agent="bob",
            to_agent="alice",
            caller_id="test-caller",
            reason="scheduling_capability",
            secret=SECRET,
            now=naive_now,
        )


def test_hmac_without_secret_is_not_verified():
    """Validation without a secret still requires the hmac field but doesn't verify it."""
    payload = _sample_payload()
    payload["hmac"] = "clearly-not-the-real-signature"
    result = validate(payload, secret=None, now=SAMPLE_VALIDATION_NOW)
    assert result.ok, result.reason


def test_extra_fields_cannot_overwrite_protocol_fields():
    with pytest.raises(ValueError, match="collides"):
        build_handoff(
            from_agent="bob",
            to_agent="alice",
            caller_id="x",
            reason="r",
            secret=SECRET,
            extra={"transcript": "stolen"},
        )


def test_extra_fields_carried_and_signed():
    payload = _sample_payload(
        extra={"context_summary": "scheduling meeting with Patrik"}
    )
    assert payload["context_summary"] == "scheduling meeting with Patrik"
    # Mutating the extra field should break the HMAC.
    tampered = dict(payload)
    tampered["context_summary"] = "mutated"
    result = validate(tampered, secret=SECRET, now=SAMPLE_VALIDATION_NOW)
    assert not result.ok
    assert "HMAC" in result.reason


# ---------- caller_hash ----------


def test_caller_hash_is_stable_and_hex():
    digest = caller_hash("+15550001234")
    assert len(digest) == 16
    assert all(c in "0123456789abcdef" for c in digest)
    assert caller_hash("+15550001234") == digest  # deterministic


# ---------- atomic_write / atomic_move ----------


def test_atomic_write_creates_parent_and_no_partial(tmp_path: Path):
    target = tmp_path / "subdir" / "payload.json"
    atomic_write(target, b'{"ok": true}')
    assert target.read_bytes() == b'{"ok": true}'
    # No leftover tempfile.
    assert not list(tmp_path.rglob(".*.tmp"))


def test_atomic_move_across_subdirs(tmp_path: Path):
    src = tmp_path / "handoff" / "a.json"
    dst = tmp_path / "claimed" / "a.json"
    atomic_write(src, b"{}")
    atomic_move(src, dst)
    assert dst.exists()
    assert not src.exists()


# ---------- HandoffWriter ----------


def test_handoff_writer_initiate_writes_signed_payload(tmp_path: Path):
    writer = HandoffWriter(tmp_path, from_agent="bob", secret=SECRET)
    published = writer.initiate(
        to_agent="alice",
        caller_id="+15550001234",
        reason="scheduling_capability",
        transcript=[
            {"role": "user", "text": "please get alice", "ts": "2026-04-21T10:00:00Z"}
        ],
    )
    assert published.path.is_file()
    on_disk = json.loads(published.path.read_text())
    assert on_disk == published.payload
    result = validate(on_disk, secret=SECRET, now=SAMPLE_VALIDATION_NOW)
    assert result.ok, result.reason
    assert on_disk["from_agent"] == "bob"
    assert on_disk["to_agent"] == "alice"
    assert on_disk["protocol_version"] == PROTOCOL_VERSION


def test_handoff_writer_sequences_multiple_initiations_for_same_caller(
    tmp_path: Path,
):
    writer = HandoffWriter(tmp_path, from_agent="bob", secret=SECRET)
    now = datetime(2026, 4, 21, 10, 0, 0, tzinfo=timezone.utc)
    first = writer.initiate(
        to_agent="alice",
        caller_id="+15550001234",
        reason="r1",
        now=now,
    )
    second = writer.initiate(
        to_agent="alice",
        caller_id="+15550001234",
        reason="r2",
        # Same instant — forces filenames to disambiguate via sequence, not timestamp.
        now=now,
    )
    assert first.path != second.path
    assert first.path.name.endswith("-0.json")
    assert second.path.name.endswith("-1.json")


def test_handoff_writer_creates_all_state_subdirs(tmp_path: Path):
    HandoffWriter(tmp_path, from_agent="bob", secret=SECRET)
    for subdir in STATE_SUBDIRS:
        assert (tmp_path / subdir).is_dir()


def test_handoff_writer_rejects_invalid_from_agent(tmp_path: Path):
    with pytest.raises(ValueError, match="not in"):
        HandoffWriter(tmp_path, from_agent="mallory", secret=SECRET)


def test_handoff_writer_rejects_empty_secret(tmp_path: Path):
    with pytest.raises(ValueError, match="secret"):
        HandoffWriter(tmp_path, from_agent="bob", secret=b"")


# ---------- GPTME_VOICE_AGENTS — deployment-configurable roster ----------


def test_get_valid_agents_unset_env_is_empty_roster(monkeypatch):
    """No built-in default roster: an unset GPTME_VOICE_AGENTS locks the
    deployment down (every handoff name invalid) until the operator configures
    the roster. Regression: pre-env-only code returned a hard-coded four-agent
    set."""
    monkeypatch.delenv("GPTME_VOICE_AGENTS", raising=False)
    assert get_valid_agents() == frozenset()


def test_get_valid_agents_respects_env_override(monkeypatch):
    monkeypatch.setenv("GPTME_VOICE_AGENTS", "alice,charlie,diana")
    agents = get_valid_agents()
    assert agents == frozenset({"alice", "charlie", "diana"})
    assert "bob" not in agents


@pytest.mark.parametrize("value", ["", " , ", ",", " ,,"])
def test_get_valid_agents_set_but_empty_is_empty_roster(monkeypatch, value):
    """A set-but-empty GPTME_VOICE_AGENTS yields an empty roster (lockdown),
    NOT the built-in default — the value replaces the default even when it
    parses to no names. Regression: pre-fix code fell back to VALID_AGENTS."""
    monkeypatch.setenv("GPTME_VOICE_AGENTS", value)
    assert get_valid_agents() == frozenset()


def test_non_default_agent_handoff_writer_succeeds(tmp_path, monkeypatch):
    """A fork with a non-default agent name can create a HandoffWriter when
    GPTME_VOICE_AGENTS includes that name — fails on the pre-fix code where
    VALID_AGENTS was a hardcoded frozenset."""
    monkeypatch.setenv("GPTME_VOICE_AGENTS", "alice,myrtle")
    writer = HandoffWriter(tmp_path, from_agent="myrtle", secret=SECRET)
    assert writer.from_agent == "myrtle"


def test_non_default_agent_build_and_validate_roundtrip(monkeypatch):
    """A handoff between two non-default agents validates end-to-end when the
    roster env var covers both names."""
    monkeypatch.setenv("GPTME_VOICE_AGENTS", "myrtle,oracle")
    payload = build_handoff(
        from_agent="myrtle",
        to_agent="oracle",
        caller_id="test-caller",
        reason="routing",
        secret=SECRET,
        now=SAMPLE_NOW,
    )
    result = validate(payload, secret=SECRET, now=SAMPLE_VALIDATION_NOW)
    assert result.ok, result.reason


def test_unset_env_warns_once_then_stays_lockdown(monkeypatch, caplog):
    """Unset GPTME_VOICE_AGENTS logs exactly one loud migration warning
    (per process) and yields an empty roster — the deprecation signal for
    deployments that relied on the old built-in default."""
    import gptme_voice.handoff as handoff_mod

    monkeypatch.delenv("GPTME_VOICE_AGENTS", raising=False)
    monkeypatch.setattr(handoff_mod, "_warned_unset_roster", False)
    with caplog.at_level(logging.WARNING, logger="gptme_voice.handoff"):
        assert get_valid_agents() == frozenset()
        assert get_valid_agents() == frozenset()
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert "GPTME_VOICE_AGENTS" in caplog.records[0].message


def test_non_default_agent_rejected_without_env(monkeypatch):
    """Without GPTME_VOICE_AGENTS the roster is empty, so every name —
    default or otherwise — is rejected."""
    monkeypatch.delenv("GPTME_VOICE_AGENTS", raising=False)
    with pytest.raises(ValueError, match="not in"):
        build_handoff(
            from_agent="myrtle",
            to_agent="bob",
            caller_id="x",
            reason="r",
            secret=SECRET,
        )


# ---------- make_state_dirs ----------


def test_make_state_dirs_is_idempotent(tmp_path: Path):
    dirs_a = make_state_dirs(tmp_path)
    dirs_b = make_state_dirs(tmp_path)
    assert dirs_a == dirs_b
    for subdir in STATE_SUBDIRS:
        assert dirs_a[subdir].is_dir()


# ---------- archive_filename ----------


def test_archive_filename_encodes_both_agents_and_id():
    payload = _sample_payload()
    name = archive_filename(payload, completed_at=1777000000)
    assert name.startswith("1777000000-bob-alice-")
    assert payload["handoff_id"] in name
    assert name.endswith(".json")
