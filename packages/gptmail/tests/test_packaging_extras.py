"""Packaging metadata must not pretend to resolve the outbound secret scanner.

``transport/agent.py`` calls ``guard_outbound(..., require_scanner=True)``, which
imports ``redact``. Without it every inter-agent send refuses to deliver, and the
failure used to surface as a misleading "blocked by secret policy"
(gptme-contrib#2051).

Declaring the dependency is not yet possible, and declaring it *wrongly* is worse
than not declaring it:

- PyPI ``redact`` 0.1.1 is an unrelated config-template renderer (Feng Zhou), so a
  bare ``redact`` requirement installs the wrong package and then fails the import.
- An extra naming an unpublished package (e.g. ``gptme-redact``) makes
  ``uv sync --all-extras`` unresolvable, which turns the whole workspace red.

So the requirement stays a documented ``--with /path/to/redact`` install path,
checked at runtime by ``gptmail agent doctor``.
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


def test_no_dependency_names_the_scanner() -> None:
    names = _names(_requirements(tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))))
    assert "redact" not in names, "PyPI redact is an unrelated project"
    assert "gptme-redact" not in names, "an unpublished extra breaks --all-extras"


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
