"""Tests for the explicit upstream Pi parser/model compatibility sentinel."""

from __future__ import annotations

import importlib.util
import io
import json
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_pi_compat.py"
FIXTURES = Path(__file__).parent / "fixtures" / "pi"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_pi_compat", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compat = _load_script()


def _models_from_fixtures() -> frozenset[tuple[str, str]]:
    models: set[tuple[str, str]] = set()
    for path in sorted(FIXTURES.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                continue
            if record.get("type") == "model_change":
                provider = record.get("provider")
                model_id = record.get("modelId")
                if isinstance(provider, str) and isinstance(model_id, str):
                    models.add((provider, model_id))
            message = record.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                provider = message.get("provider")
                model_id = message.get("model")
                if isinstance(provider, str) and isinstance(model_id, str):
                    models.add((provider, model_id))
    return frozenset(models)


def _catalogs_for_models(models: frozenset[tuple[str, str]]) -> dict[str, bytes]:
    by_provider: dict[str, dict[str, dict[str, str]]] = {}
    for provider, model_id in models:
        by_provider.setdefault(provider, {})[model_id] = {
            "provider": provider,
            "id": model_id,
        }
    catalogs: dict[str, bytes] = {}
    for filename in compat.PINNED_MODEL_CATALOG_HASHES:
        if filename == ".manifest.json":
            catalogs[filename] = b"{}"
            continue
        provider = filename.removesuffix(".json")
        catalogs[filename] = json.dumps({"api": by_provider.get(provider, {})}).encode()
    return catalogs


def _source_archive(
    *,
    session_version: int = compat.PINNED_SESSION_VERSION,
    entry_types: frozenset[str] = compat.PINNED_ENTRY_TYPES,
    stop_reasons: frozenset[str] = compat.PINNED_STOP_REASONS,
    catalogs: dict[str, bytes] | None = None,
) -> bytes:
    interfaces = "\n".join(
        f'export interface {entry_type.title().replace("_", "")}Entry {{ type: "{entry_type}"; }}'
        for entry_type in sorted(entry_types)
    )
    union = " | ".join(
        f"{entry_type.title().replace('_', '')}Entry" for entry_type in sorted(entry_types)
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
    catalogs = _catalogs_for_models(compat.PINNED_FIXTURE_MODELS)
    monkeypatch.setattr(
        compat,
        "PINNED_MODEL_CATALOG_HASHES",
        {name: compat.hashlib.sha256(raw).hexdigest() for name, raw in catalogs.items()},
    )

    assert compat.check_archive(_source_archive(catalogs=catalogs)) == []


def test_check_archive_reports_parser_and_catalog_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalogs = _catalogs_for_models(compat.PINNED_FIXTURE_MODELS)
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
    catalogs = {name: b"{}" for name in compat.PINNED_MODEL_CATALOG_HASHES}
    monkeypatch.setattr(
        compat,
        "PINNED_MODEL_CATALOG_HASHES",
        {name: compat.hashlib.sha256(raw).hexdigest() for name, raw in catalogs.items()},
    )

    failures = compat.check_archive(_source_archive(catalogs=catalogs))

    assert failures == [
        f"fixture models missing from Pi catalog: {sorted(compat.PINNED_FIXTURE_MODELS)}"
    ]


def test_check_archive_reports_unhashed_fixture_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        compat,
        "PINNED_FIXTURE_MODELS",
        frozenset({("google-antigravity", "claude-opus-4-5-thinking")}),
    )
    catalogs = {name: b"{}" for name in compat.PINNED_MODEL_CATALOG_HASHES}
    monkeypatch.setattr(
        compat,
        "PINNED_MODEL_CATALOG_HASHES",
        {name: compat.hashlib.sha256(raw).hexdigest() for name, raw in catalogs.items()},
    )

    failures = compat.check_archive(_source_archive(catalogs=catalogs))

    assert any(
        "pinned catalogs missing for fixture providers: google-antigravity.json" in failure
        for failure in failures
    )


def test_retained_fixture_models_are_pinned_or_retired() -> None:
    observed = _models_from_fixtures()
    assert compat.RETIRED_FIXTURE_MODELS <= observed
    assert compat.PINNED_FIXTURE_MODELS == observed - compat.RETIRED_FIXTURE_MODELS
    assert not (compat.PINNED_FIXTURE_MODELS & compat.RETIRED_FIXTURE_MODELS)
    needed_catalogs = {f"{provider}.json" for provider, _model in compat.PINNED_FIXTURE_MODELS}
    hashed_catalogs = {
        name for name in compat.PINNED_MODEL_CATALOG_HASHES if name != ".manifest.json"
    }
    assert needed_catalogs <= hashed_catalogs
