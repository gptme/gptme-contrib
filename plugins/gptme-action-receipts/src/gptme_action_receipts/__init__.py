"""gptme-action-receipts: append-only audit ledger for gptme tool actions."""

from .hooks.receipt_hook import register

# Register the hook at module import time so gptme can discover it.
# This makes the plugin available to gptme's plugin system.
try:
    register()
except Exception:
    # If registration fails, log but don't crash the entire gptme session.
    # Allows graceful degradation if dependencies are missing.
    pass

__all__ = []
