"""Every workspace package gptme-review imports must be a declared dependency.

An undeclared import works inside the monorepo venv (every package is
installed) and fails on a host that installs gptme-review alone.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

PACKAGE_DIR = Path(__file__).resolve().parents[1]
PACKAGES_DIR = PACKAGE_DIR.parent


def _workspace_modules() -> dict[str, str]:
    """Map import name -> distribution name for every workspace package."""
    modules: dict[str, str] = {}
    for pyproject in PACKAGES_DIR.glob("*/pyproject.toml"):
        name = tomllib.loads(pyproject.read_text())["project"]["name"]
        for src in (pyproject.parent / "src").glob("*/__init__.py"):
            modules[src.parent.name] = name
    return modules


def _imported_top_levels() -> set[str]:
    found: set[str] = set()
    for path in (PACKAGE_DIR / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                found.add(node.module.split(".")[0])
    return found


def _declared() -> set[str]:
    project = tomllib.loads((PACKAGE_DIR / "pyproject.toml").read_text())["project"]
    return {
        re.split(r"[\s;<>=!~\[]", dep, maxsplit=1)[0].lower()
        for dep in project.get("dependencies", [])
    }


def test_workspace_imports_are_declared_dependencies() -> None:
    workspace = _workspace_modules()
    own = tomllib.loads((PACKAGE_DIR / "pyproject.toml").read_text())["project"]["name"]
    needed = {
        workspace[mod]
        for mod in _imported_top_levels()
        if mod in workspace and workspace[mod] != own
    }
    missing = sorted(needed - _declared())
    assert (
        not missing
    ), f"imported but not declared in pyproject dependencies: {missing}"
