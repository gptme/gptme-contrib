from __future__ import annotations

import builtins
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from gptmail.communication_utils.outbound_redact import guard_outbound


def test_guard_outbound_allows_when_optional_redact_package_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_import = builtins.__import__

    def import_without_redact(name: str, *args: object, **kwargs: object) -> object:
        if name == "redact":
            raise ImportError("redact is optional")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_redact)

    assert guard_outbound("ordinary text", "email", tmp_path) is True
    assert "redact package not available" in caplog.text
    assert not (tmp_path / "state" / "outbound-redact-blocks.jsonl").exists()


def test_guard_outbound_blocks_and_records_only_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = SimpleNamespace(
        blocked=True,
        block_reasons=["1× api_key (critical)"],
    )
    redact_stub = types.ModuleType("redact")
    redact_stub.collect_secret_literals = lambda: {"literal-secret"}  # type: ignore[attr-defined]
    redact_stub.redact = lambda text, literals: result  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redact", redact_stub)

    assert guard_outbound("literal-secret", "twitter", tmp_path) is False

    ledger = tmp_path / "state" / "outbound-redact-blocks.jsonl"
    record = json.loads(ledger.read_text())
    assert record["channel"] == "twitter"
    assert record["reasons"] == ["1× api_key (critical)"]
    assert "literal-secret" not in ledger.read_text()


def test_guard_outbound_allows_clean_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = SimpleNamespace(blocked=False, block_reasons=[])
    redact_stub = types.ModuleType("redact")
    redact_stub.collect_secret_literals = lambda: set()  # type: ignore[attr-defined]
    redact_stub.redact = lambda text, literals: result  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "redact", redact_stub)

    assert guard_outbound("ordinary text", "email", tmp_path) is True
    assert not (tmp_path / "state" / "outbound-redact-blocks.jsonl").exists()
