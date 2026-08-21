"""Tests for the root structure checker."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "precommit"))


def test_allowed_entries_pass():
    """main() exits 0 when tracked entries are a subset of ALLOWED_ROOT_ENTRIES."""
    import check_root_structure

    # A partial subset — real repos don't always have every allowed entry present.
    partial_subset = frozenset(["scripts", "tests", "lessons", "README.md"])
    assert (
        partial_subset <= check_root_structure.ALLOWED_ROOT_ENTRIES
    ), "test precondition"
    with (
        patch.object(check_root_structure, "get_repo_root", return_value=REPO_ROOT),
        patch.object(
            check_root_structure,
            "get_tracked_root_entries",
            return_value=partial_subset,
        ),
    ):
        result = check_root_structure.main([])
    assert result == 0


def test_get_tracked_root_entries_parses_paths_correctly():
    """get_tracked_root_entries should extract first path components from git ls-files output."""
    import check_root_structure

    fake_output = "scripts/foo.py\ntests/test_bar.py\nlessons/README.md\nREADME.md\n"
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = fake_output

    with patch("subprocess.run", return_value=mock_result):
        entries = check_root_structure.get_tracked_root_entries()

    assert entries == {"scripts", "tests", "lessons", "README.md"}


def test_detects_unexpected_entry():
    """Unexpected entries should cause main() to return 1."""
    import check_root_structure

    fake_entries = check_root_structure.ALLOWED_ROOT_ENTRIES | {"unexpected_dir"}
    with (
        patch.object(check_root_structure, "get_repo_root", return_value=REPO_ROOT),
        patch.object(
            check_root_structure,
            "get_tracked_root_entries",
            return_value=fake_entries,
        ),
    ):
        result = check_root_structure.main([])
    assert result == 1


def test_no_unexpected_entry_on_known_set():
    """Passing exactly the allowed set should return 0."""
    import check_root_structure

    with (
        patch.object(check_root_structure, "get_repo_root", return_value=REPO_ROOT),
        patch.object(
            check_root_structure,
            "get_tracked_root_entries",
            return_value=set(check_root_structure.ALLOWED_ROOT_ENTRIES),
        ),
    ):
        result = check_root_structure.main([])
    assert result == 0


def test_reads_git_index_not_filesystem():
    """get_tracked_root_entries should call git ls-files --cached, not iterdir."""
    import check_root_structure

    # Hermetic: mock subprocess.run to return fixed output — no real git needed.
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "scripts/foo.py\ntests/test_bar.py\n"

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        check_root_structure.get_tracked_root_entries()

    assert mock_run.called, "subprocess.run was not called at all"
    cmd = mock_run.call_args[0][0]
    assert "ls-files" in cmd, f"Expected 'ls-files' in command, got: {cmd}"
    assert "--cached" in cmd, f"Expected '--cached' in command, got: {cmd}"


def test_allow_args_override_default_allowlist():
    """--allow args should replace ALLOWED_ROOT_ENTRIES as the allowlist."""
    import check_root_structure

    # Only two entries allowed via --allow; everything else is unexpected.
    with (
        patch.object(check_root_structure, "get_repo_root", return_value=REPO_ROOT),
        patch.object(
            check_root_structure,
            "get_tracked_root_entries",
            return_value=frozenset(["src", "tests", "README.md"]),
        ),
    ):
        # All three present, only src + tests allowed → README.md is unexpected
        result = check_root_structure.main(["--allow=src", "--allow=tests"])
    assert result == 1


def test_allow_args_pass_when_all_entries_listed():
    """main() returns 0 when --allow covers every tracked entry."""
    import check_root_structure

    with (
        patch.object(check_root_structure, "get_repo_root", return_value=REPO_ROOT),
        patch.object(
            check_root_structure,
            "get_tracked_root_entries",
            return_value=frozenset(["src", "tests", "README.md"]),
        ),
    ):
        result = check_root_structure.main(
            ["--allow=src", "--allow=tests", "--allow=README.md"]
        )
    assert result == 0


def test_allow_args_are_literal_root_entry_names():
    """Directory-like syntax is not silently normalized into another entry."""
    import check_root_structure

    with (
        patch.object(check_root_structure, "get_repo_root", return_value=REPO_ROOT),
        patch.object(
            check_root_structure,
            "get_tracked_root_entries",
            return_value=frozenset(["src"]),
        ),
    ):
        result = check_root_structure.main(["--allow=src/"])
    assert result == 1


def test_allow_args_superset_is_fine():
    """--allow may list more entries than actually exist; extras are ignored."""
    import check_root_structure

    with (
        patch.object(check_root_structure, "get_repo_root", return_value=REPO_ROOT),
        patch.object(
            check_root_structure,
            "get_tracked_root_entries",
            return_value=frozenset(["src"]),
        ),
    ):
        # Allow src + tests, but only src is tracked — should pass.
        result = check_root_structure.main(["--allow=src", "--allow=tests"])
    assert result == 0


def test_no_allow_args_falls_back_to_contrib_defaults():
    """Without --allow args, ALLOWED_ROOT_ENTRIES is used (gptme-contrib defaults)."""
    import check_root_structure

    # A valid contrib subset → should pass.
    contrib_subset = frozenset(["scripts", "lessons", "README.md"])
    with (
        patch.object(check_root_structure, "get_repo_root", return_value=REPO_ROOT),
        patch.object(
            check_root_structure,
            "get_tracked_root_entries",
            return_value=contrib_subset,
        ),
    ):
        result = check_root_structure.main([])  # no --allow
    assert result == 0
