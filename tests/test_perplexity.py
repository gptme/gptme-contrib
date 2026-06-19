"""Tests for scripts/perplexity.py model resolution and citation extraction."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

# Add scripts dir to path so we can import the PEP 723 script module
SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

# `openai` is a PEP 723 inline dep of the script, not a gptme-contrib package
# dep, so it may be absent in the test env. Inject a stub before import (same
# pattern as tests/test_twitter_eval_prompt.py).
if "openai" not in sys.modules:
    _fake_openai = types.ModuleType("openai")
    _fake_openai.OpenAI = object  # type: ignore[attr-defined]
    sys.modules["openai"] = _fake_openai

spec = importlib.util.spec_from_file_location(
    "perplexity",
    SCRIPT_DIR / "perplexity.py",
)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class _FakeResponse:
    """Minimal stand-in for an OpenAI response exposing model_dump()."""

    def __init__(self, payload: dict):
        self._payload = payload

    def model_dump(self) -> dict:
        return self._payload


def test_default_model_is_a_current_sonar_model():
    """Guard against regressing to a retired llama-3.1-sonar-* model."""
    assert mod.DEFAULT_MODEL == "sonar"
    assert "llama" not in mod.DEFAULT_MODEL


def test_get_model_defaults_when_env_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PERPLEXITY_MODEL", raising=False)
    assert mod.PerplexitySearch._get_model() == mod.DEFAULT_MODEL


def test_get_model_honors_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PERPLEXITY_MODEL", "sonar-pro")
    assert mod.PerplexitySearch._get_model() == "sonar-pro"


def test_extract_citations_prefers_flat_citations_list():
    resp = _FakeResponse({"citations": ["https://a.example", "https://b.example"]})
    assert mod._extract_citations(resp) == ["https://a.example", "https://b.example"]


def test_extract_citations_falls_back_to_search_results():
    resp = _FakeResponse(
        {"search_results": [{"url": "https://c.example"}, {"title": "no url"}]}
    )
    assert mod._extract_citations(resp) == ["https://c.example"]


def test_extract_citations_empty_when_no_sources():
    assert mod._extract_citations(_FakeResponse({})) == []


def test_extract_citations_handles_object_without_model_dump():
    assert mod._extract_citations(object()) == []
