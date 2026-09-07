"""Shared AI-review blocking policy consumed by merge gates and renderers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTRIB_LIB_SRC = REPO_ROOT / "packages" / "gptme-contrib-lib" / "src"
sys.path.insert(0, str(_CONTRIB_LIB_SRC))
from gptme_contrib_lib.ai_review_policy import blocking_shortfall  # type: ignore[import-not-found]  # noqa: E402,I001

MODULE_PATH = REPO_ROOT / "scripts" / "github" / "self-merge-check.py"
spec = importlib.util.spec_from_file_location(
    "self_merge_check_shared_policy", MODULE_PATH
)
assert spec and spec.loader
smc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smc
spec.loader.exec_module(smc)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "fp": "deadbeef1234",
        "severity": "P1",
        "isResolved": False,
        "replied": False,
        "superseded": False,
        "disposition": None,
    }
    row.update(overrides)
    return row


def test_open_p1_blocks() -> None:
    assert blocking_shortfall([_row()]) == [
        {
            "fp": "deadbeef1234",
            "severity": "P1",
            "reason": "P1 finding is not resolved",
        }
    ]


def test_resolution_requires_reply() -> None:
    assert blocking_shortfall([_row(isResolved=True)]) == [
        {
            "fp": "deadbeef1234",
            "severity": "P1",
            "reason": "P1 finding resolved without a reply (not disposed)",
        }
    ]
    assert blocking_shortfall([_row(isResolved=True, replied=True)]) == []


def test_explicit_disposition_or_supersession_clears() -> None:
    assert blocking_shortfall([_row(disposition={"reason": "rejected"})]) == []
    assert blocking_shortfall([_row(superseded=True)]) == []


def test_unreadable_severity_fails_closed_but_p2_does_not_block() -> None:
    assert blocking_shortfall([_row(severity=None)])[0]["reason"] == (
        "finding with unreadable severity is not resolved"
    )
    assert blocking_shortfall([_row(severity="P2")]) == []


def test_merge_gate_imports_the_shared_predicate() -> None:
    assert smc.blocking_shortfall is blocking_shortfall
