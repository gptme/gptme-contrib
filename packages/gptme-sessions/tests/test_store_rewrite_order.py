"""rewrite() keeps on-disk row order, so the file tail stays the newest sessions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gptme_sessions.record import SessionRecord
from gptme_sessions.store import SessionStore


def _ids(store: SessionStore) -> list[str]:
    return [r.session_id for r in store.load_all()]


def test_subset_upsert_keeps_updated_row_in_place(tmp_path: Path) -> None:
    store = SessionStore(sessions_dir=tmp_path)
    for sid in ("a", "b", "c", "d"):
        store.append(SessionRecord(session_id=sid))

    updated = SessionRecord(session_id="c", model="judged")
    store.rewrite([updated])

    assert _ids(store) == ["a", "b", "c", "d"]
    assert {r.session_id: r.model for r in store.load_all()}["c"] == "judged"


def test_new_records_are_appended_after_existing_rows(tmp_path: Path) -> None:
    store = SessionStore(sessions_dir=tmp_path)
    for sid in ("a", "b"):
        store.append(SessionRecord(session_id=sid))

    store.rewrite([SessionRecord(session_id="new"), SessionRecord(session_id="a", model="x")])

    assert _ids(store) == ["a", "b", "new"]


def test_malformed_row_stays_at_its_position(tmp_path: Path) -> None:
    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(session_id="a"))
    with store.path.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    store.append(SessionRecord(session_id="b"))

    store.rewrite([SessionRecord(session_id="a", model="x")])

    lines = store.path.read_text(encoding="utf-8").splitlines()
    assert lines[1] == "{not json"
    assert [json.loads(line)["session_id"] for line in (lines[0], lines[2])] == ["a", "b"]


@pytest.mark.parametrize("ending", [b"\r", b"", b"\n", b"\r\n"])
def test_malformed_row_has_lf_boundary_before_new_record(tmp_path: Path, ending: bytes) -> None:
    store = SessionStore(sessions_dir=tmp_path)
    malformed = b"{not json" + ending
    store.path.write_bytes(malformed)

    store.rewrite([SessionRecord(session_id="new")])

    lines = store.path.read_bytes().split(b"\n")
    assert lines[0] == malformed.removesuffix(b"\n")
    assert json.loads(lines[1])["session_id"] == "new"
    assert lines[2:] == [b""]
    assert _ids(store) == ["new"]


def test_duplicate_disk_rows_collapse_to_the_first_position(tmp_path: Path) -> None:
    store = SessionStore(sessions_dir=tmp_path)
    for sid in ("dup", "a", "dup", "b"):
        store.append(SessionRecord(session_id=sid))

    store.rewrite([SessionRecord(session_id="dup", model="x")])

    assert _ids(store) == ["dup", "a", "b"]


def test_full_rewrite_preserves_interleaved_duplicate_rows(tmp_path: Path) -> None:
    store = SessionStore(sessions_dir=tmp_path)
    rows = [
        SessionRecord(session_id="dup", timestamp="2026-10-20T01:00:00+00:00"),
        SessionRecord(session_id="a", timestamp="2026-10-20T02:00:00+00:00"),
        SessionRecord(session_id="dup", timestamp="2026-10-20T03:00:00+00:00"),
    ]
    for row in rows:
        store.append(row)

    assert store.stamp_attempt_kind("a", "repetition")

    rewritten = store.load_all()
    assert [(r.session_id, r.timestamp) for r in rewritten] == [
        (r.session_id, r.timestamp) for r in rows
    ]
    assert rewritten[1].attempt_kind == "repetition"


def test_excess_input_rows_append_in_input_order(tmp_path: Path) -> None:
    store = SessionStore(sessions_dir=tmp_path)
    store.append(SessionRecord(session_id="dup"))
    store.append(SessionRecord(session_id="existing"))

    store.rewrite(
        [
            SessionRecord(session_id="dup", model="first"),
            SessionRecord(session_id="new", model="new-first"),
            SessionRecord(session_id="dup", model="second"),
            SessionRecord(session_id="new", model="new-second"),
        ]
    )

    assert [(r.session_id, r.model) for r in store.load_all()] == [
        ("dup", "first"),
        ("existing", "unknown"),
        ("new", "new-first"),
        ("dup", "second"),
        ("new", "new-second"),
    ]
