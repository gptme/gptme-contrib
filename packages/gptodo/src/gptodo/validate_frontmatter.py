"""Schema validation for gptodo task frontmatter.

Validates the structural contract of the gptodo task format — field types,
enum values, timestamp encoding, and YAML syntax invariants. Designed for
use in precommit hooks and via ``gptodo lint``.

Agent-policy heuristics (peer-ownership waiting rules, PR-queue gates,
terminal-state cleanup) live in the consuming agent's local precommit hook
and are NOT part of this module.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from yaml.constructor import ConstructorError  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Regex that matches a YAML frontmatter block at the top of a file.
_FRONTMATTER_BLOCK_RE = re.compile(r"(?ms)^---\s*$\n(.*?)^---\s*$")

# Matches timestamp field lines in raw YAML text.
_TIMESTAMP_FIELD_RE = re.compile(
    r"^\s*(created|created_at|modified|waiting_since|wait)\s*:\s*(.+?)\s*$"
)

# Detects space-separated datetime (e.g. ``2026-07-02 20:00:00``) before YAML
# silently coerces them to a datetime object.
_SPACE_DATETIME_RE = re.compile(r"^['\"]?\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}")

# Top-level scalar: ``field: value``.  Leading whitespace = continuation.
_SCALAR_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.+?)\s*$")

# Basic ``agentid[@host[:display]]`` shape for ``assigned_to`` values.
# The agent id and host are ``[A-Za-z0-9_-]+``; the display part after the
# optional second ``:`` is free-form.  An ``@host``-leading value (no agent)
# must be YAML-quoted — the raw ``@`` at the start of a YAML value is
# unambiguously valid but we flag it for clarity.
_ASSIGNED_TO_RE = re.compile(
    r"^(?P<agent>[A-Za-z0-9_-]+)(?:@(?P<host>[A-Za-z0-9_.-]+)(?::(?P<display>.+))?)?$"
)


# ---------------------------------------------------------------------------
# Internal YAML helpers
# ---------------------------------------------------------------------------


class _UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that raises on duplicate mapping keys."""


def _construct_no_duplicates(
    loader: _UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key ({key!r})",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_no_duplicates,
)


# ---------------------------------------------------------------------------
# Raw-text checks (must run on the unparsed YAML string)
# ---------------------------------------------------------------------------


def validate_unique_frontmatter_keys(raw_yaml: str) -> list[str]:
    """Return errors for duplicate frontmatter keys.

    YAML silently uses the last value when keys are duplicated; this check
    makes the collision visible before data is lost.
    """
    try:
        yaml.load(raw_yaml, Loader=_UniqueKeyLoader)
    except ConstructorError as exc:
        return [f"Duplicate frontmatter key: {exc.problem}"]
    except yaml.YAMLError:
        # Other parse errors are surfaced elsewhere; don't double-report.
        return []
    return []


def validate_timestamp_syntax(raw_yaml: str) -> list[str]:
    """Return errors for space-separated datetime values.

    YAML's implicit datetime coercion converts ``2026-07-02 20:00:00`` to a
    Python ``datetime`` (no T separator), stripping the literal value.  Force
    ISO-8601 T-format so the raw string is preserved and round-trips cleanly.
    """
    errors: list[str] = []
    for line in raw_yaml.splitlines():
        m = _TIMESTAMP_FIELD_RE.match(line)
        if not m:
            continue
        field, raw_value = m.groups()
        # Strip trailing inline comments before checking.
        value = raw_value.split("#", maxsplit=1)[0].strip()
        if _SPACE_DATETIME_RE.match(value):
            errors.append(
                f"Field '{field}': use 'T' between date and time "
                f"(got {value!r}; ISO-8601 requires 'YYYY-MM-DDTHH:MM')"
            )
    return errors


