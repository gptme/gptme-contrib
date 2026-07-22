"""gptme-action-receipts: append-only audit ledger for gptme tool actions."""

from gptme.plugins.plugin import GptmePlugin

from .hooks.receipt_hook import register

plugin = GptmePlugin(
    name="action_receipts",
    register_hooks=register,
)

__all__ = ["plugin", "register"]
