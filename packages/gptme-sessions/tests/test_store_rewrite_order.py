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
    # The derived id is persisted (stable identity across rewrites); the
    # invented "now" timestamp never reaches the file.
    assert rows[0]["session_id"].startswith("noid-")
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
    # A timestamp that was on disk is real data and is kept; the derived id is
    # persisted so later rewrites match this row by id.
    assert rows[0]["timestamp"] == "2026-01-02T03:04:05+00:00"
    assert rows[0]["session_id"].startswith("noid-")
    assert rows[1]["session_id"] == "b"


def test_caller_assigned_id_on_id_less_row_is_persisted(tmp_path: Path) -> None:
    store = SessionStore(sessions_dir=tmp_path)
    store.path.write_text(json.dumps({"model": "opus"}) + "\n", encoding="utf-8")

    record = store.load_all()[0]
    record.session_id = "assigned"
    record.timestamp = "2026-01-02T03:04:05+00:00"
    store.rewrite([record])

    rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
    # Assigning an id to a loaded id-less row renames it in place: the derived
    # id is indexed alongside the new one, so the row is not kept as a second
    # copy (#2338).
    assert len(rows) == 1
    assert rows[0]["session_id"] == "assigned"
    assert rows[0]["timestamp"] == "2026-01-02T03:04:05+00:00"
    # Content the caller did not touch survives the rename.
    assert rows[0]["model"] == "opus"


def test_rename_of_persisted_synthetic_id_replaces_in_place(tmp_path: Path) -> None:
    """A reloaded ``noid-`` row is renamed in place, not duplicated (#2338)."""
    store = SessionStore(sessions_dir=tmp_path)
    store.path.write_text(json.dumps({"model": "opus"}) + "\n", encoding="utf-8")
    store.rewrite(store.load_all())  # persist the derived id

    record = store.load_all()[0]
    assert record.session_id.startswith("noid-")
    record.session_id = "assigned"
    store.rewrite([record])

    rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["session_id"] == "assigned"
    assert rows[0]["model"] == "opus"


def test_user_id_that_looks_like_a_derived_id_is_not_synthetic(tmp_path: Path) -> None:
    """A caller-assigned id is never reclassified as store-derived.

    The derived id used to be recognised by its *shape* (``noid-`` + 12 hex),
    which made a user id that happened to match that shape indistinguishable
    from ours — it would be aliased on rename and could drop a genuine row.
    Identity now comes from the marker the store writes itself, so no id shape
    is reserved (#2338).
    """
    store = SessionStore(sessions_dir=tmp_path)
    for user_id in ("noid-abc", "noid-0123456789ab"):
        store.path.write_text(
            json.dumps({"session_id": user_id, "model": "opus"}) + "\n",
            encoding="utf-8",
        )

        record = store.load_all()[0]
        assert record.session_id == user_id
        # No rename alias is registered: the id is the caller's, not ours.
        assert "session_id" not in record._store_invented

        store.rewrite([record])
        rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
        assert [row["session_id"] for row in rows] == [user_id]
        # The store did not stamp its marker onto a caller's row.
        assert "session_id_derived" not in rows[0]
        # A row that had no timestamp on disk still does not gain a fabricated one.
        assert "timestamp" not in rows[0]


def test_derived_id_is_marked_on_disk_and_read_back(tmp_path: Path) -> None:
    """The persisted derived id carries a marker, and the marker is stripped.

    ``session_id_derived`` is store metadata, not a record field: it must not
    leak into ``to_dict()``/``_legacy_fields``, and it must not follow a row
    that the caller renames (#2338).
    """
    store = SessionStore(sessions_dir=tmp_path)
    store.path.write_text(json.dumps({"model": "opus"}) + "\n", encoding="utf-8")
    store.rewrite(store.load_all())

    rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["session_id_derived"] is True
    assert rows[0]["session_id"].startswith("noid-")

    reloaded = store.load_all()[0]
    assert reloaded.session_id == rows[0]["session_id"]
    assert "session_id_derived" not in reloaded.to_dict()
    assert "_store_invented" not in reloaded.to_dict()

    # Renaming drops the marker: the id on disk is the caller's now.
    reloaded.session_id = "assigned"
    store.rewrite([reloaded])
    rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["session_id"] == "assigned"
    assert "session_id_derived" not in rows[0]


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
    assert rows[0]["session_id"].startswith("noid-")


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
    assert rows[0]["session_id"].startswith("noid-")


@pytest.mark.parametrize("interleaving", ["subset", "stamp"])
def test_unrelated_rewrite_keeps_outstanding_id_less_copy_matched(
    tmp_path: Path, interleaving: str
) -> None:
    """Canonicalization alone must not invalidate a previously loaded row."""
    store = SessionStore(sessions_dir=tmp_path)
    store.path.write_text(
        '{"model":"opus", "harness":"claude-code", "extra":{"z":1,"a":2}}\n'
        '{"session_id":"keep01"}\n',
        encoding="utf-8",
    )
    records = store.load_all()
    if interleaving == "stamp":
        assert store.stamp_attempt_kind("keep01", "repetition")
    else:
        store.rewrite([records[1]])

    # Use the outstanding copy, not a reload after the intervening rewrite.
    records[0].category = "code"
    store.rewrite([records[0]])

    rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["category"] == "code"
    assert rows[0]["extra"] == {"z": 1, "a": 2}
    assert rows[0]["session_id"].startswith("noid-")
    assert "timestamp" not in rows[0]
    assert rows[1]["session_id"] == "keep01"
    if interleaving == "stamp":
        assert rows[1]["attempt_kind"] == "repetition"


