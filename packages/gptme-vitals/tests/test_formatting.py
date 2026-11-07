from __future__ import annotations

import pytest
from gptme_vitals.formatting import as_float, as_int, format_pct


@pytest.mark.parametrize(
    ("value", "f", "i"),
    [
        (True, None, None),
        (3, 3.0, 3),
        (2.9, 2.9, 2),
        ("3", None, None),
        (None, None, None),
    ],
)
def test_coercion(value, f, i):
    assert as_float(value) == f
    assert as_int(value) == i


def test_format_pct():
    assert format_pct(0.256) == "26%"
    assert format_pct("x") == "?"
    assert format_pct(True) == "?"
