"""Tests for the cross-agent voice handoff library (protocol v1)."""

from __future__ import annotations

import json
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
    make_state_dirs,
    validate,
)

SECRET = b"bob-alice-handoff-test-secret-v1!"


# ---------- compute_hmac / validate ----------


def _sample_payload(
    *,
    now: datetime | None = None,
    secret: bytes = SECRET,
    **overrides,
) -> dict:
    now = now or datetime(2026, 4, 21, 10, 0, 0, tzinfo=timezone.utc)
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
    result = validate(payload, secret=SECRET)
    assert not result.ok
    assert "protocol_version" in result.reason


def test_missing_required_field_rejected():
    payload = _sample_payload()
    del payload["transcript"]
    result = validate(payload, secret=SECRET)
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
    result = validate(payload, secret=SECRET)
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


def test_hmac_without_secret_is_not_verified():
    """Validation without a secret still requires the hmac field but doesn't verify it."""
    payload = _sample_payload()
    payload["hmac"] = "clearly-not-the-real-signature"
    result = validate(payload, secret=None)
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
    result = validate(tampered, secret=SECRET)
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
    result = validate(on_disk, secret=SECRET)
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
