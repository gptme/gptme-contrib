"""Tests for bobutils.jsonl — canonical semantics for every equivalence class
found in the #1043 duplicate inventory."""

import gzip
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


def test_gzip_transparent(tmp_path):
    path = tmp_path / "data.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write('{"a": 1}\n{"b": 2}\n')
    assert load_jsonl(path) == [{"a": 1}, {"b": 2}]


def test_accepts_str_path(jsonl_file):
    assert load_jsonl(str(jsonl_file)) == [{"a": 1}, {"b": 2}]
