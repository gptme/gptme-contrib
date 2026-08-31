"""Tests for the explicit upstream Pi parser/model compatibility sentinel."""

from __future__ import annotations

import importlib.util
import io
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_pi_compat.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_pi_compat", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compat = _load_script()


def _source_archive(
    *,
    session_version: int = compat.PINNED_SESSION_VERSION,
    entry_types: frozenset[str] = compat.PINNED_ENTRY_TYPES,
    stop_reasons: frozenset[str] = compat.PINNED_STOP_REASONS,
    catalogs: dict[str, bytes] | None = None,
) -> bytes:
    interfaces = "\n".join(
        f'export interface {entry_type.title().replace("_", "")}Entry '
        f'{{ type: "{entry_type}"; }}'
        for entry_type in sorted(entry_types)
    )
    union = " | ".join(
        f'{entry_type.title().replace("_", "")}Entry' for entry_type in sorted(entry_types)
    )
    session_manager = (
        f"export const CURRENT_SESSION_VERSION = {session_version};\n"
        f"{interfaces}\nexport type SessionEntry = {union};\n"
    ).encode()
    ai_types = (
        "export type StopReason = "
        + " | ".join(f'"{reason}"' for reason in sorted(stop_reasons))
        + ";\n"
    ).encode()

    if catalogs is None:
        catalogs = {filename: b"{}" for filename in compat.PINNED_MODEL_CATALOG_HASHES}

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        members = {
            compat._SESSION_MANAGER: session_manager,
            compat._AI_TYPES: ai_types,
            **{f"{compat._MODEL_DATA_DIR}/{filename}": raw for filename, raw in catalogs.items()},
        }
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_check_archive_accepts_exact_pinned_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    catalogs = {
        ".manifest.json": b"{}",
        "openai-codex.json": (b'{"api":{"luna":{"provider":"openai-codex","id":"gpt-5.6-luna"}}}'),
        "xai.json": b'{"api":{"grok":{"provider":"xai","id":"grok-4.6"}}}',
    }
    monkeypatch.setattr(
        compat,
        "PINNED_MODEL_CATALOG_HASHES",
        {name: compat.hashlib.sha256(raw).hexdigest() for name, raw in catalogs.items()},
    )

    assert compat.check_archive(_source_archive(catalogs=catalogs)) == []


def test_check_archive_reports_parser_and_catalog_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalogs = {
        ".manifest.json": b"{}",
        "openai-codex.json": (b'{"api":{"luna":{"provider":"openai-codex","id":"gpt-5.6-luna"}}}'),
        "xai.json": b'{"api":{"grok":{"provider":"xai","id":"grok-4.6"}}}',
    }
    expected_hashes = {
        name: compat.hashlib.sha256(raw).hexdigest() for name, raw in catalogs.items()
    }
    expected_hashes["xai.json"] = "0" * 64
    monkeypatch.setattr(compat, "PINNED_MODEL_CATALOG_HASHES", expected_hashes)

    failures = compat.check_archive(
        _source_archive(
            session_version=4,
            entry_types=compat.PINNED_ENTRY_TYPES | {"checkpoint"},
            stop_reasons=compat.PINNED_STOP_REASONS | {"paused"},
            catalogs=catalogs,
        )
    )

    assert any("session version drift" in failure for failure in failures)
    assert any("checkpoint" in failure for failure in failures)
    assert any("paused" in failure for failure in failures)
    assert any("model catalog drift in xai.json" in failure for failure in failures)


def test_check_archive_reports_fixture_model_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalogs = {
        ".manifest.json": b"{}",
        "openai-codex.json": b"{}",
        "xai.json": b"{}",
    }
    monkeypatch.setattr(
        compat,
        "PINNED_MODEL_CATALOG_HASHES",
        {name: compat.hashlib.sha256(raw).hexdigest() for name, raw in catalogs.items()},
    )

    failures = compat.check_archive(_source_archive(catalogs=catalogs))

    assert failures == [
        "fixture models missing from Pi catalog: "
        "[('openai-codex', 'gpt-5.6-luna'), ('xai', 'grok-4.6')]"
    ]
