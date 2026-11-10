"""Packaging metadata must name the outbound secret scanner correctly.

``transport/agent.py`` calls ``guard_outbound(..., require_scanner=True)``, which
imports ``redact``. Without it every inter-agent send refuses to deliver
(gptme-contrib#2051).

- PyPI ``redact`` 0.1.1 is an unrelated config-template renderer (Feng Zhou), so a
  bare ``redact`` requirement installs the wrong package and then fails the import.
- The scanner's distribution is ``gptme-redact`` (gptme-contrib#2055), a workspace
  member, so the ``secrets`` extra resolves under ``uv sync --all-extras``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _requirements(metadata: dict) -> list[str]:
    project = metadata["project"]
    reqs = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        reqs.extend(group)
    return reqs


def _names(requirements: list[str]) -> set[str]:
    return {canonicalize_name(Requirement(req).name) for req in requirements}


WORKSPACE = PYPROJECT.parents[2] / "pyproject.toml"


def test_no_dependency_names_bare_redact() -> None:
    names = _names(_requirements(tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))))
    assert "redact" not in names, "PyPI redact is an unrelated project"


def test_secrets_extra_names_the_scanner_distribution() -> None:
    extras = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    assert _names(extras["secrets"]) == {"gptme-redact"}
    # gptmail supports 3.10 but the scanner needs 3.11; an unmarked requirement
    # would make `gptmail[secrets,...]` uninstallable on 3.10.
    (req,) = (Requirement(r) for r in extras["secrets"])
    assert req.marker is not None
    assert req.marker.evaluate({"python_version": "3.10"}) is False
    assert req.marker.evaluate({"python_version": "3.11"}) is True


def test_scanner_distribution_is_a_workspace_member() -> None:
    # An extra naming a package uv cannot resolve breaks `uv sync --all-extras`.
    sources = tomllib.loads(WORKSPACE.read_text(encoding="utf-8"))["tool"]["uv"]["sources"]
    assert sources["gptme-redact"] == {"workspace": True}
    assert (WORKSPACE.parent / "packages" / "gptme-redact" / "pyproject.toml").is_file()


@pytest.mark.parametrize(
    ("requirement", "name"),
    [
        ("redact<1", "redact"),
        ("redact[templates]>=0.1", "redact"),
        ('REDACT!=0.1; python_version >= "3.10"', "redact"),
        ("redact @ https://example.invalid/redact.whl", "redact"),
        ("gptme_redact~=0.1", "gptme-redact"),
        ("GPTME.Redact[scanner]<=1", "gptme-redact"),
        ("requests>=2.28", "requests"),
    ],
)
def test_requirement_names(requirement: str, name: str) -> None:
    assert _names([requirement]) == {name}
