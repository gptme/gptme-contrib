"""Test basic package imports."""


def test_package_import():
    """Test that gptmail package can be imported."""
    import gptmail

    assert gptmail.__version__ == "0.1.0"


def test_submodule_imports():
    """Test that core modules can be imported."""
    # These should be importable without errors
    import gptmail.cli
    import gptmail.lib
    import gptmail.communication_utils

    # Verify they exist
    assert hasattr(gptmail.cli, "__file__")
    assert hasattr(gptmail.lib, "__file__")

    # Note: watcher.py skipped - initializes logging at import time
    # requiring filesystem structure. Needs refactoring to lazy-init logging.
