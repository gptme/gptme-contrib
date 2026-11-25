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


def test_rewrite_does_not_duplicate_id_less_rows(tmp_path: Path) -> None:
    """An id-less legacy row survives load_all() -> rewrite() cycles unchanged (#2325)."""
    store = SessionStore(sessions_dir=tmp_path)
    legacy = {"harness": "claude-code", "model": "opus", "outcome": "productive"}
    store.path.write_text(
        json.dumps(legacy)
        + "\n"
        + json.dumps({"session_id": "keep01", "harness": "gptme", "outcome": "noop"})
        + "\n",
        encoding="utf-8",
    )

    snapshots = []
    for _ in range(3):
        records = store.load_all()
        assert len(records) == 2
        store.rewrite(records)
        snapshots.append(store.path.read_text(encoding="utf-8"))

    rows = [json.loads(line) for line in snapshots[-1].splitlines()]
    assert len(rows) == 2
    # Neither an invented id nor an invented "now" timestamp reaches the file.
    assert "session_id" not in rows[0]
    assert "timestamp" not in rows[0]
    assert {k: rows[0][k] for k in legacy} == legacy
    assert rows[1]["session_id"] == "keep01"
    # Repeated cycles are a fixed point: no copies, no moving dates.
    assert snapshots[0] == snapshots[1] == snapshots[2]


def test_id_less_row_gets_stable_id_across_loads(tmp_path: Path) -> None:
    store = SessionStore(sessions_dir=tmp_path)
    store.path.write_text(json.dumps({"model": "opus"}) + "\n", encoding="utf-8")

    first, second = store.load_all()[0], store.load_all()[0]

    assert first.session_id == second.session_id
    assert first.session_id.startswith("noid-")


def test_update_to_id_less_row_replaces_it_in_place(tmp_path: Path) -> None:
    store = SessionStore(sessions_dir=tmp_path)
    store.path.write_text(
        json.dumps({"model": "opus", "timestamp": "2026-01-02T03:04:05+00:00"})
        + "\n"
        + json.dumps({"session_id": "b"})
        + "\n",
        encoding="utf-8",
    )

    records = store.load_all()
    records[0].outcome = "productive"
    store.rewrite(records)

    rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["outcome"] == "productive"
    # A timestamp that was on disk is real data and is kept; the id stays absent.
    assert rows[0]["timestamp"] == "2026-01-02T03:04:05+00:00"
    assert "session_id" not in rows[0]
    assert rows[1]["session_id"] == "b"


def test_caller_assigned_id_on_id_less_row_is_persisted(tmp_path: Path) -> None:
    store = SessionStore(sessions_dir=tmp_path)
    store.path.write_text(json.dumps({"model": "opus"}) + "\n", encoding="utf-8")

    record = store.load_all()[0]
    record.session_id = "assigned"
    record.timestamp = "2026-01-02T03:04:05+00:00"
    store.rewrite([record])

    rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
    # The derived id no longer matches the re-read row, so the caller's copy is
    # appended (upsert semantics); its explicitly set values are written.
    assert rows[-1]["session_id"] == "assigned"
    assert rows[-1]["timestamp"] == "2026-01-02T03:04:05+00:00"


def test_store_invented_marker_is_never_serialized() -> None:
    record = SessionRecord.from_dict({"session_id": "x", "_store_invented": {"a": 1}})
    assert record._store_invented == {}
    assert "_store_invented" not in record.to_dict()


def test_two_rewrites_of_the_same_records_keep_one_id_less_row(tmp_path: Path) -> None:
    """A second rewrite of the same in-memory list must replace, not append (#2338).

    ``rewrite()`` writes the record's canonical form, so the bytes on disk
    differ from the legacy row. An identity derived from those bytes would
    change under the first rewrite and the second one would append a copy.
    """
    store = SessionStore(sessions_dir=tmp_path)
    store.path.write_text(json.dumps({"model": "opus"}) + "\n", encoding="utf-8")

    records = store.load_all()
    store.rewrite(records)
    store.rewrite(records)

    rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["model"] == "opus"
    assert "session_id" not in rows[0]


def test_mutate_rewrite_twice_without_reload_keeps_one_id_less_row(tmp_path: Path) -> None:
    """Stamp, rewrite, then stamp again on the same loaded objects (the repro)."""
    store = SessionStore(sessions_dir=tmp_path)
    store.path.write_text(json.dumps({"model": "opus"}) + "\n", encoding="utf-8")

    records = store.load_all()
    records[0].attempt_kind = "repetition"
    store.rewrite(records)
    records[0].category = "code"
    store.rewrite(records)

    rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["attempt_kind"] == "repetition"
    assert rows[0]["category"] == "code"
    assert "session_id" not in rows[0]
