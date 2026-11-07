"""Packaging metadata must declare the outbound secret scanner.

``transport/agent.py`` calls ``guard_outbound(..., require_scanner=True)``, which
imports ``redact``. Without it every inter-agent send refuses to deliver, and the
failure surfaced as a misleading "blocked by secret policy" (gptme-contrib#2051).
Declaring the requirement in ``[project.optional-dependencies].secrets`` keeps the
dependency visible in the metadata even though ``redact`` is not on PyPI yet.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_secrets_extra_declares_the_scanner() -> None:
    metadata = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    secrets = metadata["project"]["optional-dependencies"]["secrets"]
    names = {req.split(">")[0].split("=")[0].strip().lower() for req in secrets}
    assert "gptme-redact" in names


def test_secrets_extra_never_names_the_look_alike_package() -> None:
    """PyPI `redact` 0.1.1 is an unrelated project (Feng Zhou). Naming it would
    install the wrong distribution and fail the import, so the requirement must
    stay on the name we control."""
    metadata = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    secrets = metadata["project"]["optional-dependencies"]["secrets"]
    names = {req.split(">")[0].split("=")[0].strip().lower() for req in secrets}
    assert "redact" not in names
