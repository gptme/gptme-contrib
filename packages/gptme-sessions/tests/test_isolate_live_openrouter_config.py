"""Default OpenRouter resolution must not read operator gptme config."""

from __future__ import annotations

from pathlib import Path

import pytest

from gptme_sessions import judge


def test_default_openrouter_resolver_does_not_read_live_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "synthetic-default.toml"
    config.write_text('[env]\nOPENROUTER_API_KEY="synthetic-default"\n')
    # These are the original import-bound defaults, not the patched module name.
    default_paths = judge._resolve_openrouter_api_key.__kwdefaults__["config_paths"]
    real_exists = Path.exists
    real_open = Path.open

    def exists(path: Path) -> bool:
        return path in default_paths or real_exists(path)

    def open_path(path: Path, *args, **kwargs):
        return real_open(config if path in default_paths else path, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "open", open_path)
    for context in ("JUDGE", "EVAL", "LLM_JUDGE"):
        resolved = judge._resolve_openrouter_api_key(context, environ={})
        if resolved is not None:
            raise AssertionError(
                f"default {context} resolver returned a credential; "
                "autouse isolation failed (value omitted)"
            )
    assert (
        judge._resolve_openrouter_api_key(
            "JUDGE",
            environ={"OPENROUTER_API_KEY_JUDGE": "synthetic-working"},
            config_paths=(),
        )
        == "synthetic-working"
    )


def test_explicit_config_paths_still_load(tmp_path) -> None:
    config = tmp_path / "synthetic.toml"
    config.write_text('[env]\nOPENROUTER_API_KEY_JUDGE="synthetic-config"\n')
    assert (
        judge._resolve_openrouter_api_key(
            "JUDGE",
            environ={},
            config_paths=(config,),
        )
        == "synthetic-config"
    )
