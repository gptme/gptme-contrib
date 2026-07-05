"""gptme-action-receipts: append-only audit ledger for gptme tool actions."""

from .hooks.receipt_hook import register

__all__ = ["register"]
