"""Test configuration for gptme-tts plugin."""

import sys
from pathlib import Path


def pytest_configure():
    """Add plugin root to sys.path for importing flat backend modules."""
    plugin_root = Path(__file__).resolve().parent.parent  # plugins/gptme-tts/
    if str(plugin_root) not in sys.path:
        sys.path.insert(0, str(plugin_root))
