"""Tests for bobutils.jsonl — canonical semantics for every equivalence class
found in the #1043 duplicate inventory."""

import gzip
import json
import logging

import pytest
from bobutils.jsonl import iter_jsonl, load_jsonl


@pytest.fixture
def jsonl_file(tmp_path):
    path = tmp_path / "data.jsonl"
    path.write_text(
        '{"a": 1}\n\n   \n{"b": 2}\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def dirty_file(tmp_path):
    path = tmp_path / "dirty.jsonl"
    path.write_text(
        '{"a": 1}\nnot json at all\n[1, 2, 3]\n"bare string"\n{"b": 2}\n',
        encoding="utf-8",
    )
    return path


def test_load_basic(jsonl_file):
    assert load_jsonl(jsonl_file) == [{"a": 1}, {"b": 2}]


def test_iter_is_lazy(jsonl_file):
    it = iter_jsonl(jsonl_file)
    assert next(it) == {"a": 1}


def test_missing_file_returns_empty(tmp_path):
    assert load_jsonl(tmp_path / "nope.jsonl") == []


def test_missing_file_must_exist_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_jsonl(tmp_path / "nope.jsonl", must_exist=True)
    # iter_jsonl validates eagerly — raises at call time, not at first next()
    with pytest.raises(FileNotFoundError):
        iter_jsonl(tmp_path / "nope.jsonl", must_exist=True)


def test_skip_invalid_default(dirty_file):
    assert load_jsonl(dirty_file) == [{"a": 1}, {"b": 2}]


def test_raise_on_invalid_json(dirty_file):
    with pytest.raises(ValueError, match=r"dirty\.jsonl:2: invalid JSON"):
        load_jsonl(dirty_file, on_invalid="raise")


def test_raise_on_non_dict_row(tmp_path):
    path = tmp_path / "arr.jsonl"
    path.write_text('{"a": 1}\n[1, 2]\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"arr\.jsonl:2: non-dict row of type list"):
        load_jsonl(path, on_invalid="raise")


def test_warn_on_invalid(dirty_file, caplog):
    with caplog.at_level(logging.WARNING, logger="bobutils.jsonl"):
        rows = load_jsonl(dirty_file, on_invalid="warn")
    assert rows == [{"a": 1}, {"b": 2}]
    assert len(caplog.records) == 3
    assert "skipping line" in caplog.records[0].getMessage()


def test_bad_on_invalid_mode_raises_eagerly(jsonl_file):
    # iter_jsonl validates on_invalid at call time — no next() needed
    with pytest.raises(ValueError, match="on_invalid must be one of"):
        iter_jsonl(jsonl_file, on_invalid="explode")


def test_tail(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text("".join(f'{{"n": {i}}}\n' for i in range(10)), encoding="utf-8")
    assert load_jsonl(path, tail=3) == [{"n": 7}, {"n": 8}, {"n": 9}]
    assert load_jsonl(path, tail=0) == []
    assert len(load_jsonl(path, tail=100)) == 10


@pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"])
def test_tail_preserves_universal_newlines(tmp_path, newline):
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(newline.join(['{"n": 1}', '{"n": 2}', ""]).encode())
    assert list(iter_jsonl(path)) == [{"n": 1}, {"n": 2}]
    assert load_jsonl(path, tail=1) == [{"n": 2}]
    assert load_jsonl(path, tail=10) == [{"n": 1}, {"n": 2}]


@pytest.mark.parametrize("newline", [b"\r", b"\r\n"])
def test_tail_newline_at_block_boundary(tmp_path, newline):
    path = tmp_path / "ledger.jsonl"
    # The rightmost 1 MiB block starts immediately after CR (inside CRLF).
    ending = b'{"n": 2}' + b" " * (1024 * 1024 - len(b'{"n": 2}') - len(newline) + 1)
    path.write_bytes(b'{"n": 1}' + newline + ending)
    full = list(iter_jsonl(path))
    assert full == [{"n": 1}, {"n": 2}]
    assert load_jsonl(path, tail=1) == full[-1:]
    assert load_jsonl(path, tail=10) == full


@pytest.mark.parametrize("tail", [0, -1])
@pytest.mark.parametrize("on_invalid", ["raise", "warn"])
def test_nonpositive_tail_preserves_diagnostics(dirty_file, caplog, tail, on_invalid):
    if on_invalid == "raise":
        with pytest.raises(ValueError, match=r"dirty\.jsonl:2: invalid JSON"):
            load_jsonl(dirty_file, tail=tail, on_invalid=on_invalid)
    else:
        with caplog.at_level(logging.WARNING, logger="bobutils.jsonl"):
            assert load_jsonl(dirty_file, tail=tail, on_invalid=on_invalid) == []
        assert len(caplog.records) == 3
        assert "dirty.jsonl:2: invalid JSON" in caplog.records[0].getMessage()


def test_tail_reads_backwards_across_blocks_like_full_scan(tmp_path):
    path = tmp_path / "ledger.jsonl"
    pad = "é" * 1500  # multibyte rows straddling the 1 MiB block boundaries
    lines = [json.dumps({"n": i, "pad": pad}, ensure_ascii=False) for i in range(2000)]
    lines[1990] = "not json"
    lines[1995] = "[1, 2]"
    path.write_text("\n".join(lines) + "\n\n", encoding="utf-8")
    full = list(iter_jsonl(path))
    for tail in (1, 7, 900, 1997, 5000):
        assert load_jsonl(path, tail=tail) == full[-tail:]


def test_gzip_transparent(tmp_path):
    path = tmp_path / "data.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write('{"a": 1}\n{"b": 2}\n')
    assert load_jsonl(path) == [{"a": 1}, {"b": 2}]


def test_accepts_str_path(jsonl_file):
    assert load_jsonl(str(jsonl_file)) == [{"a": 1}, {"b": 2}]
