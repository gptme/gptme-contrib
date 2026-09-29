"""Tests for subscription-token-probe.py _default_slots() filesystem discovery."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_PATH = REPO_ROOT / "scripts" / "subscription-token-probe.py"


def _load_probe_module(creds_dir: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "subscription_token_probe", PROBE_PATH
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Override after exec so _default_slots() picks up the patched value at call time
    # (functions read module globals at call time, not at definition time).
    mod.CREDS_DIR = creds_dir  # type: ignore[attr-defined]
    return mod


def test_default_slots_respects_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GPTME_SUBSCRIPTION_SLOTS", "alice, bob ,  carol")
    mod = _load_probe_module(tmp_path)
    assert mod._default_slots() == ["alice", "bob", "carol"]


def test_default_slots_discovers_from_credentials_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GPTME_SUBSCRIPTION_SLOTS", raising=False)
    (tmp_path / ".credentials.json.alice").write_text("{}")
    (tmp_path / ".credentials.json.bob").write_text("{}")
    # Non-matching file must be ignored.
    (tmp_path / ".credentials.json").symlink_to(tmp_path / ".credentials.json.bob")
    (tmp_path / "unrelated.txt").write_text("ignored")

    mod = _load_probe_module(tmp_path)
    assert mod._default_slots() == ["alice", "bob"]


def test_default_slots_returns_empty_when_no_creds_and_no_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GPTME_SUBSCRIPTION_SLOTS", raising=False)
    mod = _load_probe_module(tmp_path)
    assert mod._default_slots() == []


def test_default_slots_returns_empty_on_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GPTME_SUBSCRIPTION_SLOTS", raising=False)
    nonexistent = tmp_path / "missing"
    mod = _load_probe_module(nonexistent)
    assert mod._default_slots() == []
