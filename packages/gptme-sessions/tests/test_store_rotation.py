"""Rotation of the session-records store: move old records into monthly archives."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from gptme_sessions.record import SessionRecord
from gptme_sessions.store import SessionStore

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _rec(session_id: str, days_ago: int) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        timestamp=(NOW - timedelta(days=days_ago)).isoformat(),
    )


@pytest.fixture
def store(tmp_path):
    return SessionStore(sessions_dir=tmp_path)


def test_rotate_moves_old_records_into_monthly_archives(store):
    store.append(_rec("old-jul", 60))  # 2026-07
    store.append(_rec("old-aug", 33))  # 2026-08-01
    store.append(_rec("recent", 3))

    stats = store.rotate(keep_days=30, now=NOW)

    assert stats == {"archived": 2, "kept": 1, "skipped_duplicate": 0}
    assert [r.session_id for r in store.load_all()] == ["recent"]
    names = [p.name for p in store.archive_paths()]
    assert names == [
        "session-records-archive-2026-07.jsonl",
        "session-records-archive-2026-08.jsonl",
    ]


def test_rotate_preserves_every_record_via_include_archives(store):
    for i, days in enumerate([90, 60, 40, 10, 1]):
        store.append(_rec(f"s{i}", days))

    store.rotate(keep_days=30, now=NOW)

    all_ids = {r.session_id for r in store.load_all(include_archives=True)}
    assert all_ids == {"s0", "s1", "s2", "s3", "s4"}
    # Default stays active-only so mutation paths don't re-inject history.
    assert {r.session_id for r in store.load_all()} == {"s3", "s4"}


def test_rotate_is_idempotent(store):
    store.append(_rec("old", 60))
    store.append(_rec("new", 1))

    first = store.rotate(keep_days=30, now=NOW)
    second = store.rotate(keep_days=30, now=NOW)

    assert first["archived"] == 1
    assert second == {"archived": 0, "kept": 1, "skipped_duplicate": 0}
    archive = store.archive_paths()[0]
    assert len(archive.read_text().strip().splitlines()) == 1


def test_rotate_skips_an_identical_reappended_record(store):
    """Crash recovery may leave an exact copy active; do not archive it twice."""
    store.append(_rec("old", 60))
    store.rotate(keep_days=30, now=NOW)
    store.append(_rec("old", 60))

    stats = store.rotate(keep_days=30, now=NOW)

    assert stats["archived"] == 0
    assert stats["skipped_duplicate"] == 1
    archive = store.archive_paths()[0]
    assert len(archive.read_text().strip().splitlines()) == 1


def test_rotate_preserves_distinct_records_with_the_same_session_id(store):
    """Deduplication must never discard changed or genuinely duplicate rows."""
    first = _rec("old", 60)
    second = _rec("old", 59)
    store.append(first)
    store.rotate(keep_days=30, now=NOW)
    store.append(second)

    stats = store.rotate(keep_days=30, now=NOW)

    assert stats["archived"] == 1
    assert stats["skipped_duplicate"] == 0
    archived = store.load_all(include_archives=True)
    assert [(r.session_id, r.timestamp) for r in archived] == [
        ("old", first.timestamp),
        ("old", second.timestamp),
    ]

    # Identical rows in one active batch represent two historical events. They
    # are both retained on the first rotation; hash dedup only suppresses lines
    # already durable in the archive from a previous partial rotation.
    twin = _rec("twin", 58)
    store.append(twin)
    store.append(twin)
    stats = store.rotate(keep_days=30, now=NOW)

    assert stats["archived"] == 2
    assert stats["skipped_duplicate"] == 0
    assert [r.session_id for r in store.load_all(include_archives=True)].count("twin") == 2


def test_rotate_keeps_records_without_a_usable_timestamp(store):
    store.append(_rec("dated", 60))
    with open(store.path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"session_id": "no-ts", "timestamp": ""}) + "\n")
        f.write(json.dumps({"session_id": "bad-ts", "timestamp": "not-a-date"}) + "\n")
        f.write("{not json at all\n")

    stats = store.rotate(keep_days=30, now=NOW)

    assert stats["archived"] == 1
    assert stats["kept"] == 3
    kept = store.path.read_text()
    assert "no-ts" in kept and "bad-ts" in kept and "{not json at all" in kept


def test_rotate_no_op_when_nothing_is_old_enough(store):
    store.append(_rec("a", 1))
    store.append(_rec("b", 2))

    assert store.rotate(keep_days=30, now=NOW) == {
        "archived": 0,
        "kept": 2,
        "skipped_duplicate": 0,
    }
    assert store.archive_paths() == []


def test_rotate_on_missing_store_is_a_no_op(store):
    assert not store.path.exists()
    assert store.rotate(keep_days=30, now=NOW) == {
        "archived": 0,
        "kept": 0,
        "skipped_duplicate": 0,
    }


def test_rotate_rejects_negative_keep_days(store):
    with pytest.raises(ValueError, match="keep_days"):
        store.rotate(keep_days=-1, now=NOW)


def test_rewrite_after_rotation_does_not_resurrect_archived_records(store):
    """The write-amplification fix: a post-rotation field update stays small."""
    store.append(_rec("old", 60))
    store.append(_rec("recent", 1))
    store.rotate(keep_days=30, now=NOW)

    records = store.load_all()
    records[0].attempt_kind = "real"
    store.rewrite(records)

    assert [r.session_id for r in store.load_all()] == ["recent"]
    assert {r.session_id for r in store.load_all(include_archives=True)} == {
        "old",
        "recent",
    }
