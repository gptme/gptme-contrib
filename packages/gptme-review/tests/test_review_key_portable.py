"""review_api_key() must work without a workspace scripts/openrouter_keys.py."""

import sys
from pathlib import Path

import pytest
from gptme_review import core


def _write_config(path: Path, env: dict[str, str]) -> Path:
    body = "[env]\n" + "".join(f'{k} = "{v}"\n' for k, v in env.items())
    path.write_text(body)
    return path


def test_scoped_key_in_config_beats_generic_env(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path / "c.toml", {"OPENROUTER_API_KEY_REVIEW": "cfg-review"}
    )
    got = core._resolve_review_key_portable(
        environ={"OPENROUTER_API_KEY": "env-generic"}, config_paths=(cfg,)
    )
    assert got == "cfg-review"


def test_env_beats_config_within_same_name(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path / "c.toml", {"OPENROUTER_API_KEY_REVIEW": "cfg-review"}
    )
    got = core._resolve_review_key_portable(
        environ={"OPENROUTER_API_KEY_REVIEW": "env-review"}, config_paths=(cfg,)
    )
    assert got == "env-review"


def test_local_config_overrides_base_and_generic_fallback(tmp_path: Path) -> None:
    base = _write_config(tmp_path / "base.toml", {"OPENROUTER_API_KEY": "base"})
    local = _write_config(tmp_path / "local.toml", {"OPENROUTER_API_KEY": "local"})
    got = core._resolve_review_key_portable(environ={}, config_paths=(base, local))
    assert got == "local"


def test_missing_or_broken_config_resolves_none(tmp_path: Path) -> None:
    broken = tmp_path / "broken.toml"
    broken.write_text("not = [valid")
    got = core._resolve_review_key_portable(
        environ={"OPENROUTER_API_KEY_REVIEW": "  "},
        config_paths=(tmp_path / "missing.toml", broken),
    )
    assert got is None


def test_review_api_key_falls_back_without_workspace_resolver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # sys.modules[name] = None makes `import name` raise ImportError.
    monkeypatch.setitem(sys.modules, "openrouter_keys", None)
    cfg = _write_config(tmp_path / "c.toml", {"OPENROUTER_API_KEY_REVIEW": "portable"})
    monkeypatch.setattr(core, "REVIEW_KEY_CONFIG_PATHS", (cfg,))
    monkeypatch.delenv("OPENROUTER_API_KEY_REVIEW", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert core.review_api_key() == "portable"


@pytest.mark.parametrize(
    ("resolver_source", "error_type", "error_match"),
    [
        (
            "import missing_review_resolver_dependency\n",
            ModuleNotFoundError,
            "missing_review_resolver_dependency",
        ),
        (
            "# No resolver function exported\n",
            ImportError,
            "resolve_openrouter_api_key",
        ),
    ],
)
def test_review_api_key_propagates_broken_workspace_resolver(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    resolver_source: str,
    error_type: type[ImportError],
    error_match: str,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "openrouter_keys.py").write_text(resolver_source)
    monkeypatch.setattr(core, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    # Register restoration even when the module was not previously imported.
    monkeypatch.setitem(sys.modules, "openrouter_keys", None)
    monkeypatch.delitem(sys.modules, "openrouter_keys")
    monkeypatch.setattr(core, "_resolve_review_key_portable", lambda: "generic-key")

    with pytest.raises(error_type, match=error_match):
        core.review_api_key()
