"""Tests for workspace-extensible frontmatter schema.

Two schemas competing for "what is a valid task field" is worse than one
imperfect schema. Before this, gptodo's KNOWN_FRONTMATTER_FIELDS did not know
several fields a workspace's own pre-commit validator actively enforced, so
`gptodo lint` flagged 284 findings across 205 real tasks — a warning volume
that trains everyone to ignore the warning.

The fix has two halves, both covered here:
  1. Genuinely generic fields (wait_kind, probe, follow_on_from,
     coordination_family) move into the upstream known set.
  2. Workspace-specific fields (Bob's `erik_gate_class` names a specific human)
     register via GPTODO_EXTRA_FRONTMATTER_FIELDS or
     [tool.gptodo] extra_frontmatter_fields in the workspace pyproject.toml.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gptodo.utils import (
    EXTRA_FRONTMATTER_FIELDS_ENV,
    HAVE_TOML_PARSER,
    KNOWN_FRONTMATTER_FIELDS,
    _pyproject_extra_fields,
    lint_frontmatter_fields,
    resolve_known_frontmatter_fields,
)


needs_toml = pytest.mark.skipif(
    not HAVE_TOML_PARSER,
    reason="pyproject config needs tomllib (3.11+) or the tomli backport",
)


@pytest.fixture(autouse=True)
def _clear_pyproject_cache():
    """_pyproject_extra_fields is lru_cached; tests reuse tmp_path names."""
    _pyproject_extra_fields.cache_clear()
    yield
    _pyproject_extra_fields.cache_clear()


@pytest.mark.parametrize(
    "field",
    ["wait_kind", "probe", "follow_on_from", "coordination_family"],
)
def test_generic_workflow_fields_are_known_upstream(field: str) -> None:
    assert field in KNOWN_FRONTMATTER_FIELDS
    assert lint_frontmatter_fields({"state": "waiting", field: "x"}) == []


def test_underscore_discovered_from_is_flagged_as_deprecated() -> None:
    """`discovered-from` is the real field; the underscore twin is never read."""
    findings = lint_frontmatter_fields({"discovered_from": "some-task"})
    assert [f[0] for f in findings] == ["warn-deprecated"]
    assert "discovered-from" in findings[0][2]


def test_unknown_field_still_warns_by_default() -> None:
    findings = lint_frontmatter_fields({"state": "todo", "erik_gate_class": "spend"})
    assert [(sev, fld) for sev, fld, _ in findings] == [("warn-unknown", "erik_gate_class")]


def test_env_var_registers_extra_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXTRA_FRONTMATTER_FIELDS_ENV, "erik_gate_class, house_style")
    known = resolve_known_frontmatter_fields()
    assert {"erik_gate_class", "house_style"} <= known
    assert (
        lint_frontmatter_fields(
            {"erik_gate_class": "spend", "house_style": "loud"}, known_fields=known
        )
        == []
    )


def test_env_var_absent_or_empty_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXTRA_FRONTMATTER_FIELDS_ENV, "   ")
    assert resolve_known_frontmatter_fields() == set(KNOWN_FRONTMATTER_FIELDS)
    monkeypatch.delenv(EXTRA_FRONTMATTER_FIELDS_ENV)
    assert resolve_known_frontmatter_fields() == set(KNOWN_FRONTMATTER_FIELDS)


@needs_toml
def test_pyproject_registers_extra_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(EXTRA_FRONTMATTER_FIELDS_ENV, raising=False)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n\n'
        "[tool.gptodo]\n"
        'extra_frontmatter_fields = ["erik_gate_class", "premise_check"]\n'
    )
    known = resolve_known_frontmatter_fields(tmp_path)
    assert {"erik_gate_class", "premise_check"} <= known
    assert lint_frontmatter_fields({"erik_gate_class": "spend"}, known_fields=known) == []


@needs_toml
def test_deprecated_beats_workspace_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace can't opt back into an anti-design-goal field."""
    monkeypatch.delenv(EXTRA_FRONTMATTER_FIELDS_ENV, raising=False)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.gptodo]\nextra_frontmatter_fields = ["modified"]\n'
    )
    known = resolve_known_frontmatter_fields(tmp_path)
    findings = lint_frontmatter_fields({"modified": "2026-08-05"}, known_fields=known)
    assert [f[0] for f in findings] == ["warn-deprecated"]


@pytest.mark.parametrize(
    "content",
    [
        "",  # no [tool.gptodo]
        "[tool.gptodo]\n",  # section but no key
        '[tool.gptodo]\nextra_frontmatter_fields = "not-a-list"\n',
        "[tool.gptodo]\nextra_frontmatter_fields = [1, 2]\n",  # non-str entries
        "this is not = valid toml [[[",
        'tool = "not-a-dict"\n',  # tool is a scalar, not a table — was AttributeError
        '[tool]\ngptodo = "not-a-dict"\n',  # tool.gptodo scalar — was AttributeError
    ],
)
@needs_toml
def test_malformed_pyproject_degrades_to_base_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """Schema lint is a soft check — bad config must never break a command."""
    monkeypatch.delenv(EXTRA_FRONTMATTER_FIELDS_ENV, raising=False)
    (tmp_path / "pyproject.toml").write_text(content)
    assert resolve_known_frontmatter_fields(tmp_path) == set(KNOWN_FRONTMATTER_FIELDS)


def test_missing_pyproject_degrades_to_base_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(EXTRA_FRONTMATTER_FIELDS_ENV, raising=False)
    assert resolve_known_frontmatter_fields(tmp_path) == set(KNOWN_FRONTMATTER_FIELDS)


@needs_toml
def test_env_and_pyproject_are_additive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXTRA_FRONTMATTER_FIELDS_ENV, "from_env")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.gptodo]\nextra_frontmatter_fields = ["from_toml"]\n'
    )
    known = resolve_known_frontmatter_fields(tmp_path)
    assert {"from_env", "from_toml"} <= known
