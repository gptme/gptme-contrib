"""
gptmail - Email automation for gptme agents

This package provides email automation capabilities for gptme agents,
including CLI tools, background watchers, and shared communication utilities.
"""

from pathlib import Path

__version__ = "0.1.0"
__all__ = ["__version__"]

# Package root directory
PACKAGE_ROOT = Path(__file__).parent
