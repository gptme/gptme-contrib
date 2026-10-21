"""The root ``requires-python`` floor must not undercut the workspace lock.

Dependabot derives its Python interpreter from the root ``pyproject.toml``
``project.requires-python`` and pins the lowest pre-installed version that
satisfies it. ``uv.lock`` records the intersection of every workspace member's
floor. When the root floor sits below that intersection, every Dependabot
``uv`` update aborts with "incompatible with the project's Python
requirement" before it can open a security PR (gptme-contrib, 2026-10-20/21:
root ``>=3.10`` versus a ``>=3.11`` lock after gptme-review raised its floor).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import tomllib

ROOT = Path(__file__).resolve().parent.parent
_LOWER_BOUND = re.compile(r">=\s*(\d+)\.(\d+)(?:\.(\d+))?")


def _lower_bound(spec: str) -> tuple[int, int, int]:
    match = _LOWER_BOUND.search(spec)
    assert match, f"no >=X.Y[.Z] lower bound in {spec!r}"
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def _lock_requires_python() -> str:
    with (ROOT / "uv.lock").open("rb") as handle:
        return str(tomllib.load(handle)["requires-python"])


def _root_requires_python() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["requires-python"])


@pytest.mark.parametrize(
    ("root_spec", "lock_spec", "accepted"),
    [
        (">=3.11", ">=3.11.12, <4.0", False),
        (">=3.11.11", ">=3.11.12, <4.0", False),
        (">=3.11.12", ">=3.11.12, <4.0", True),
        (">=3.11.13", ">=3.11.12, <4.0", True),
        (">=3.11", ">=3.11.0, <4.0", True),
        (">=3.11.0", ">=3.11, <4.0", True),
        (">=3.12", ">=3.11.12, <4.0", True),
    ],
)
def test_python_floor_comparison_preserves_patch_versions(
    monkeypatch: pytest.MonkeyPatch, root_spec: str, lock_spec: str, accepted: bool
) -> None:
    monkeypatch.setitem(globals(), "_root_requires_python", lambda: root_spec)
    monkeypatch.setitem(globals(), "_lock_requires_python", lambda: lock_spec)
    if accepted:
        test_root_python_floor_matches_workspace_lock()
    else:
        with pytest.raises(AssertionError, match="admits a Python below"):
            test_root_python_floor_matches_workspace_lock()


def test_root_python_floor_matches_workspace_lock() -> None:
    root_spec = _root_requires_python()
    lock_spec = _lock_requires_python()
    assert _lower_bound(root_spec) >= _lower_bound(lock_spec), (
        f"root requires-python {root_spec!r} admits a Python below the workspace "
        f"lock floor {lock_spec!r}; Dependabot pins the lowest root-admitted "
        "interpreter and then fails to resolve uv.lock. Raise the root floor "
        "to the lock's, or lower the member floor that raised the lock."
    )
