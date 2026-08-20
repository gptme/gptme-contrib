"""Tests for the root structure checker."""

import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "precommit"))


def test_allowed_entries_pass():
    """main() exits 0 when tracked entries are a subset of ALLOWED_ROOT_ENTRIES."""
    from unittest.mock import patch

    import check_root_structure

    # A partial subset — real repos don't always have every allowed entry present.
    # This is hermetic: no git subprocess, no filesystem dependency.
    partial_subset = frozenset(["scripts", "tests", "lessons", "README.md"])
    assert (
        partial_subset <= check_root_structure.ALLOWED_ROOT_ENTRIES
    ), "test precondition"
    with patch.object(
        check_root_structure, "get_tracked_root_entries", return_value=partial_subset
    ):
        result = check_root_structure.main()
    assert result == 0


def test_get_tracked_root_entries_parses_paths_correctly():
    """get_tracked_root_entries should extract first path components from git ls-files output."""
    from unittest.mock import MagicMock

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
    with patch.object(
        check_root_structure, "get_tracked_root_entries", return_value=fake_entries
    ):
        result = check_root_structure.main()
    assert result == 1


def test_no_unexpected_entry_on_known_set():
    """Passing exactly the allowed set should return 0."""
    import check_root_structure

    with patch.object(
        check_root_structure,
        "get_tracked_root_entries",
        return_value=set(check_root_structure.ALLOWED_ROOT_ENTRIES),
    ):
        result = check_root_structure.main()
    assert result == 0


def test_reads_git_index_not_filesystem():
    """get_tracked_root_entries should call git ls-files --cached, not iterdir."""
    from unittest.mock import MagicMock

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