def test_equivalent_id_less_rows_each_consume_one_replacement(tmp_path: Path) -> None:
    """Formatting-equivalent rows retain multiplicity and input order."""
    store = SessionStore(sessions_dir=tmp_path)
    store.path.write_text('{"model":"opus"}\n{ "model": "opus" }\n', encoding="utf-8")
    records = store.load_all()
    store.rewrite(records)
    records[0].category = "code"
    records[1].category = "research"
    store.rewrite(records)

    rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
    assert [row["category"] for row in rows] == ["code", "research"]


@pytest.mark.parametrize("source", ["changed", "archive", "removed"])
def test_appended_id_less_row_stays_matched_on_later_rewrites(tmp_path: Path, source: str) -> None:
    """Unmatched loaded rows must remember their appended canonical bytes too."""
    store = SessionStore(sessions_dir=tmp_path)
    legacy = json.dumps({"model": "opus"}) + "\n"
    if source == "archive":
        archive = tmp_path / "session-records-archive-2026-01.jsonl"
        archive.write_text(legacy, encoding="utf-8")
        records = store.load_all(include_archives=True)
    else:
        store.path.write_text(legacy, encoding="utf-8")
        records = store.load_all()
        if source == "changed":
            store.path.write_text(json.dumps({"model": "other"}) + "\n", encoding="utf-8")
        else:
            store.path.unlink()

    expected_models = ["other", "opus"] if source == "changed" else ["opus"]
    for category in ("code", "research", "knowledge"):
        records[0].category = category
        store.rewrite(records)
        rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
        assert [row["model"] for row in rows] == expected_models
        assert rows[-1]["category"] == category
        assert all(
            str(row.get("session_id", "")).startswith("noid-") and "timestamp" not in row
            for row in rows
        )

    if source == "archive":
        assert archive.read_text(encoding="utf-8") == legacy


def test_stale_copy_after_same_row_change_replaces_in_place(tmp_path: Path) -> None:
    """Another writer changing the same id-less row must not spawn a duplicate.

    Regression for the P1 on #2338: the persisted ``noid-`` id is stable, so a
    stale outstanding copy still matches the changed on-disk row by id.

    The stale copy wins: ``rewrite()`` is last-writer-wins per id, and this is
    the *same* documented lost update a row with a real id gets when two
    writers hold it without ``lock()``. Giving an id-less row a stable id makes
    it participate in that documented contract instead of escaping it by
    duplicating; it does not introduce a new class of data loss. Holding
    ``lock()`` across load -> mutate -> rewrite is the caller's defence, for
    real-id and derived-id rows alike.
    """
    store = SessionStore(sessions_dir=tmp_path)
    store.path.write_text(json.dumps({"model": "opus"}) + "\n", encoding="utf-8")

    x = store.load_all()[0]
    # A different writer stamps the same row (its own load -> mutate -> rewrite).
    assert store.stamp_attempt_kind(x.session_id, "repetition")

    x.category = "code"
    store.rewrite([x])

    rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
    # One row, not two. The stale copy replaces in place (losing the other
    # writer's stamp), the same documented lost-update a real-id row gets.
    assert len(rows) == 1
    assert rows[0]["category"] == "code"
    assert rows[0]["session_id"].startswith("noid-")


def test_derived_id_is_stable_across_format_only_rewrite(tmp_path: Path) -> None:
    """A no-op rewrite re-serialises the row canonically but keeps its id."""
    store = SessionStore(sessions_dir=tmp_path)
    store.path.write_text(json.dumps({"model": "opus"}) + "\n", encoding="utf-8")

    old_id = store.load_all()[0].session_id
    store.rewrite(store.load_all())  # unrelated, formatting-only rewrite
    new_id = store.load_all()[0].session_id

    assert old_id == new_id
    # An id obtained before the rewrite still resolves afterwards.
    assert store.stamp_attempt_kind(old_id, "repetition")


def test_repeated_rewrite_of_noncanonical_field_value_keeps_one_row(tmp_path: Path) -> None:
    """A value normalised on read must not change the persisted id.

    Regression for the P1 on #2338: ``run_type`` is normalised by
    ``SessionRecord.from_dict`` but not by ``to_dict``, so the id has to come
    from the canonical content rather than the raw in-memory field.
    """
    store = SessionStore(sessions_dir=tmp_path)
    store.path.write_text(json.dumps({"model": "opus"}) + "\n", encoding="utf-8")

    record = store.load_all()[0]
    record.run_type = "123"  # normalised to "autonomous" on the next read
    for _ in range(3):
        store.rewrite([record])

    rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["session_id"].startswith("noid-")


def test_append_of_loaded_id_less_row_does_not_fabricate_timestamp(tmp_path: Path) -> None:
    """Re-appending a loaded id-less row keeps its invented "now" out of the file."""
    src = SessionStore(sessions_dir=tmp_path / "src")
    src.path.parent.mkdir(parents=True)
    src.path.write_text(json.dumps({"model": "opus"}) + "\n", encoding="utf-8")

    dst = SessionStore(sessions_dir=tmp_path / "dst")
    dst.append(src.load_all()[0])

    row = json.loads(dst.path.read_text(encoding="utf-8").splitlines()[0])
    assert "timestamp" not in row
    assert row["session_id"].startswith("noid-")
    assert row["model"] == "opus"
