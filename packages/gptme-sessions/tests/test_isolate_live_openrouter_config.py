"""Default OpenRouter resolution must not read operator gptme config."""

from __future__ import annotations

from gptme_sessions import judge


def test_default_openrouter_resolver_does_not_read_live_config() -> None:
    for context in ("JUDGE", "EVAL", "LLM_JUDGE"):
        resolved = judge._resolve_openrouter_api_key(context)
        if resolved is not None:
            raise AssertionError(
                f"default {context} resolver returned a live credential; "
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
