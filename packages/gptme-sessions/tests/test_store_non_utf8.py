"""A store row with undecodable bytes must not crash reads or be lost on rewrite."""

from pathlib import Path

from gptme_sessions import SessionRecord, SessionStore

BAD_ROWS = [
    b'{"session_id": "x"}\xff\xfe\n',  # undecodable bytes after valid JSON
    b'{"session_id": "bad\xe2\x82", "model": "m"}\n',  # torn multibyte char inside a string
    b" \xff \r\n",  # whitespace and CRLF must survive byte-for-byte
    # old timestamp must not let rotation relocate a corrupt row to an archive
    b'{"session_id": "bad\xff", "timestamp": "2020-01-01T00:00:00Z"}\n',
]


def _store_with_bad_rows(tmp_path: Path) -> tuple[SessionStore, SessionRecord]:
    store = SessionStore(sessions_dir=tmp_path)
    rec = SessionRecord(model="opus")
    store.append(rec)
    with open(store.path, "ab") as f:
        for row in BAD_ROWS:
            f.write(row)
    return store, rec


def test_load_all_skips_undecodable_rows(tmp_path: Path) -> None:
    store, rec = _store_with_bad_rows(tmp_path)
    assert [r.session_id for r in store.load_all()] == [rec.session_id]


def test_rewrite_preserves_undecodable_bytes(tmp_path: Path) -> None:
    store, _ = _store_with_bad_rows(tmp_path)
    store.rewrite(store.load_all())
    raw = store.path.read_bytes()
    for row in BAD_ROWS:
        assert row in raw


def test_rotate_keeps_undecodable_rows_active(tmp_path: Path) -> None:
    store, _ = _store_with_bad_rows(tmp_path)
    store.rotate(keep_days=0)
    raw = store.path.read_bytes()
    for row in BAD_ROWS:
        assert row in raw


def test_append_after_unterminated_malformed_row_is_loadable(tmp_path: Path) -> None:
    store = SessionStore(sessions_dir=tmp_path)
    store.path.write_bytes(b"not-json")  # no trailing newline
    rec = SessionRecord(model="opus")
    store.append(rec)
    assert [r.session_id for r in store.load_all()] == [rec.session_id]
    assert store.path.read_bytes().endswith(b"\n")


def test_rotate_preserves_crlf_in_decodable_malformed_rows(tmp_path: Path) -> None:
    """rotate() must keep the original bytes for decodable-but-malformed rows.

    A row like b'{bad json  \\r\\n' is valid UTF-8, passes encode(), but yields
    month=None (unparseable JSON).  Before the fix it was stripped and re-added
    as ``line + "\\n"``, silently dropping the trailing CRLF.
    """
    store = SessionStore(sessions_dir=tmp_path)
    rec = SessionRecord(model="opus", timestamp="2020-01-01T00:00:00+00:00")
    store.append(rec)
    crlf_row = b'{"not": "json"\r\n'
    with open(store.path, "ab") as f:
        f.write(crlf_row)
    store.rotate(keep_days=0)
    assert crlf_row in store.path.read_bytes()


def test_rotate_onto_unterminated_archive_keeps_record(tmp_path: Path) -> None:
    store = SessionStore(sessions_dir=tmp_path)
    rec = SessionRecord(model="opus", timestamp="2020-01-01T00:00:00+00:00")
    store.append(rec)
    archive = store.sessions_dir / "session-records-archive-2020-01.jsonl"
    archive.write_bytes(b"\xff")  # unterminated corrupt archive
    store.rotate(keep_days=0)
    loaded = store.load_all(include_archives=True)
    assert [r.session_id for r in loaded] == [rec.session_id]
    assert archive.read_bytes().endswith(b"\n")
