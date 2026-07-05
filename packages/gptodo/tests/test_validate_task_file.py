"""Regression tests for validate_task_file's `created` field typing.

An unquoted YAML date-only value (``created: 2026-05-27``) is parsed by PyYAML
as a ``datetime.date``. Because ``datetime`` subclasses ``date`` (not the
reverse), ``isinstance(a_date, datetime)`` is False — so the validator
previously rejected date-only created values with
"Field created must be str or datetime", even though gptodo's own task
generator writes date-only ISO dates (``date.today().isoformat()``).
"""

from datetime import date, datetime

from gptodo.frontmatter_compat import loads
from gptodo.utils import validate_task_file


def _post(created_value: str):
    return loads(f"---\nstate: backlog\ncreated: {created_value}\n---\n# Task\n")


def test_date_only_created_is_valid(tmp_path):
    """`created: 2026-05-27` (parsed as datetime.date) must validate cleanly."""
    post = _post("2026-05-27")
    assert isinstance(post.metadata["created"], date)
    assert not isinstance(post.metadata["created"], datetime)

    issues = validate_task_file(tmp_path / "task.md", post)
    assert issues == [], f"date-only created should be valid, got: {issues}"


def test_full_datetime_created_is_valid(tmp_path):
    """Full ISO timestamps (parsed as datetime) remain valid."""
    post = _post("2026-05-27T12:00:00+00:00")
    assert isinstance(post.metadata["created"], datetime)

    issues = validate_task_file(tmp_path / "task.md", post)
    assert issues == [], f"datetime created should be valid, got: {issues}"


def test_quoted_string_created_is_valid(tmp_path):
    """Quoted ISO date strings remain valid."""
    post = _post("'2026-05-27'")
    assert isinstance(post.metadata["created"], str)

    issues = validate_task_file(tmp_path / "task.md", post)
    assert issues == [], f"string created should be valid, got: {issues}"
