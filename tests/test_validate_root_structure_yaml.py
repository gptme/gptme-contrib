"""Tests for the config-driven root structure validator."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "precommit" / "validators"))

import validate_root_structure_yaml as validator  # noqa: E402


def write_config(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "root-structure-allowlist.yaml"
    config.write_text(body)
    return config


def test_allowed_entries_pass(tmp_path):
    """Tracked entries that are a subset of the allowlist exit 0."""
    config = write_config(
        tmp_path,
        "allowed_entries:\n  - README.md\n  - scripts\n  - tests\n",
    )
    with (
        patch.object(validator, "get_repo_root", return_value=tmp_path),
        patch.object(
            validator,
            "get_tracked_root_entries",
            return_value={"README.md", "scripts"},
        ),
    ):
        assert validator.main(["--config", str(config)]) == 0


def test_unexpected_entry_fails(tmp_path, capsys):
    """An entry missing from the allowlist exits 1 and is named in the output."""
    config = write_config(tmp_path, "allowed_entries:\n  - README.md\n")
    with (
        patch.object(validator, "get_repo_root", return_value=tmp_path),
        patch.object(
            validator,
            "get_tracked_root_entries",
            return_value={"README.md", "stray_dir"},
        ),
    ):
        assert validator.main(["--config", str(config)]) == 1
    assert "stray_dir" in capsys.readouterr().out


def test_missing_config_reports_creation_hint(tmp_path, capsys):
    """A missing config file explains how to create one."""
    missing = tmp_path / "root-structure-allowlist.yaml"
    with patch.object(validator, "get_repo_root", return_value=tmp_path):
        assert validator.main(["--config", str(missing)]) == 1
    err = capsys.readouterr().err
    assert "config file not found" in err
    assert "If this is intentional, create" in err


def test_empty_allowlist_is_not_reported_as_missing(tmp_path, capsys):
    """A valid but empty allowlist rejects entries without the 'create it' hint.

    Regression guard: conflating "load failed" with "loaded an empty list" made
    a present-but-empty config print the missing-file hint.
    """
    config = write_config(tmp_path, "allowed_entries: []\n")
    with (
        patch.object(validator, "get_repo_root", return_value=tmp_path),
        patch.object(validator, "get_tracked_root_entries", return_value={"README.md"}),
    ):
        assert validator.main(["--config", str(config)]) == 1
    captured = capsys.readouterr()
    assert "README.md" in captured.out
    assert "If this is intentional, create" not in captured.err


def test_malformed_yaml_fails_cleanly(tmp_path, capsys):
    """Unparseable YAML exits 1 with a parse error, not a traceback."""
    config = write_config(tmp_path, "allowed_entries: [unclosed\n")
    with patch.object(validator, "get_repo_root", return_value=tmp_path):
        assert validator.main(["--config", str(config)]) == 1
    assert "failed to parse YAML config" in capsys.readouterr().err


def test_missing_allowed_entries_key_fails(tmp_path, capsys):
    """A YAML file without the 'allowed_entries' key exits 1."""
    config = write_config(tmp_path, "something_else:\n  - README.md\n")
    with patch.object(validator, "get_repo_root", return_value=tmp_path):
        assert validator.main(["--config", str(config)]) == 1
    assert "missing 'allowed_entries' key" in capsys.readouterr().err


def test_non_list_allowed_entries_fails(tmp_path, capsys):
    """A scalar 'allowed_entries' value exits 1 rather than iterating a string."""
    config = write_config(tmp_path, "allowed_entries: README.md\n")
    with patch.object(validator, "get_repo_root", return_value=tmp_path):
        assert validator.main(["--config", str(config)]) == 1
    assert "must be a list" in capsys.readouterr().err


def test_config_file_itself_must_be_in_allowlist(tmp_path, capsys):
    """The config file lives at the repo root and must be listed in the allowlist.

    Bootstrap requirement: when a consumer creates root-structure-allowlist.yaml,
    that file becomes a tracked top-level entry. It must appear in allowed_entries
    or the hook immediately reports it as unexpected.
    """
    config = write_config(tmp_path, "allowed_entries:\n  - README.md\n")
    with (
        patch.object(validator, "get_repo_root", return_value=tmp_path),
        patch.object(
            validator,
            "get_tracked_root_entries",
            return_value={"README.md", "root-structure-allowlist.yaml"},
        ),
    ):
        assert validator.main(["--config", str(config)]) == 1
    assert "root-structure-allowlist.yaml" in capsys.readouterr().out


def test_relative_config_resolves_against_repo_root(tmp_path):
    """A relative --config path is resolved from the repo root, not the cwd."""
    write_config(tmp_path, "allowed_entries:\n  - README.md\n")
    with (
        patch.object(validator, "get_repo_root", return_value=tmp_path),
        patch.object(validator, "get_tracked_root_entries", return_value={"README.md"}),
    ):
        assert validator.main(["--config", "root-structure-allowlist.yaml"]) == 0


def test_repo_root_failure_reports_cleanly(tmp_path, capsys):
    """main() fails cleanly when git cannot resolve a repository root."""
    error = subprocess.CalledProcessError(128, ["git", "rev-parse", "--show-toplevel"])
    with patch.object(validator, "get_repo_root", side_effect=error):
        assert validator.main([]) == 1
    assert "unable to find git repo root" in capsys.readouterr().err


def test_get_repo_root_preserves_non_utf8_path_bytes():
    """Repository paths round-trip undecodable bytes via surrogateescape."""
    with patch("subprocess.run") as run:
        run.return_value.stdout = b"/tmp/repo-\xff\n"
        repo_root = validator.get_repo_root()
    assert str(repo_root).encode("utf-8", errors="surrogateescape") == b"/tmp/repo-\xff"


def test_allowlist_entries_with_trailing_slash_match(tmp_path):
    """Allowlist entries written with a trailing slash match the normalized tracked entry."""
    config = write_config(tmp_path, "allowed_entries:\n  - src/\n  - README.md\n")
    with (
        patch.object(validator, "get_repo_root", return_value=tmp_path),
        patch.object(
            validator,
            "get_tracked_root_entries",
            return_value={"src", "README.md"},
        ),
    ):
        assert validator.main(["--config", str(config)]) == 0


def test_allowlist_entries_with_dot_slash_prefix_match(tmp_path):
    """Allowlist entries written with a leading ./ match the normalized tracked entry."""
    config = write_config(tmp_path, "allowed_entries:\n  - ./src\n  - README.md\n")
    with (
        patch.object(validator, "get_repo_root", return_value=tmp_path),
        patch.object(
            validator,
            "get_tracked_root_entries",
            return_value={"src", "README.md"},
        ),
    ):
        assert validator.main(["--config", str(config)]) == 0


def test_non_string_entry_in_allowlist_fails(tmp_path, capsys):
    """A non-string entry in allowed_entries exits 1 with a clear error."""
    config = write_config(tmp_path, "allowed_entries:\n  - README.md\n  - [foo]\n")
    with patch.object(validator, "get_repo_root", return_value=tmp_path):
        assert validator.main(["--config", str(config)]) == 1
    assert "non-string entry" in capsys.readouterr().err


def test_get_tracked_root_entries_takes_first_path_component():
    """Nested paths collapse to their top-level entry."""
    with patch("subprocess.run") as run:
        run.return_value.stdout = b"README.md\0scripts/a/b.py\0tests/test_x.py\0"
        entries = validator.get_tracked_root_entries(Path("/tmp/repo"))
    assert entries == {"README.md", "scripts", "tests"}
