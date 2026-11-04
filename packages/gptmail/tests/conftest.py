"""Explicit clean scanner for legacy agent transport/CLI behavior tests."""

import sys
import types
from types import SimpleNamespace

import pytest


@pytest.fixture
def clean_agent_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep filesystem/CLI tests independent of workspace secret inventories."""
    scanner = types.ModuleType("redact")
    scanner.collect_secret_literals = lambda: set()  # type: ignore[attr-defined]
    scanner.redact = lambda text, literals: SimpleNamespace(  # type: ignore[attr-defined]
        blocked=False, block_reasons=[]
    )
    monkeypatch.setitem(sys.modules, "redact", scanner)
