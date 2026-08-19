"""Tests for the root structure checker."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "precommit"))


def test_allowed_entries_pass():
    """Running against the actual repo should pass (no unexpected entries)."""
    import check_root_structure

    result = check_root_structure.main()
    assert (
        result == 0
    ), "check_root_structure.main() should return 0 on the current repo"


def test_get_tracked_root_entries_parses_paths_correctly():
    """get_tracked_root_entries should extract first path components from git ls-files output."""
    from unittest.mock import MagicMock

    import check_root_structure

    fake_output = "scripts/foo.py\ntests/test_bar.py\nlessons/README.md\nREADME.md\n"
    mock_result = MagicMock()
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
    """get_tracked_root_entries should use git ls-files (index), not iterdir."""
    import check_root_structure

    # Verify subprocess call uses git ls-files --cached
    calls = []
    real_run = subprocess.run

    def spy_run(args, **kwargs):
        calls.append(args)
        return real_run(args, **kwargs)

    with patch("subprocess.run", side_effect=spy_run):
        check_root_structure.get_tracked_root_entries()

    assert any(
        "ls-files" in str(c) and "--cached" in str(c) for c in calls
    ), "Expected git ls-files --cached to be called"
