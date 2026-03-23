"""Pytest configuration for gptme-contrib-lib tests."""


def pytest_collection_modifyitems(items):
    """Automatically add asyncio marker to async test functions."""
    for item in items:
        if item.get_closest_marker("asyncio") is not None:
            # Already marked, ensure mode is set
            pass
