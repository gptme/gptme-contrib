"""Test basic package imports."""


def test_package_import():
    """Test that gptmail package can be imported."""
    import gptmail

    assert gptmail.__version__ == "0.1.0"


def test_submodule_imports():
    """Test that core modules can be imported."""
    # These should be importable without errors
    import gptmail.cli
    import gptmail.communication_utils
    import gptmail.lib

    # Verify they exist
    assert hasattr(gptmail.cli, "__file__")
    assert hasattr(gptmail.lib, "__file__")

    # Note: watcher.py skipped - initializes logging at import time
    # requiring filesystem structure. Needs refactoring to lazy-init logging.


def test_cli_import_outside_git_is_silent(tmp_path):
    """Importing the CLI outside a checkout must not leak git's stderr.

    A forced-command receiver runs from the login home, which is often not a
    checkout; git's "fatal: not a git repository" would reach the sender.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import gptmail.cli"],
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "GIT_CEILING_DIRECTORIES": str(tmp_path.parent),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "not a git repository" not in result.stderr