def validate_unquoted_hash_scalars(raw_yaml: str) -> list[str]:
    """Return errors for unquoted scalars that contain ``#``.

    A bare ``#`` after whitespace starts a YAML comment, silently truncating
    the value.  A value that *begins* with ``#`` is treated as a comment and
    becomes ``null``.  Both must be quoted.
    """
    errors: list[str] = []
    for line in raw_yaml.splitlines():
        stripped = line.strip()
        # Skip comments, blank lines, and continuation lines.
        if not stripped or stripped.startswith("#") or line[:1] in (" ", "\t"):
            continue
        m = _SCALAR_LINE_RE.match(line)
        if not m:
            continue
        field, raw_value = m.groups()
        value = raw_value.lstrip()
        # Already quoted or a block/flow indicator.
        if value[:1] in ("'", '"', "[", "{", "|", ">"):
            continue
        if value.startswith("#"):
            errors.append(
                f"Field '{field}': unquoted '#' at start — YAML treats "
                "it as a comment and the value becomes null. "
                "Quote the entire value."
            )
        elif " #" in value:
            errors.append(
                f"Field '{field}': unquoted ' #' — YAML treats the rest "
                "as a comment and truncates the value. "
                "Quote the entire value."
            )
    return errors


# ---------------------------------------------------------------------------
# Parsed-metadata checks
# ---------------------------------------------------------------------------


def validate_assigned_to_format(value: Any) -> list[str]:
    """Return errors when ``assigned_to`` does not match the expected shape.

    Valid forms:
    - ``agentid``                    e.g. ``bob``, ``alice``
    - ``agentid@host``               e.g. ``bob@cluster1``
    - ``agentid@host:display``       e.g. ``bob@cluster1:DISPLAY=:1``
    - ``<session-token>@host``       e.g. ``a3f9@bob`` (opaque lock id)

    A value beginning with ``@`` (no agent prefix) is technically valid YAML
    when quoted but is flagged here because leading-``@`` values must be
    YAML-quoted — authors often omit the quotes and silently produce a
    parse error.
    """
    if not isinstance(value, str):
        return ["assigned_to must be a string"]
    if not value.strip():
        return ["assigned_to cannot be empty"]
    v = value.strip()
    if v.startswith("@"):
        return [
            f"assigned_to '{v}' starts with '@' — YAML requires this value "
            "to be quoted (leading '@' is a YAML indicator character in "
            "some contexts). Use 'agent@host' form or quote the value."
        ]
    if not _ASSIGNED_TO_RE.fullmatch(v):
        return [
            f"assigned_to '{v}' does not match the expected shape "
            "'agentid[@host[:display]]'. "
            "Characters must be alphanumeric, dash, or underscore "
            "(plus '@' and ':' as separators)."
        ]
    return []


# ---------------------------------------------------------------------------
# High-level entry point
# ---------------------------------------------------------------------------


def validate_schema(file: Path) -> list[str]:
    """Run structural schema checks on a task file and return a list of errors.

    Covers YAML-level invariants (duplicate keys, unquoted '#', space-separated
    timestamps) and parsed-metadata invariants (required fields, valid enum
    values, assigned_to format).

    Does NOT flag deprecated or unknown field names — those are style warnings
    handled by ``lint_frontmatter_fields()`` and surfaced by ``gptodo lint``.

    Returns an empty list when the file is structurally valid.
    """
    # Defer imports to avoid circular dependencies on gptodo internals.
    from gptodo.utils import validate_task_file  # noqa: PLC0415
    from gptodo.frontmatter_compat import frontmatter  # noqa: PLC0415

    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"Cannot read file: {exc}"]

    errors: list[str] = []

    # Extract the raw frontmatter block for text-level checks.
    fm_match = _FRONTMATTER_BLOCK_RE.search(text)
    if fm_match:
        raw_yaml = fm_match.group(1)
        errors.extend(validate_timestamp_syntax(raw_yaml))
        errors.extend(validate_unique_frontmatter_keys(raw_yaml))
        errors.extend(validate_unquoted_hash_scalars(raw_yaml))

    # Parse the frontmatter.
    try:
        post = frontmatter.load(file)
    except Exception as exc:
        errors.append(f"Failed to parse frontmatter: {exc}")
        return errors

    metadata: dict[str, Any] = post.metadata

    # Delegate core field checks (required fields, enum values) to validate_task_file.
    errors.extend(validate_task_file(file, post))

    # assigned_to shape check.
    if "assigned_to" in metadata:
        errors.extend(validate_assigned_to_format(metadata["assigned_to"]))

    return errors
