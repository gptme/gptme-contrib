"""Agent brain repos join the gptme review family from host config, not literals."""

from pathlib import Path

import pytest
from gptme_review import core

NAME = core.REVIEW_BRAIN_REPOS_ENV


def _config(path: Path, value: str) -> Path:
    path.write_text(f'[env]\n{NAME} = "{value}"\n')
    return path


def test_package_has_no_agent_brain_literals() -> None:
    for _, _, repos in core.REPO_FAMILIES:
        assert not any(r.startswith("ErikBjare/") for r in repos)


def test_config_env_lists_brains(tmp_path: Path) -> None:
    cfg = _config(tmp_path / "c.toml", "ErikBjare/bob, ErikBjare/alice")
    got = core.review_brain_repos(environ={}, config_paths=(cfg,))
    assert got == {"ErikBjare/bob", "ErikBjare/alice"}


def test_environment_beats_config(tmp_path: Path) -> None:
    cfg = _config(tmp_path / "c.toml", "ErikBjare/bob")
    got = core.review_brain_repos(
        environ={NAME: "ErikBjare/alice  ErikBjare/bob"}, config_paths=(cfg,)
    )
    assert got == {"ErikBjare/alice", "ErikBjare/bob"}
    blank = core.review_brain_repos(environ={NAME: "  "}, config_paths=(cfg,))
    assert blank == {"ErikBjare/bob"}


def test_unconfigured_host_has_no_brains(tmp_path: Path) -> None:
    missing = tmp_path / "absent.toml"
    assert core.review_brain_repos(environ={}, config_paths=(missing,)) == frozenset()


def test_configured_brain_joins_gptme_family(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(core, "REVIEW_KEY_CONFIG_PATHS", (tmp_path / "absent.toml",))
    monkeypatch.setenv(NAME, "ErikBjare/bob,ErikBjare/alice")
    entry = core.repo_family("ErikBjare/alice")
    assert entry is not None
    name, family_type, repos = entry
    assert (name, family_type) == ("gptme", "shared-tooling")
    assert {"ErikBjare/bob", "gptme/gptme-contrib"} <= repos
    assert core.repo_family("gptme/gptme")[2] == repos  # type: ignore[index]
    assert "cross_repo_placement" in core.select_aspects("", repo="ErikBjare/alice")


def test_unconfigured_brain_has_no_family(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(core, "REVIEW_KEY_CONFIG_PATHS", (tmp_path / "absent.toml",))
    monkeypatch.delenv(NAME, raising=False)
    assert core.repo_family("ErikBjare/bob") is None
    assert core.repo_family("gptme/gptme") is not None
    assert core.repo_family("ActivityWatch/aw-server") is not None
