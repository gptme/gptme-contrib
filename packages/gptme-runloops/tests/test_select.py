"""Tests for the backend + model select command."""

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from gptme_runloops.cli import main
from gptme_runloops.select import (
    Candidate,
    SelectConfig,
    SelectResult,
    _is_block_active,
    load_select_config,
    select_backend,
)

# ---------------------------------------------------------------------------
# Unit: SelectConfig
# ---------------------------------------------------------------------------


def test_ordered_returns_ascending_priority():
    config = SelectConfig(
        candidates=[
            Candidate(backend="gptme", model="x", priority=2),
            Candidate(backend="claude-code", model="y", priority=1),
        ]
    )
    ordered = config.ordered
    assert ordered[0].backend == "claude-code"
    assert ordered[1].backend == "gptme"


# ---------------------------------------------------------------------------
# Unit: load_select_config
# ---------------------------------------------------------------------------


def test_load_select_config_missing_returns_empty(tmp_path: Path):
    config = load_select_config(tmp_path / "nonexistent.toml")
    assert config.candidates == []


def test_load_select_config_parses_candidates(tmp_path: Path):
    toml = tmp_path / "harness-quota.toml"
    toml.write_text(
        '[[ candidates ]]\nbackend = "gptme"\nmodel = "openrouter/m"\npriority = 1\n'
    )
    config = load_select_config(toml)
    assert len(config.candidates) == 1
    assert config.candidates[0].backend == "gptme"
    assert config.candidates[0].model == "openrouter/m"
    assert config.candidates[0].priority == 1


def test_load_select_config_skips_incomplete_entries(tmp_path: Path):
    toml = tmp_path / "harness-quota.toml"
    # entry without 'model' should be skipped
    toml.write_text('[[ candidates ]]\nbackend = "gptme"\n')
    config = load_select_config(toml)
    assert config.candidates == []


# ---------------------------------------------------------------------------
# Unit: select_backend — binary gate
# ---------------------------------------------------------------------------


def test_select_skips_missing_binary(tmp_path: Path):
    """A candidate whose binary is not in PATH is skipped."""
    config = SelectConfig(
        candidates=[Candidate(backend="__nonexistent_backend__", model="m", priority=0)]
    )
    result = select_backend(config=config, state_dir=tmp_path)
    assert result is None


def test_select_returns_first_available_binary(tmp_path: Path):
    """Finds first candidate whose binary exists."""
    config = SelectConfig(
        candidates=[
            Candidate(backend="__nonexistent__", model="m1", priority=0),
            Candidate(backend="gptme", model="openrouter/m", priority=1),
        ]
    )
    # mock gptme binary as available but __nonexistent__ not; pin the
    # block-registry gate so the result depends only on binary availability,
    # not on whether gptme_block_registry happens to be installed.
    with (
        patch("gptme_runloops.select.shutil.which") as mock_which,
        patch("gptme_runloops.select._is_block_active", return_value=False),
    ):
        mock_which.side_effect = lambda b: "/usr/bin/gptme" if b == "gptme" else None
        result = select_backend(config=config, state_dir=tmp_path)
    assert result is not None
    assert result.backend == "gptme"
    assert result.model == "openrouter/m"


# ---------------------------------------------------------------------------
# Unit: select_backend — block-registry gate
# ---------------------------------------------------------------------------


def test_select_skips_blocked_candidate(tmp_path: Path):
    """A block-registry-blocked candidate is skipped."""
    config = SelectConfig(
        candidates=[
            Candidate(backend="gptme", model="m", priority=0),
            Candidate(backend="claude-code", model="n", priority=1),
        ]
    )

    def fake_binary(backend: str) -> bool:
        return True

    def fake_block(state_dir: Path, backend: str, model: str) -> bool:
        return backend == "gptme"  # first candidate is blocked

    with (
        patch("gptme_runloops.select._binary_available", fake_binary),
        patch("gptme_runloops.select._is_block_active", fake_block),
    ):
        result = select_backend(config=config, state_dir=tmp_path)
    assert result is not None
    assert result.backend == "claude-code"


# ---------------------------------------------------------------------------
# Unit: _is_block_active — missing registry fails closed when state exists
# ---------------------------------------------------------------------------


def test_block_active_fails_closed_when_state_exists(tmp_path: Path):
    """Missing block-registry package + existing block-shaped state = blocked."""
    import sys

    (tmp_path / "gptme-blocked-until.txt").write_text("2026-01-01T00:00:00+00:00")
    sentinel = {
        mod: None
        for mod in (
            "gptme_block_registry",
            "gptme_block_registry.contract",
            "gptme_block_registry.writers",
        )
    }
    saved = {m: sys.modules.get(m) for m in sentinel}
    sys.modules.update(sentinel)
    try:
        assert _is_block_active(tmp_path, "gptme", "m") is True
    finally:
        for m, v in saved.items():
            if v is None:
                del sys.modules[m]
            else:
                sys.modules[m] = v


