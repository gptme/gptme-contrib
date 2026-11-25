"""Regression tests for gptme/gptme-contrib#2304 items 1 and 8.

YAML turns an unquoted ISO timestamp into a ``datetime`` (and a bare date into a
``date``), so frontmatter values reach these helpers as objects, not strings.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import yaml
from gptodo.utils import has_new_activity
from gptodo.waiting import check_time


def test_has_new_activity_accepts_yaml_datetime_waiting_since() -> None:
    waiting_since = yaml.safe_load("w: 2026-11-20T10:00:00Z")["w"]
    assert isinstance(waiting_since, datetime)
    assert has_new_activity("2026-11-21T10:00:00Z", waiting_since)
    assert not has_new_activity("2026-11-19T10:00:00Z", waiting_since)


def test_has_new_activity_naive_datetime_waiting_since() -> None:
    assert has_new_activity("2026-11-21T10:00:00Z", datetime(2026, 11, 20, 10))


def test_check_time_accepts_yaml_datetime_ref() -> None:
    ref = yaml.safe_load("ref: 2020-01-01T00:00:00Z")["ref"]
    assert isinstance(ref, datetime)
    assert check_time(ref) == (True, None)
    resolved, error = check_time(datetime(2999, 1, 1, tzinfo=timezone.utc))
    assert not resolved
    assert error is not None and error.startswith("Waiting until")


def test_check_time_accepts_yaml_date_ref() -> None:
    assert check_time(date(2020, 1, 1)) == (True, None)
    assert check_time(date(2999, 1, 1))[0] is False
