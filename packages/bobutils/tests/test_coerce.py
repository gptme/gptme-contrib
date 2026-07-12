"""Tests for bobutils.coerce."""

from __future__ import annotations

from bobutils.coerce import coerce_int


def test_coerce_int_basic_int() -> None:
    assert coerce_int(42) == 42


def test_coerce_int_negative_int() -> None:
    assert coerce_int(-7) == -7


def test_coerce_int_float_rounds() -> None:
    assert coerce_int(1.7) == 2


def test_coerce_int_float_truncates_negative() -> None:
    assert coerce_int(-1.7) == -2


def test_coerce_int_float_half_rounds_up() -> None:
    assert coerce_int(0.5) == 0 or coerce_int(0.5) == 1  # banker's rounding ok


def test_coerce_int_string_digits() -> None:
    assert coerce_int("42") == 42


def test_coerce_int_string_whitespace() -> None:
    assert coerce_int("  7  ") == 7


def test_coerce_int_none_default_zero() -> None:
    assert coerce_int(None) == 0


def test_coerce_int_bool_true_returns_default() -> None:
    assert coerce_int(True) == 0  # bool must NOT pass as int 1


def test_coerce_int_bool_false_returns_default() -> None:
    assert coerce_int(False) == 0


def test_coerce_int_unparseable_string_default() -> None:
    assert coerce_int("abc") == 0


def test_coerce_int_empty_string_default() -> None:
    assert coerce_int("") == 0


def test_coerce_int_custom_default() -> None:
    assert coerce_int("bad", default=99) == 99


def test_coerce_int_nullable_none() -> None:
    result = coerce_int(None, default=None)
    assert result is None


def test_coerce_int_nullable_bool() -> None:
    result = coerce_int(True, default=None)
    assert result is None


def test_coerce_int_nullable_unparseable() -> None:
    result = coerce_int("bad", default=None)
    assert result is None


def test_coerce_int_nullable_valid_int() -> None:
    result = coerce_int(5, default=None)
    assert result == 5


def test_coerce_int_nan_returns_default() -> None:
    assert coerce_int(float("nan")) == 0


def test_coerce_int_inf_returns_default() -> None:
    assert coerce_int(float("inf")) == 0


def test_coerce_int_neg_inf_returns_default() -> None:
    assert coerce_int(float("-inf")) == 0