def test_block_active_fails_open_when_only_incidental_files(tmp_path: Path):
    """Missing registry + only incidental files = no block artifact, not blocked."""
    import sys

    (tmp_path / "README.md").write_text("not a block")
    (tmp_path / "lock").write_text("")
    sentinel = {
        mod: None
        for mod in (
            "gptme_block_registry",
            "gptme_block_registry.contract",
            "gptme_block_registry.writers",
        )
    }
    saved = {m: sys.modules.get(m) for m in sentinel}
    sys.modules.update(sentinel)
    try:
        assert _is_block_active(tmp_path, "gptme", "m") is False
    finally:
        for m, v in saved.items():
            if v is None:
                del sys.modules[m]
            else:
                sys.modules[m] = v


def test_block_active_fails_open_when_no_state(tmp_path: Path):
    """Missing block-registry package + no state dir = no blocks possible."""
    import sys

    sentinel = {
        mod: None
        for mod in (
            "gptme_block_registry",
            "gptme_block_registry.contract",
            "gptme_block_registry.writers",
        )
    }
    saved = {m: sys.modules.get(m) for m in sentinel}
    sys.modules.update(sentinel)
    try:
        assert _is_block_active(tmp_path / "nonexistent", "gptme", "m") is False
    finally:
        for m, v in saved.items():
            if v is None:
                del sys.modules[m]
            else:
                sys.modules[m] = v


def test_select_returns_none_when_all_blocked(tmp_path: Path):
    config = SelectConfig(
        candidates=[Candidate(backend="gptme", model="m", priority=0)]
    )
    with (
        patch("gptme_runloops.select._binary_available", return_value=True),
        patch("gptme_runloops.select._is_block_active", return_value=True),
    ):
        result = select_backend(config=config, state_dir=tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# Unit: SelectResult.to_dict
# ---------------------------------------------------------------------------


def test_select_result_to_dict():
    result = SelectResult(
        backend="gptme",
        model="openrouter/m",
        candidates=[Candidate("gptme", "openrouter/m", 0)],
    )
    d = result.to_dict()
    assert d["backend"] == "gptme"
    assert d["model"] == "openrouter/m"
    assert isinstance(d["candidates"], list)
    assert d["candidates"][0]["backend"] == "gptme"


# ---------------------------------------------------------------------------
# CLI: select command
# ---------------------------------------------------------------------------


def test_cli_select_json_output(tmp_path: Path):
    toml = tmp_path / "harness-quota.toml"
    toml.write_text(
        '[[ candidates ]]\nbackend = "gptme"\nmodel = "openrouter/m"\npriority = 1\n'
    )
    runner = CliRunner()
    with (
        patch("gptme_runloops.select._binary_available", return_value=True),
        patch("gptme_runloops.select._is_block_active", return_value=False),
    ):
        result = runner.invoke(main, ["select", "--config", str(toml), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["backend"] == "gptme"
    assert data["model"] == "openrouter/m"
    assert isinstance(data["candidates"], list)


def test_cli_select_plain_output(tmp_path: Path):
    toml = tmp_path / "harness-quota.toml"
    toml.write_text(
        '[[ candidates ]]\nbackend = "gptme"\nmodel = "openrouter/m"\npriority = 1\n'
    )
    runner = CliRunner()
    with (
        patch("gptme_runloops.select._binary_available", return_value=True),
        patch("gptme_runloops.select._is_block_active", return_value=False),
    ):
        result = runner.invoke(main, ["select", "--config", str(toml)])
    assert result.exit_code == 0, result.output
    assert "backend=gptme" in result.output
    assert "model=openrouter/m" in result.output


def test_cli_select_no_config_raises(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(
        main, ["select", "--config", str(tmp_path / "nonexistent.toml")]
    )
    assert result.exit_code != 0


def test_cli_select_all_blocked_raises(tmp_path: Path):
    toml = tmp_path / "harness-quota.toml"
    toml.write_text(
        '[[ candidates ]]\nbackend = "gptme"\nmodel = "openrouter/m"\npriority = 1\n'
    )
    runner = CliRunner()
    with (
        patch("gptme_runloops.select._binary_available", return_value=True),
        patch("gptme_runloops.select._is_block_active", return_value=True),
    ):
        result = runner.invoke(main, ["select", "--config", str(toml)])
    assert result.exit_code != 0
