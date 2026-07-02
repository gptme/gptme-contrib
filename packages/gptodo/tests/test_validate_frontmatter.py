"""Tests for gptodo.validate_frontmatter schema checks."""

from pathlib import Path


from gptodo.utils import lint_frontmatter_fields
from gptodo.validate_frontmatter import (
    validate_assigned_to_format,
    validate_schema,
    validate_timestamp_syntax,
    validate_unique_frontmatter_keys,
    validate_unquoted_hash_scalars,
)


# ---------------------------------------------------------------------------
# validate_unique_frontmatter_keys
# ---------------------------------------------------------------------------


def test_unique_keys_ok():
    assert validate_unique_frontmatter_keys("state: active\ncreated: 2026-01-01\n") == []


def test_duplicate_key_detected():
    raw = "state: active\nstate: waiting\n"
    errors = validate_unique_frontmatter_keys(raw)
    assert any("duplicate" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# validate_timestamp_syntax
# ---------------------------------------------------------------------------


def test_space_datetime_rejected():
    raw = "created: 2026-07-02 20:00:00\n"
    errors = validate_timestamp_syntax(raw)
    assert len(errors) == 1
    assert "created" in errors[0]
    assert "T" in errors[0]


def test_iso_datetime_ok():
    raw = "created: 2026-07-02T20:00:00+00:00\n"
    assert validate_timestamp_syntax(raw) == []


def test_date_only_ok():
    raw = "created: 2026-07-02\n"
    assert validate_timestamp_syntax(raw) == []


def test_quoted_space_datetime_rejected():
    # Even a quoted space-datetime is wrong — it's not ISO-8601.
    raw = "waiting_since: '2026-07-02 10:00'\n"
    errors = validate_timestamp_syntax(raw)
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# validate_unquoted_hash_scalars
# ---------------------------------------------------------------------------


def test_unquoted_hash_at_start():
    raw = "next_action: #377 fix something\n"
    errors = validate_unquoted_hash_scalars(raw)
    assert len(errors) == 1
    assert "next_action" in errors[0]


def test_unquoted_inline_hash():
    raw = "waiting_for: PR #815 review\n"
    errors = validate_unquoted_hash_scalars(raw)
    assert len(errors) == 1
    assert "waiting_for" in errors[0]


def test_quoted_hash_ok():
    raw = "waiting_for: 'PR #815 review'\n"
    assert validate_unquoted_hash_scalars(raw) == []


def test_hash_in_trailing_comment_flagged():
    # YAML silently truncates at ' #', so even a true end-of-line comment on a
    # value line is flagged.  Authors should either remove the comment or quote
    # the value.  This is intentionally conservative.
    raw = "state: active  # current\n"
    errors = validate_unquoted_hash_scalars(raw)
    assert len(errors) == 1


def test_standalone_comment_line_ignored():
    # A line whose first non-space character is '#' is a comment line, not a scalar.
    raw = "# This is a comment\nstate: active\n"
    assert validate_unquoted_hash_scalars(raw) == []


# ---------------------------------------------------------------------------
# validate_assigned_to_format
# ---------------------------------------------------------------------------


def test_plain_agent_id():
    assert validate_assigned_to_format("bob") == []


def test_agent_at_host():
    assert validate_assigned_to_format("bob@cluster1") == []


def test_agent_at_host_with_display():
    assert validate_assigned_to_format("bob@cluster1:DISPLAY=:1") == []


def test_opaque_session_lock_at_bob():
    """Placement form: opaque lock id at a Bob host.  Must pass schema check."""
    assert validate_assigned_to_format("a3f9@bob") == []


def test_leading_at_rejected():
    errors = validate_assigned_to_format("@alice-vm")
    assert len(errors) == 1
    assert "@" in errors[0]


def test_non_string_rejected():
    errors = validate_assigned_to_format(42)  # type: ignore[arg-type]
    assert len(errors) == 1
    assert "string" in errors[0]


def test_empty_string_rejected():
    errors = validate_assigned_to_format("")
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# validate_schema (integration)
# ---------------------------------------------------------------------------


def _write_task(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "task.md"
    p.write_text(content, encoding="utf-8")
    return p


def test_valid_minimal_task(tmp_path):
    p = _write_task(
        tmp_path,
        "---\nstate: backlog\ncreated: 2026-07-01\n---\n# My task\n",
    )
    assert validate_schema(p) == []


def test_missing_state(tmp_path):
    p = _write_task(tmp_path, "---\ncreated: 2026-07-01\n---\n# task\n")
    errors = validate_schema(p)
    assert any("state" in e for e in errors)


def test_missing_created(tmp_path):
    p = _write_task(tmp_path, "---\nstate: backlog\n---\n# task\n")
    errors = validate_schema(p)
    assert any("created" in e for e in errors)


def test_invalid_state(tmp_path):
    p = _write_task(
        tmp_path,
        "---\nstate: invalid_state\ncreated: 2026-07-01\n---\n# task\n",
    )
    errors = validate_schema(p)
    assert any("state" in e for e in errors)


def test_owner_field_rejected(tmp_path):
    """The `owner` field must trigger a deprecation warning pointing at assigned_to.

    `owner` is handled by lint_frontmatter_fields() (a style warning), not by
    validate_schema() (which covers structural errors only).
    """
    p = _write_task(
        tmp_path,
        "---\nstate: backlog\ncreated: 2026-07-01\nowner: bob\n---\n# task\n",
    )
    # validate_schema covers structural errors — `owner` passes structural checks.
    schema_errors = validate_schema(p)
    assert not any(
        "owner" in e for e in schema_errors
    ), "owner should not be a structural schema error"
    # lint_frontmatter_fields() catches it as a deprecation warning.
    findings = lint_frontmatter_fields({"state": "backlog", "owner": "bob"})
    assert any("owner" in f[1] for f in findings), "owner should be flagged as deprecated"


def test_duplicate_key_caught_by_schema(tmp_path):
    raw = "---\nstate: backlog\nstate: active\ncreated: 2026-07-01\n---\n# task\n"
    p = _write_task(tmp_path, raw)
    errors = validate_schema(p)
    assert any("duplicate" in e.lower() for e in errors)


def test_space_datetime_caught_by_schema(tmp_path):
    p = _write_task(
        tmp_path,
        "---\nstate: backlog\ncreated: 2026-07-01 10:00:00\n---\n# task\n",
    )
    errors = validate_schema(p)
    assert any("T" in e for e in errors)


def test_unquoted_hash_caught_by_schema(tmp_path):
    p = _write_task(
        tmp_path,
        "---\nstate: backlog\ncreated: 2026-07-01\nwaiting_for: PR #815 fix\n---\n# task\n",
    )
    errors = validate_schema(p)
    assert any("#" in e or "hash" in e.lower() or "comment" in e.lower() for e in errors)


def test_placement_form_passes_schema(tmp_path):
    """assigned_to: bob@cluster1 must NOT trigger a schema error."""
    p = _write_task(
        tmp_path,
        "---\nstate: backlog\ncreated: 2026-07-01\nassigned_to: bob@cluster1\n---\n# task\n",
    )
    assert validate_schema(p) == []


def test_opaque_session_at_host_passes_schema(tmp_path):
    """assigned_to: a3f9@bob (opaque lock id) must NOT trigger a schema error."""
    p = _write_task(
        tmp_path,
        "---\nstate: backlog\ncreated: 2026-07-01\nassigned_to: a3f9@bob\n---\n# task\n",
    )
    assert validate_schema(p) == []
